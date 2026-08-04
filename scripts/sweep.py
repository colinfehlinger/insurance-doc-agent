#!/usr/bin/env python3
"""Scheduled sweep -- run the Document-Chase Agent across every matter needing a
decision. The production trigger is a daily EventBridge cron; this script is the
same logic, runnable by hand, which is what stage 1 of the rollout uses.

SAFETY POSTURE. This is the first thing in the system that acts unsupervised and
at volume, so every default is the safe one and every unsafe setting must be
turned on explicitly:

  DRY_RUN                 default TRUE  -- decide and record, dispatch NOTHING
  MAX_MATTERS_PER_RUN     default 50    -- bounds Bedrock spend per tick
  MAX_ESCALATIONS_PER_RUN default 10    -- email safety valve; see below

Rollout stages (ADR / sweep design, 2026-08-04) -- do not skip stage 1:
  1. DRY_RUN=true for several days against real data. AUDIT# rows are written so
     the decisions are reviewable; nothing is dispatched and no email is sent.
  2. DRY_RUN=false with MAX_MATTERS_PER_RUN=5.
  3. Cap raised once stage 2 is boring.

Three guards had to be correct before stage 1 could start at all:
  * PRE-FILTER  -- matters already carrying an ACTION#escalate row are skipped
                   BEFORE the model is called (see should_skip).
  * NORMALISATION -- the escalate Lambda canonicalises docType so its conditional
                   put stays a real idempotency guard (infra/lambdas/tools/escalate).
  * ISOLATION   -- one matter's failure must not abort the batch, and must leave
                   a trace (see sweep_once).
"""

import importlib.util
import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))

# agent-loop.py holds decide_and_act, the port-clean core. Loaded rather than
# duplicated so the sweep and a single-matter run can never diverge in judgment.
_spec = importlib.util.spec_from_file_location("agent_loop", os.path.join(HERE, "agent-loop.py"))
agent_loop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_loop)

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("MATTER_TABLE", "ida-dev-matters")
GSI = os.environ.get("GSI_NAME", "GSI1")

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
MAX_MATTERS_PER_RUN = int(os.environ.get("MAX_MATTERS_PER_RUN", "50"))
MAX_ESCALATIONS_PER_RUN = int(os.environ.get("MAX_ESCALATIONS_PER_RUN", "10"))
# How far ahead a missing document is worth chasing. Bounds the STATUS#missing
# query so the sweep does not pull documents nobody would act on yet.
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "7"))
# Bounds how many candidates the pre-filter will examine per tick. The filter
# runs BEFORE the cap (see select_eligible), so without a bound a large backlog
# of already-escalated matters would mean one query per matter, per tick.
MAX_CANDIDATES_EXAMINED = int(os.environ.get("MAX_CANDIDATES_EXAMINED", str(MAX_MATTERS_PER_RUN * 10)))


# --- candidate selection ------------------------------------------------------
def find_candidates(table, as_of: date) -> list[dict]:
    """Return deduped matter ids needing a decision, most-overdue-first.

    GSI1 COVERAGE, verified by query on 2026-08-04 -- known limitations, not
    assumptions:

      STATUS#missing    GSI1SK = DUE#<date>       range-queryable by due date.
                                                  This is the partition the index
                                                  was designed for and the only
                                                  one that gives ordering for free.
      STATUS#in-review  GSI1SK = RECEIVED#<ts>    keyed by RECEIPT, so it CANNOT
                                                  be range-queried by due date.
                                                  Pulled whole and filtered here.
      META rows         no GSI keys               invisible to any GSI query, so
                                                  the sweep is DOCUMENT-driven:
                                                  a matter with no actionable doc
                                                  row is never seen. A blocked
                                                  matter whose documents are all
                                                  received will not surface.

    REVISIT THRESHOLD: the in-review client-side filter is fine while that
    partition stays small. Revisit the GSI shape (a second index, or a sort key
    change with backfill) once STATUS#in-review exceeds ~1,000 documents, at
    which point pulling the whole partition each tick stops being cheap.

    Dedupe matters: one matter legitimately appears in BOTH partitions (a census
    in-review and an application missing -- MTR-2026-0142 does exactly this), and
    invoking the agent twice on one matter would double the cost and risk two
    escalations for one decision.
    """
    horizon = (as_of + timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    found: dict[str, dict] = {}

    def note(item, why):
        pk = item.get("PK", "")
        if not pk.startswith("MATTER#"):
            return
        mid = pk[len("MATTER#"):]
        due = item.get("dueDate")
        prev = found.get(mid)
        # keep the earliest due date seen, so ordering reflects the most urgent
        # document on the matter
        if prev is None or (due and (prev["dueDate"] is None or due < prev["dueDate"])):
            found[mid] = {"matterId": mid, "dueDate": due, "why": why}
        elif why not in prev["why"]:
            prev["why"] = f"{prev['why']}+{why}"

    # 1. Missing documents at or before the horizon -- ordered by due date.
    resp = table.query(
        IndexName=GSI,
        KeyConditionExpression=Key("GSI1PK").eq("STATUS#missing") & Key("GSI1SK").lte(f"DUE#{horizon}"),
        ScanIndexForward=True,
    )
    for it in resp.get("Items", []):
        note(it, "missing")

    # 2. In-review documents -- no due-date ordering available (see above), so
    #    the whole partition is read and filtered client-side. Low-confidence
    #    extractions warrant a look regardless of due date (the S2 case), so the
    #    horizon is NOT applied here; the agent makes the call.
    resp = table.query(IndexName=GSI, KeyConditionExpression=Key("GSI1PK").eq("STATUS#in-review"))
    for it in resp.get("Items", []):
        note(it, "in-review")

    # Most-overdue-first; matters with no due date sort last.
    return sorted(found.values(), key=lambda c: (c["dueDate"] is None, c["dueDate"] or ""))


# --- PRE-FILTER ---------------------------------------------------------------
def should_skip(table, matter_id: str) -> str | None:
    """Return a reason to skip this matter before spending a model call, or None.

    A matter already carrying ANY ACTION#escalate row has been handed to a human;
    re-deciding it is at best wasted tokens and at worst a duplicate escalation.

    This exists because the alternative guard is probabilistic. The agent DOES
    abstain on already-escalated matters -- the ADR-001 eval's S3 scenario, 3/3
    for both eligible models -- but that is the model choosing correctly, not the
    system being unable to act twice. At one invocation a day that distinction is
    academic; across a daily sweep of N matters it is the difference between a
    guarantee and a probability. Structural check first, model second.
    """
    rows = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}") & Key("SK").begins_with("ACTION#escalate"),
        ConsistentRead=True,
    )["Items"]
    if rows:
        return f"already escalated ({len(rows)} row(s): {rows[0]['SK']})"
    return None


