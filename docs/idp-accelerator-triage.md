# Step 4 — GenAI IDP Accelerator triage

**Date:** 2026-07-22
**Scope:** investigation only. No infrastructure code, no BDA project, nothing deployed.
**Decision recorded in:** [ADR-004](decisions/ADR-004-idp-accelerator-adopt-vs-direct.md)
**Does not re-open:** [ADR-002](decisions/ADR-002-bda-vs-textract.md) — BDA Pattern 1 stays the extraction approach. This is about *how much framework* to take on around it.

All findings below come from the **published type declarations** of the packages
themselves (`npm pack` → `lib/**/*.d.ts`), not from marketing pages, and from
npm registry metadata. Where a claim comes from the web it is marked.

---

## 1. Package survey

| | `@cdklabs/genai-idp` | `@cdklabs/genai-idp-bda-processor` |
|---|---|---|
| Version | **0.3.1** | **0.3.1** |
| First publish | 2025-09-24 | 2025-09-24 |
| Last publish | **2026-05-18** (~2 months stale) | **2026-05-18** |
| Total releases ever | **7** (`0.0.0 … 0.3.1`) | 7 |
| License | Apache-2.0 | Apache-2.0 |
| Tarball | **5.72 MB** | 0.07 MB |

Siblings, both also 0.3.1: `@cdklabs/genai-idp-bedrock-llm-processor`
(Pattern 2 — Textract + Bedrock, the ADR-002 escape hatch) and
`@cdklabs/genai-idp-sagemaker-udop-processor` (Pattern 3).

### Version tracking — the packages lag upstream

The source carries `@since 0.5.2` annotations on interfaces that npm publishes
as `0.3.1`. That is not a typo: the CDK packages are versioned independently
from the upstream solution they mirror
(`aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws`).
The sibling Terraform port makes the scheme explicit — it publishes as
`0.5.12-tf.0`, "compatible with upstream IDP v0.5.12" *(web)*.

So the CDK constructs track roughly **upstream 0.5.2** while upstream itself is
at **0.5.12**, and the last CDK publish was two months ago. Adopting means
sitting ~10 upstream patch releases behind, on a package with 7 lifetime
releases.

### Dependency compatibility — no conflict, but a large surface

`aws-cdk-lib ^2.241.0` and `constructs ^10.5.1` are both satisfied by our pinned
**2.261.0** / **^10.7.1**. No version conflict. CDK CLI 2.1132.0 is unaffected.

The problem is not compatibility, it is **surface area**. Peer dependencies:

| Peer dependency | Status |
|---|---|
| `@aws-cdk/aws-bedrock-alpha` | ⚠️ **alpha** |
| `@aws-cdk/aws-bedrock-agentcore-alpha` | ⚠️ **alpha** |
| `@aws-cdk/aws-glue-alpha` | ⚠️ **alpha** |
| `@aws-cdk/aws-lambda-python-alpha` | ⚠️ **alpha** — and requires **Docker** to bundle |
| `@aws-cdk/aws-sagemaker-alpha` | ⚠️ **alpha** — *dead weight on a BDA-only path* |
| `@cdklabs/generative-ai-cdk-constructs ^0.1.314` | 0.x (latest 0.1.318) |

All five alpha modules **do** publish at `2.261.0-alpha.0`, so they can be
aligned to our CDK version — that was checked, not assumed. But alpha CDK
modules are version-locked to the `aws-cdk-lib` release, which means **every
future `aws-cdk-lib` bump becomes a six-package lockstep upgrade**.

`infra/` currently has **6 direct dependencies**. Adopting takes that to **14+,
five of them alpha**, to run a pattern that uses none of SageMaker, Glue, or
AgentCore.

**Docker gate:** `aws-lambda-python-alpha` bundles Python Lambdas in a Docker
container. Docker CLI 29.1.2 is installed on this machine but **the daemon is
not currently running** — so `cdk synth` for `infra/` would go from a
zero-dependency operation to one requiring Docker Desktop up.

---

