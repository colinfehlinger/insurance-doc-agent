#!/usr/bin/env python3
"""Document-Chase Agent -- client-side reasoning loop (Path 2, ADR-007).

Replaces the managed AgentCore Harness, which did not inject tools into the
ConverseStream request for its runtime version (ADR-007: gateway, inline, and
built-in tools were all absent from the literal logged request). This loop owns
the orchestration the Harness could not:

  1. read the matter's real state from DynamoDB;
  2. call Bedrock Converse directly with a toolConfig built from the escalate
     tool's schema and toolChoice=auto (so "do nothing" stays a valid outcome);
  3. on a tool_use, dispatch the call through the AgentCore Gateway (SigV4 / MCP
     tools/call) to the escalate Lambda, which writes the ACTION#escalate row and
     sends the SNS email -- the governed tool surface still does the acting;
  4. write a coherent AUDIT# decision record tying reasoning -> decision ->
     outcome, with correlation ids into the model-invocation logs and the
     Gateway, so "the agent's decision is auditable" survives the pivot.

`decide_and_act()` is the port-clean core: no argv / print / sys.exit inside it,
clients injected, config from env. The production trigger (a state-change or
scheduled-sweep EventBridge rule) wraps this same core in a Lambda handler; this
script's __main__ is only the operator wrapper. See ADR-007 for the decision.

The judgment (escalate vs remind vs do-nothing) lives in agent/system-prompt.md,
loaded verbatim as the Converse system prompt -- a change to it is a change to
the control environment, and its sha is recorded on every decision.

Usage:
    EXPECTED_ACCOUNT=<id> python scripts/agent-loop.py [MTR-2026-0142] [--dry-run]

--dry-run reasons and prints the decision but does NOT dispatch the tool or write
the audit row -- for validating the loop without producing side effects.
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("MATTER_TABLE", "ida-dev-matters")
# Production model: Claude Haiku 4.5, selected by the ADR-001 eval (2026-08-04).
# It matched Sonnet 4.6 action-for-action on all 21 scored runs across 7 frozen
# scenarios -- zero missed escalations, zero false claims in dispatched content,
# zero schema violations -- at roughly half the latency (3.6s vs 7.0s median).
#
# FALLBACK / REFERENCE: Claude Sonnet 4.6, `us.anthropic.claude-sonnet-4-6`.
# It is the other model that cleared the ADR-001 bar, so if Haiku regresses in
# production the switch is one line -- no redeploy, no code change:
#     MODEL_ID=us.anthropic.claude-sonnet-4-6 python scripts/agent-loop.py <matter>
# Both Nova candidates were DISQUALIFIED at the escalation boundary; do not
# substitute one here on cost grounds without re-running the eval.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "ida-dev-gateway")
GATEWAY_TOOL = os.environ.get("GATEWAY_TOOL", "escalate-to-human___escalate_to_human")
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "agent", "system-prompt.md")

# The tool schema handed to the model. Mirrors the Gateway target's inlinePayload
# schema (infra/lib/agent-stack.ts) EXACTLY, so the arguments the model produces
# are the arguments the Gateway will accept and the escalate Lambda expects.
ESCALATE_TOOLSPEC = {
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
}


# --- account guard (script-only; the Lambda's account is fixed by deployment) --
def assert_account() -> str:
    expected = os.environ.get("EXPECTED_ACCOUNT")
    if not expected:
        sys.exit(
            "Set EXPECTED_ACCOUNT to the 12-digit account the agent lives in, e.g.\n"
            "  EXPECTED_ACCOUNT=<id> python scripts/agent-loop.py MTR-2026-0142"
        )
    actual = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    if actual != expected:
        sys.exit(
            f"WRONG ACCOUNT: credentials resolve to {actual}, expected {expected}.\n"
            "  unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN\n"
            "then re-check with:  aws sts get-caller-identity"
        )
    return actual


# --- matter state -------------------------------------------------------------
def matter_state(table, matter_id: str) -> dict:
    items = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}"),
        ConsistentRead=True,
    )["Items"]
    meta, docs, actions = {}, [], []
    for it in items:
        sk = it["SK"]
        if sk == "META":
            meta = {k: v for k, v in it.items() if k not in ("PK", "SK")}
        elif sk.startswith("DOC#"):
            docs.append({
                "docType": sk[len("DOC#"):],
                "status": it.get("status"),
                "dueDate": it.get("dueDate"),
                "extractionConfidence": it.get("extractionConfidence"),
            })
        elif sk.startswith("ACTION#"):
            actions.append({"action": it.get("action"), "actor": it.get("actor"), "reason": it.get("reason")})
    return {"matterId": matter_id, "meta": meta, "documents": docs, "actionHistory": actions}


def build_prompt(state: dict) -> str:
    return (
        f"Today's date is {date.today().isoformat()}.\n\n"
        f"Decide the single next action for this matter and, if the state warrants "
        f"it, take that action by calling the appropriate tool. If the state does "
        f"not justify an action, do nothing and say why. Base your decision only "
        f"on the state below.\n\nMatter state:\n{json.dumps(state, default=str, indent=2)}"
    )


# --- Converse (direct; the path proven to emit real tool_use) -----------------
def converse_decide(brt, system_text: str, prompt: str) -> dict:
    return brt.converse(
        modelId=MODEL_ID,
        system=[{"text": system_text}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        toolConfig={"tools": [ESCALATE_TOOLSPEC], "toolChoice": {"auto": {}}},
        inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
    )


# --- Gateway dispatch (SigV4 / MCP; validated against the live gateway) --------
def _mcp_post(url, region, creds, method, params, notify=False, rpc_id=1):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notify:
        msg["id"] = rpc_id
    body = json.dumps(msg).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    aws = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(aws)
    req = urllib.request.Request(url, data=body, headers=dict(aws.prepare().headers), method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=45)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _sse_json(text: str) -> dict:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:500]}


def resolve_gateway_url(region: str, gateway_name: str) -> str:
    acc = boto3.client("bedrock-agentcore-control", region_name=region)
    for g in acc.list_gateways().get("items", []):
        if g.get("name") == gateway_name:
            d = acc.get_gateway(gatewayIdentifier=g["gatewayId"])
            return (d.get("gateway") or d)["gatewayUrl"]  # top-level on get_gateway
    raise RuntimeError(f"gateway named {gateway_name!r} not found in {region}")


def gateway_dispatch(url, region, creds, tool_name, arguments) -> dict:
    """Call the tool through the Gateway (MCP tools/call). The Gateway holds the
    credentials to invoke the Lambda and logs the call -- the governed surface."""
    _mcp_post(url, region, creds, "initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "ida-agent-loop", "version": "1.0"},
    })
    status, text = _mcp_post(url, region, creds, "tools/call",
                             {"name": tool_name, "arguments": arguments}, rpc_id=2)
    resp = _sse_json(text)
    if "error" in resp:
        raise RuntimeError(f"Gateway tools/call error (HTTP {status}): {resp['error']}")
    return resp.get("result", resp)


def parse_tool_result(result: dict) -> dict:
    """The escalate Lambda's JSON return, unwrapped from the MCP tools/call result."""
    if isinstance(result, dict):
        if result.get("isError"):
            return {"status": "error", "raw": result}
        for c in result.get("content", []):
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except json.JSONDecodeError:
                    return {"status": "ok", "text": c["text"]}
    return {"status": "ok", "raw": result}


