#!/usr/bin/env python3
"""ADR-001 model eval -- fixture matrix + scoring harness. DECIDE-ONLY.

Runs every candidate model against every frozen scenario n times, scores tool
selection and content truthfulness, and reports cost/latency. Step 0
(scripts/eval-step0-smoke.py) gates capability; this measures judgment.

NO SIDE EFFECTS -- structural, not disciplinary:
  * Only `sts` (account guard) and `bedrock-runtime` (Converse) clients exist in
    this file. No DynamoDB resource, no Lambda client, no SNS client, no Gateway
    call. The emitted tool call is scored and discarded -- never dispatched.
  * Inputs come from evals/scenarios.json (synthetic matter ids, pinned
    asOfDate). Nothing is read from or written to the matters table.

Usage:
    python scripts/eval-models.py --self-test          # scorer only, no API calls
    EXPECTED_ACCOUNT=<id> python scripts/eval-models.py --dry-run
    EXPECTED_ACCOUNT=<id> python scripts/eval-models.py [--runs 3] [--models ...]
"""

import argparse
import csv
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import date

import boto3
from botocore.exceptions import ClientError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REGION = os.environ.get("AWS_REGION", "us-east-1")
HERE = os.path.dirname(__file__)
PROMPT_PATH = os.path.join(HERE, "..", "agent", "system-prompt.md")
SCENARIOS_PATH = os.path.join(HERE, "..", "evals", "scenarios.json")
RESULTS_DIR = os.path.join(HERE, "..", "evals", "results")

# Verified ACTIVE in-account (ADR-001). All four cleared the Step-0 capability gate.
CANDIDATES = {
    "sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "nova-lite": "us.amazon.nova-lite-v1:0",
    "nova-micro": "us.amazon.nova-micro-v1:0",
}

# Must stay identical to the toolConfig in eval-step0-smoke.py -- the smoke test is
# deliberately standalone, so the definition is duplicated rather than imported.
# send_reminder is SCHEMA-ONLY: no Lambda behind it, never dispatched. Its presence
# is what makes selection a real three-way choice instead of a one-hammer test.
TOOL_CONFIG = {
    "tools": [
        {"toolSpec": {
            "name": "escalate_to_human",
            "description": ("Escalate a matter to a human owner with a reason. Records the "
                            "escalation on the matter (audit trail) and notifies the owner."),
            "inputSchema": {"json": {"type": "object", "properties": {
                "matterId": {"type": "string", "description": "The matter to escalate."},
                "docType": {"type": "string", "description": "The document the escalation concerns. Optional."},
                "reason": {"type": "string", "description": "Why this needs a human, in terms of the matter state relied on: which document, which dates, which prior actions."},
            }, "required": ["matterId", "reason"]}},
        }},
        {"toolSpec": {
            "name": "send_reminder",
            "description": ("Send a reminder to the counterparty for an outstanding document. "
                            "Use when a document is missing or incomplete and its due date has not yet passed."),
            "inputSchema": {"json": {"type": "object", "properties": {
                "matterId": {"type": "string", "description": "The matter the reminder concerns."},
                "docType": {"type": "string", "description": "The outstanding document."},
                "message": {"type": "string", "description": "The reminder body. Short and specific: name the exact document outstanding and its exact due date."},
            }, "required": ["matterId", "message"]}},
        }},
    ],
    "toolChoice": {"auto": {}},
}

ACTION_FOR_TOOL = {"escalate_to_human": "escalate", "send_reminder": "remind", None: "none"}