## 2. Resource inventory — what it actually stands up

Counted from the packages' own declaration files.

### Core (`@cdklabs/genai-idp`)

**59 Lambda function constructs.** Named, grouped by what they serve:

| Group | Functions | Needed for our thin slice? |
|---|---|---|
| **Document pipeline** | `queue-sender`, `queue-processor`, `workflow-tracker`, `lookup`, `update-configuration` | partly |
| **BDA path** (in processor pkg) | `bda-invoke`, `bda-completion`, `bda-processresults` | **yes — this is the useful part** |
| Other extraction | `ocr`, `classification`, `extraction`, `assessment`, `summarization`, `processresults` | no (BDA does these internally) |
| Evaluation / accuracy | `evaluation`, `save-reporting-data`, `rule-validation`, `rule-validation-orchestration` | no |
| **AppSync GraphQL resolvers** | ~15 (`upload-`, `delete-document-`, `reprocess-document-`, `get-file-contents-`, `list-documents-gsi-`, `list-documents-range-`, `configuration-`, `copy-to-baseline-`, `query-knowledge-base-`, `chat-with-document-`, …) | no |
| **Agent analytics** | `agent-processor`, `agent-request-handler`, `list-available-agents`, `agentcore-analytics-processor`, `agentcore-gateway-manager` | no |
| **Agent companion chat** | `agent-chat-processor`, `chat-session-resolver`, `delete-agent-chat-session`, `get-agent-chat-messages`, `list-agent-chat-sessions`, `agent-chat-resolver` | no |
| **Test studio** | `test-runner`, `test-results-resolver`, `test-set-resolver`, `docsplit-testset-deployer`, `fcc-dataset-deployer`, `ocr-benchmark-deployer` | no |
| **User management** | `user-management`, `user-sync` | no |
| Capacity planning | `calculate-capacity`, `calculate-capacity-resolver` | no |
| Document discovery | `discovery-processor`, `discovery-upload-resolver` | no |
| HITL | `complete-section-review` | later, maybe |

Plus **12.67 MB of bundled Python assets** in the tarball.

Other resources the core creates or can create:

- **DynamoDB:** `TrackingTable` (composite PK/SK + `TypeDateIndex` GSI),
  `ConfigurationTable`, `ConcurrencyTable`
- **SQS:** a document queue (private, not injectable)
- **Step Functions:** the processor's state machine
- **AppSync GraphQL API** (`ProcessingEnvironmentApi`) — optional
- **Cognito** (`UserIdentity`) + **web UI** (`WebApplication`) — optional
- **Reporting environment** (Glue/Athena) — optional
- **Knowledge base / vector store** — optional
- CloudWatch metrics namespace, log groups, optional X-Ray, optional VPC config

### BDA processor (`@cdklabs/genai-idp-bda-processor`)

`BdaProcessor` is a **facade over `UnifiedDocumentProcessor`**. It exposes
~22 CloudWatch metrics — including `metricBDARequestsThrottles`,
`metricBDARequestsRetrySuccess`, `metricBDARequestsMaxRetriesExceeded`,
`metricBDARequestsNonRetryableErrors`. That metric list is itself evidence:
someone has already hit BDA throttling in anger and handled it.

> **Correction to the Step 4 brief.** The brief assumed "Pattern 1 requires an
> existing BDA project configuration." It does not. `BdaProcessor` **creates the
> blueprints and the Data Automation Project itself, at CDK synth time**, from
> the class definitions in its configuration:
> *"Creates BDA blueprints and a Data Automation Project from the
> configuration's class definitions at CDK synth time."*
> What you author is the **configuration** (a YAML class/schema definition), not
> a pre-built project.

---

## 3. The critical boundary — overlap with what we already own

### It composes better than expected

`ProcessingEnvironmentProps` **requires** buckets to be passed in — it does not
create them:

```ts
readonly inputBucket:   IBucket;   // REQUIRED
readonly outputBucket:  IBucket;   // REQUIRED
readonly workingBucket: IBucket;   // REQUIRED
readonly key?:          kms.IKey;  // optional — accepts our CMK
```

