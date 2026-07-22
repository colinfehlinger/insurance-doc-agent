# ADR-004 — Adopt the GenAI IDP Accelerator constructs, or call BDA directly?

**Status:** **Decided — do not adopt for Step 5.** Call BDA directly; keep the accelerator as a reference implementation. Revisit on the triggers below.
**Date:** 2026-07-22
**Related:** [ADR-002](ADR-002-bda-vs-textract.md) (BDA vs Textract — *not* re-opened here), [ADR-003](ADR-003-agentcore-cdk-fold-in.md) (the same alpha-dependency trade, in the other direction)
**Evidence:** [docs/idp-accelerator-triage.md](../idp-accelerator-triage.md)

---

## Context

ADR-002 settled *what* does extraction: Bedrock Data Automation, Pattern 1. It
did not settle *how much framework* comes with it. The
`@cdklabs/genai-idp` + `@cdklabs/genai-idp-bda-processor` packages are the
CDK-native form of the AWS GenAI IDP Accelerator and were the assumed reuse
target from the outset. This ADR tests that assumption.

The full survey is in the triage document. The facts that decide it:

**It composes better than expected.** `ProcessingEnvironmentProps` *requires*
`inputBucket`, `outputBucket`, and `workingBucket` as `IBucket` and accepts an
optional `key?: kms.IKey`. Our `ida-dev-raw-*` bucket and shared CMK can be
passed straight in. This removes the objection that mattered most going in.

**But the footprint is a product, not a library.** 59 Lambda function
constructs and 12.67 MB of bundled Python assets, spanning AppSync resolvers,
Cognito user management, a web UI, agent companion chat, agent analytics,
capacity planning, document discovery, a test studio, and Glue/Athena
reporting. Roughly **three** of those functions do the work our thin slice
needs (`bda-invoke`, `bda-completion`, `bda-processresults`).

**The dependency surface is the real cost.** Six peer dependencies, **five of
them alpha CDK modules** — `aws-bedrock-alpha`, `aws-bedrock-agentcore-alpha`,
`aws-glue-alpha`, `aws-lambda-python-alpha`, `aws-sagemaker-alpha` — plus
`generative-ai-cdk-constructs` at 0.1.x. `infra/` has **6 direct dependencies
today**; adopting takes it to **14+**. Alpha CDK modules are version-locked to
the `aws-cdk-lib` release, so every future CDK bump becomes a six-package
lockstep upgrade. `aws-lambda-python-alpha` also makes **Docker a prerequisite
for `cdk synth`**, which today needs nothing but Node.

**Maintenance signal is thin.** npm 0.3.1, **7 releases ever**, last published
2026-05-18. Source annotations read `@since 0.5.2` — the CDK packages track
roughly upstream **0.5.2** while the upstream solution is at **0.5.12**.

**Nothing here is a compatibility blocker.** `aws-cdk-lib ^2.241.0` is satisfied
by our 2.261.0, and all five alpha modules publish at `2.261.0-alpha.0` —
verified, not assumed. The objection is surface area and coupling, not
versions.

**The thin slice is small.** Step 5 is: synthetic document lands in S3 → BDA
classifies and extracts → matter state updates → agent decides once → readout.
At one document per test run, Step Functions, a queue, and a concurrency table
solve problems we do not have and cannot yet size.

## Decision

**Call the BDA APIs directly. Do not take either package as a dependency.**

Build one Lambda: S3 event → invoke BDA → poll the async job → parse the result
→ hand extracted fields and per-field confidence to a mapper that updates
`ida-dev-matters`. Create the Data Automation Project and its blueprints
ourselves.

**Keep the accelerator as a reference implementation** — but for a narrower
slice than first assessed. See "BDA is event-driven" below.

This is not "build everything ourselves." It is one Lambda pair, one BDA
project, and a deliberate steal of someone else's *submit-side* error handling.

### BDA is event-driven — this shrinks what adopting would have bought