# --- claim verification -------------------------------------------------------
# Deterministic, conservative. Flags only unambiguous contradictions against the
# fixture's pinned values; a phrase it cannot confidently interpret is ignored
# rather than reported, because a false accusation is worse than a missed one in
# a rubric that disqualifies models.
# A due-date assertion, e.g. "due 2026-07-30" / "due on ..." / "due by ...".
# This REPLACED a blanket "any ISO date not in the fixture is fabricated" check,
# which flagged models for citing asOfDate -- i.e. for correctly saying what
# today is, which is good grounding, not a false claim. Only a date asserted AS
# a document's due date can be checked for truth.
DUE_DATE_CLAIM = re.compile(r"\bdue\s+(?:on\s+|by\s+|date\s+(?:of\s+|is\s+)?)?(\d{4}-\d{2}-\d{2})\b", re.I)
FORWARD_N = re.compile(r"\bdue\s+(?:in|within)\s+(\d+)\s+(?:business\s+)?days?\b", re.I)
FORWARD_SOFT = re.compile(r"\bdue\s+(?:in|within)\s+\w+\s+days?\b|\bnot\s+yet\s+due\b|\bstill\s+has\s+time\b", re.I)
BACKWARD_N = re.compile(r"\b(\d+)\s+days?\s+(?:overdue|late|past\s+due)\b", re.I)
# "was due" deliberately excluded: past tense about a future date ("the census,
# which was due on 2026-08-05") is a phrasing quirk, not an assertion of
# overdue-ness, and flagged correct reminders.
BACKWARD_SOFT = re.compile(r"\boverdue\b|\bpast\s+due\b|\bmissed\s+(?:the\s+)?due\s+date\b", re.I)
CLAIM_RECEIVED = re.compile(r"\breceived\b|\bhas\s+arrived\b", re.I)
CLAIM_MISSING = re.compile(r"\bis\s+missing\b|\bstill\s+outstanding\b", re.I)

_NEG = re.compile(r"\b(?:no|not|never|without|nothing|none|neither|cannot|awaiting|pending)\b|n't", re.I)


def _negated(text: str, start: int) -> bool:
    """True if the match at `start` sits in a negated clause.

    Without this, "no document received" reads as a claim that it WAS received,
    and "nothing is overdue" as a claim that something is. Both were flagged as
    false claims against correct messages. Scope is the current clause only --
    scanning further back suppresses real assertions.
    """
    bound = max(text.rfind(c, 0, start) for c in (".", ";", "\n", "!", "?", "—"))
    return bool(_NEG.search(text[bound + 1:start]))


def _asserts(pattern: re.Pattern, text: str):
    """First match of `pattern` that is actually asserted, not negated."""
    for m in pattern.finditer(text):
        if not _negated(text, m.start()):
            return m
    return None


def _subject_doc(fixture: dict, args: dict, text: str) -> dict | None:
    """Which document is a claim about? Prefer the tool's docType arg; else the
    only docType named in the text; else None (checks that need a doc are skipped)."""
    docs = {d["docType"]: d for d in fixture["state"]["documents"]}
    dt = (args or {}).get("docType")
    if dt in docs:
        return docs[dt]
    named = [d for k, d in docs.items() if k.lower() in text.lower()]
    return named[0] if len(named) == 1 else None


