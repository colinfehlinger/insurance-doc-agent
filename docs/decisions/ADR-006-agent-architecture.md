# ADR-006 — Agent architecture, and the ADR-003 fold-in resolution

**Status:** **Decided, and partly amended.** Build the real agent natively in `infra/` with the
stable AgentCore L1 constructs; retire the Step-3 probe and its alpha sub-project.
**Date:** 2026-07-25
**Amended by:** [ADR-007](ADR-007-harness-tool-injection-failure.md) (2026-07-31)
**Resolves:** [ADR-003](ADR-003-agentcore-cdk-fold-in.md) (the deferred fold-in — Step 6 is its named trigger)
**Related:** [ADR-001](ADR-001-foundation-model.md) (model), [ADR-005](ADR-005-document-matter-correlation.md), [agent/tools/README.md](../../agent/tools/README.md), [agent/system-prompt.md](../../agent/system-prompt.md)

---

> **Read this with [ADR-007](ADR-007-harness-tool-injection-failure.md).** Two
> things described below as available have since moved, and the text is left as
> written because an ADR records what was decided, not what is currently true:
>
> - **The managed Harness does not orchestrate.** ADR-007 established that its
>   runtime injects no tools into the model request, and retired it. This ADR's
>   fold-in and native-L1 decisions stand; its "the Harness owns orchestration"
>   premise does not. Orchestration runs as a client-side loop, and the Gateway
>   still owns governed execution.
> - **Cedar and Observability are inventoried here, not built.** The L1 surface
>   below is read off the pinned library and is accurate as a *capability*
>   inventory. Neither is deployed. Cedar policy waits for `send_reminder` —
>   there is no outbound tool with a real abuse surface to police yet — and the
>   decision audit trail is written directly to DynamoDB as append-only `AUDIT#`
>   rows rather than through AgentCore Observability, whose span export was the
>   second defect ADR-007 recorded.

## Context

Step 6 turns the `IdaAgentProbe` hello-world into the real Document-Chase Agent.
ADR-003 named exactly this moment as its revisit trigger: *"when the real agent
needs to share `infra/` resources (the matter table ARN, the CMK)."* It now does
— `update_matter` writes the matter table, `send_reminder` uses SES and the CMK,
`schedule_followup` uses EventBridge.

ADR-003 deferred the fold-in because the generated `agentcore/cdk/` dragged in
**five alpha CDK modules** and an alpha `@aws/agentcore-cdk` L3, and putting that
churn inside the deterministic pipeline's toolchain was the thing to avoid. That
was the correct call at the time.

**That constraint is gone, and it changes the decision.** Verified against the
pinned `aws-cdk-lib` **2.261.0** — the version `infra/` already uses, no new
dependency:

```
aws-cdk-lib/aws-bedrockagentcore  (STABLE L1, generated from the CFN spec):
  CfnHarness         CfnGateway        CfnGatewayTarget
  CfnPolicy          CfnPolicyEngine   CfnMemory
  CfnRuntime         CfnRuntimeEndpoint  CfnWorkloadIdentity
  CfnBrowserCustom   CfnCodeInterpreterCustom  ...
```

`CfnHarness` in particular maps the whole agent as configuration (confirmed
prop shape):

| Harness prop | Role |
|---|---|
| `model` (`HarnessModelConfigurationProperty`) | Bedrock / Gemini / OpenAI / LiteLLM |
| `systemPrompt` (`HarnessSystemContentBlockProperty[]`) | our `agent/system-prompt.md` |
| `allowedTools` + `HarnessAgentCoreGatewayConfigProperty` | tools via a Gateway |
| `memory`, `maxIterations`, `maxTokens`, `executionRoleArn`, `environmentVariables` | run config |

Tools attach through `CfnGateway` + one `CfnGatewayTarget` per Lambda tool.
Cedar guardrails attach at the Gateway via `CfnPolicyEngine` + `CfnPolicy`
(`policyEngineConfiguration` on the gateway; `enforcementMode` on the policy).
Observability traces every action automatically. All of it is L1 in the pinned
library.

