"""Document-Chase Agent -- the decision core (ADR-007, Path 2).

The managed AgentCore Harness did not inject tools into the model request for its
runtime version, so orchestration lives here instead: read matter state, call
Bedrock Converse directly with a toolConfig, and on a tool_use dispatch through
the AgentCore Gateway. See ADR-007.

This module is imported identically by three callers -- scripts/agent-loop.py,
scripts/sweep.py, and infra/lambdas/sweep/index.py -- so a single-matter run, a
hand-run sweep, and the deployed Lambda can never diverge in judgment.

PATHS ARE PACKAGE-RELATIVE ON PURPOSE. The system prompt resolves against THIS
file's location (agent/core/decide.py -> ../system-prompt.md), not against the
caller's working directory or a repo-root-relative path. The previous form,
`dirname(__file__)/../agent/system-prompt.md` from scripts/, resolved to
/var/agent/system-prompt.md inside a Lambda -- a cold-start FileNotFoundError
that is invisible locally. Bundling the `agent/` tree keeps this path correct in
both contexts.
"""

import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE = os.environ.get("MATTER_TABLE", "ida-dev-matters")

# Production model: Claude Haiku 4.5, selected by the ADR-001 eval (2026-08-04).
# It matched Sonnet 4.6 action-for-action on all 21 scored runs -- zero missed
# escalations, zero false claims in dispatched content, zero schema violations --
# at roughly half the latency.
#
# FALLBACK / REFERENCE: `us.anthropic.claude-sonnet-4-6`, the only other candidate
# that cleared the bar. Switching is one env var, no redeploy. Both Nova models
# were DISQUALIFIED at the escalation boundary; do not substitute one on cost
# grounds without re-running the eval.
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
GATEWAY_TOOL = os.environ.get("GATEWAY_TOOL", "escalate-to-human___escalate_to_human")
GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "ida-dev-gateway")
PROMPT_PATH = os.environ.get(
    "AGENT_PROMPT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system-prompt.md"),
)

# Mirrors the Gateway target's inlinePayload schema (infra/lib/agent-stack.ts)
# EXACTLY, so the arguments the model produces are the arguments the Gateway
# accepts and the escalate Lambda expects.
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


def load_prompt() -> str:
    """The system prompt IS the control environment; its sha is recorded on every
    decision, so a change to it is a change that shows up in the audit trail."""
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


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


def build_prompt(state: dict, as_of: date | None = None) -> str:
    return (
        f"Today's date is {(as_of or date.today()).isoformat()}.\n\n"
        f"Decide the single next action for this matter and, if the state warrants "
        f"it, take that action by calling the appropriate tool. If the state does "
        f"not justify an action, do nothing and say why. Base your decision only "
        f"on the state below.\n\nMatter state:\n{json.dumps(state, default=str, indent=2)}"
    )


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


def resolve_gateway_url(region: str = REGION, gateway_name: str = GATEWAY_NAME) -> str:
    """Look the gateway URL up at runtime.

    PREFER THE GATEWAY_URL ENV VAR. The deployed Lambda receives the URL as a
    synth-time environment variable from the CfnGateway attribute, which removes
    a cold-start API call, a failure mode, and the bedrock-agentcore-control IAM
    permission this function would otherwise require. This path exists for local
    scripts, where the URL is not injected.
    """
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
        "clientInfo": {"name": "ida-agent-core", "version": "1.0"},
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


def write_audit(table, item: dict) -> None:
    table.put_item(Item=item)  # AUDIT#<ts>#<id8> is unique per decision -> append-only


# --- the port-clean core ------------------------------------------------------
def decide_and_act(matter_id: str, clients: dict, cfg: dict) -> dict:
    """Read state -> decide via Converse -> (maybe) dispatch -> write audit.

    No argv, no print, no sys.exit; clients injected, config passed in. That is
    what lets the CLI wrappers and the Lambda handler share it unchanged.
    """
    decision_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    state = matter_state(clients["table"], matter_id)
    if not state["meta"]:
        raise RuntimeError(f"no matter {matter_id} in {cfg.get('table', TABLE)}")

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
        # Deliberately `is True` on an opt-IN key, not falsiness on an opt-OUT
        # one. The earlier form was `elif cfg.get("dry_run")`, so a cfg that
        # merely OMITTED the key returned None -> falsy -> fell through and
        # dispatched. A missing key is exactly what a Lambda handler assembling
        # cfg from env produces, and the failure mode was "sends real email".
        #
        # Anything else -- False, None, missing key, empty cfg, the STRING
        # "true" -- cannot reach gateway_dispatch, the single call site here.
        dispatch_enabled = cfg.get("dispatch") is True
        if name != "escalate_to_human":
            outcome = {"status": "error", "error": f"unexpected tool {name}"}
        elif not dispatch_enabled:
            outcome = {"status": "not_dispatched",
                       "note": "decision recorded; dispatch not enabled for this run"}
        else:
            args.setdefault("matterId", matter_id)
            result = gateway_dispatch(cfg["gateway_url"], cfg["region"], clients["creds"],
                                      cfg["gateway_tool"], args)
            outcome = parse_tool_result(result)
            gateway_call = {"tool": cfg["gateway_tool"], "via": "agentcore_gateway"}
            # Hand the tool result back so the agent states the outcome for the
            # audit record. Best-effort: the escalation already happened.
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
        "actor": cfg.get("actor", "agent/client-loop"),
        "matterId": matter_id,
        "inputs": {
            "asOfDate": date.today().isoformat(),
            "documents": state["documents"],
            "priorActions": len(state["actionHistory"]),
        },
        "model": cfg.get("model_id", MODEL_ID),
        "promptVersion": cfg["prompt_version"],
        "reasoning": reasoning,
        "decision": decision,
        "outcome": outcome,
        "closingSummary": closing,
        "stopReason": stop,
        "modelRequestId": request_id,
        "gatewayCall": gateway_call,
    }
    # dry_run suppresses DISPATCH; write_audit controls PERSISTENCE. Separate,
    # because the sweep's stage-1 rollout needs exactly "decide and record, send
    # nothing" -- an audit trail of what the agent would have done.
    if cfg.get("write_audit", not cfg.get("dry_run")):
        write_audit(clients["table"], audit)
    return audit


def confirm_execution(table, matter_id: str) -> list[dict]:
    """The ONLY proof a tool actually ran: a strongly-consistent read of the
    persisted artifact, carrying this account's escalate-Lambda schema.

    It accepts no model output -- not the emitted tool_use block, not the stated
    intent -- so a confirmation cannot be fabricated from what the agent SAID it
    would do. An earlier verifier formatted the model's intent as if it were a
    persisted row and reported success for a write that never happened.
    """
    rows = table.query(
        KeyConditionExpression=Key("PK").eq(f"MATTER#{matter_id}")
        & Key("SK").begins_with("ACTION#escalate"),
        ConsistentRead=True,
    )["Items"]
    return [r for r in rows if r.get("action") == "escalated" and r.get("escalatedAt")]
