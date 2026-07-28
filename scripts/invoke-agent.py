#!/usr/bin/env python3
"""Invoke the Document-Chase Agent (managed Harness) on ONE matter and show its
decision. The Step-6 thin-slice driver.

Reads the matter's real state from DynamoDB, hands it to the harness, and streams
back the agent's reasoning and the tool it chose. It does NOT act -- the tool
(escalate_to_human) does, through the Gateway; this script only invokes and
observes.

Why Python/boto3 (the seed/readout scripts shell out to the aws CLI): the
InvokeHarness API is NOT in the installed CLI (2.32.9) but IS in boto3
(botocore 1.43.40). Confirmed against the live harness on 2026-07-27.

Usage (from the repo root or anywhere):
    python scripts/invoke-agent.py            # defaults to MTR-2026-0142
    python scripts/invoke-agent.py MTR-2026-0157
"""

import json
import os
import sys
import uuid
from datetime import date

import boto3
from boto3.dynamodb.conditions import Key

# Force UTF-8 stdout. The agent's reasoning contains characters like the arrow
# (escalate -> not remind) that crash on Windows' default cp1252 console mid-run,
# which previously aborted the script before it could report the outcome.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REGION = "us-east-1"
STACK = "Ida-Dev-Agent"
TABLE = "ida-dev-matters"


def matter_state(matter_id: str) -> dict:
    """Read META + DOC# + ACTION# rows for a matter into a compact dict."""
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    items = table.query(KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}"))["Items"]
    meta, docs, actions = {}, [], []
    for it in items:
        sk = it["SK"]
        if sk == "META":
            meta = {k: v for k, v in it.items() if k not in ("PK", "SK")}
        elif sk.startswith("DOC#"):
            docs.append(
                {
                    "docType": sk[len("DOC#"):],
                    "status": it.get("status"),
                    "dueDate": it.get("dueDate"),
                    "extractionConfidence": it.get("extractionConfidence"),
                }
            )
        elif sk.startswith("ACTION#"):
            actions.append(
                {"action": it.get("action"), "actor": it.get("actor"), "reason": it.get("reason")}
            )
    return {"matterId": matter_id, "meta": meta, "documents": docs, "actionHistory": actions}


def build_prompt(state: dict) -> str:
    """Present the matter state and today's date, and ask for the next action.
    The judgment framing (remind vs escalate vs wait) lives in the harness's
    system prompt (agent/system-prompt.md); this only supplies the facts."""
    return (
        f"Today's date is {date.today().isoformat()}.\n\n"
        f"Decide the single next action for this matter, and take it by calling "
        f"the appropriate tool. Base your decision only on the state below.\n\n"
        f"Matter state:\n{json.dumps(state, default=str, indent=2)}"
    )


def assert_account() -> str:
    """Fail LOUD if the shell's credentials resolve to the wrong account.

    This script pins its region (us-east-1) but boto3 resolves the ACCOUNT from
    ambient credentials. A stray AWS_PROFILE / AWS_ACCESS_KEY_ID export silently
    sent earlier runs to a different account, where the whole invoke->tool->row
    flow happened -- while the operator's CLI checks looked at the real account
    and (correctly) saw nothing. That is how a confirmation script produced
    hollow "EXECUTION CONFIRMED" reads against the wrong table.

    The account id is not hard-coded (repo rule: no account id in the tree); it
    comes from EXPECTED_ACCOUNT, the same guard pattern as decom-A.sh. Unset ->
    hard failure, so the script can never silently run wherever the ambient
    credentials happen to point.
    """
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit(
            "Set EXPECTED_ACCOUNT to the 12-digit account the agent lives in, e.g.\n"
            "  EXPECTED_ACCOUNT=<id> python scripts/invoke-agent.py MTR-2026-0142"
        )
    actual = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(
            f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.\n"
            "A stray AWS_PROFILE / AWS_ACCESS_KEY_ID export is likely redirecting boto3.\n"
            "  unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN\n"
            "then re-check with:  aws sts get-caller-identity"
        )
    print(f"account: {actual} (matches EXPECTED_ACCOUNT) | region: {REGION}")
    return actual


def main() -> None:
    matter_id = sys.argv[1] if len(sys.argv) > 1 else "MTR-2026-0142"

    assert_account()  # refuse to run against the wrong account (loud, before anything)

    cf = boto3.client("cloudformation", region_name=REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    }
    harness_arn = outputs["HarnessArn"]

    state = matter_state(matter_id)
    if not state["meta"]:
        sys.exit(f"No matter {matter_id} found in {TABLE}. Seed it first.")

    print(f"=== invoking agent on {matter_id} ===")
    print(f"harness: {harness_arn}")
    for d in state["documents"]:
        print(f"  doc {d['docType']}: {d['status']} (due {d['dueDate']})")
    print("--- agent decision ---")

    ac = boto3.client("bedrock-agentcore", region_name=REGION)
    resp = ac.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=str(uuid.uuid4()),
        messages=[{"role": "user", "content": [{"text": build_prompt(state)}]}],
    )

    # Stream the converse-style events: accumulate assistant text, and capture any
    # tool call (name + assembled JSON input).
    text_parts: list[str] = []
    tool_name = None
    tool_input_parts: list[str] = []
    stop_reason = None

    for event in resp["stream"]:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tool_name = start["toolUse"].get("name")
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text_parts.append(delta["text"])
            if "toolUse" in delta and "input" in delta["toolUse"]:
                tool_input_parts.append(delta["toolUse"]["input"])
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
        elif "internalServerException" in event or "validationException" in event:
            print("  STREAM ERROR:", json.dumps(event, default=str))

    reasoning = "".join(text_parts).strip()
    if reasoning:
        print(reasoning)
    print()
    if tool_name:
        args = "".join(tool_input_parts)
        print(f">>> agent EMITTED a tool call: {tool_name}")
        print(f"    args: {args}")
    else:
        print(">>> agent emitted NO tool call (stop reason: %s)" % stop_reason)

    # Emitting a tool-use block is NOT the same as the tool executing. The earlier
    # hollow "passes" came from trusting the emitted block. So verify EXECUTION in
    # the account: does the escalation row exist on the matter? That -- plus the
    # email in the inbox -- is the real bar, per the Step-6 DoD.
    print("\n--- verifying EXECUTION in DynamoDB (not just the emitted block) ---")
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    rows = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}")
        & Key("SK").begins_with("ACTION#escalate")
    )["Items"]
    if rows:
        for r in rows:
            print(f"    [OK] {r['SK']}: status={r.get('status')} "
                  f"notified={r.get('notified')} at={r.get('escalatedAt')}")
            print(f"         reason: {r.get('reason')}")
        print(">>> EXECUTION CONFIRMED: the tool ran and wrote matter state.")
    else:
        print("    [ABSENT] no ACTION#escalate row on this matter.")
        print(">>> NOT EXECUTED: the agent may have reasoned/emitted a call, but the")
        print("    tool did not run. The slice has NOT met its bar (row + email).")


if __name__ == "__main__":
    main()