The strongest argument for adopting was battle-tested async orchestration, on
the premise that BDA is submit → poll → fetch and naive polling rots. **That
premise is wrong, and it was the load-bearing one.**

`InvokeDataAutomationAsync` accepts a notification configuration — verified
against the API model itself, not documentation:

```jsonc
{
  "clientToken": "",                       // idempotency
  "dataAutomationProfileArn": "",          // us.data-automation-v1 CRIS profile
  "encryptionConfiguration": { "kmsKeyId": "" },   // our CMK
  "notificationConfiguration": {
    "eventBridgeConfiguration": { "eventBridgeEnabled": true }
  }
}
```

With that enabled, BDA emits an EventBridge event on completion. Source
`aws.bedrock`, with **four distinct detail-types**:

- `Bedrock Data Automation Job Created`
- `Bedrock Data Automation Job Succeeded`
- `Bedrock Data Automation Job Failed With Client Error`
- `Bedrock Data Automation Job Failed With Service Error`

So the completion half is a rule and a Lambda. **There is no poll loop to build
or to get wrong** — the part most likely to be built badly is avoidable at the
API level, not something the accelerator uniquely solves.

Better still, **the event taxonomy does our error classification for us**:
client error vs service error is precisely the non-retryable vs retryable split,
delivered by the event type rather than inferred from an exception.

**What genuinely remains ours** — and this is now the entire scope of the
reference-implementation work:

- **Submit-side throttling and retry.** `InvokeDataAutomationAsync` can throttle;
  that call still needs backoff.
- **Idempotency.** `clientToken` exists for this. Duplicate S3 events are real.
- **Metrics**, so this ADR's primary reversal trigger is measurable.

The revised buy column is therefore small: submit-side resilience patterns and
an error-classification approach, not an orchestration engine.

### Reasoning, ordered

1. **Thin-slice discipline.** The machinery solves scale problems we have not
   got. Adopting it now is designing for a load profile we cannot measure.
2. **The asymmetry is stark.** 6 → 14+ direct dependencies, five alpha, to avoid
   writing perhaps 150–250 lines of Lambda.
3. **Precedent, and consistency.** ADR-003 kept the generated AgentCore `cdk/`
   at arm's length *specifically* to keep alpha-library churn out of our pinned
   toolchain. Adopting here reverses that in the more sensitive place: the
   AgentCore alpha sits in a separate, disposable sub-project; this would land
   **inside `infra/`**, whose stability is the product's selling point.
4. **Maintenance risk.** A 0.x package with 7 lifetime releases, two months
   stale, ten upstream patch releases behind, in the deploy path of the
   deterministic half of the system.
5. **Docker.** `cd infra && npx cdk synth` currently requires only Node. Keeping
   that true is worth something.
6. **The cost of being wrong is low and symmetric** — see below.

### Why this is safe to decide now

Responsibility ends at the same place under either option: **extracted fields +
per-field confidence, out of the pipeline, into a mapper we own.** ADR-002
already requires that seam be kept narrow so the processor stays swappable. As
long as that holds, adopting the accelerator later is a contained change behind
a stable interface — not a rewrite. The decision is reversible in both
directions, which is exactly why the cheaper option wins today.

## ⚠️ Boundary that must not be crossed

The accelerator's `TrackingTable` and our `ida-dev-matters` are **different
concepts and must never be conflated**:

| | `TrackingTable` | `ida-dev-matters` |
|---|---|---|
| Models | a **document's** trip through the pipeline | a **matter's** completeness |
| Answers | *did this file OCR, classify, extract? did it fail?* | *what does this renewal still need, by when, and what have we done about it?* |
| Lifetime | ends when processing ends (default 365-day retention) | the life of the matter — it is the record |

`ITrackingTable extends ITable` with no additional members, so passing our
matter table where a tracking table is expected would **compile cleanly** and
then write processing-status items into the product's system of record. If this
ADR is ever reversed, the accelerator gets its own tracking table and we let it.

## Measurement criteria / reversal triggers

Revisit when **any** fires:

