# Step 6 — the agent (the brain): design and decisions

**Design pass only. No code, no deploy.** Forks and recommendations for review.
**Date:** 2026-07-25 · build + first-deploy findings appended 2026-07-26

> **First-deploy findings** — see the section at the end of this doc. The build
> is done and synth-clean; the first `Ida-Dev-Agent` deploy attempt surfaced
> AgentCore resource **naming constraints** (now confirmed against the service
> model and fixed before redeploy). The two contracts confirmed pre-deploy
> (tool-type enum, IAM actions) held.

This is where `IdaAgentProbe` (Step 3 hello-world) becomes the real
Document-Chase Agent. The deterministic body (Steps 1–5) already produces the
matter state the agent reasons over. Step 6 adds the one part of the system
allowed to exercise judgment — and nothing else.

Related: [ADR-006](decisions/ADR-006-agent-architecture.md) (architecture — the
gating decision), [ADR-001](decisions/ADR-001-foundation-model.md) (model),
[ADR-005](decisions/ADR-005-document-matter-correlation.md),
[agent/system-prompt.md](../agent/system-prompt.md),
[agent/tools/README.md](../agent/tools/README.md).

---

## Tooling, re-confirmed (2026-07-25)

Checked against current docs and the pinned `aws-cdk-lib` 2.261.0, not memory:

| Capability | State | How it's used |
|---|---|---|
| **Managed Harness** | GA 2026-06-17 | Declares the agent as config: `model`, `systemPrompt`, `allowedTools`, `memory`, `maxIterations/maxTokens`. Powered by Strands. `CfnHarness` L1 in `aws-cdk-lib`. Default model Claude Sonnet 4.6. |
| **Gateway** | GA | Tools attach here; the agent never holds AWS creds, every call is logged. Lambda → tool via `CfnGatewayTarget` with a `ToolDefinition` (`name`, `description`, `inputSchema`, optional `outputSchema`). |
| **Policy** | GA 2026-03-03 | Cedar, default-deny, sits **inside the Gateway**, evaluates every tool call. Attaches via the gateway's `policyEngineConfiguration` (`CfnPolicyEngine` + `CfnPolicy`, `enforcementMode`). Now also supports Bedrock Guardrails. |
| **Observability** | GA | Every action traced automatically; unified view. This is the decision audit trail — the compliance deliverable. |
| **Memory** | GA | Short- and long-term, per session, `CfnMemory`. Consumption-priced. |

**The one finding that reframed the architecture:** all of the above are
**stable L1 constructs in the pinned `aws-cdk-lib` 2.261.0** — no alpha library.
That is what makes ADR-006's fold-in clean. See ADR-006.

---

## Fork 1 — architecture: fold in or stay separate → **ADR-006**

**Resolved: fold in, natively.** Build the agent in `infra/` with the stable
`CfnHarness`/`CfnGateway`/`CfnGatewayTarget`/`CfnPolicy*` L1s; **retire the probe
and its alpha sub-project** rather than migrate it. The alpha dependency that
made "stay separate" attractive in ADR-003 no longer exists for the real agent,
and resource-sharing (matter table, CMK, SES, EventBridge) is far cheaper with
typed references than with SSM-ARN-passing. Full reasoning in
[ADR-006](decisions/ADR-006-agent-architecture.md). This gates the rest.

---

## Fork 2 — probe → real agent: rebuild, not refactor

The probe was **Runtime + hand-coded Strands** (calculator/fetcher tools,
`agentcore deploy`, alpha `cdk/`). The real agent is the **managed Harness as
config** (`CfnHarness`). So this is a small **rebuild**, not a refactor — and
that is the right shape, because the Harness turns the agent into declaration:

| Probe (Step 3) | Real agent (Step 6) |
|---|---|
| Strands app in `app/IdaAgentProbe/main.py` | `CfnHarness` config |
| Default calculator/fetcher tools | the five real tools via Gateway targets |
| `DEFAULT_SYSTEM_PROMPT = "helpful assistant"` | `agent/system-prompt.md` → `systemPrompt` |
| Nova Micro (probe-only) | build on a strong model, eval down (Fork 5) |
| `agentcore deploy`, alpha `@aws/agentcore-cdk` | `cdk deploy`, stable `aws-cdk-lib` |
| Runtime + container/CodeZip | Harness, no custom container needed |