def verify_claims(text: str, fixture: dict, args: dict) -> list[str]:
    """Return a list of confirmed false factual claims in generated content.

    Any non-empty result is a DISQUALIFYING error under ADR-001: a message that
    tells a counterparty something untrue about their own matter is a failure of
    commission -- it cannot be recalled and enters the correspondence record.
    """
    if not text:
        return []
    problems = []
    as_of = date.fromisoformat(fixture["asOfDate"])
    doc = _subject_doc(fixture, args, text)
    if not doc or not doc.get("dueDate"):
        return problems
    due = doc["dueDate"]
    delta = (date.fromisoformat(due) - as_of).days  # < 0 == overdue

    # 1. A date asserted as a due date must be SOME document's real due date.
    #    Checked against every document in the matter, not just the subject one:
    #    models routinely discuss several documents in a single message, and
    #    attributing every date found to one subject flagged correct text as
    #    fabricated (Sonnet and Haiku on S1/S4 both stated two correct due dates
    #    and were accused of inventing one).
    all_due = {d.get("dueDate") for d in fixture["state"]["documents"] if d.get("dueDate")}
    all_due.add(fixture["state"]["meta"].get("targetCloseDate"))  # the matter is "due" then too
    for m in DUE_DATE_CLAIM.finditer(text):
        if m.group(1) not in all_due:
            problems.append(f"asserts a due date of {m.group(1)}; no document in this matter is due then")

    # 2. Direction inversion -- the Nova Lite failure class.
    fwd_n = _asserts(FORWARD_N, text)
    if (fwd_n or _asserts(FORWARD_SOFT, text)) and delta < 0:
        problems.append(
            f"claims '{doc['docType']}' is still upcoming, but it was due {due} "
            f"and is {abs(delta)} days OVERDUE as of {fixture['asOfDate']}"
        )
    bwd_n = _asserts(BACKWARD_N, text)
    if (bwd_n or _asserts(BACKWARD_SOFT, text)) and delta > 0:
        problems.append(
            f"claims '{doc['docType']}' is overdue, but it is not due until {due} "
            f"({delta} days away as of {fixture['asOfDate']})"
        )

    # 3. Day-count magnitude, only when the direction is already right.
    if fwd_n and delta > 0 and int(fwd_n.group(1)) != delta:
        problems.append(f"says due in {fwd_n.group(1)} days; actual is {delta}")
    if bwd_n and delta < 0 and int(bwd_n.group(1)) != abs(delta):
        problems.append(f"says {bwd_n.group(1)} days overdue; actual is {abs(delta)}")

    # 4. Status assertions (negation-aware -- "no document received" is not a
    #    claim that one was).
    if _asserts(CLAIM_RECEIVED, text) and doc.get("status") == "missing":
        problems.append(f"implies '{doc['docType']}' was received; status is missing")
    if _asserts(CLAIM_MISSING, text) and doc.get("status") == "received":
        problems.append(f"claims '{doc['docType']}' is outstanding; status is received")
    return problems


# --- scoring ------------------------------------------------------------------
def check_schema(tool: str, args: dict) -> tuple[set, set]:
    spec = next(t["toolSpec"] for t in TOOL_CONFIG["tools"] if t["toolSpec"]["name"] == tool)
    schema = spec["inputSchema"]["json"]
    return set(schema.get("required", [])) - set(args or {}), set(args or {}) - set(schema["properties"])


def score_facts(text: str, facts: list[str]) -> tuple[int, int]:
    if not facts:
        return 0, 0
    low = (text or "").lower()
    return sum(1 for f in facts if f.lower() in low), len(facts)


def classify_errors(expected, observed: str, false_claims: list, schema_bad: bool) -> list[str]:
    """Return EVERY applicable error class, not the first one that matches.

    The original returned a single class and checked false_factual_claim first,
    so a run that both missed an escalation AND asserted something false was
    recorded only as the latter -- silently zeroing the missed-escalation count,
    which is the exact metric the pass bar is built on. A run can fail in more
    than one way and must be counted in each.

    `expected` may be a list when a scenario is genuinely ambiguous (see S5);
    any listed action counts as correct.
    """
    ok = set(expected) if isinstance(expected, (list, tuple)) else {expected}
    errs = []
    if "escalate" in ok and observed not in ok:
        errs.append("missed_escalation")       # disqualifying
    if false_claims:
        errs.append("false_factual_claim")     # disqualifying
    if schema_bad:
        errs.append("schema_violation")        # disqualifying
    if observed not in ok:
        if observed == "escalate" and "none" in ok:
            errs.append("spurious_escalation")
        elif observed == "escalate" and "remind" in ok:
            errs.append("over_caution")
        elif "escalate" not in ok:
            errs.append("wrong_action")
    return errs


DISQUALIFYING = {"false_factual_claim", "missed_escalation", "schema_violation"}


