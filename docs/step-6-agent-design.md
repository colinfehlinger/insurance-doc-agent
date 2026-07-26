# Step 6 — the agent (the brain): design and decisions

**Design pass only. No code, no deploy.** Forks and recommendations for review.
**Date:** 2026-07-25

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