# --- audit record -------------------------------------------------------------
def write_audit(table, item: dict) -> None:
    table.put_item(Item=item)  # AUDIT#<ts>#<id8> is unique per decision -> append-only


# --- the port-clean core ------------------------------------------------------
def decide_and_act(matter_id: str, clients: dict, cfg: dict) -> dict:
    """Read state -> decide via Converse -> (maybe) dispatch tool -> write audit.
    Pure of I/O concerns beyond the injected clients; returns the audit record.
    Ports to a Lambda handler unchanged (handler just supplies matter_id+clients)."""
    decision_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    state = matter_state(clients["table"], matter_id)
    if not state["meta"]:
        raise RuntimeError(f"no matter {matter_id} in {cfg['table']}")

    resp = converse_decide(clients["brt"], cfg["system_text"], build_prompt(state))
    stop = resp["stopReason"]
    request_id = resp.get("ResponseMetadata", {}).get("RequestId")
    blocks = resp["output"]["message"]["content"]
    reasoning = " ".join(b["text"] for b in blocks if "text" in b).strip()
    tool_use = next((b["toolUse"] for b in blocks if "toolUse" in b), None)

    decision = {"action": "none", "toolName": None, "toolInput": None}
    outcome = {"status": "no_action"}
    gateway_call = None
    closing = None

    if tool_use:
        name, args = tool_use["name"], dict(tool_use.get("input", {}))
        decision = {"action": "escalate" if name == "escalate_to_human" else name,
                    "toolName": name, "toolInput": args}
        # DISPATCH GATE -- fail-safe by construction.
        #
        # This is deliberately `is True` on an opt-IN key, not falsiness on an
        # opt-OUT one. The previous form was `elif cfg.get("dry_run")`, which
        # meant a cfg that simply OMITTED the key returned None -> falsy -> fell
        # through to dispatch. A missing key is exactly what a new caller, a
        # partial refactor, or a Lambda handler assembling cfg from env is most
        # likely to produce, and the failure mode was "sends real email".
        #
        # Inverted: dispatch happens only when a caller has explicitly said so.
        # Anything else -- False, None, missing key, empty cfg -- cannot reach
        # gateway_dispatch, which is the single call site in this module.
        dispatch_enabled = cfg.get("dispatch") is True
        if name != "escalate_to_human":
            outcome = {"status": "error", "error": f"unexpected tool {name}"}
        elif not dispatch_enabled:
            outcome = {"status": "not_dispatched",
                       "note": "decision recorded; dispatch not enabled for this run"}
        else:
            args.setdefault("matterId", matter_id)
            result = gateway_dispatch(cfg["gateway_url"], cfg["region"], clients["creds"], cfg["gateway_tool"], args)
            outcome = parse_tool_result(result)
            gateway_call = {"tool": cfg["gateway_tool"], "via": "agentcore_gateway"}
            # Close the loop: hand the tool result back so the agent states the outcome
            # for the audit record. Guarded -- the escalation already happened.
            try:
                closing_msgs = [
                    {"role": "user", "content": [{"text": build_prompt(state)}]},
                    {"role": "assistant", "content": blocks},
                    {"role": "user", "content": [{"toolResult": {
                        "toolUseId": tool_use["toolUseId"],
                        "content": [{"text": json.dumps(outcome)}],
                        "status": "error" if outcome.get("status") == "error" else "success",
                    }}]},
                ]
                cr = clients["brt"].converse(
                    modelId=MODEL_ID, system=[{"text": cfg["system_text"]}],
                    messages=closing_msgs,
                    toolConfig={"tools": [ESCALATE_TOOLSPEC], "toolChoice": {"auto": {}}},
                    inferenceConfig={"maxTokens": 512, "temperature": 0.0},
                )
                closing = " ".join(b["text"] for b in cr["output"]["message"]["content"] if "text" in b).strip()
            except Exception as e:  # noqa: BLE001 -- closing summary is best-effort
                closing = f"(closing summary unavailable: {type(e).__name__})"

    audit = {
        "PK": f"MATTER#{matter_id}",
        "SK": f"AUDIT#{ts}#{decision_id[:8]}",
        "decisionId": decision_id,
        "ts": ts,
        "actor": "agent/client-loop",
        "matterId": matter_id,
        "inputs": {
            "asOfDate": date.today().isoformat(),
            "documents": state["documents"],
            "priorActions": len(state["actionHistory"]),
        },
        "model": cfg["model_id"],
        "promptVersion": cfg["prompt_version"],
        "reasoning": reasoning,
        "decision": decision,
        "outcome": outcome,
        "closingSummary": closing,
        "stopReason": stop,
        "modelRequestId": request_id,
        "gatewayCall": gateway_call,
    }
    # dry_run suppresses DISPATCH; write_audit controls PERSISTENCE. They are
    # separate because the sweep's stage-1 rollout needs exactly the combination
    # "decide and record, but send nothing" -- an audit trail of what the agent
    # would have done, with zero outbound effect. Defaults keep this script's
    # own --dry-run fully side-effect-free.
    if cfg.get("write_audit", not cfg.get("dry_run")):
        write_audit(clients["table"], audit)
    return audit