# --- ERROR ISOLATION ----------------------------------------------------------
def write_error_audit(table, matter_id: str, err: Exception) -> str:
    """Record a failed matter as an AUDIT#-shaped row.

    A matter that throws must not vanish. Without this a systematically-failing
    matter is skipped every night forever and nothing anywhere says so. Written
    in the same AUDIT# shape as a successful decision so ONE query per matter
    returns its decisions and its failures together, in order.
    """
    ts = datetime.now(timezone.utc).isoformat()
    sk = f"AUDIT#{ts}#{uuid.uuid4().hex[:8]}"
    table.put_item(Item={
        "PK": f"MATTER#{matter_id}", "SK": sk,
        "ts": ts, "actor": "agent/sweep", "matterId": matter_id,
        "decision": {"action": "error", "toolName": None, "toolInput": None},
        "outcome": {"status": "error", "errorType": type(err).__name__, "error": str(err)[:900]},
        "reasoning": None, "stopReason": None,
    })
    return sk


# --- the sweep ----------------------------------------------------------------
def select_eligible(table, candidates: list[dict], cap: int, examine_limit: int):
    """Pre-filter BEFORE the cap, then take the top `cap` survivors.

    ORDER MATTERS, and getting it backwards is a production failure mode rather
    than an edge case. Capping first and filtering second means already-escalated
    matters consume cap slots and are then discarded, so a backlog of escalated
    matters starves the sweep: throughput degrades toward zero while genuinely
    new matters wait for a tick that never has room for them. Escalated matters
    accumulate -- they stay escalated until a human resolves them -- so the
    degradation is monotonic and silent. Nothing errors; the sweep just quietly
    stops doing work.

    Filtering first is affordable because the filter is a DynamoDB query with no
    model cost, which is the whole reason the pre-filter is cheap enough to run
    before the expensive thing.

    `examine_limit` bounds the query count so a pathological backlog cannot turn
    one tick into an unbounded scan; candidates are walked most-overdue-first, so
    whatever the limit truncates is always the least urgent tail.
    """
    eligible, skipped, examined = [], [], 0
    for cand in candidates:
        if len(eligible) >= cap or examined >= examine_limit:
            break
        examined += 1
        reason = should_skip(table, cand["matterId"])
        if reason:
            skipped.append({"matterId": cand["matterId"], "reason": reason})
        else:
            eligible.append(cand)
    return eligible, skipped, examined


