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
import json
import os
import re
import statistics
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
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
FORWARD_N = re.compile(r"\bdue\s+(?:in|within)\s+(\d+)\s+(?:business\s+)?days?\b", re.I)
FORWARD_SOFT = re.compile(r"\bdue\s+(?:in|within)\s+\w+\s+days?\b|\bnot\s+yet\s+due\b|\bstill\s+has\s+time\b", re.I)
BACKWARD_N = re.compile(r"\b(\d+)\s+days?\s+(?:overdue|late|past\s+due)\b", re.I)
BACKWARD_SOFT = re.compile(r"\boverdue\b|\bpast\s+due\b|\bwas\s+due\b|\bmissed\s+(?:the\s+)?due\s+date\b", re.I)
CLAIM_RECEIVED = re.compile(r"\b(?:we\s+)?(?:have\s+)?received\b|\bhas\s+arrived\b", re.I)
CLAIM_MISSING = re.compile(r"\bis\s+missing\b|\bhas\s+not\s+been\s+(?:received|submitted)\b|\bstill\s+outstanding\b", re.I)


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
    docs = fixture["state"]["documents"]
    valid_dates = {d.get("dueDate") for d in docs if d.get("dueDate")}
    valid_dates.add(fixture["state"]["meta"].get("targetCloseDate"))

    # 1. Any ISO date asserted must correspond to a real date in the matter.
    for m in ISO_DATE.finditer(text):
        if m.group(1) not in valid_dates:
            problems.append(f"cites date {m.group(1)}, which is not any dueDate or the targetCloseDate")

    doc = _subject_doc(fixture, args, text)
    if doc and doc.get("dueDate"):
        delta = (date.fromisoformat(doc["dueDate"]) - as_of).days  # <0 == overdue

        # 2. Direction inversion -- the Nova Lite failure class.
        fwd_n = FORWARD_N.search(text)
        if (fwd_n or FORWARD_SOFT.search(text)) and delta < 0:
            problems.append(
                f"claims '{doc['docType']}' is still upcoming, but it was due "
                f"{doc['dueDate']} and is {abs(delta)} days OVERDUE as of {fixture['asOfDate']}"
            )
        if (BACKWARD_N.search(text) or BACKWARD_SOFT.search(text)) and delta > 0:
            problems.append(
                f"claims '{doc['docType']}' is overdue, but it is not due until "
                f"{doc['dueDate']} ({delta} days away as of {fixture['asOfDate']})"
            )

        # 3. Day-count magnitude, only when the direction is already right.
        if fwd_n and delta > 0 and int(fwd_n.group(1)) != delta:
            problems.append(f"says due in {fwd_n.group(1)} days; actual is {delta}")
        bwd_n = BACKWARD_N.search(text)
        if bwd_n and delta < 0 and int(bwd_n.group(1)) != abs(delta):
            problems.append(f"says {bwd_n.group(1)} days overdue; actual is {abs(delta)}")

        # 4. Status assertions.
        if CLAIM_RECEIVED.search(text) and doc.get("status") == "missing":
            problems.append(f"implies '{doc['docType']}' was received; status is missing")
        if CLAIM_MISSING.search(text) and doc.get("status") == "received":
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


def classify_error(expected: str, observed: str, false_claims: list, schema_bad: bool) -> str | None:
    if false_claims:
        return "false_factual_claim"          # disqualifying
    if expected == "escalate" and observed != "escalate":
        return "missed_escalation"            # disqualifying
    if schema_bad:
        return "schema_violation"             # disqualifying
    if expected == "none" and observed == "escalate":
        return "spurious_escalation"
    if expected == "remind" and observed == "escalate":
        return "over_caution"
    if expected != observed:
        return "wrong_action"
    return None


DISQUALIFYING = {"false_factual_claim", "missed_escalation", "schema_violation"}


def score_run(fixture: dict, tool: str | None, args: dict, text: str) -> dict:
    observed = ACTION_FOR_TOOL.get(tool, "none")
    missing, extra = check_schema(tool, args) if tool else (set(), set())
    claim_text = " ".join(filter(None, [(args or {}).get("message"), (args or {}).get("reason"), text]))
    false_claims = verify_claims(claim_text, fixture, args)
    cited, avail = score_facts(claim_text, fixture.get("groundedFacts", []))
    err = classify_error(fixture["expected"], observed, false_claims, bool(missing or extra))
    return {
        "observed": observed, "match": observed == fixture["expected"],
        "schemaMissing": sorted(missing), "schemaExtra": sorted(extra),
        "falseClaims": false_claims, "factsCited": cited, "factsAvailable": avail,
        "errorClass": err, "disqualifying": err in DISQUALIFYING,
    }