# --- operator wrapper (script only) -------------------------------------------
def confirm(table, matter_id: str) -> None:
    esc = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}") & Key("SK").begins_with("ACTION#escalate"),
        ConsistentRead=True,
    )["Items"]
    aud = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}") & Key("SK").begins_with("AUDIT#"),
        ConsistentRead=True,
    )["Items"]
    valid = [r for r in esc if r.get("action") == "escalated" and r.get("escalatedAt")]
    print("\n--- verification (strong-consistent) ---")
    print(f"  ACTION#escalate rows: {len(valid)}  AUDIT# rows: {len(aud)}")
    for r in valid:
        print(f"    [OK] {r['SK']}: action={r.get('action')} docType={r.get('docType')} at={r.get('escalatedAt')}")
    if valid:
        print(">>> EXECUTION CONFIRMED (row present; verify the email + audit yourself).")
    else:
        print(">>> NO escalate row on a consistent read.")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    matter_id = args[0] if args else "MTR-2026-0142"

    assert_account()
    region = REGION
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    table = boto3.resource("dynamodb", region_name=region).Table(TABLE)
    brt = boto3.client("bedrock-runtime", region_name=region)
    system_text = open(PROMPT_PATH, encoding="utf-8").read()
    cfg = {
        "region": region, "table": TABLE, "model_id": MODEL_ID,
        "gateway_url": resolve_gateway_url(region, GATEWAY_NAME),
        "gateway_tool": GATEWAY_TOOL, "system_text": system_text,
        "prompt_version": hashlib.sha256(system_text.encode()).hexdigest()[:12],
        "dry_run": dry_run,
        "dispatch": not dry_run,   # explicit opt-in; see the DISPATCH GATE
        "write_audit": not dry_run,
    }
    clients = {"table": table, "brt": brt, "creds": creds}

    print(f"=== agent-loop on {matter_id} {'(DRY RUN)' if dry_run else ''} ===")
    print(f"model {MODEL_ID} | gateway {cfg['gateway_url']} | promptVersion {cfg['prompt_version']}")
    audit = decide_and_act(matter_id, clients, cfg)

    print("\n--- reasoning ---")
    print(audit["reasoning"] or "(none)")
    print(f"\n--- decision: {audit['decision']['action']} (stopReason={audit['stopReason']}) ---")
    if audit["decision"]["toolInput"]:
        print("  tool input:", json.dumps(audit["decision"]["toolInput"]))
    print("  outcome:", json.dumps(audit["outcome"]))
    if audit["closingSummary"]:
        print("  closing:", audit["closingSummary"])
    if dry_run:
        print("\n(DRY RUN -- no tool dispatched, no audit row written)")
    else:
        print(f"\n  audit row: {audit['SK']}  (modelRequestId={audit['modelRequestId']})")
        confirm(table, matter_id)


if __name__ == "__main__":
    main()