def sweep_once(table, clients, cfg, as_of: date) -> dict:
    """One sweep tick. Returns a summary; never raises for a single bad matter."""
    candidates = find_candidates(table, as_of)
    considered = len(candidates)

    # PRE-FILTER, THEN CAP -- see select_eligible for why this order is load-bearing.
    capped, skipped, examined = select_eligible(
        table, candidates, MAX_MATTERS_PER_RUN, MAX_CANDIDATES_EXAMINED
    )

    results = {"consideredCandidates": considered, "examined": examined,
               "processed": 0, "skipped": skipped,
               "decisions": [], "errors": [], "escalations": 0,
               "capReached": len(capped) >= MAX_MATTERS_PER_RUN, "valveTripped": False}
    for s in skipped:
        print(f"  {s['matterId']:16s} SKIP  {s['reason']}")

    for cand in capped:
        mid = cand["matterId"]

        # EMAIL SAFETY VALVE. Beyond this many escalations in one tick, stop
        # dispatching and flag. A morning that produces 20 escalations is one
        # anomaly worth a human look, not 20 independent findings worth 20
        # emails -- and mass-mailing a broker list is not recoverable. Matters
        # past the valve are simply left for the next tick.
        if results["escalations"] >= MAX_ESCALATIONS_PER_RUN and not cfg["dry_run"]:
            results["valveTripped"] = True
            results["skipped"].append({"matterId": mid, "reason": "escalation valve tripped"})
            print(f"  {mid:16s} HOLD  escalation valve ({MAX_ESCALATIONS_PER_RUN}) reached")
            continue

        # ISOLATION: one matter's failure must not end the batch.
        try:
            audit = agent_loop.decide_and_act(mid, clients, cfg)
            action = audit["decision"]["action"]
            results["processed"] += 1
            results["decisions"].append({"matterId": mid, "action": action, "auditSK": audit["SK"]})
            if action == "escalate" and not cfg["dry_run"]:
                results["escalations"] += 1
            print(f"  {mid:16s} {action:9s} {'(dry-run, not dispatched)' if cfg['dry_run'] else ''}")
        except Exception as e:  # noqa: BLE001 -- deliberate: isolate, record, continue
            sk = write_error_audit(table, mid, e)
            results["errors"].append({"matterId": mid, "error": f"{type(e).__name__}: {e}", "auditSK": sk})
            print(f"  {mid:16s} ERROR {type(e).__name__}: {str(e)[:90]}")
            continue

    return results


def main() -> None:
    acct = agent_loop.assert_account()
    as_of = date.today()
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    system_text = open(agent_loop.PROMPT_PATH, encoding="utf-8").read()
    import hashlib
    cfg = {
        "region": REGION, "table": TABLE, "model_id": agent_loop.MODEL_ID,
        "gateway_url": agent_loop.resolve_gateway_url(REGION, agent_loop.GATEWAY_NAME),
        "gateway_tool": agent_loop.GATEWAY_TOOL, "system_text": system_text,
        "prompt_version": hashlib.sha256(system_text.encode()).hexdigest()[:12],
        # DRY_RUN is the operator-facing switch; `dispatch` is the gate the core
        # actually reads. Stated as an explicit opt-in so an omitted key can
        # never mean "send". write_audit stays True in every stage: stage 1's
        # whole purpose is a reviewable record of what it WOULD have done.
        "dry_run": DRY_RUN,
        "dispatch": not DRY_RUN,
        "write_audit": True,
    }
    clients = {"table": table, "brt": boto3.client("bedrock-runtime", region_name=REGION),
               "creds": boto3.Session().get_credentials().get_frozen_credentials()}

    mode = "DRY RUN -- deciding and recording, dispatching NOTHING" if DRY_RUN else "LIVE -- will dispatch"
    print(f"=== sweep {as_of} | account {acct} | {mode} ===")
    print(f"model {cfg['model_id']} | promptVersion {cfg['prompt_version']}")
    print(f"caps: {MAX_MATTERS_PER_RUN} matters/run, {MAX_ESCALATIONS_PER_RUN} escalations/run, "
          f"{LOOKAHEAD_DAYS}d lookahead\n")

    t0 = time.time()
    r = sweep_once(table, clients, cfg, as_of)
    r["elapsedMs"] = int((time.time() - t0) * 1000)

    print(f"\n=== summary ===")
    print(f"  examined         : {r['examined']} (pre-filter runs BEFORE the cap)")
    print(f"  candidates found : {r['consideredCandidates']}"
          f"{'  (CAP REACHED -- remainder deferred to next tick)' if r['capReached'] else ''}")
    print(f"  processed        : {r['processed']}")
    print(f"  skipped          : {len(r['skipped'])}")
    print(f"  errors           : {len(r['errors'])}  <- isolated; batch continued")
    print(f"  escalations      : {r['escalations']}"
          f"{'  (VALVE TRIPPED)' if r['valveTripped'] else ''}")
    print(f"  elapsed          : {r['elapsedMs']}ms")
    for d in r["decisions"]:
        print(f"    {d['matterId']:16s} {d['action']}")
    for e in r["errors"]:
        print(f"    ERROR {e['matterId']:16s} {e['error'][:80]}")
    if DRY_RUN:
        print("\nDRY RUN: AUDIT# rows written, nothing dispatched, no email sent.")


if __name__ == "__main__":
    main()