def score_run(fixture: dict, tool: str | None, args: dict, text: str) -> dict:
    expected = fixture["expected"]
    ok = set(expected) if isinstance(expected, (list, tuple)) else {expected}
    observed = ACTION_FOR_TOOL.get(tool, "none")
    missing, extra = check_schema(tool, args) if tool else (set(), set())
    # The ADR class is "false factual claim in DISPATCHED content" -- what would
    # actually reach a counterparty or the audit row. That is the tool call's
    # message/reason, NOT the model's free narration or <thinking> block, which
    # is never sent anywhere. Scoring narration as dispatched content accused
    # models of falsehoods in text they were only reasoning with.
    dispatched = " ".join(filter(None, [(args or {}).get("message"), (args or {}).get("reason")]))
    false_claims = verify_claims(dispatched, fixture, args) if dispatched else []
    # Grounding falls back to narration when no tool was called, since a
    # "do nothing and say why" decision has no dispatched content but must still
    # be justified.
    cited, avail = score_facts(dispatched or text, fixture.get("groundedFacts", []))
    errs = classify_errors(expected, observed, false_claims, bool(missing or extra))
    return {
        "observed": observed, "match": observed in ok,
        "schemaMissing": sorted(missing), "schemaExtra": sorted(extra),
        "falseClaims": false_claims, "factsCited": cited, "factsAvailable": avail,
        "errorClasses": errs, "disqualifying": bool(DISQUALIFYING & set(errs)),
    }


# --- self-test ----------------------------------------------------------------
def self_test(scenarios: dict) -> int:
    """Adversarial self-test. Every case is a VERBATIM model output from a real
    run, chosen because it previously broke the scorer or could plausibly do so.

    The first version of this test passed with three cases and still shipped
    three bugs, because none of its cases contained a negation or a citation of
    asOfDate. A self-test that only confirms what you already believe is not a
    test. Cases 2-4 exist specifically to fail the old detector.
    """
    by_id = {s["id"]: s for s in scenarios["scenarios"]}
    print("=== scorer self-test (adversarial, no API calls) ===\n")
    ok = True

    def case(name, sid, tool, args, expect_classes=None, forbid_classes=None, facts_lt=None):
        nonlocal ok
        r = score_run(by_id[sid], tool, args, "")
        got = set(r["errorClasses"])
        print(f"[{name}] {sid}")
        print(f"  observed={r['observed']} classes={sorted(got) or ['-']} "
              f"facts={r['factsCited']}/{r['factsAvailable']}")
        for p in r["falseClaims"]:
            print(f"    FLAGGED: {p}")
        bad = []
        for c in (expect_classes or []):
            if c not in got:
                bad.append(f"expected class '{c}' missing")
        for c in (forbid_classes or []):
            if c in got:
                bad.append(f"class '{c}' should NOT be present")
        if facts_lt is not None and r["factsCited"] >= facts_lt:
            bad.append(f"grounding {r['factsCited']} should be < {facts_lt}")
        if bad:
            ok = False
            for b in bad:
                print(f"  !! FAIL -- {b}")
        else:
            print("  PASS")
        print()
        return r

    # 1. BUG-3 REGRESSION: both a missed escalation and a false claim. The old
    #    classifier returned only the false claim, zeroing missed_escalation --
    #    the metric the pass bar keys on. Both classes must now appear.
    case("known-positive: Nova Lite Step-0, remind on an overdue matter",
         "S1", "send_reminder",
         {"matterId": "EVAL-S1", "docType": "signed-employer-application",
          "message": "The signed employer application is missing and is due in 2 days. Please submit it by 2026-07-30."},
         expect_classes=["missed_escalation", "false_factual_claim"])

    # 2. BUG-1 REGRESSION: cites asOfDate ("now 2026-08-03"). Correct grounding,
    #    previously flagged as a fabricated date.
    case("known-negative: Haiku S3, cites today's date correctly",
         "S3", None,
         {}, forbid_classes=["false_factual_claim"])
    r = score_run(by_id["S3"], None,
                  {}, "This matter has already been escalated to a human. The escalation captured the critical issues: "
                      "Census is overdue (due 2026-07-25, now 2026-08-03 - 9 days past due) and arrived with very low "
                      "extraction confidence (0.256).")
    print("[known-negative] Haiku S3 full text w/ asOfDate citation")
    print(f"  classes={sorted(r['errorClasses']) or ['-']}")
    for p in r["falseClaims"]:
        print(f"    FLAGGED: {p}")
    if "false_factual_claim" in r["errorClasses"]:
        print("  !! FAIL -- citing asOfDate flagged as fabricated"); ok = False
    else:
        print("  PASS -- asOfDate citation accepted")
    print()

    # 3. BUG-2 REGRESSION: "no response and no document received" -- negated.
    case("known-negative: Sonnet S7, negated 'received'",
         "S7", "escalate_to_human",
         {"matterId": "EVAL-S7", "docType": "census",
          "reason": "The census document remains missing and is due in 2 days (2026-08-05). All 3 permitted reminders "
                    "have been sent to broker Nadia Fischer with no response and no document received. The reminder "
                    "cap of 3 is exhausted; human intervention is required."},
         forbid_classes=["false_factual_claim", "missed_escalation"])

    # 4. Grounded correct escalation -- must score clean and fully grounded.
    case("known-negative: Haiku Step-0, fully grounded escalation",
         "S1", "escalate_to_human",
         {"matterId": "EVAL-S1", "reason": "Matter is blocked with two past-due documents: census due 2026-07-25 "
                                           "(9 days overdue, in-review with very low extraction confidence of 0.256) "
                                           "and signed employer application due 2026-07-30."},
         forbid_classes=["false_factual_claim", "missed_escalation"])

    # 5. Correct action, thin grounding -- no error, but low fact score.
    case("known-negative: Nova Micro Step-0, correct but ungrounded",
         "S1", "escalate_to_human",
         {"matterId": "EVAL-S1", "docType": "census",
          "reason": "The matter is blocked and past the target close date. The census document is in review with low "
                    "extraction confidence."},
         forbid_classes=["false_factual_claim", "missed_escalation"], facts_lt=3)

    print("=== self-test: " + ("ALL PASS" if ok else "FAILURES ABOVE") + " ===")
    return 0 if ok else 1