| Trigger | Why it flips |
|---|---|
| **Our BDA orchestration proves unreliable in Step 5** — dropped jobs, unhandled throttling, silent polling failures | This is the single most likely trigger and the one to watch first. The accelerator's retry handling is its strongest asset; if ours is worse in practice, that is decisive and measurable. |
| **More than ~3 document classes need reliable classification** | Blueprint and project management by hand stops being trivial; declarative config earns its keep. |
| **A real human-in-the-loop review workflow is required** (not just a threshold + flag) | HITL is built in. Rebuilding a review UI and state machine is a much bigger job than a Lambda. |
| **Document volume becomes real** — sustained concurrent processing, throttling under normal load | The queue, concurrency table, and Step Functions orchestration start solving actual problems. |
| **The packages reach 1.0 / non-alpha peer deps** | Removes the strongest objection. Re-evaluate rather than auto-adopt — footprint would still be 59 Lambdas. |

If none fire by the end of the series, the correct outcome is to **stay direct
and record why**. Not adopting is a legitimate terminal state, not a deferral.

## Consequences

- **The interface ADR-002 requires becomes load-bearing immediately.** The
  understanding layer must expose exactly: *document in → classified type +
  extracted fields + per-field confidence out.* No BDA-shaped types leak into
  the matter-state writer. This is what preserves both the ADR-002 processor
  swap and the ADR-004 reversal.
- We own BDA async orchestration: submit → poll → fetch → handle throttles,
  retries, and non-retryable errors. **Instrument it from day one** with the
  equivalents of the accelerator's metrics, or the reversal trigger above is
  unmeasurable.
- We own blueprint and Data Automation Project lifecycle, including versioning
  when a schema changes.
- No Step Functions, no queue, no concurrency control in Step 5. If load ever
  becomes real, that is the trigger — not a surprise.
- `infra/` keeps its 6 direct dependencies, its Docker-free synth, and its
  single-version CDK upgrade path.
- HITL is a threshold and a flag for now, not a workflow. Fine while a human is
  watching every run; not fine at volume.
- **Read the accelerator's source before writing the invocation Lambda.** This
  ADR only pays off if the reference implementation is genuinely used as one —
  and "read their code" evaporates under deadline, so it is written below as a
  concrete prerequisite task rather than an intention.

## Step 5 prerequisite task — extract the reference implementation

**Do this before writing our BDA invocation code, not after.** Timeboxed and
specific so it survives schedule pressure.

**Source:** `@cdklabs/genai-idp` (Apache-2.0), functions `bda-invoke`,
`bda-completion`, `bda-processresults`. Obtain with
`npm pack @cdklabs/genai-idp@0.3.1` and read the bundled Python assets — no
dependency is added by reading a tarball.

**Extract and write down:**

1. **Submit-side throttle/retry/backoff** — what they retry, backoff shape, cap.
   This is the part we still own; the completion half is EventBridge's now.
2. **Error classification** — which failures they treat as retryable vs
   terminal, and how that maps onto the `…Client Error` / `…Service Error`
   event split.
3. **Idempotency** — whether and how they use `clientToken`, and what they key
   it on.

**Emit their metric set from day one.** ADR-004's primary reversal trigger is
"our orchestration proves unreliable" — unmeasurable without equivalents of:

| Metric | Why it matters here |
|---|---|
| `BDARequestsThrottles` | Submit-side pressure — the part we own |
| `BDARequestsRetrySuccess` | Backoff is working |
| `BDARequestsMaxRetriesExceeded` | Backoff is *not* working — trigger condition |
| `BDARequestsNonRetryableErrors` | Classification is wrong, or input is bad |
| `BDAJobsSucceeded` / `BDAJobsFailed` | Completion-side truth, from the events |

**Deliberately out of scope:** their polling machinery, Step Functions
orchestration, and tracking-table writes. We are not rebuilding those — the
first is unnecessary, the last is the boundary below.

**Done when:** a short written summary of (1)–(3) exists and the metric list is
in the invocation Lambda's design, before any BDA call is written.