So `ida-dev-raw-*` **can** be the `inputBucket`, and our shared CMK **can** be
the encryption key. That is a genuinely good design and removes the biggest
adoption objection.

It would, however, need **two more buckets** (`outputBucket`, `workingBucket`)
plus a `configurationBucket` on the processor — so three new buckets we do not
currently have.

Tables are the inverse — optional, created if omitted:

```ts
readonly configurationTable?: IConfigurationTable;
readonly trackingTable?:      ITrackingTable;
readonly concurrencyTable?:   IConcurrencyTable;
```

### ⚠️ The tracking table and the matter table are different concepts — do not conflate

This is the most important line in this document.

| | Accelerator `TrackingTable` | Our `ida-dev-matters` |
|---|---|---|
| **Models** | one **document's** journey through the pipeline | one **matter's** completeness |
| Key | composite `PK`/`SK` + `TypeDateIndex` GSI | `matterId` (PK-only today) |
| Answers | *"has this file been OCR'd, classified, extracted? did it fail? when?"* | *"which documents does this renewal still need, when are they due, what have we already done about it?"* |
| Lifetime | ends when the document finishes processing | persists for the life of the matter |
| Owner | the pipeline | **the product** |
| Retention | `dataTrackingRetention`, default 365 days | indefinite; it is the record |

A document can process perfectly (tracking table: `COMPLETE`) and still leave a
matter incomplete — because it turned out to be the *wrong* document, or the
matter needs four more. Conversely a matter can be complete while a document
processing job failed and was retried.

**We must never supply `ida-dev-matters` as the `trackingTable`.** The
interface (`ITrackingTable extends ITable`) is loose enough that it would
*compile* — and then the accelerator would write processing-status items into
the product's system of record. If the accelerator is ever adopted, it gets its
own tracking table and we let it.

### Where responsibility ends

```
  ITS RESPONSIBILITY                    │  OURS
  ──────────────────────────────────────┼────────────────────────────────
  object lands in inputBucket           │
  → queue → invoke BDA                  │
  → poll job → parse result             │
  → write extraction + confidence       │
     to outputBucket / trackingTable    │
                                        │  ← THE SEAM
                                        │  read extraction + confidence
                                        │  decide: accept or human review
                                        │  map document → matter + doc type
                                        │  update required/received/missing
                                        │  update ida-dev-matters
                                        │  (agent then reads matter state)
```

**The seam is: extracted fields + per-field confidence, out of the pipeline,
into a mapper we own that updates matter state.** That is the interface ADR-002
requires be kept narrow, and it is identical whether the left-hand side is the
accelerator or our own Lambda. That equivalence is what makes the decision in
ADR-004 low-risk.

---

## 4. Adopt vs. direct — the honest comparison

Our Step 5 thin slice, stated precisely:

> synthetic document lands in S3 → BDA classifies and extracts → matter state
> updates in DynamoDB → agent decides and calls `send_reminder` once → readout

| | (a) Adopt the accelerator | (b) Direct BDA calls |
|---|---|---|
| Packages added | 2 + **6 peer deps (5 alpha)** | 0 (SDK already present) |
| `infra/` direct deps | 6 → **14+** | 6 |
| Lambdas deployed | **59-construct library**, ~8 relevant | **1** |
| New buckets needed | 3 (output, working, configuration) | 0–1 |
| New tables | 3 (tracking, configuration, concurrency) | 0 |
| Docker required for synth | **yes** (`aws-lambda-python-alpha`) | no |
| BDA project creation | **done for us** from config | we call `CreateDataAutomationProject` once |
| BDA retry/throttle handling | **mature, metric-instrumented** | we write it |
| Step Functions orchestration | included | not needed at this scale |
| HITL workflow | included | we write it (or defer) |
| Web UI / GraphQL / Cognito | included, unused | n/a |
| Upgrade cost | 6-package lockstep on every CDK bump | none |
| Blast radius of a 0.x breaking change | **`infra/` synth** | none |

