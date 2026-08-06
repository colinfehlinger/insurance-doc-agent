#!/usr/bin/env python3
"""Pause / resume the sweep's DISPATCH without stopping the sweep itself.

The middle rung of the three levers:

  pause  (this script)        stops DISPATCH, keeps the daily run and its full
                              audit trail -- you come back to a complete record
                              of what the agent WOULD have done, not a gap
  schedule DISABLED           stops unattended runs, hand-invoke still works
  concurrency 0               stops everything, including the record

Sets DRY_RUN, which the handler treats as a HARD FLOOR: with it true, no event
payload can force a dispatch. That is what makes this a real pause rather than a
default that something else can override.

WHY A SCRIPT AND NOT A ONE-LINER: update-function-configuration REPLACES the
entire environment map. A hand-written --environment flag that forgets
GATEWAY_URL does not fail loudly -- it deploys a sweep that raises KeyError on
every run. This reads the live map, changes exactly one key, and verifies the
rest survived.

Usage:
    EXPECTED_ACCOUNT=<id> python scripts/sweep-pause.py status
    EXPECTED_ACCOUNT=<id> python scripts/sweep-pause.py pause
    EXPECTED_ACCOUNT=<id> python scripts/sweep-pause.py resume
"""

import os
import sys
import time

import boto3

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REGION = os.environ.get("AWS_REGION", "us-east-1")
FN = os.environ.get("SWEEP_FUNCTION", "ida-dev-sweep")


def assert_account(lam) -> str:
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit("Set EXPECTED_ACCOUNT to the 12-digit account the sweep lives in.")
    actual = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.")
    return actual


def show(env: dict, label: str) -> None:
    print(f"  {label}: DRY_RUN={env.get('DRY_RUN')!r}  "
          f"({'PAUSED -- dispatch blocked' if env.get('DRY_RUN') == 'true' else 'LIVE -- will dispatch'})")


def set_dry_run(lam, value: str) -> dict:
    cfg = lam.get_function_configuration(FunctionName=FN)
    env = dict(cfg["Environment"]["Variables"])
    before = set(env)
    show(env, "before")
    if env.get("DRY_RUN") == value:
        print(f"  already {value!r} -- nothing to do")
        return env
    env["DRY_RUN"] = value
    lam.update_function_configuration(FunctionName=FN, Environment={"Variables": env})
    # Wait for the update to settle; reading back too early returns the old map.
    for _ in range(30):
        time.sleep(2)
        cfg = lam.get_function_configuration(FunctionName=FN)
        if cfg.get("LastUpdateStatus") != "InProgress":
            break
    after = dict(cfg["Environment"]["Variables"])
    show(after, "after ")
    # The whole reason this is a script: prove nothing else was dropped.
    lost = before - set(after)
    if lost:
        sys.exit(f"  !! ENV KEYS LOST: {sorted(lost)} -- the sweep will break. Restore immediately.")
    print(f"  all {len(after)} env keys preserved: {sorted(after)}")
    return after


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    lam = boto3.client("lambda", region_name=REGION)
    acct = assert_account(lam)
    print(f"=== sweep dispatch control | {FN} | account {acct} ===")

    if action == "status":
        env = lam.get_function_configuration(FunctionName=FN)["Environment"]["Variables"]
        show(env, "current")
        conc = lam.get_function_concurrency(FunctionName=FN).get("ReservedConcurrentExecutions")
        print(f"  reserved concurrency: {conc if conc is not None else '(none -- not killed)'}")
        sched = boto3.client("scheduler", region_name=REGION).get_schedule(Name=f"{FN}-daily")
        print(f"  schedule: {sched['State']} {sched['ScheduleExpression']} {sched['ScheduleExpressionTimezone']}")
    elif action == "pause":
        set_dry_run(lam, "true")
        print("\n  PAUSED. The sweep still runs daily and still writes AUDIT# rows;")
        print("  it dispatches nothing. No payload can override this.")
    elif action == "resume":
        set_dry_run(lam, "false")
        print("\n  RESUMED. The sweep will DISPATCH on its next run.")
    else:
        sys.exit(f"unknown action {action!r} -- use status | pause | resume")


if __name__ == "__main__":
    main()