def rescore(path: str, scenarios: dict) -> int:
    """Replay a stored results file through the CURRENT scorer.

    This is how a scorer fix is verified against reality rather than against a
    synthetic case: the stored run holds each model's verbatim output, so
    re-scoring it shows exactly what the fixed classifier now reports for runs
    whose true classification is already known.
    """
    by_id = {s["id"]: s for s in scenarios["scenarios"]}
    raw = json.load(open(path, encoding="utf-8"))
    rows = raw["runs"] if isinstance(raw, dict) else raw
    print(f"=== rescoring {os.path.basename(path)} with the current scorer ===\n")
    counts = {}
    for r in rows:
        if r.get("observed") == "ERROR":
            continue
        fx = by_id[r["scenario"]]
        s = score_run(fx, r.get("tool"), r.get("args") or {}, r.get("text") or "")
        key = (r["model"], r["scenario"])
        counts.setdefault(key, []).append(s["errorClasses"])
    print(f"{'model':12s} {'scen':5s} {'runs':>4s}  classes now reported")
    print("-" * 78)
    for (m, sc), runs in counts.items():
        flat = sorted({c for run in runs for c in run})
        print(f"{m:12s} {sc:5s} {len(runs):>4d}  {flat or ['-']}")
    missed = sum(1 for runs in counts.values() for run in runs if "missed_escalation" in run)
    print(f"\nmissed_escalation runs now surfaced: {missed}")
    return 0


# --- run ----------------------------------------------------------------------
def assert_account() -> str:
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit("Set EXPECTED_ACCOUNT to the 12-digit account to run in.")
    actual = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.")
    return actual


def build_prompt(fixture: dict) -> str:
    return (
        f"Today's date is {fixture['asOfDate']}.\n\n"
        f"Decide the single next action for this matter and, if the state warrants "
        f"it, take that action by calling the appropriate tool. If the state does "
        f"not justify an action, do nothing and say why. Base your decision only "
        f"on the state below.\n\nMatter state:\n{json.dumps(fixture['state'], indent=2)}"
    )