### What adopting genuinely buys — smaller than it first appeared

The initial assessment listed two benefits. **Checking the BDA API removed most
of the second one, which was the load-bearing argument.**

1. **Blueprint + Data Automation Project creation from declarative config.**
   Real. Otherwise we call the BDA control-plane APIs ourselves and manage
   blueprint versioning by hand.
2. ~~Battle-tested async orchestration, because BDA is submit → poll → fetch and
   naive polling rots.~~ — **the premise was wrong. BDA is event-driven.**

#### BDA emits EventBridge events on completion — there is no poll loop to build

Verified against the API model itself (`aws bedrock-data-automation-runtime
invoke-data-automation-async --generate-cli-skeleton`), not from documentation:

```jsonc
{
  "clientToken": "",                             // idempotency
  "inputConfiguration":  { "s3Uri": "" },
  "outputConfiguration": { "s3Uri": "" },
  "dataAutomationConfiguration": { "dataAutomationProjectArn": "", "stage": "LIVE" },
  "encryptionConfiguration": { "kmsKeyId": "" }, // our CMK
  "dataAutomationProfileArn": "",                // us.data-automation-v1 CRIS profile
  "blueprints": [{ "blueprintArn": "", "version": "", "stage": "" }],
  "notificationConfiguration": {
    "eventBridgeConfiguration": { "eventBridgeEnabled": true }   // <-- this
  }
}
```

With `eventBridgeEnabled: true`, BDA emits an EventBridge event on completion.
Source `aws.bedrock`, with four distinct detail-types *(web — AWS EventBridge
Bedrock events reference)*:

- `Bedrock Data Automation Job Created`
- `Bedrock Data Automation Job Succeeded`
- `Bedrock Data Automation Job Failed With Client Error`
- `Bedrock Data Automation Job Failed With Service Error`

So the completion half is **an EventBridge rule and a Lambda**. The part most
likely to be built badly is not something we must rebuild — it is avoidable at
the API level, and the accelerator is wrapping machinery we simply do not need.

**The event taxonomy also does our error classification.** Client error vs
service error is exactly the non-retryable vs retryable split, delivered by
event type rather than inferred from an exception.

#### What genuinely remains ours

- **Submit-side throttling and retry.** `InvokeDataAutomationAsync` can throttle;
  that call needs backoff.
- **Idempotency** via `clientToken`. Duplicate S3 events are real.
- **Metrics**, so ADR-004's reversal trigger is measurable.

That is the revised scope of what the accelerator is worth reading for:
**submit-side resilience and error classification — not an orchestration
engine.**

### What adopting costs

Everything in the table, but the decisive item is this: **we would take on five
alpha CDK modules and a 59-Lambda library to use three Lambdas' worth of
behaviour.** ADR-003 already established a position on precisely this trade —
we kept the generated AgentCore `cdk/` at arm's length specifically to keep
alpha-library churn out of `infra/`'s toolchain. Adopting here contradicts that
for weaker reasons: the AgentCore alpha dependency at least sits in a separate,
disposable sub-project. This one would land **inside `infra/`**, the
deterministic pipeline whose stability is the whole selling point.

### Recommendation: **(b) direct BDA calls. Do not adopt for Step 5.**

Keep the accelerator as a **reference implementation**, scoped to submit-side
resilience and error classification (see the EventBridge finding above — the
polling machinery is moot). Apache-2.0, so reading it costs nothing.

This is *not* "build it all ourselves." It is: an S3-event Lambda that submits
with `clientToken`, an EventBridge rule on the BDA completion events, a
completion Lambda that parses and maps to matter state, and one BDA project.

**"Read their code" is written up as a concrete Step 5 prerequisite task in
ADR-004**, with a timebox, a named deliverable, and the metric set to emit —
because an intention to read source evaporates under deadline.

**Reasons, ordered:**

1. **Thin-slice discipline.** Step 5 processes synthetic documents at roughly
   one per test run. Step Functions, a concurrency table, and a queue are
   solutions to problems we do not have and cannot yet size.