Discarded: the probe's Python app and generated `cdk/`. Kept: the toolchain
lesson (Step-3 notes) and `system-prompt.md`, which was written for exactly this.

---

## Fork 3 — the five tools → AWS mapping via Gateway

Each tool is a Lambda behind a `CfnGatewayTarget`. Narrow typed inputs
(`matterId` + a small payload), per [agent/tools/README.md](../agent/tools/README.md).

| Tool | Lambda does | Infra | New or reuse |
|---|---|---|---|
| `update_matter` | appends an `ACTION#<ts>` row (append-only; never rewrites extracted fields) | DynamoDB `ida-dev-matters` | **reuse** |
| `escalate_to_human` | appends `ACTION#escalated` + publishes to an escalation topic | **SNS topic** + DynamoDB | **new** (SNS) |
| `flag_anomaly` | marks the matter for review; publishes to the same topic | DynamoDB + SNS | reuse + new |
| `schedule_followup` | sets a future check on the matter | **EventBridge Scheduler** | **new** |
| `send_reminder` | emails the broker/employer the outstanding docs + due date | SES | reuse (**gated**, below) |

**New infra:** one SNS escalation topic (email subscription to the verified
gmail), and EventBridge Scheduler wiring for `schedule_followup`. **Reuse:** the
matter table, the CMK, SES.

### ⚠️ SES reality gates `send_reminder`

No domain is registered (`fehlingerops.com` still 404 as of 2026-07-25; ADR-005),
and the only verified SES identity is `test-recipient@example.com`. So
`send_reminder` in dev can only send **from and to that gmail** — it cannot yet
email a real broker. This is why the thin slice (Fork 6) deliberately starts with
`escalate_to_human`, whose SNS path **needs no verified SES sender at all** (SNS
handles delivery), sidestepping the gap entirely. `send_reminder` comes online in
the tool-expansion pass, gmail-only until the domain track (ADR-005) completes.

### Idempotency (carried from tools/README open questions)

A retried Harness invocation must not send two reminders or two escalations.
Recommendation: each tool writes its `ACTION#<deterministic-key>` row
conditionally (attribute_not_exists), and the side effect (SES/SNS/Scheduler)
fires only after the conditional write wins — the same "record-then-act" shape
the mapper uses. Deterministic key = `matterId + docType + action-type + period`.

---

## Fork 4 — guardrails: Cedar Policy vs prompt

The hard rules in `system-prompt.md` split by whether they must be
**non-probabilistic**:

| Rule | Home | Why |
|---|---|---|
| Never contact an insured/claimant directly | **Cedar** | Deny `send_reminder` unless recipient ∈ broker/employer set. A safety boundary must not depend on the model. |
| Reminder cadence cap (≤ N per doc) | **Cedar** | Deny `send_reminder` when the matter's reminder count ≥ cap (count as a Cedar entity attribute). |
| Overdue → must escalate, may not remind | **Cedar** | Deny `send_reminder` when the doc is past due; force the escalate path. This is the exact boundary the thin slice tests. |
| `update_matter` is append-only | **structural + Cedar** | Enforced by the tool's narrow write; Cedar can additionally deny any non-append shape. |
| Never fabricate receipt of a document | **structural** | The agent has no tool that can mark a doc `received` — only the pipeline's mapper writes that. Nothing to fabricate with. |
| Tone, phrasing, which doc to chase first, when waiting is wiser than acting | **prompt** | Genuine judgment — framing, not a boundary. |

**Recommendation: Cedar Policy is DEFERRED past the thin slice, reserved in the
design.** The slice has exactly one tool (`escalate_to_human`) with no dangerous
branch to guard — escalation is always safe. Cedar earns its place the moment
`send_reminder` exists, because that is the tool with an abuse surface (wrong
recipient, over-cadence, reminding when it should escalate). So: **thin slice =
prompt only; tool-expansion pass = add the Cedar policy set above at the
Gateway.** Stated plainly so it is a scheduled addition, not an omission.

There is a subtlety worth surfacing: the thin slice is *testing whether the model
makes the escalate-vs-remind call correctly*. If Cedar forced that call, the test
would be meaningless. Leaving it to the model for the slice — then backstopping
it with Cedar once `send_reminder` is real — is deliberate, not lax.

---

## Fork 5 — model (ADR-001): build strong, eval down

