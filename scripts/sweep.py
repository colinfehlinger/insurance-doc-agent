#!/usr/bin/env python3
"""Hand-run the scheduled sweep. Thin CLI wrapper -- all logic is in
agent/core/sweep.py, which the deployed Lambda imports identically.

Usage:
    EXPECTED_ACCOUNT=<id> python scripts/sweep.py              # DRY RUN (default)
    EXPECTED_ACCOUNT=<id> DRY_RUN=false python scripts/sweep.py

Note the deployed Lambda -- not this script -- is what stage 1 of the rollout
runs. This exercises the judgment but none of the packaging, bundling, or IAM,
which are the parts most likely to be wrong.
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import boto3  # noqa: E402

from agent.core import decide, sweep  # noqa: E402


def assert_account() -> str:
    """Refuse to run against the wrong account. Script-only -- a Lambda's account
    is fixed by where it is deployed, so the handler does not call this."""
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit("Set EXPECTED_ACCOUNT to the 12-digit account the agent lives in.")
    actual = boto3.client("sts", region_name=decide.REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.")
    return actual


def main() -> None:
    acct = assert_account()
    table = boto3.resource("dynamodb", region_name=decide.REGION).Table(decide.TABLE)
    gateway_url = os.environ.get("GATEWAY_URL") or decide.resolve_gateway_url()
    cfg = sweep.build_cfg(decide.load_prompt(), gateway_url)
    clients = {"table": table,
               "brt": boto3.client("bedrock-runtime", region_name=decide.REGION),
               "creds": boto3.Session().get_credentials().get_frozen_credentials()}

    mode = "DRY RUN -- deciding and recording, dispatching NOTHING" if cfg["dry_run"] else "LIVE -- will dispatch"
    print(f"=== sweep {date.today()} | account {acct} | {mode} ===")
    print(f"model {cfg['model_id']} | promptVersion {cfg['prompt_version']}")
    print(f"caps: {sweep.MAX_MATTERS_PER_RUN} matters/run, "
          f"{sweep.MAX_ESCALATIONS_PER_RUN} escalations/run, {sweep.LOOKAHEAD_DAYS}d lookahead\n")

    r = sweep.sweep_once(table, clients, cfg, date.today())

    print("\n=== summary ===")
    print(f"  candidates found : {r['consideredCandidates']}")
    print(f"  examined         : {r['examined']} (pre-filter runs BEFORE the cap)")
    print(f"  processed        : {r['processed']}")
    print(f"  skipped          : {len(r['skipped'])}")
    print(f"  errors           : {len(r['errors'])}  <- isolated; batch continued")
    print(f"  escalations      : {r['escalations']}{'  (VALVE TRIPPED)' if r['valveTripped'] else ''}")
    if cfg["dry_run"]:
        print("\nDRY RUN: AUDIT# rows written, nothing dispatched, no email sent.")


if __name__ == "__main__":
    main()