# --- self-test ----------------------------------------------------------------
def self_test(scenarios: dict) -> int:
    """Validate the scorer against real recorded outputs before trusting it.

    A detector that cannot catch the failure that motivated it is worthless, so
    the known-positive here is the verbatim Nova Lite message from Step 0, and
    the known-negative is Haiku's correct reason from the same run.
    """
    s1 = next(s for s in scenarios["scenarios"] if s["id"] == "S1")
    print("=== scorer self-test (no API calls) ===\n")
    ok = True

    print("[known-positive] Nova Lite, Step 0, verbatim:")
    args = {"matterId": "EVAL-S1", "docType": "signed-employer-application",
            "message": "The signed employer application is missing and is due in 2 days. Please submit it by 2026-07-30."}
    r = score_run(s1, "send_reminder", args, "")
    print(f"  observed={r['observed']} errorClass={r['errorClass']} disqualifying={r['disqualifying']}")
    for p in r["falseClaims"]:
        print(f"    FALSE CLAIM: {p}")
    if r["errorClass"] != "false_factual_claim":
        print("  !! FAIL -- detector missed the inversion it was built for"); ok = False
    else:
        print("  PASS -- inversion caught")

    print("\n[known-negative] Haiku 4.5, Step 0, verbatim:")
    args = {"matterId": "EVAL-S1", "reason": "Matter is blocked with two past-due documents: census due 2026-07-25 (9 days overdue, in-review with very low extraction confidence of 0.256) and signed employer application due 2026-07-30."}
    r = score_run(s1, "escalate_to_human", args, "")
    print(f"  observed={r['observed']} errorClass={r['errorClass']} facts={r['factsCited']}/{r['factsAvailable']}")
    for p in r["falseClaims"]:
        print(f"    FALSE CLAIM: {p}")
    if r["falseClaims"] or r["errorClass"]:
        print("  !! FAIL -- flagged a correct message"); ok = False
    else:
        print("  PASS -- correct message not flagged; facts scored")

    print("\n[known-negative] Nova Micro, Step 0, verbatim (correct action, thin grounding):")
    args = {"matterId": "EVAL-S1", "docType": "census",
            "reason": "The matter is blocked and past the target close date. The census document is in review with low extraction confidence."}
    r = score_run(s1, "escalate_to_human", args, "")
    print(f"  observed={r['observed']} errorClass={r['errorClass']} facts={r['factsCited']}/{r['factsAvailable']}")
    if r["errorClass"]:
        print("  !! FAIL -- correct escalation flagged as an error"); ok = False
    elif r["factsCited"] >= 3:
        print("  !! FAIL -- thin reason scored as fully grounded"); ok = False
    else:
        print(f"  PASS -- correct action, and grounding scored low ({r['factsCited']}/{r['factsAvailable']}) as expected")

    print("\n=== self-test: " + ("ALL PASS" if ok else "FAILURES ABOVE") + " ===")
    return 0 if ok else 1


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
    ap.add_argument("--dry-run", action="store_true", help="show the plan, make no calls")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--models", nargs="*", default=list(CANDIDATES))
    a = ap.parse_args()

    scenarios = json.load(open(SCENARIOS_PATH, encoding="utf-8"))
    if a.self_test:
        sys.exit(self_test(scenarios))

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
                                 "errorClass": "api_error", "disqualifying": True,
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
                             "match": s["match"], "errorClass": s["errorClass"],
                             "disqualifying": s["disqualifying"],
                             "schemaMissing": s["schemaMissing"], "schemaExtra": s["schemaExtra"],
                             "falseClaims": s["falseClaims"],
                             "factsCited": s["factsCited"], "factsAvailable": s["factsAvailable"],
                             "latencyMs": r["latencyMs"],
                             "inputTokens": u.get("inputTokens"), "outputTokens": u.get("outputTokens"),
                             "args": r["args"], "text": r["text"][:400]})
                flag = "OK " if s["match"] and not s["errorClass"] else f"!! {s['errorClass']}"
                print(f"  {name:11s} {fx['id']} run{run}: {s['observed']:8s} (exp {fx['expected']:8s}) {flag}")
                for p in s["falseClaims"]:
                    print(f"      FALSE CLAIM: {p}")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    with open(os.path.join(RESULTS_DIR, f"runs-{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str)
    cols = ["model", "scenario", "run", "expected", "observed", "tool", "match", "errorClass",
            "disqualifying", "factsCited", "factsAvailable", "latencyMs", "inputTokens", "outputTokens"]
    with open(os.path.join(RESULTS_DIR, f"runs-{stamp}.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)

    print("\n=== SUMMARY (cost is tokens only -- rates applied at report time, ADR-001 standing rule) ===")
    print(f"{'model':12s} {'correct':>9s} {'missedEsc':>10s} {'falseClaim':>11s} {'schema':>7s} "
          f"{'facts':>8s} {'medLat':>8s} {'p90Lat':>8s} {'tokIn':>7s} {'tokOut':>7s}  verdict")
    print("-" * 118)
    for name in a.models:
        mr = [r for r in rows if r["model"] == name]
        correct = sum(1 for r in mr if r.get("match"))
        missed = sum(1 for r in mr if r.get("errorClass") == "missed_escalation")
        false_c = sum(1 for r in mr if r.get("errorClass") == "false_factual_claim")
        schema = sum(1 for r in mr if r.get("errorClass") == "schema_violation")
        cited = sum(r.get("factsCited", 0) for r in mr); avail = sum(r.get("factsAvailable", 0) for r in mr)
        lats = sorted(r["latencyMs"] for r in mr if r.get("latencyMs"))
        med = int(statistics.median(lats)) if lats else 0
        p90 = lats[int(len(lats) * 0.9) - 1] if len(lats) >= 2 else (lats[0] if lats else 0)
        ti = sum(r.get("inputTokens") or 0 for r in mr); to = sum(r.get("outputTokens") or 0 for r in mr)
        dq = missed or false_c or schema
        verdict = "DISQUALIFIED" if dq else "eligible"
        print(f"{name:12s} {correct:>6d}/{len(mr):<2d} {missed:>10d} {false_c:>11d} {schema:>7d} "
              f"{cited:>4d}/{avail:<3d} {med:>7d}ms {p90:>7d}ms {ti:>7d} {to:>7d}  {verdict}")

    print("\nPass bar (ADR-001): ANY missed escalation, false factual claim, or schema violation")
    print("in ANY single run disqualifies -- not averaged. A compliance system does not get")
    print("to average away a miss. Latency: median and p90; routed region varies per call.")
    print(f"\nresults -> evals/results/runs-{stamp}.{{json,csv}}")


if __name__ == "__main__":
    main()