**Sequencing (recommendation):** build the agent on a **strong** model so any
tool-calling bug is in the wiring, not the model — the Harness default is Claude
Sonnet 4.6; use it (or Haiku 4.5) while wiring. **Then** run the ADR-001 eval to
find the cheapest model that holds the bar. Picking the cheap model first would
conflate "the wiring is wrong" with "the model is weak."

**The eval needs the agent + tools built to be meaningful**, so it is a
**Step-6-tail / Step-7 task, not a prerequisite.** Design (detail in the ADR-001
update):

- **Inputs:** the synthetic matter set, expanded to cover every branch —
  send-reminder, escalate (overdue), do-nothing (not yet due), flag-anomaly
  (low confidence / mismatch), and the boundary cases.
- **Candidates:** Nova Micro, Nova Lite, Claude Haiku 4.5, Claude Sonnet — with
  **prompt caching on for all** (the system prompt is static and long, so cached
  input dominates and its ~90% discount materially changes cost/decision).
- **Metrics:** correct-tool-selection %, **guardrail adherence (100%, no
  tolerance — one breach disqualifies)**, escalation-boundary accuracy (tracked
  separately — the deciding metric), cost/decision (measured, with caching).
- **Tiering hypothesis:** cheap model for routine, escalate to a stronger model
  near the judgment boundary — validated only if the cheap model matches except
  at the boundary.

---

## Fork 6 — the thin slice: agent → one matter → one tool → Observability

**Confirmed: `escalate_to_human` on the overdue MTR-2026-0142 census.** Your lean
is right, and it is right for three reasons that happen to align:

1. **It is the correct action.** MTR-2026-0142's census is `in-review` and its
   due date is in the past → the right move is escalate, not remind. The agent
   choosing escalate is a *true* decision, not a contrived one.
2. **It exercises the ADR-001 escalate-vs-remind boundary** — the single most
   important thing to prove the brain can do, and the deciding metric of the
   model eval.
3. **It sidesteps the SES sender-identity gap** — escalation goes to an SNS
   topic, which needs no verified SES sender.

**One push-back, on the destination.** `escalate_to_human` needs somewhere to
go. Options: (a) SNS topic with an email subscription to the verified gmail; (b)
just an `ACTION#escalated` row on the matter. Recommend **(a) SNS + the action
row** — SNS makes the escalation a real, observable outbound signal (proving the
tool *does something*, not just writes state), while the action row keeps the
audit trail complete. One new SNS topic; email sub to the gmail; no SES sender
needed.

**Minimum scope that proves the brain:**

```
manual single-matter invoke (MTR-2026-0142)
  → Harness reasons over the matter's REAL state (read from ida-dev-matters)
  → selects escalate_to_human   (over send_reminder / do-nothing)
  → Gateway target Lambda: append ACTION#escalated + SNS publish
  → the decision + reasoning captured in Observability (the audit trail)
```

**Explicitly out of the slice:** the other four tools wired live; Cedar Policy;
Memory; the scheduled sweep; `send_reminder`'s SES path. Each is a named later
addition, not a gap.

**Definition of done:** invoking the agent on MTR-2026-0142 produces an
escalation (SNS message + `ACTION#escalated` row visible in the readout) and a
traced decision in Observability explaining *why* — citing the overdue date and
the in-review status. If it reminds instead of escalates, the slice fails, and
that is exactly the signal we want the test to be able to give.

---

## Fork 7 — invocation, Observability, Memory

**Invocation.** Single-matter **manual invoke** for the slice (invoke the Harness
endpoint with `{matterId}`). The **GSI-backed scheduled sweep** — already proven
queryable in Step 5 (`STATUS#missing` by due date) — is the later expansion that
makes it autonomous. Building the manual path first keeps the slice thin and
makes the agent's decision reproducible on demand.

**Observability — wired from the start, non-negotiable.** The decision audit
trail *is* the compliance story ("why did the agent do that?"). The Harness
traces every action automatically, so this is on by default; the slice's DoD
explicitly requires the reasoning to be inspectable, not just the outcome.

**Memory — defer, with reason.** The thin slice is a single stateless decision
on one matter, and the durable per-matter state the agent needs **already exists
in the matter table** — that is what it reads. AgentCore Memory
(per-matter/counterparty continuity, so a second reminder doesn't read like the
first) matters once there are multi-touch sequences over time; it adds state and
per-event cost without changing whether the brain can make a correct decision.
Defer to the multi-touch / cadence pass.

