#!/usr/bin/env python3
"""Run the Document-Chase Agent on ONE matter. Thin CLI wrapper -- all logic is
in agent/core/decide.py, which the sweep and the deployed Lambda import
identically, so a single-matter run and a swept run cannot diverge in judgment.

Usage:
    EXPECTED_ACCOUNT=<id> python scripts/agent-loop.py [MTR-2026-0142] [--dry-run]

--dry-run decides and prints but neither dispatches nor writes an audit row, so
it is fully side-effect-free. (The SWEEP's dry-run differs deliberately: it does
write audit rows, because stage 1's purpose is a reviewable record.)
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import boto3  # noqa: E402
import hashlib  # noqa: E402

from agent.core import decide  # noqa: E402


def assert_account() -> str:
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit(
            "Set EXPECTED_ACCOUNT to the 12-digit account the agent lives in, e.g.\n"
            "  EXPECTED_ACCOUNT=<id> python scripts/agent-loop.py MTR-2026-0142"
        )
    actual = boto3.client("sts", region_name=decide.REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(
            f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.\n"
            "  unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN"
        )
    return actual


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    matter_id = args[0] if args else "MTR-2026-0142"

    acct = assert_account()
    table = boto3.resource("dynamodb", region_name=decide.REGION).Table(decide.TABLE)
    system_text = decide.load_prompt()
    gateway_url = os.environ.get("GATEWAY_URL") or decide.resolve_gateway_url()
    cfg = {
        "region": decide.REGION, "table": decide.TABLE, "model_id": decide.MODEL_ID,
        "gateway_url": gateway_url, "gateway_tool": decide.GATEWAY_TOOL,
        "system_text": system_text,
        "prompt_version": hashlib.sha256(system_text.encode()).hexdigest()[:12],
        "dry_run": dry_run,
        "dispatch": not dry_run,     # explicit opt-in; see the DISPATCH GATE
        "write_audit": not dry_run,
    }
    clients = {"table": table,
               "brt": boto3.client("bedrock-runtime", region_name=decide.REGION),
               "creds": boto3.Session().get_credentials().get_frozen_credentials()}

    print(f"=== agent-loop on {matter_id} {'(DRY RUN)' if dry_run else ''} ===")
    print(f"account {acct} | model {decide.MODEL_ID} | promptVersion {cfg['prompt_version']}")

    audit = decide.decide_and_act(matter_id, clients, cfg)

    print("\n--- reasoning ---")
    print(audit["reasoning"] or "(none)")
    print(f"\n--- decision: {audit['decision']['action']} (stopReason={audit['stopReason']}) ---")
    if audit["decision"]["toolInput"]:
        print("  tool input:", json.dumps(audit["decision"]["toolInput"]))
    print("  outcome:", json.dumps(audit["outcome"]))
    if audit["closingSummary"]:
        print("  closing:", audit["closingSummary"])

    if dry_run:
        print("\n(DRY RUN -- nothing dispatched, no audit row written)")
        return

    print(f"\n  audit row: {audit['SK']}  (modelRequestId={audit['modelRequestId']})")
    rows = decide.confirm_execution(table, matter_id)
    print("\n--- verification (strongly-consistent read; the ONLY proof) ---")
    for r in rows:
        print(f"    [OK] {r['SK']}: action={r.get('action')} docType={r.get('docType')} at={r.get('escalatedAt')}")
    print(">>> EXECUTION CONFIRMED" if rows else ">>> no ACTION#escalate row on a consistent read")


if __name__ == "__main__":
    main()
