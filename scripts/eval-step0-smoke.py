#!/usr/bin/env python3
"""ADR-001 eval, Step 0 -- tool-use smoke test. STANDALONE, DECIDE-ONLY.

Answers one question before any harness gets built: can each candidate model
emit a real `tool_use` block on Bedrock Converse with `toolChoice: auto` and a
two-tool toolConfig? Nova's tool-calling behaviour is unverified in this account,
and if it can't emit a tool call the 100x cost argument that motivates ADR-001
collapses -- worth knowing in four invocations, not eighty-four.

This is NOT the fixture matrix and NOT the scoring harness. Those are built
afterwards, only for models that pass here.

NO SIDE EFFECTS -- guaranteed structurally, not by discipline:
  * The only AWS clients constructed are `sts` (account guard) and
    `bedrock-runtime` (Converse). There is no DynamoDB resource, no Lambda
    client, no SNS client, and no Gateway call anywhere in this file.
  * Nothing is dispatched. The emitted tool call is inspected and discarded, so
    no ACTION#escalate row is written, no email is sent, and the escalate
    Lambda's idempotency guard is never touched.
  * The matter state is inlined below with a synthetic id (SMOKE-S1) and a
    pinned asOfDate. Nothing is read from the matters table, so a smoke run can
    neither depend on nor disturb real matter state.

Usage:
    EXPECTED_ACCOUNT=<id> python scripts/eval-step0-smoke.py
"""

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "agent", "system-prompt.md")

# Verified ACTIVE in-account via list_inference_profiles (ADR-001, 2026-08-03).
# These are looked-up IDs, not recalled ones -- a fabricated model id cost a full
# cycle in Step 6.
CANDIDATES = [
    ("sonnet-4-6 (incumbent)", "us.anthropic.claude-sonnet-4-6"),
    ("haiku-4-5", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("nova-lite", "us.amazon.nova-lite-v1:0"),
    ("nova-micro", "us.amazon.nova-micro-v1:0"),
]

# The same two-tool toolConfig the real eval will use (ADR-001). escalate_to_human
# mirrors the deployed Gateway target's schema exactly; send_reminder is
# SCHEMA-ONLY -- it has no Lambda behind it and is never dispatched. Its presence
# is what makes tool selection a genuine three-way choice rather than a
# one-hammer test that any escalate-everything model would pass.
TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "escalate_to_human",
                "description": (
                    "Escalate a matter to a human owner with a reason. Records the "
                    "escalation on the matter (audit trail) and notifies the owner."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "matterId": {"type": "string", "description": "The matter to escalate, e.g. MTR-2026-0142."},
                            "docType": {
                                "type": "string",
                                "description": "The document the escalation concerns, e.g. census. Optional; defaults to the matter as a whole.",
                            },
                            "reason": {
                                "type": "string",
                                "description": "Why this needs a human, in terms of the matter state relied on: which document, which dates, which prior actions.",
                            },
                        },
                        "required": ["matterId", "reason"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "send_reminder",
                "description": (
                    "Send a reminder to the counterparty for an outstanding document. "
                    "Use when a document is missing or incomplete and its due date has "
                    "not yet passed."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "matterId": {"type": "string", "description": "The matter the reminder concerns."},
                            "docType": {"type": "string", "description": "The outstanding document, e.g. census."},
                            "message": {
                                "type": "string",
                                "description": "The reminder body. Short and specific: name the exact document outstanding and its exact due date.",
                            },
                        },
                        "required": ["matterId", "message"],
                    }
                },
            }
        },
    ],
    "toolChoice": {"auto": {}},
}

