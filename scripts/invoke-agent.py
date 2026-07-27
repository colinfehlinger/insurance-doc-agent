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
import sys
import uuid
from datetime import date

import boto3
from boto3.dynamodb.conditions import Key

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


def main() -> None:
    matter_id = sys.argv[1] if len(sys.argv) > 1 else "MTR-2026-0142"

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
        print(f">>> TOOL CALLED: {tool_name}")
        print(f"    args: {args}")
    else:
        print(">>> NO TOOL CALLED (stop reason: %s)" % stop_reason)
        print("    For MTR-2026-0142 (overdue in-review census), the slice EXPECTS")
        print("    escalate_to_human. No tool call = the slice has correctly failed.")


if __name__ == "__main__":
    main()