2. **The dependency asymmetry is stark.** 6 → 14+ direct deps, five alpha, to
   avoid writing perhaps 150–250 lines of Lambda.
3. **Precedent.** ADR-003 chose to keep alpha churn out of `infra/`. Adopting
   would reverse that, in the more sensitive place.
4. **Maintenance signal.** 0.3.1, 7 lifetime releases, two months since last
   publish, tracking ~upstream 0.5.2 while upstream is at 0.5.12.
5. **The seam is identical either way.** Because responsibility ends at
   "extracted fields + confidence → mapper → matter state", adopting later is a
   contained change, not a rewrite. **The cost of being wrong is low and
   symmetric**, which is what makes the cheaper option correct now.
6. **Docker becomes a synth prerequisite.** Today `cd infra && npx cdk synth`
   needs nothing but Node. That property is worth keeping.

**What would change the answer** — see ADR-004 for the formal triggers, but in
short: real document volume, more than ~3 document classes needing
classification, a genuine HITL review workflow, or our own BDA orchestration
proving unreliable in Step 5.

---

## 5. BDA project prerequisites — what we would author either way

This work is required whether we adopt or not; only the mechanism differs
(accelerator YAML config → synth-time creation, vs. a `CreateDataAutomationProject`
call we make once).

BDA's own docs: **BDA handles OCR, classification, extraction, and assessment
internally.** What we define is the *schema of what we want out*.

### The pieces

| Piece | What it is | Our decision |
|---|---|---|
| **Data Automation Project** | container; invoked via the `us.data-automation-v1` CRIS profile | one per stage (`ida-dev-*`) |
| **Blueprint** | per document class: the extraction schema | one per class below |
| **Document class** | the label BDA assigns during classification | see table |
| **Field schema** | typed fields per blueprint, with descriptions that drive extraction | sketched below |
| **Confidence threshold** | per-field; below it → human review | **not guessable — derive from ADR-002's calibration data** |

### Sketch for our document types

Deliberately a sketch. Real field lists come from real documents, which we do
not have yet.

| Document class | Why it matters to a matter | Candidate fields |
|---|---|---|
| **Group renewal census** | headcount drives rating; usually a spreadsheet, not prose | employer name, effective date, employee count, per-employee rows (name/DOB/coverage tier/salary band) |
| **Summary of Benefits & Coverage (SBC)** | standardised federal form — the most reliably extractable class | plan name, carrier, plan year, deductible (individual/family), OOP max, coinsurance, copays |
| **Claim form** | high variance; likely the weakest class | claim number, claimant, date of service, provider, diagnosis codes, billed amount |
| **Onboarding / signed employer application** | the signature is the point | employer name, signatory name, **signature present (bool)**, signature date, tax ID, group size |

### Cross-cutting notes

- **Correlation is not a blueprint concern — it is the product's central design
  question.** See §5b below and **[ADR-005](decisions/ADR-005-document-matter-correlation.md)**.
- **"Signature present" is a boolean that decides whether a matter can close.**
  Worth a dedicated field and a high confidence threshold.
- The census is a **spreadsheet**; BDA's document handling is strongest on
  document-shaped input. Validate early — it may need different treatment from
  the other three.
- Thresholds start conservative (route more to human review) and tighten only
  once ADR-002's confidence-calibration data exists. **A false-confidence
  failure silently corrupts matter state**, which is the expensive direction.

---

## 5b. The document → matter correlation gap

**This is the product's central design question, not a Step 5 detail.** It has
its own decision record: **[ADR-005](decisions/ADR-005-document-matter-correlation.md)**.

**Nothing on a document says which matter it belongs to.** BDA extracts what is
on the page. A census names an employer; an SBC names a plan; a claim form names
a claimant. None carry our identifiers, because ours did not exist when the
document was created.

It matters this much because:

1. **It determines ingestion design.** Per-matter email aliases vs. an S3 key
   convention vs. an operator upload UI are different products. The ingestion
   stack follows from this decision, not the reverse.