# S1 scenario shape (overdue + low confidence + missing doc), matching the real
# MTR-2026-0142 state that produced a verified escalation in Step 6 -- so this
# smoke test exercises the same decision the eval will. asOfDate is PINNED, so
# the input never drifts; the matter id is synthetic and exists in no table.
AS_OF_DATE = "2026-08-03"
S1_STATE = {
    "matterId": "SMOKE-S1",
    "meta": {
        "clientName": "Northwind Manufacturing",
        "matterType": "group-renewal",
        "status": "blocked",
        "counterpartyName": "Dana Whitfield",
        "targetCloseDate": "2026-08-01",
    },
    "documents": [
        {"docType": "census", "status": "in-review", "dueDate": "2026-07-25", "extractionConfidence": 0.256},
        {"docType": "signed-employer-application", "status": "missing", "dueDate": "2026-07-30", "extractionConfidence": None},
    ],
    "actionHistory": [
        {"action": "reminder_sent", "actor": "agent", "reason": "Census missing, due 2026-07-24."}
    ],
}


def assert_account() -> str:
    """Refuse to run against the wrong account -- same guard as agent-loop.py."""
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit(
            "Set EXPECTED_ACCOUNT to the 12-digit account to run in, e.g.\n"
            "  EXPECTED_ACCOUNT=<id> python scripts/eval-step0-smoke.py"
        )
    actual = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(
            f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.\n"
            "  unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN"
        )
    return actual


def build_prompt(state: dict) -> str:
    return (
        f"Today's date is {AS_OF_DATE}.\n\n"
        f"Decide the single next action for this matter and, if the state warrants "
        f"it, take that action by calling the appropriate tool. If the state does "
        f"not justify an action, do nothing and say why. Base your decision only "
        f"on the state below.\n\nMatter state:\n{json.dumps(state, indent=2)}"
    )


def check_schema(tool_name: str, args: dict) -> tuple[set, set]:
    """Return (missing required fields, hallucinated fields) for the emitted args.

    The hallucinated-field check is not hypothetical: when toolConfig was absent
    entirely, the model invented an `urgency` field (Step 6, ADR-007). A model
    that binds the schema loosely is a model that will produce tool calls the
    Gateway rejects.
    """
    spec = next(
        t["toolSpec"] for t in TOOL_CONFIG["tools"] if t["toolSpec"]["name"] == tool_name
    )
    schema = spec["inputSchema"]["json"]
    props = set(schema["properties"])
    required = set(schema.get("required", []))
    return required - set(args), set(args) - props