*(Sources: AgentCore Harness GA 2026-06-17; Policy GA 2026-03-03, Cedar,
default-deny, attaches at the Gateway; harness docs — model/tools/instructions
are configuration, "trying a different model or adding a new tool is a config
change, not a code rewrite.")*

## The two paths (ADR-003's framing, re-costed)

**(a) Fold in.** Build the agent in `infra/` with the stable L1s, wiring the
matter table, CMK, SES, and EventBridge by **typed CDK references and `grant*`
helpers**.

**(b) Keep separate.** Leave the agent as the probe's sub-project
(`agentcore deploy`, alpha `@aws/agentcore-cdk`), and pass `infra/` resource ARNs
across via SSM parameters / env vars — losing typed references and grant helpers.

What changed since ADR-003: path (a) no longer requires the alpha library. The
entire reason (b) was ever attractive — keeping alpha churn out of the pipeline —
**evaporates**, because the fold-in now uses the same stable `aws-cdk-lib`
`infra/` is already pinned to. Meanwhile (b)'s cost is now concrete and daily:
every resource the agent touches would move as a stringly-typed ARN through SSM,
re-granted by hand, with no compile-time check that the wiring is correct — on
the compliance-sensitive path where getting IAM wrong is the expensive failure.

## Decision

**Path (a), built natively — and specifically NOT by folding the probe's
generated `cdk/` in.** The probe's alpha sub-project is **retired**, not
migrated. The real agent is new `infra/` stack code using
`CfnHarness` / `CfnGateway` / `CfnGatewayTarget` / `CfnPolicyEngine` /
`CfnPolicy`, replacing the `Ida-Dev-Agent` SSM stub.

Concretely:

- A real `Ida-Dev-Agent` stack (replacing the stub) holds the Harness, the
  Gateway, one Gateway target per tool Lambda, and (later) the Policy engine.
- Tool Lambdas live in `infra/lambdas/tools/*` alongside the existing
  `submit`/`mapper`, and receive `grant*`-based access to the matter table, CMK,
  SES, EventBridge, and the escalation SNS topic.
- `agent/system-prompt.md` is loaded into the Harness `systemPrompt` at synth.
- **The probe** (`agent/runtime/IdaAgentProbe/`, `AgentCore-IdaAgentProbe-dev`)
  is decommissioned once the real Harness is invocable. It did its job in Step 3
  — proving `create → deploy → invoke` — and is now scaffolding.

### Why native L1 over the managed-Harness CLI or the alpha L2

The Step-3 probe used the AgentCore **CLI** (`agentcore deploy`) with a generated
CDK app on alpha constructs — the fast hello-world path. For the real system, the
stable L1 in `aws-cdk-lib` is the better home: one toolchain, one `cdk deploy`,
one pinned dependency set, and typed cross-stack references to the pipeline it
must integrate with. The managed Harness itself is still what we use — `CfnHarness`
*is* the managed harness — we just declare it in our own CDK rather than through
the CLI's generated project. This also keeps the Node-only-synth property
(no Docker), because a Gateway-tools + Bedrock-model + system-prompt Harness needs
no custom container, sidestepping the container-image ordering gotcha the probe
sidestepped with CodeZip.

## Consequences

- **ADR-003 is resolved** — outcome: fold in, via native L1s. Its divergence
  table (alpha deps, Docker-for-synth, `@types/node` skew, CLI-enforced stack
  names) becomes moot because the generated `cdk/` is retired rather than
  integrated.
- `infra/` gains the AgentCore L1 surface but **no new npm dependency** — it is
  all in `aws-cdk-lib` 2.261.0.
- Retiring the probe is a small cleanup task in the Step-6 build: destroy
  `AgentCore-IdaAgentProbe-dev`, remove `agent/runtime/IdaAgentProbe/`. Keep
  `agent/runtime/README.md`'s findings as a Step-3 record, or move them into the
  Step-3 notes.
- The five tools become real Lambdas with least-privilege roles; the matter
  table's append-only rule (`update_matter`) is enforced structurally by the
  tool's narrow write, not by trust.
- One judgment call deferred to the build: whether the Harness is its own stack
  or a construct within a combined agent stack. Recommend a single
  `Ida-Dev-Agent` stack containing Gateway + targets + Harness (+ Policy later),
  since they share a lifecycle and all depend on the tool Lambdas.

## Reversal trigger

If the stable `CfnHarness` L1 proves materially behind the service (a needed
Harness feature exists in the API but not the CFN resource), fall back to the
alpha L2 (`@aws/agentcore-cdk`) **for the Harness construct only**, still inside
`infra/`, rather than reviving the separate sub-project. The resource-sharing
argument for fold-in stands regardless of which construct layer builds the
Harness.

## Note (2026-07-27) — the implicit base-permission contract (fold-in seam)

Fold-in held, but it surfaced a seam worth recording. The managed Harness has an
**implicit base execution-role contract** the AgentCore CLI auto-provisions and
CDK does not: `bedrock-agentcore:GetWorkloadAccessToken`/`…ForJWT` (the workload
token the Harness uses to authenticate to the Gateway for **tool discovery**),
plus `xray:*` (traces), `logs:*`, `cloudwatch:PutMetricData`, and ECR-public
pull. A hand-built role missing these does not error — the Harness builds the
model request with an **empty `toolConfig`** and the agent reasons but never
calls a tool (the "silent empty-toolConfig" failure; full write-up in
`docs/step-6-agent-design.md`).

This does **not** reverse ADR-006 — the AWS docs confirm the managed Harness
auto-injects gateway tools, so the client stays thin and the managed
orchestration is intact; it was an auth gap, not an architecture problem. The
takeaway for the fold-in: **building AgentCore resources natively in CDK means
owning the base-permission contract the CLI would have hidden.** When more tools
or primitives (Memory, Browser, Code Interpreter) are added, their base grants
must be added to the execution role explicitly — the CLI's convenience is exactly
what CDK trades away for typed, in-`infra/` control.