2. **It is the first question in any TPA demo** — *"how does it know which
   renewal that belongs to?"*
3. **Getting it wrong is credibility-ending.** The agent chases the wrong
   renewal: emailing a broker about documents they already sent, or closing a
   matter on another client's census. Confidently wrong, and **outbound**.

The failure modes are asymmetric: **unattributed is a cost; misattributed is a
defect.** A missed association makes a matter look incomplete and it gets chased
— annoying, self-correcting. A wrong association makes a matter look complete
when it is not, and generates correspondence to the wrong party.

### Candidates (full evaluation in ADR-005)

| Mechanism | Failure mode |
|---|---|
| S3 key prefix `inbound/<matterId>/…` | only as good as the writer; pushes the problem upstream |
| Per-matter inbound email alias | wrong thread, forwards to the generic address; alias sprawl |
| Explicit operator association at upload | human error; does not scale to inbound email |
| Extracted-field matching (group no., policy ID, employer name) | **produces confident wrong answers** — names collide and abbreviate, numbers get transcribed wrong, and a renewal and a claim for the same employer are different matters |

**ADR-005's decision:** correlation is established **at ingestion** and carried
as metadata — never inferred from content alone. Extracted fields may *verify*
an association; they may never *create* one. Unassociated documents go to a
human triage queue rather than being matched by guessing. Mechanism deferred;
leading candidate is per-matter email alias with operator fallback, because the
agent's own `send_reminder` email is the thread the document returns on.

**Deciding the mechanism is an explicit Step 5 prerequisite**, ahead of the
ingestion design.

---

## 5c. Boundary guard — the type system will not help you

Recorded here because it is the failure that would be hardest to detect and
most damaging.

```ts
export interface ITrackingTable extends ITable {}   // no additional members
```

`ITrackingTable` adds **nothing** to `ITable`. Under TypeScript's structural
typing, passing `ida-dev-matters` where a tracking table is expected
**compiles cleanly** — and then the accelerator writes document-processing-status
items into the product's system of record. No type error, no runtime error, just
silent corruption of the table the agent reasons over.

**The rule, which survives any future reversal of ADR-004:**

> **Processing status and matter state stay in separate tables with separate
> lifecycles.** Processing status is transient (365-day default retention in the
> accelerator's own model); matter state is indefinite — it *is* the record. If
> the accelerator is ever adopted, it gets its own tracking table and we let it.

The sentence to keep, because it is the whole distinction in one line:

> **A document can process perfectly and leave a matter incomplete.**

The converse is equally true: a matter can be complete while a processing job
failed and was retried. The two never model each other.

This rule is also mirrored as a TODO in `infra/lib/understanding-stack.ts`, so
it is visible at the point where someone would be tempted to wire them together.

---

## 6. Summary — reuse / skip / build

| | |
|---|---|
| **Reuse** | The accelerator as a **reference implementation, scoped to submit-side throttle/retry/backoff, error classification, and its metric set** (Apache-2.0). Written up as a concrete Step 5 prerequisite task in ADR-004. ADR-002's Pattern 2 sibling stays the documented escape hatch. |
| **Skip** | Both npm packages as dependencies; the 59-Lambda library; Step Functions; tracking/configuration/concurrency tables; AppSync + Cognito + web UI; agent chat/analytics; test studio; capacity planning; discovery; reporting/Glue. **And their polling machinery** — EventBridge completion events make it unnecessary. |
| **Build** | An S3-event Lambda that submits with `clientToken` and `eventBridgeEnabled: true`; an EventBridge rule on the four BDA job detail-types; a completion Lambda that parses and maps to matter state. One BDA project + blueprints per class. A narrow internal interface (`classified type + extracted fields + per-field confidence`) so ADR-002's swap and ADR-004's reversal both stay cheap. Plus a **triage queue** for unassociated documents (ADR-005). |
| **Decide first** | **ADR-005** — the document → matter correlation mechanism, ahead of the ingestion design. |