def smoke_one(brt, label: str, model_id: str, system_text: str, prompt: str) -> dict:
    """One Converse call. Returns a result row; never raises, never dispatches."""
    row = {
        "label": label, "modelId": model_id, "stopReason": None, "tool": None,
        "args": None, "missing": set(), "extra": set(), "latencyMs": None,
        "wallMs": None, "error": None, "text": "",
    }
    t0 = time.time()
    try:
        resp = brt.converse(
            modelId=model_id,
            system=[{"text": system_text}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            toolConfig=TOOL_CONFIG,
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
    except ClientError as e:
        # Report the exact error. No retry, no workaround, no fallback -- an API
        # rejection IS the Step-0 finding for that model.
        row["error"] = f"{e.response['Error']['Code']}: {e.response['Error']['Message']}"
        row["wallMs"] = int((time.time() - t0) * 1000)
        return row
    except Exception as e:  # noqa: BLE001 -- surface anything else verbatim too
        row["error"] = f"{type(e).__name__}: {e}"
        row["wallMs"] = int((time.time() - t0) * 1000)
        return row

    row["wallMs"] = int((time.time() - t0) * 1000)
    row["latencyMs"] = resp.get("metrics", {}).get("latencyMs")
    row["stopReason"] = resp.get("stopReason")
    row["usage"] = resp.get("usage", {})

    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            row["tool"] = block["toolUse"]["name"]
            row["args"] = block["toolUse"].get("input", {})
        elif "text" in block:
            row["text"] += block["text"]

    if row["tool"]:
        row["missing"], row["extra"] = check_schema(row["tool"], row["args"] or {})
    return row


def verdict(row: dict) -> tuple[str, str]:
    """Step 0 gates CAPABILITY (can it emit a bound tool call), not judgment.
    Which tool it chose is reported as an early signal but does not gate here --
    tool-selection quality is what the fixture matrix measures."""
    if row["error"]:
        return "FAIL", "API error"
    if row["stopReason"] != "tool_use" or not row["tool"]:
        return "FAIL", f"no tool_use (stopReason={row['stopReason']})"
    if row["missing"]:
        return "FAIL", f"missing required {sorted(row['missing'])}"
    if row["extra"]:
        return "FAIL", f"hallucinated fields {sorted(row['extra'])}"
    return "PASS", "emitted bound tool call"


def main() -> None:
    acct = assert_account()
    system_text = open(PROMPT_PATH, encoding="utf-8").read()
    prompt = build_prompt(S1_STATE)
    brt = boto3.client("bedrock-runtime", region_name=REGION)

    print("=== ADR-001 Step 0 -- tool-use smoke test (decide-only, no side effects) ===")
    print(f"account {acct} | region {REGION} | asOfDate {AS_OF_DATE} | scenario S1 (expect escalate)")
    print(f"toolConfig: escalate_to_human + send_reminder (schema-only) | toolChoice: auto\n")

    rows = []
    for label, model_id in CANDIDATES:
        print(f"--- {label} ---")
        row = smoke_one(brt, label, model_id, system_text, prompt)
        rows.append(row)
        v, why = verdict(row)
        print(f"  verdict     : {v} ({why})")
        if row["error"]:
            print(f"  ERROR       : {row['error']}")
        else:
            print(f"  stopReason  : {row['stopReason']}")
            print(f"  tool        : {row['tool']}")
            if row["args"] is not None:
                print(f"  args        : {json.dumps(row['args'])[:220]}")
            print(f"  latencyMs   : {row['latencyMs']} (wall {row['wallMs']}ms)")
            u = row.get("usage") or {}
            print(f"  tokens      : in={u.get('inputTokens')} out={u.get('outputTokens')}")
            if not row["tool"] and row["text"]:
                print(f"  text (head) : {row['text'].strip()[:200]}")
        print()

    print("=== STEP 0 RESULT ===")
    print(f"{'model':26s} {'verdict':6s} {'stopReason':12s} {'tool':20s} {'schema':10s} {'latency':>9s}")
    print("-" * 92)
    for row in rows:
        v, why = verdict(row)
        schema = "-" if row["error"] or not row["tool"] else (
            "ok" if not row["missing"] and not row["extra"] else "BAD"
        )
        lat = f"{row['latencyMs']}ms" if row["latencyMs"] else f"~{row['wallMs']}ms"
        print(f"{row['label']:26s} {v:6s} {str(row['stopReason'] or 'ERROR'):12s} "
              f"{str(row['tool'] or '-'):20s} {schema:10s} {lat:>9s}")

    passed = [r["label"] for r in rows if verdict(r)[0] == "PASS"]
    failed = [r["label"] for r in rows if verdict(r)[0] == "FAIL"]
    print(f"\npass -> eligible for the fixture matrix: {passed or 'NONE'}")
    print(f"fail -> excluded, see error above       : {failed or 'none'}")

    # Early judgment signal only -- NOT a Step-0 gate. S1 unambiguously warrants
    # escalation; a model that emitted a valid call to the wrong tool passes
    # Step 0 (it can call tools) but is flagged here for the matrix to measure.
    wrong = [r["label"] for r in rows
             if verdict(r)[0] == "PASS" and r["tool"] != "escalate_to_human"]
    if wrong:
        print(f"\nNOTE: passed capability but chose a tool other than escalate_to_human "
              f"on S1: {wrong}\n      Capability is the Step-0 gate; judgment is what the "
              f"matrix measures. Recorded, not disqualifying.")


if __name__ == "__main__":
    main()