---

## Fork 8 — seed change: keep MTR-2026-0142 deliberately overdue

Per the Step-5 clock-drift finding, the census due date was `2026-07-24` and is
now in the past — which is what makes the escalate path correct. Make that
**robust**, not accidental: set the census due date **relative to now**
(e.g. `now − 2 days`) in the seed so the escalate-vs-remind boundary is *always*
exercised whenever the seed runs, and keep another matter's document due in the
**future** (`now + N days`) so the *remind* / *do-nothing* branch also exists.
This turns the clock-drift wrinkle into a permanent test fixture for both sides
of the boundary.

---

## Build-plan summary (for the build pass, after review)

1. Retire the probe: destroy `AgentCore-IdaAgentProbe-dev`, remove
   `agent/runtime/IdaAgentProbe/` (ADR-006).
2. `escalate_to_human` tool Lambda + SNS escalation topic (email sub → gmail) +
   append `ACTION#escalated`, in `infra/`.
3. `Ida-Dev-Agent` stack: real `CfnGateway` + `CfnGatewayTarget` (escalate) +
   `CfnHarness` (model, `system-prompt.md`, the one tool), replacing the stub.
   Typed references/grants to the matter table, CMK, SNS.
4. Seed change (Fork 8): relative overdue + one future-due doc.
5. Invoke on MTR-2026-0142; confirm escalation + traced reasoning; extend the
   readout to show the escalation action.
6. Deferred/next: remaining four tools, Cedar Policy set, model eval, scheduled
   sweep, Memory, `send_reminder` SES path.

**Thin-slice discipline check:** one new tool, one new SNS topic, one Harness,
one Gateway, one manual invoke. No Policy, no Memory, no sweep, no multi-tool
wiring. Everything else is named and deferred.

---

## First-deploy findings (2026-07-26)

The first `Ida-Dev-Agent` deploy failed at **early validation with no resources
created** (clean fail — the account's v30 bootstrap upgrade had cleared the
earlier version block). Every failure was an **AgentCore resource naming
constraint**, not a logic error. Confirmed against the bundled
`bedrock-agentcore-control` service model (`service-2.json`, authoritative — the
same min/max/pattern the API enforces) and fixed before redeploy, so the whole
class is closed in one pass rather than one rollback at a time.

### The constraints, from the service model

| Field | Pattern / limit | Underscores? | Hyphens? |
|---|---|---|---|
| `GatewayName` | `([0-9a-zA-Z][-]?){1,100}` | ❌ no | ✅ yes |
| `TargetName` | `([0-9a-zA-Z][-]?){1,100}` | ❌ no | ✅ yes |
| `GatewayDescription` / `TargetDescription` | `min 1, max 200` | — | — |
| `HarnessName` | `[a-zA-Z][a-zA-Z0-9_]{0,39}` | ✅ yes | ❌ no |
| `ToolDefinition.name` (agent-visible tool) | `String`, unconstrained | ✅ yes | ✅ yes |

### The trap: Gateway/Target and Harness have OPPOSITE rules

The gateway family (`GatewayName`, `TargetName`) **forbids underscores, allows
hyphens**. The harness (`HarnessName`, and by extension harness-side tool config
names) **forbids hyphens, allows underscores**. So they need *different* naming
conventions, not a shared one — a single project-wide convention would violate
one of them. Fixes applied:

- `TargetName` `escalate_to_human` → **`escalate-to-human`** (hyphens)
- `TargetDescription` 224 → **≤200 chars** (trimmed)
- `HarnessName` `ida-dev-doc-chase-agent` → **`ida_dev_doc_chase_agent`** (underscores, 23 chars)
- HarnessTool config `name` `gateway-tools` → **`gateway_tools`** (proactive — same harness no-hyphen rule; not flagged by the failed deploy but the same class)

### The name the agent invokes is unchanged

Only *resource* names changed. The tool the agent actually calls is the
`ToolDefinition.name` — unconstrained, so it stays **`escalate_to_human`**, and
the Harness `allowedTools: ['escalate_to_human']` matches it. Verified in the
synthesized template: the agent-visible tool name is `escalate_to_human`.

### Method note

Rather than fix the three the deploy reported and risk a fourth, every AgentCore
`name`/`description` in the stack was validated against the service-model
patterns programmatically at synth time. All pass. This is the same
"confirm the contract against the authoritative source, not the rollback"
discipline used for the Step-5 BDA blueprint schema and the tool-type enum —
applied to a whole constraint class at once.

