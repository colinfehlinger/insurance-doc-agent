"""Scheduled sweep -- select matters needing a decision and run the agent on each.

Imported identically by scripts/sweep.py (hand runs) and
infra/lambdas/sweep/index.py (the deployed Lambda), so what a hand run does and
what the cron does are the same code.

SAFETY POSTURE -- every default is the safe one, every unsafe setting explicit:
  DRY_RUN                 default TRUE, and only the exact string "false"
                          disables it, so DRY_RUN=0 or a typo still dry-runs
  MAX_MATTERS_PER_RUN     default 50   bounds Bedrock spend per tick
  MAX_ESCALATIONS_PER_RUN default 10   email safety valve

Three guards had to be correct before any unsupervised run:
  PRE-FILTER    matters already carrying an ACTION#escalate row are skipped
                BEFORE the model is called, and before the cap (see below)
  NORMALISATION the escalate Lambda canonicalises docType so its conditional put
                stays a real idempotency guard (infra/lambdas/tools/escalate)
  ISOLATION     one matter's failure must not abort the batch, and must leave a
                trace
"""

import os
import uuid
from datetime import date, datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from . import decide

GSI = os.environ.get("GSI_NAME", "GSI1")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
MAX_MATTERS_PER_RUN = int(os.environ.get("MAX_MATTERS_PER_RUN", "50"))
MAX_ESCALATIONS_PER_RUN = int(os.environ.get("MAX_ESCALATIONS_PER_RUN", "10"))
# How far ahead a missing document is worth chasing.
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "7"))
# Bounds how many candidates the pre-filter examines per tick. The filter runs
# BEFORE the cap, so without a bound a large backlog of already-escalated matters
# would mean one query per matter, per tick.
MAX_CANDIDATES_EXAMINED = int(os.environ.get("MAX_CANDIDATES_EXAMINED", str(MAX_MATTERS_PER_RUN * 10)))


def find_candidates(table, as_of: date) -> list[dict]:
    """Deduped matter ids needing a decision, most-overdue-first.

    GSI1 COVERAGE, verified by live query 2026-08-04 -- limits, not assumptions:

      STATUS#missing    GSI1SK = DUE#<date>     range-queryable by due date; the
                                                partition the index was built for
      STATUS#in-review  GSI1SK = RECEIVED#<ts>  keyed by RECEIPT, so it CANNOT be
                                                range-queried by due date; pulled
                                                whole and filtered here
      META rows         no GSI keys             invisible to any GSI query, so the
                                                sweep is DOCUMENT-driven: a
                                                blocked matter whose documents are
                                                all received never surfaces

    REVISIT THRESHOLD: reading the in-review partition whole is fine while it
    stays small. Revisit the GSI shape (second index, or a sort-key change with
    backfill) once STATUS#in-review exceeds ~1,000 documents.

    Dedupe: one matter legitimately appears in BOTH partitions (a census
    in-review and an application missing -- MTR-2026-0142 does exactly this), and
    invoking twice would double the cost and risk two escalations for one
    decision.
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
        if prev is None or (due and (prev["dueDate"] is None or due < prev["dueDate"])):
            found[mid] = {"matterId": mid, "dueDate": due, "why": why}
        elif why not in prev["why"]:
            prev["why"] = f"{prev['why']}+{why}"

    resp = table.query(
        IndexName=GSI,
        KeyConditionExpression=Key("GSI1PK").eq("STATUS#missing") & Key("GSI1SK").lte(f"DUE#{horizon}"),
        ScanIndexForward=True,
    )
    for it in resp.get("Items", []):
        note(it, "missing")

    # Low-confidence extractions warrant a look regardless of due date (the S2
    # case), so the horizon is NOT applied here; the agent makes the call.
    resp = table.query(IndexName=GSI, KeyConditionExpression=Key("GSI1PK").eq("STATUS#in-review"))
    for it in resp.get("Items", []):
        note(it, "in-review")

    return sorted(found.values(), key=lambda c: (c["dueDate"] is None, c["dueDate"] or ""))


def should_skip(table, matter_id: str) -> str | None:
    """Reason to skip this matter before spending a model call, or None.

    A matter already carrying ANY ACTION#escalate row has been handed to a human.
    The agent DOES abstain on those -- ADR-001 S3, 3/3 both eligible models -- but
    that is the model choosing correctly, not the system being unable to act
    twice. Across a nightly sweep of N matters that is the difference between a
    guarantee and a probability. Structural check first, model second.

    Prefix-matches ACTION#escalate deliberately, so it catches legacy keys
    written before docType normalisation as well as normalised ones.
    """
    rows = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}") & Key("SK").begins_with("ACTION#escalate"),
        ConsistentRead=True,
    )["Items"]
    return f"already escalated ({len(rows)} row(s): {rows[0]['SK']})" if rows else None


def select_eligible(table, candidates: list[dict], cap: int, examine_limit: int):
    """Pre-filter BEFORE the cap, then take the top `cap` survivors.

    ORDER IS LOAD-BEARING, and getting it backwards is a production failure mode
    rather than an edge case. Capping first means already-escalated matters
    consume cap slots and are then discarded, so a backlog starves the sweep:
    throughput degrades toward zero while new matters wait for a tick that never
    has room. Escalated matters accumulate until a human resolves them, so the
    degradation is monotonic and silent -- nothing errors, the sweep just stops
    working. Verified: 60 escalated matters sorting ahead of 5 new ones against a
    cap of 50 gives 0-of-5 under the old order and 5-of-5 under this one.

    Filtering first is affordable precisely because the filter is a DynamoDB
    query with no model cost. `examine_limit` bounds the query count; candidates
    are walked most-overdue-first, so whatever it truncates is the least urgent
    tail.
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