def invoke(brt, model_id: str, system_text: str, prompt: str) -> dict:
    t0 = time.time()
    try:
        resp = brt.converse(
            modelId=model_id, system=[{"text": system_text}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            toolConfig=TOOL_CONFIG, inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
    except ClientError as e:
        return {"error": f"{e.response['Error']['Code']}: {e.response['Error']['Message']}",
                "wallMs": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "wallMs": int((time.time() - t0) * 1000)}

    out = {"error": None, "wallMs": int((time.time() - t0) * 1000),
           "latencyMs": resp.get("metrics", {}).get("latencyMs"),
           "stopReason": resp.get("stopReason"), "usage": resp.get("usage", {}),
           "tool": None, "args": {}, "text": ""}
    for b in resp["output"]["message"]["content"]:
        if "toolUse" in b:
            out["tool"] = b["toolUse"]["name"]; out["args"] = b["toolUse"].get("input", {})
        elif "text" in b:
            out["text"] += b["text"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true", help="validate the scorer, no API calls")
    ap.add_argument("--rescore", metavar="FILE", help="re-score a stored results file with the current scorer")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, make no calls")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=list(CANDIDATES))
    a = ap.parse_args()

    scenarios = json.load(open(SCENARIOS_PATH, encoding="utf-8"))
    if a.self_test:
        sys.exit(self_test(scenarios))
    if a.rescore:
        sys.exit(rescore(a.rescore, scenarios))

    fixtures = scenarios["scenarios"]
    total = len(a.models) * len(fixtures) * a.runs
    if a.dry_run:
        print(f"plan: {len(a.models)} models x {len(fixtures)} scenarios x {a.runs} runs = {total} invocations")
        print(f"models   : {a.models}")
        print(f"scenarios: {[(f['id'], f['expected']) for f in fixtures]}")
        print("decide-only: no dispatch, no DynamoDB, no SNS")
        return

    acct = assert_account()
    system_text = open(PROMPT_PATH, encoding="utf-8").read()
    brt = boto3.client("bedrock-runtime", region_name=REGION)
    print(f"=== ADR-001 eval matrix -- account {acct} | {total} invocations | decide-only ===\n")

    rows = []
    for name in a.models:
        model_id = CANDIDATES[name]
        for fx in fixtures:
            prompt = build_prompt(fx)
            for run in range(1, a.runs + 1):
                r = invoke(brt, model_id, system_text, prompt)
                if r.get("error"):
                    rows.append({"model": name, "scenario": fx["id"], "run": run,
                                 "expected": fx["expected"], "observed": "ERROR",
                                 "errorClasses": ["api_error"], "disqualifying": False,
                                 "apiError": r["error"], "latencyMs": None,
                                 "inputTokens": None, "outputTokens": None,
                                 "factsCited": 0, "factsAvailable": 0, "falseClaims": []})
                    print(f"  {name:11s} {fx['id']} run{run}: API ERROR {r['error'][:70]}")
                    continue
                s = score_run(fx, r["tool"], r["args"], r["text"])
                u = r["usage"]
                rows.append({"model": name, "scenario": fx["id"], "run": run,
                             "expected": fx["expected"], "observed": s["observed"],
                             "tool": r["tool"], "stopReason": r["stopReason"],
                             "match": s["match"], "errorClasses": s["errorClasses"],
                             "disqualifying": s["disqualifying"],
                             "schemaMissing": s["schemaMissing"], "schemaExtra": s["schemaExtra"],
                             "falseClaims": s["falseClaims"],
                             "factsCited": s["factsCited"], "factsAvailable": s["factsAvailable"],
                             "latencyMs": r["latencyMs"],
                             "inputTokens": u.get("inputTokens"), "outputTokens": u.get("outputTokens"),
                             "args": r["args"], "text": r["text"][:400]})
                flag = "OK " if s["match"] and not s["errorClasses"] else "!! " + ",".join(s["errorClasses"])
                exp = fx["expected"]
                exp_s = "|".join(exp) if isinstance(exp, list) else exp
                print(f"  {name:11s} {fx['id']} run{run}: {s['observed']:8s} (exp {exp_s:12s}) {flag}")
                for p in s["falseClaims"]:
                    print(f"      FALSE CLAIM: {p}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                         cwd=os.path.join(HERE, ".."), text=True).strip()
    except Exception:
        commit = "unknown"
    meta = {
        "timestamp": stamp,
        "account": acct,
        "region": REGION,
        # promptVersion pins the CONTROL ENVIRONMENT: results are only comparable
        # across runs that share it. A prompt change invalidates the comparison.
        "promptVersion": hashlib.sha256(system_text.encode()).hexdigest()[:12],
        "harnessCommit": commit,
        "scenariosSha": hashlib.sha256(open(SCENARIOS_PATH, "rb").read()).hexdigest()[:12],
        "models": {n: CANDIDATES[n] for n in a.models},
        "runsPerCell": a.runs,
        "note": "decide-only; no tool dispatched, no matter state touched",
    }
    print(f"\npromptVersion {meta['promptVersion']} | harness {commit} | scenarios {meta['scenariosSha']}")
    with open(os.path.join(RESULTS_DIR, f"runs-{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "runs": rows}, f, indent=2, default=str)
    cols = ["model", "scenario", "run", "expected", "observed", "tool", "match", "errorClasses",
            "disqualifying", "factsCited", "factsAvailable", "latencyMs", "inputTokens", "outputTokens"]
    with open(os.path.join(RESULTS_DIR, f"runs-{stamp}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)

    print("\n=== SUMMARY (cost is tokens only -- rates applied at report time, ADR-001 standing rule) ===")
    print(f"{'model':12s} {'correct':>9s} {'missedEsc':>10s} {'falseClaim':>11s} {'schema':>7s} "
          f"{'apiErr':>7s} {'facts':>8s} {'medLat':>8s} {'p90Lat':>8s} {'tokIn':>7s} {'tokOut':>7s}  verdict")
    print("-" * 126)
    for name in a.models:
        mr = [r for r in rows if r["model"] == name]
        # Infrastructure failures are not model failures. A transient
        # EndpointConnectionError says nothing about judgment, so api_error runs
        # are counted and reported separately, never scored against the model.
        api_err = sum(1 for r in mr if "api_error" in (r.get("errorClasses") or []))
        mr = [r for r in mr if "api_error" not in (r.get("errorClasses") or [])]
        correct = sum(1 for r in mr if r.get("match"))
        missed = sum(1 for r in mr if "missed_escalation" in (r.get("errorClasses") or []))
        false_c = sum(1 for r in mr if "false_factual_claim" in (r.get("errorClasses") or []))
        schema = sum(1 for r in mr if "schema_violation" in (r.get("errorClasses") or []))
        cited = sum(r.get("factsCited", 0) for r in mr); avail = sum(r.get("factsAvailable", 0) for r in mr)
        lats = sorted(r["latencyMs"] for r in mr if r.get("latencyMs"))
        med = int(statistics.median(lats)) if lats else 0
        p90 = lats[int(len(lats) * 0.9) - 1] if len(lats) >= 2 else (lats[0] if lats else 0)
        ti = sum(r.get("inputTokens") or 0 for r in mr); to = sum(r.get("outputTokens") or 0 for r in mr)
        dq = missed or false_c or schema
        verdict = "DISQUALIFIED" if dq else "eligible"
        print(f"{name:12s} {correct:>6d}/{len(mr):<2d} {missed:>10d} {false_c:>11d} {schema:>7d} "
              f"{api_err:>7d} {cited:>4d}/{avail:<3d} {med:>7d}ms {p90:>7d}ms {ti:>7d} {to:>7d}  {verdict}")

    print("\nPass bar (ADR-001): ANY missed escalation, false factual claim, or schema violation")
    print("in ANY single run disqualifies -- not averaged. A compliance system does not get")
    print("to average away a miss. Latency: median and p90; routed region varies per call.")
    print(f"\nresults -> evals/results/runs-{stamp}.{{json,csv}}")


if __name__ == "__main__":
    main()