### Still an honest post-deploy unknown

How the Gateway namespaces the tool name it exposes to the agent (bare
`escalate_to_human`, or a `<target>___<tool>` composite). If it composes,
`allowedTools` may need the composite form. `allowedTools` is optional, so the
fallback is to drop it (the gateway has one tool anyway). Confirmed on the first
successful invoke, not guessed now.

---

## First-run findings (2026-07-27) — getting the agent to actually act

The build synthesized clean and deployed, but the agent did not act until four
distinct problems were peeled back, each hidden behind the previous one. The
decision leg (correct reasoning) worked from the start; the **execution leg** did
not. Verifying against the **DynamoDB row** — not the invoke's output — is what
exposed each one. These are the reusable lessons.

### The headline lesson: the managed Harness has an implicit base-permission contract

**Symptom (the "silent empty-toolConfig" failure):** the agent reasons correctly,
returns `stopReason: end_turn` with **no tool call**, the tool Lambda has **zero
invocations**, and nothing errors. It looks like the model "chose" not to act.

**Cause:** the managed Harness authenticates to the Gateway — to *discover and
call* its tools — using a **Workload Identity token** (`bedrock-agentcore:GetWorkloadAccessToken`).
The Harness execution role, hand-built in CDK, was missing that permission (and
the rest of the base contract: X-Ray, logs, metrics, ECR-public). With no
workload token, tool discovery **fails silently**, the Harness builds the
ConverseStream request with an **empty `toolConfig`**, and the model has no tool
to call. Everything downstream is a consequence.

**Why it bites CDK specifically:** the AgentCore **CLI auto-provisions** this base
role; a **self-built role does not get it**. The AWS docs say so explicitly
(harness-security.html: *"The AgentCore CLI creates a role with these permissions
automatically… The policy above is for cases where you create the role
yourself."*). This is the exact seam a "build it natively in CDK" decision
(ADR-006) lands on, and it is invisible until you notice the tool never runs.

**The base contract (least-privilege scoped in `agent-stack.ts`):**
`GetWorkloadAccessToken`/`…ForJWT` on the workload-identity resources (the tool
fix), `xray:PutTraceSegments…` (the missing-trace fix), `logs:*` on
`/aws/bedrock-agentcore/runtimes/*`, `cloudwatch:PutMetricData` in the
`bedrock-agentcore` namespace, and `ecr-public:GetAuthorizationToken` +
`sts:GetServiceBearerToken`.

**Layer confirmed before fixing (ADR-006 held):** the AWS "Connect to tools" doc
states the managed Harness *auto-injects* gateway tools (*"Reference a gateway
ARN and every tool configured on that gateway becomes available"*). So this was a
config/permission bug, **not** an architecture where the client must supply
tools — the client stays thin, the managed orchestration stays intact.

### The other three, and the meta-lesson

1. **Auto-provisioned memory needed permissions too.** Leaving the harness
   `memory` unset made it auto-create a session-memory resource whose events the
   role couldn't read (`ListEvents` AccessDenied), breaking the loop at start-up.
   Fixed by `memory: { disabled: {} }` — which also honored the design's
   defer-Memory decision.
2. **A fabricated model id.** `anthropic.claude-sonnet-4-6-20260514-v1:0` was
   guessed and invalid; Sonnet 4.6 has no date suffix and is inference-profile-
   only (`us.anthropic.claude-sonnet-4-6`). Should have been confirmed against
   Bedrock, as the Step-3 probe model was.
3. **AgentCore resource naming constraints** (GatewayTarget forbids underscores,
   Harness forbids hyphens, description ≤200) — caught pre-deploy against the
   service model.

**The meta-lesson — metrics lie, rows don't.** Multiple times an invoke printed
`TOOL CALLED` (an *emitted* tool-use block) and a CloudWatch datapoint appeared,
while the DynamoDB row — the thing the tool actually writes — was absent. The
Lambda **log stream** (created synchronously on first execution, lag-free) and
the **matter row** were the only reliable artifacts. A verification bar of
"the row exists AND the email arrived," never "toolConfig is present now" or
"should work," is what kept four hollow passes from being recorded as a milestone.
`scripts/invoke-agent.py` now queries the row after invoking, so its own output
can't repeat the false pass.