def write_error_audit(table, matter_id: str, err: Exception) -> str:
    """Record a failed matter as an AUDIT#-shaped row.

    A matter that throws must not vanish. Without this, a systematically-failing
    matter is skipped every night forever and nothing says so. Same shape as a
    successful decision, so ONE query per matter returns decisions and failures
    together, in order.
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


def sweep_once(table, clients, cfg, as_of: date, log=print) -> dict:
    """One sweep tick. Returns a summary; never raises for a single bad matter."""
    candidates = find_candidates(table, as_of)
    considered = len(candidates)

    capped, skipped, examined = select_eligible(
        table, candidates, MAX_MATTERS_PER_RUN, MAX_CANDIDATES_EXAMINED
    )

    results = {"consideredCandidates": considered, "examined": examined,
               "processed": 0, "skipped": skipped, "decisions": [], "errors": [],
               "escalations": 0, "capReached": len(capped) >= MAX_MATTERS_PER_RUN,
               "valveTripped": False, "dryRun": bool(cfg.get("dry_run"))}
    for s in skipped:
        log(f"  {s['matterId']:16s} SKIP  {s['reason']}")

    for cand in capped:
        mid = cand["matterId"]

        # EMAIL SAFETY VALVE. Past this many escalations in one tick, stop
        # dispatching and flag. A morning producing 20 escalations is one anomaly
        # worth a human look, not 20 independent findings worth 20 emails -- and
        # mass-mailing a broker list is not recoverable. Held matters simply wait
        # for the next tick.
        if results["escalations"] >= MAX_ESCALATIONS_PER_RUN and not cfg.get("dry_run"):
            results["valveTripped"] = True
            results["skipped"].append({"matterId": mid, "reason": "escalation valve tripped"})
            log(f"  {mid:16s} HOLD  escalation valve ({MAX_ESCALATIONS_PER_RUN}) reached")
            continue

        # ISOLATION: one matter's failure must not end the batch.
        try:
            audit = decide.decide_and_act(mid, clients, cfg)
            action = audit["decision"]["action"]
            results["processed"] += 1
            results["decisions"].append({"matterId": mid, "action": action, "auditSK": audit["SK"]})
            if action == "escalate" and cfg.get("dispatch") is True:
                results["escalations"] += 1
            log(f"  {mid:16s} {action:9s} {'(dry-run, not dispatched)' if cfg.get('dry_run') else ''}")
        except Exception as e:  # noqa: BLE001 -- deliberate: isolate, record, continue
            sk = write_error_audit(table, mid, e)
            results["errors"].append({"matterId": mid, "error": f"{type(e).__name__}: {e}", "auditSK": sk})
            log(f"  {mid:16s} ERROR {type(e).__name__}: {str(e)[:90]}")
            continue

    return results


def build_cfg(system_text: str, gateway_url: str, *, dry_run: bool = DRY_RUN) -> dict:
    """Assemble the config every caller passes to decide_and_act.

    `dispatch` is an explicit opt-in derived from dry_run rather than an absent
    key, because the dispatch gate treats anything that is not literally True as
    "do not send". `write_audit` stays True in every stage: stage 1's whole
    purpose is a reviewable record of what the agent WOULD have done.
    """
    import hashlib
    return {
        "region": decide.REGION,
        "table": decide.TABLE,
        "model_id": decide.MODEL_ID,
        "gateway_url": gateway_url,
        "gateway_tool": decide.GATEWAY_TOOL,
        "system_text": system_text,
        "prompt_version": hashlib.sha256(system_text.encode()).hexdigest()[:12],
        "actor": "agent/sweep",
        "dry_run": dry_run,
        "dispatch": not dry_run,
        "write_audit": True,
    }
