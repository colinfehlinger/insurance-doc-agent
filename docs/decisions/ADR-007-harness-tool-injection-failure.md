# ADR-007 — The managed Harness runtime did not inject tools; move orchestration client-side

**Status:** **Decided.** The managed AgentCore Harness runtime does not populate
`toolConfig` on its model request from any tool source. Keep the Gateway + tool
Lambda + Harness resources, but move the agent **orchestration** (the reasoning
loop and the tool-call dispatch) from the managed Harness into a client-side
loop that calls Bedrock Converse directly and invokes the tool itself.
**Date:** 2026-07-31
**Amends:** [ADR-006](ADR-006-agent-architecture.md) (the thin-client / "managed Harness auto-injects gateway tools" premise — that half no longer holds; the fold-in and native-L1 decisions stand)
**Related:** [ADR-001](ADR-001-foundation-model.md) (model), [agent/system-prompt.md](../../agent/system-prompt.md), [docs/step-6-agent-design.md](../step-6-agent-design.md)

---

## Context

Step 6's thin slice — agent decides on one overdue matter and calls
`escalate_to_human` through the Gateway — never executed the tool. The agent
reasoned correctly and *decided* to escalate, but emitted the call as JSON in its
message text (`stopReason: end_turn`), never a native `tool_use` block, so the
Lambda never ran and no `ACTION#escalate` row was written.

ADR-006's premise was that the **managed Harness auto-injects Gateway tools**
("Reference a gateway ARN and every tool configured on that gateway becomes
available"), so the client stays thin. That premise is what this ADR overturns.

## The investigation (all read from source)

The decisive artifact is the **literal ConverseStream request** the Harness sends
the model, captured via Bedrock **model-invocation logging** enabled in
000000000000/us-east-1 for this diagnosis.

**Three-invocation evidence table:**

| Invocation (logged ConverseStream request body) | `toolConfig` |
|---|---|
| Harness with the **configured Gateway tool** (`agentcore_gateway` → READY gateway) | **absent** |
| Harness with an **inline tool** passed at invoke time (`inline_function`, docs-supported) | **absent** |
| **Direct** Bedrock Converse, **same model**, `toolConfig` supplied by our code | **present → real `tool_use` emitted, correct schema** |

**Corroborating evidence:**

- A 4-cell matrix (gateway/inline × with/without `allowedTools`) — **all absent**.
  So `allowedTools` name-matching is not the cause.
- The built-in `shell` / `file_operations` tools, which the docs say are present
  in **every** session unless `allowedTools` excludes them, were **also absent**
  (no-`allowedTools` cells, ~1263 input tokens; the built-ins alone should add
  ~900). The runtime is not injecting even its own guaranteed tools.
- **Logger validity check:** a direct Converse *with* a `toolConfig` was logged
  **with** its `toolConfig` intact — so "absent in the log" means genuinely
  absent in the request, not stripped by logging.
- **The model is fine:** `us.anthropic.claude-sonnet-4-6` via direct Converse
  emits a correct `tool_use` (`matterId`/`reason`, no hallucinated fields).
- **The config is correct:** `get_harness` shows `tools` → the READY gateway
  (`ida-dev-gateway-ruoxwjqach`) and `allowedTools`
  (`escalate-to-human___escalate_to_human`) matching the READY target; the
  execution role carries `GetWorkloadAccessToken`, `InvokeGateway`, `xray`. This
  **matches AWS's own documented-working example** (AWS_IAM gateway + `awsIam`
  outbound + `InvokeGateway`).
- **No runtime lever exists on our side:** `environmentArtifact` is null (the
  AWS-managed image `public.ecr.aws/i0n3d3i5/harness-us-east-1:latest`, v1), and
  CreateHarness exposes no runtime-version or tool-enable field — only
  lifecycle/network/filesystem.

**Conclusion:** the managed Harness **runtime** does not translate any tool
declaration — configured Gateway tool, invoke-time inline tool, or its own
built-ins — into the model's `toolConfig`. The failure is upstream of Gateway
resolution (an inline tool needs no gateway, no workload token, no `tools/list`,
and still never appears), so it is **not** a workload-identity / `tools/list`
problem. It is a defect in the managed harness runtime image, not our
configuration. Ruling out gateway resolution overturns the earlier hypothesis in
[docs/step-6-agent-design.md](../step-6-agent-design.md).

### A separate defect in the same runtime (recorded, not causal)

The runtime's OTEL GenAI-span export also fails — `"Failed to export logs batch
… The specified log stream does not exist."` — which is why the Observability
console shows no trace for the model request. This is a **telemetry-delivery**
defect, a different code path from request assembly. It is **not** evidence the
harness can't reach the Gateway (the inline result already rules that out). Two
independent managed-runtime defects; recorded together only because they share
the runtime image.

## Decision

**Path 2 — client-side agent loop.** Keep the Gateway, the `escalate_to_human`
tool Lambda, the SNS topic, and even the Harness resource (cheap, and re-usable
if AWS fixes the runtime), but move the **orchestration** out of the managed
Harness:

- Our code calls Bedrock **Converse** directly with `us.anthropic.claude-sonnet-4-6`,
  the version-controlled system prompt, the matter state, and a `toolConfig`
  built from the `escalate_to_human` schema — the path proven to work.
- On `stopReason: tool_use`, our code dispatches the call to the tool and returns
  the `toolResult` for the agent's closing summary.
- **Definition of Done is unchanged:** a real `ACTION#escalate#census` row
  (this account's schema — `action`/`actor`/`reason`/`docType`/`escalatedAt`),
  the SNS email, and a trace. Only the orchestration *location* moves.

Path 1 (a timeboxed check for a runtime version/flag/config that makes tools
reach `toolConfig`) was attempted and exhausted: our config already matches the
documented-working pattern, and nothing we control changes the runtime's
behavior.

## Consequences

- **ADR-006's thin-client premise is partially reversed.** The fold-in and the
  native-L1 build decisions stand; the "managed Harness owns orchestration and
  auto-injects tools" half does not, for this runtime version. The client now
  owns the reasoning loop and the tool dispatch.
- The Gateway's governance value (agent-holds-no-credentials, per-call audit,
  Cedar policy) is **not** exercised while the client invokes the tool directly.
  Restoring the tool call through the Gateway (client → Gateway `tools/call` →
  Lambda) is the production-aligned follow-up once the loop is green; recorded as
  a deferred item.
- **Reversal trigger:** if AWS ships a harness runtime that populates `toolConfig`
  (re-test with the same three-invocation table), the managed Harness can
  reclaim orchestration and this ADR's client loop becomes removable — the
  Harness/Gateway resources were deliberately kept to make that cheap.
- Bedrock **model-invocation logging** remains enabled in the dev account
  (delivery role `ida-dev-bedrock-invlog-role`, log group
  `/ida/dev/bedrock-model-invocations`) — useful for the ADR-001 model eval;
  tracked for later cleanup.
