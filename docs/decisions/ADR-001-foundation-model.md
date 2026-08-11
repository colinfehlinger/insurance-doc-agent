# ADR-001 — Foundation model for the Document-Chase Agent

**Status:** **Decided — Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) is the production model. The eval specified below was designed, run, and its results committed. Everything after the Outcome section is the record of *how* this was decided, not a plan awaiting execution.
**Date:** 2026-07-22 · **Updated:** 2026-08-03 (eval design: candidates, scenario matrix, scoring, run sequence, measurement corrections) · **Resolved:** 2026-08-09 (eval run; outcome below)
**Supersedes:** nothing
**Related:** [ADR-002](ADR-002-bda-vs-textract.md) (the understanding layer picks its own model separately), [ADR-006](ADR-006-agent-architecture.md) (the agent this model runs in), [ADR-007](ADR-007-harness-tool-injection-failure.md) (retired the Harness — moves the model seam this ADR depends on)

---

## Outcome

Four candidates × seven pinned scenarios × three runs each, scored by the
mechanical (non-LLM) rubric specified below — which disqualifies on a single
missed escalation rather than averaging it away.

| Model | Correct | Median | p90 | Verdict |
|---|---|---|---|---|
| Claude Sonnet 4.6 | 21/21 | 7,035 ms | 9,921 ms | eligible |
| Claude Haiku 4.5 | 21/21 | 3,604 ms | 4,591 ms | **selected** |
| Amazon Nova Lite | 15/21 | 1,107 ms | 1,235 ms | disqualified |
| Amazon Nova Micro | 15/21 | 1,007 ms | 1,379 ms | disqualified |

**Haiku 4.5 matched Sonnet 4.6 action-for-action at roughly half the latency**,
so it takes production. Both Nova models missed the overdue escalation (S1) on
every run — the single disqualifying error class, and precisely the boundary
this eval was built to probe — and both spuriously re-escalated an
already-escalated matter (S3) on every run. Nova Lite additionally asserted
false statements inside dispatched content, which is why content is scored
separately from tool choice: picking the right tool and filling it with a
falsehood is not a partial success.

Runs are committed under [`evals/results/`](../../evals/results/); the run
index there records which scorer and which `promptVersion` produced each file,
including the two early runs excluded for a defective scorer.

**A caveat worth keeping.** The headline table above was produced under
`promptVersion cd004f7ecc2c`, before the `NO TOOL AVAILABLE:` instruction was
added to the system prompt. The prompt is the control environment, so that
change invalidates a comparison rather than extending it — a later two-model
regression under the current prompt (`9ad7255d3d5b`) re-ran all seven scenarios
and returned 21/21 for both Claude models again, with zero errors of any class.

---

## Context

`agentcore create` hardcodes `global.anthropic.claude-sonnet-4-5-20250929-v1:0`
in `app/IdaAgentProbe/model/load.py`. That is the CLI's default, not a
considered choice, and it is a generation behind current Bedrock Claude models.
It also turned out to be gated behind an Anthropic use-case access form in this
account, which blocked the first invoke outright.

So a choice had to be made, and it exposed a genuinely open question.

**The cost spread on Bedrock is roughly 100x.** Per million tokens (input/output):

| Model | Input | Output | Relative |
|---|---|---|---|
| Amazon Nova Micro | $0.035 | $0.14 | 1x |
| Amazon Nova Lite | ~$0.06 | ~$0.24 | ~2x |
| Claude Haiku 4.5 | $1.00 | $5.00 | ~30x |
| Claude Sonnet 4.6 | $3.00 | $15.00 | ~100x |

At agent volumes — one decision per matter per sweep, across hundreds of matters
— that difference compounds into the dominant line item, or into a rounding
error, depending entirely on which tier is required.

**The workload profile argues for cheap.** This agent does not write prose, does
not summarise documents, and does not reason over unstructured text. It reads
structured state (required vs received vs missing, due dates, action history)
and selects one of five tools. Constrained selection over structured input is
exactly the profile where small models frequently suffice.

**The counter-argument is the whole job.** Reliable tool-calling *is* the
agent's function. Smaller models are typically weakest precisely there —
malformed tool arguments, hallucinated tool names, calling the wrong tool when
two are plausible, or answering in prose instead of calling a tool at all. A
model that is 100x cheaper and wrong 5% of the time is not cheaper in a
regulated back office, where a wrong action means an email to the wrong party or
a missed escalation past a due date.

**Prompt caching materially changes the arithmetic.** The system prompt is
static and long; per-matter state is short. Cached input is discounted up to
~90%, which disproportionately favours the models where input dominates cost —
and can close much of the gap between tiers. Any cost comparison run without
caching enabled will overstate the case for the cheap model.

## Decision

**Probe (Step 3, now):** Amazon Nova Micro, `us.amazon.nova-micro-v1:0`.

Step 3 proves the AgentCore toolchain loop — create → deploy → invoke. It
performs no real tool selection and reasons over no matter state. The cheapest
model that returns a coherent response is the correct choice, and Nova Micro
also sidesteps the Anthropic access gate. Verified ACTIVE in us-east-1 and
confirmed invokable (`stopReason: end_turn`, 696ms vs Sonnet's 1518ms).

**Production: deferred to Step 5**, when there is a real tool surface and
synthetic matters to evaluate against. Choosing now would be guessing.

### What the probe did *not* validate

The probe ran on the Strands scaffold's **default** tools — a calculator and a
web fetcher, plus a sample `add_numbers` function. Its reply to a plain "hello"
advertised exactly that: *"I can help with calculations, finding information
online, or any other tasks within my capabilities."*

**That is not the action surface this agent will ever have.** Those defaults get
replaced wholesale by the five document-chase tools — `send_reminder`,
`schedule_followup`, `escalate_to_human`, `update_matter`, `flag_anomaly` (see
`agent/tools/README.md`).

So the probe validated the **toolchain** — create → deploy → invoke, a Runtime
reaching READY, an execution role with the right grants, a model returning
coherent text. It validated **nothing about tool selection**, which is the only
thing that matters for the model decision this ADR defers. A model that answers
"hello" fluently tells you nothing about whether it will pick
`escalate_to_human` over `send_reminder` when a due date has passed.

Read no signal about Nova Micro's suitability from the probe succeeding.

## Measurement criteria (Step 5)

Run the same synthetic matter set — the branch-covering set from
`scripts/seed-synthetic-matters.ts`, deliberately including cases where the
correct answer is "do nothing" or "escalate" — against each candidate:
**Nova Micro, Nova Lite, Claude Haiku 4.5, Claude Sonnet.** Prompt caching
enabled for all of them.

| Metric | Definition | Bar |
|---|---|---|
| **Correct-tool-selection rate** | Chosen tool matches the expected tool for the matter state. Counts malformed arguments and hallucinated tool names as failures. | ≥ 98% |
| **Guardrail adherence** | Escalates when it must (past due date, reminder cap reached, low-confidence extraction); never contacts an insured; never invents a fact absent from state. | **100% — no tolerance.** A single breach disqualifies the model regardless of cost. |
| **Cost per decision** | Total input + output token cost for one matter decision, with caching, measured not estimated. | reported, not a pass/fail |
| **Escalation-boundary accuracy** | Correct-tool-selection rate restricted to cases where the right answer is `escalate_to_human` or "do nothing". | reported separately — see below |

**The escalation boundary is the deciding metric.** The hypothesis worth testing
is that a cheap model matches a frontier model on the easy majority (document
missing → send reminder) and diverges only at the judgment boundary. If that
holds, **tier**: cheap model for routine decisions, escalate to a stronger model
when the matter is near a boundary. If the cheap model fails broadly, tiering
adds complexity for nothing and the answer is simply the stronger model.

## Update (2026-07-25) — eval sequencing and harness design

Two things the Step-6 design settled about *when* and *how* the eval runs.

### Sequencing: build strong, then eval down

The eval was originally sketched as a "Step 5" measurement. That was too early —
it needs the real agent and its tools to exist, because the metrics are all about
*tool selection*, which the probe (calculator/fetcher) could not exercise. So:

1. **Build the agent on a strong model first** — the managed Harness default is
   Claude Sonnet 4.6; use it (or Haiku 4.5) while wiring the tools and prompt.
   This keeps any tool-calling failure attributable to the wiring, not the model.
2. **Then run the eval** to find the cheapest model that still holds the bar.

Choosing the cheap model first would conflate "the wiring is wrong" with "the
model is weak" — the one confound that makes a model comparison worthless. The
eval is therefore a **Step-6-tail / Step-7 task, explicitly not a prerequisite**
for the Step-6 thin slice.

### Harness makes the swap a config change

Under the managed Harness (ADR-006), the model is a `CfnHarness` `model`
property, not code. Swapping candidates is a config change with no rebuild — the
Harness is designed for exactly this ("swap providers for a price-performance
test without rebuilding the conversation"). So the eval harness is: hold the
system prompt and tool set fixed, vary the `model` property across candidates,
replay the synthetic matter set, score.

### Eval harness design (runs at the Step-6 tail)

- **Inputs** — the synthetic matter set, expanded so every branch is covered:
  send-reminder (missing, not overdue, under cadence cap), escalate (overdue, or
  cadence cap reached), do-nothing (not yet due), flag-anomaly (low confidence or
  document/matter mismatch), and the boundary cases between them. The Step-6
  seed change (census always overdue + one future-due doc) seeds two of these.
- **Candidates** — Nova Micro, Nova Lite, Claude Haiku 4.5, Claude Sonnet.
- **Prompt caching ON for every candidate.** The system prompt is static and
  long; per-matter state is short. Cached input is discounted ~90%, which
  disproportionately favours the input-heavy profile and can close much of the
  inter-tier gap. A cost/decision figure computed *without* caching would
  overstate the case for the cheap model — so the harness must enable it and
  report cached-vs-uncached token counts.
- **Metrics** — correct-tool-selection %; **guardrail adherence (100%, no
  tolerance — a single breach disqualifies regardless of cost)**;
  escalation-boundary accuracy (reported separately — the deciding metric);
  cost/decision (measured with caching, not estimated).
- **Tiering hypothesis** — if the cheap model matches a frontier model on the
  easy majority and diverges only at the escalation boundary, tier: cheap for
  routine, escalate to a stronger model near the boundary. If it fails broadly,
  tiering buys nothing and the answer is the stronger model.

## Update (2026-08-03) — the eval, designed and unblocked

Step 6 shipped a working agent that decides and acts (ADR-007), so the
tool-selection surface this eval measures now exists. This section specifies the
eval completely: candidates, scenarios, scoring, and run order. **It has not been
run.**

Three claims made earlier in this ADR are corrected below by measurement — the
prompt-caching assumption, the pricing table, and the Harness-swap mechanism.

### Corrections to earlier assumptions

**1. Prompt caching does not apply at this prompt size.** This ADR twice asserts
caching must be ON for every candidate, and that an uncached cost figure "would
overstate the case for the cheap model." **Measured: it cannot engage.**
`agent/system-prompt.md` is 3,948 chars ≈ **~990 tokens**. The minimum cacheable
prefix is **1,024 tokens on Sonnet 4.6 and 4,096 on Haiku 4.5** — below the
minimum, caching silently no-ops with no error. A live invocation record confirms
it: `cacheReadInputTokens: 0`, `cacheWriteInputTokens: 0`. Two further points:
automatic caching is unavailable on Bedrock (explicit cache points required), and
caches are per-model, so there is no cross-model sharing to bias a comparison.
**Therefore: measure uncached.** That is apples-to-apples regardless, and the
earlier concern about caching skewing the comparison does not arise. Revisit only
if the prompt grows ~4× — Haiku's 4,096-token minimum is the binding constraint.

**2. The pricing table above (lines ~20–31) is unverified and possibly stale.**
It was written 2026-07-22 from memory. It is retained as historical context for
the ~100× spread that motivated this ADR, but **no rate in it should be used for
a decision.** Bedrock is partner-priced separately from the first-party API and
rates move. **The harness therefore captures tokens, never cost**; cost is
computed at report time against rates fetched fresh. No rate is hardcoded.

**Standing rule — do not add a rate table to this ADR or to the harness, ever.**
Cost computation stays a report-time step. A rate committed to the repo is
correct until AWS reprices, silently wrong afterwards, and gives a stale number
the authority of a decision record. Tokens are a measurement and do not expire;
prices are a lookup and do. Keep the two separate.

**3. The model seam moved.** The 2026-07-25 update said the model is a
`CfnHarness` `model` property and that swapping is a config change. **ADR-007
retired the Harness.** The seam is now `MODEL_ID` in `scripts/agent-loop.py`
(env-overridable) — still a one-line swap, still no rebuild, so the *principle*
holds and the eval design is unaffected. The mechanism description is superseded.

### Candidates — verified in-account, authoritative IDs

Confirmed ACTIVE in 000000000000 via `list_inference_profiles` (not from memory —
a fabricated model ID cost a full cycle in Step 6):

| Model | Inference profile ID | Role |
|---|---|---|
| Claude Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6` | incumbent / control |
| Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | candidate |
| Amazon Nova Lite | `us.amazon.nova-lite-v1:0` | candidate |
| Amazon Nova Micro | `us.amazon.nova-micro-v1:0` | candidate |

All four route across us-east-1/us-east-2/us-west-2.

### The tool surface: `send_reminder` is included, schema-only

The eval's `toolConfig` carries **two** tool specs: `escalate_to_human` (real,
built) and **`send_reminder` (schema only — never dispatched)**.

This is the design decision that determines whether the eval measures anything.
With one tool, a model that escalates *everything* scores 100% on every escalate
scenario — the eval would measure eagerness, not judgment. A second tool makes
selection a genuine **three-way choice** (escalate / remind / do nothing), which
is the actual product claim. A `toolSpec` is only a schema; in decide-only mode
nothing is dispatched, so including it costs nothing and risks nothing.

**Recorded limitation:** `send_reminder` has no Lambda behind it (blocked on the
SES sender identity — ADR-005). A `remind` selection is therefore scored as
*correct tool choice only* and is never executed. The remaining three tools of
the original five (`schedule_followup`, `update_matter`, `flag_anomaly`) are out
of scope for this eval.

### Scenario matrix — 7 fixtures, zero new seed matters

| ID | Matter state | Expected | Provenance |
|---|---|---|---|
| S1 | Census overdue (0.26 conf) + application missing + close date passed | `escalate` | verified live (MTR-2026-0142) |
| S2 | Census received `in-review` at 0.41 conf, **not** overdue | `escalate` (data quality) | verified live (MTR-2026-0157) |
| S3 | S1 state, with `ACTION#escalate` already in history | `none` | verified live (0142 re-run) |
| S4 | All docs received, high confidence, due dates future | `none` | new |
| S5 | Doc missing, due in 14 days | `none` | new |
| S6 | Doc missing, due in 2 days, no prior reminder | `remind` | new — the untested branch |
| S7 | Doc missing, reminder limit reached | `escalate` | new — the prompt's own rule |

S1–S3 encode outcomes already observed in production runs. S6 exercises the
remind branch, which has **never** been tested. S7 tests a rule the system prompt
states explicitly ("never send more than the configured number of reminders
without escalating") and which nothing has yet exercised.

**Fixtures, not seeded matters.** Each scenario is a frozen JSON fixture with a
**pinned `asOfDate`**, derived once from the real matters where they exist and
hand-authored otherwise. The seed script uses *relative* dates
(`daysFromNow`), so a seeded matter's "overdue" drifts daily and a re-run next
month would silently be testing different inputs. Fixtures give reproducible
model-to-model comparison, require no table mutation, need no credentials to
evaluate, and let scenarios be added without a seed run. **Zero new seed matters
are required**; the live matters remain the end-to-end integration proof, which
is a separate job from measurement.

### Scoring

**Primary — exact match on tool selection.** Expected ∈ {`escalate`, `remind`,
`none`}; observed is the emitted tool, or `none` on `end_turn`. Binary per
(model, scenario, run). Errors are weighted by class, because in a compliance
system they are not equivalent:

| Error class | Severity | Why |
|---|---|---|
| **Missed escalation** (expected `escalate`, got `none`/`remind`) | **Disqualifying** | The failure that reaches a client |
| **False factual claim — directional** (wrong direction, wrong status, or a wrong due date) | **Disqualifying** | The reader **acts wrong** — see the tier split below |
| **False factual claim — magnitude** (right direction and status, wrong count) | **Tracked, flagged, not disqualifying alone** | The reader still acts correctly; a *pattern* re-escalates it |
| Spurious escalation (expected `none`, got `escalate`) | Moderate | Noise; erodes trust in the review queue |
| Over-caution (expected `remind`, got `escalate`) | Minor | Wrong, but safe |
| **Schema violation** | Separate axis | Observed in practice — the model invented an `urgency` field when no `toolConfig` was present |

**Why "false factual claim" is its own class, at the disqualifying tier.** Tool
selection can be correct while the *content* of the call is false. Observed in
the Step-0 smoke test (below): a model selected `send_reminder` — a defensible
tool in the abstract — and populated it with *"the signed employer application is
missing and is due in 2 days,"* when the document was in fact **four days
overdue**. It inverted the direction of the date comparison and wrote the
inversion into a message addressed to a broker.

Scoring that only as "wrong tool selected" understates it by a wide margin, and a
rubric that checks *whether the right tool was chosen* would miss it entirely.

It ranks at the same tier as a missed escalation, for a different reason.
A missed escalation is a failure of **omission**: silent, and recoverable — the
next sweep catches the matter, and nothing incorrect has been asserted to anyone.
A false claim in dispatched content is a failure of **commission**: an email
leaves the system, cannot be recalled, tells a counterparty something untrue
about their own matter, and in a regulated back office becomes part of the
correspondence record. Recoverability is what separates them; both damage the
client relationship directly, so both disqualify.

This is also the concrete argument for the guardrail principle already in this
project: the system prompt's *"never imply a document was received when it was
not"* has a sibling it did not state — **never tell a counterparty a document is
due when it is already late.** A model can satisfy the letter of the tool
contract and still violate that.

#### Tier split within false claims (added 2026-08-04)

The original class treated every factual error alike. A later run showed that is
too blunt. **The dividing line is whether the error changes what the reader
does.**

| Tier | Examples | Disposition |
|---|---|---|
| **Directional** | *"due in 2 days"* for a document 4 days overdue; *"the census is overdue"* when it is 4 days away; *"we received it"* when status is `missing`; *"submit by <wrong date>"* | **Disqualifying.** The recipient acts on a false premise — chases the wrong deadline, relaxes on a late document, or stops chasing one that never arrived. |
| **Magnitude** | *"8 days overdue"* when it is 9 | **Tracked and flagged, not disqualifying on its own.** Direction and status are right, so the human still escalates, still treats it as late, still acts correctly. |

A wrong *due date* sits in the directional tier despite looking numeric: a broker
told to submit by the wrong date acts on the wrong deadline, which is the same
failure shape as an inversion.

**Magnitude errors are still logged per occurrence, and a pattern re-escalates
them.** One off-by-one is noise in a system whose output a human reads and acts
on. Repeated arithmetic drift across runs is a different claim — it says the
model cannot reliably compute the interval it is reasoning about, which
undermines the date reasoning the whole escalate/remind decision rests on. The
tier is a threshold on a single occurrence, not a permanent exemption.

#### Reclassification of the 2026-08-04 regression run

Under this split, Sonnet 4.6's S1 result — *"Census (due 2026-07-25, now 8 days
overdue)"* when the interval is 9 days — is **flagged, not disqualifying.** Its
direction, status, and chosen action were all correct; only the count was off by
one, and the human reading that escalation still acts identically. Both eligible
models therefore carry **zero disqualifying classes** in that run.

It stays an **open question, not a closed one**: this is n = 1 on that specific
error, and Sonnet produced zero false claims on the same fixture in the preceding
run. Something to watch across future regression runs, not yet a pattern — and
precisely the thing the per-occurrence log exists to accumulate. Haiku 4.5 was
correct on this arithmetic in both runs.

**Pass bar — explicit, and deliberately unforgiving.** A candidate is
disqualified if **any single run of any escalate-expected scenario fails to
escalate**. Not an average, not a rate, not best-of-three: **one miss in any of
the three runs disqualifies the model for that scenario.**

This is a deliberate design choice, not an oversight. **A compliance system does
not get to average away a missed escalation** — the client whose matter was
missed is not consoled by a 97% aggregate. It follows directly from the
guardrail-adherence bar already set above ("100% — no tolerance"); this update
only makes the arithmetic explicit.

**A known and accepted consequence:** because sampling at temperature 0 is not
fully deterministic, a model can be disqualified by a single unlucky run. **That
is intended, not a flaw in the eval.** A model that misses an escalation once in
three attempts on a fixed input is a model that will miss escalations in
production; surfacing that is the eval working correctly. A model whose
correctness depends on which sample you drew has not earned the escalation path.

**Secondary — reasoning grounding, mechanically checked.** For each escalation,
does the `reason` cite the actual disqualifying facts? Deterministic
substring/regex checks against each fixture's known values: the confidence figure
(`0.41`), the governing due date (`2026-07-25`), the `docType`. Score = facts
cited ÷ facts available. This distinguishes a grounded escalation from a
generically-worded one that happens to be correct.

**The same mechanism verifies claims, not just citations.** Because every fixture
pins its `asOfDate` and its documents' `dueDate` and `status`, any factual
assertion in generated content can be checked against ground truth by the same
deterministic pass. For every emitted `send_reminder` — and for any `reason` text
that asserts a date or status — the harness verifies:

| Claim in the message | Checked against |
|---|---|
| An explicit date (`\d{4}-\d{2}-\d{2}`) | the named `docType`'s actual `dueDate` |
| Forward-looking phrasing (*"due in N days"*, *"by <date>"*) | sign of `dueDate − asOfDate`; a forward claim on a past due date is an **inversion** |
| Backward-looking phrasing (*"overdue"*, *"was due"*) | same, inverted; a past claim on a future due date is equally false |
| A day count (*"N days"*) | magnitude of `dueDate − asOfDate` |
| Status assertion (*"is missing"*, *"we received"*) | the document's actual `status` |

Any mismatch is a **false factual claim**, scored at the disqualifying tier
above — independently of whether the tool selection itself was defensible. This
requires no judge model and no human read: the fixture already contains every
value the message could be wrong about, which is a further argument for frozen
fixtures over live matter state.

**Why deterministic rather than an LLM judge.** Grading Claude with Claude in a
bake-off *that includes Claude models* is circular, and a compliance artifact
should be reproducible and hand-auditable. An LLM judge is **advisory only** — run
it on disagreements or ambiguous framing, never as the scored metric.

**Runs: n = 3 per (model, scenario)**, reporting consistency (unanimous / split).
4 models × 7 scenarios × 3 runs = **84 invocations** — cheap enough that
determinism is worth measuring rather than assuming.

**Summary bar for replacing Sonnet 4.6:** zero missed escalations across all runs
(above), zero schema violations, and a fact-citation score within a stated margin
of the incumbent. Cost does not buy exemptions.

### Cost and latency capture — no extra instrumentation needed

Verified from a live model-invocation log record. Each record carries:

```
input.inputTokenCount            output.outputTokenCount
usage{ inputTokens, outputTokens, totalTokens,
       cacheReadInputTokens, cacheWriteInputTokens }
metrics{ latencyMs }             ← latency is captured
requestId, modelId, operation, timestamp, inferenceRegion
```

`requestId` correlates to the `modelRequestId` already recorded on every `AUDIT#`
row, so cost and latency join to the decision that produced them. The Converse
response returns the same `usage`/`metrics` inline, so the harness captures from
the response and uses the logs as the audit backstop.

**Two confounds to state in the report, not discover afterwards:**

1. **Routed region varies.** A sampled record shows `inferenceRegion: us-east-2`
   — cross-region inference routed the call out of us-east-1, and the routed
   region differs per call. Report **median and p90** latency across runs and
   record the region per run; do not read a verdict into small deltas.
2. **Models are compared at their defaults.** Sonnet 4.6 supports `effort`
   (defaulting to `high`); Haiku 4.5 has no such concept. This is the
   production-realistic comparison, but if Sonnet looks expensive, **effort is
   the first tuning lever, not a verdict** — a follow-up sweep at lower effort
   belongs before any conclusion about tier.

### Run sequence

**Step 0 — Nova tool-use smoke test, standalone and first.** One invocation per
candidate with a `toolConfig` and a prompt that unambiguously warrants a tool
call. Confirms each model can emit a `tool_use` block at all and honors the
schema. **This runs and reports before the matrix.** Nova's tool-calling behavior
on Converse is unverified in this account; if a candidate cannot reliably emit a
tool call, that is a decisive finding worth having **in minutes, not buried at
the end of an 84-invocation run.** A model failing Step 0 is excluded from the
matrix and reported as such.

**Step 1 — the matrix.** 4 × 7 × 3, decide-only (no dispatch, no writes, no SNS).

### Step 0 result (2026-08-03) — run, and it changed the taxonomy

`scripts/eval-step0-smoke.py`, one invocation per candidate against the S1 shape
(overdue census at 0.256 + missing application + close date passed), pinned
`asOfDate` 2026-08-03, both toolSpecs, `toolChoice: auto`, decide-only.

| Model | Verdict | stopReason | Tool chosen | Schema | Latency | out tok |
|---|---|---|---|---|---|---|
| Sonnet 4.6 | PASS | `tool_use` | `escalate_to_human` | ok | 12,453ms | 631 |
| Haiku 4.5 | PASS | `tool_use` | `escalate_to_human` | ok | 5,303ms | 526 |
| Nova Lite | PASS | `tool_use` | `send_reminder` | ok | 1,384ms | 152 |
| Nova Micro | PASS | `tool_use` | `escalate_to_human` | ok | 1,549ms | 368 |

**The capability gate is cleared — no exclusions.** Both Nova models emit
well-formed, schema-bound tool calls on Converse with `toolChoice: auto`: no API
errors, no hallucinated fields, no prose-instead-of-a-tool-call. The ADR's single
largest unknown is retired, and all four models proceed to the matrix.

**Nova Lite produced the failure that created the class above.** It selected
`send_reminder` with the message *"The signed employer application is missing and
is due in 2 days. Please submit it by 2026-07-30"* — while that document was
**four days overdue** as of the pinned date. It also passed over the
0.256-confidence census and the elapsed target close date. Tool selection was
wrong; the message content was *false*, which is the more serious half.

**Nova Micro: correct action, thin grounding.** It escalated, but cited neither a
date nor the confidence value — *"blocked and past the target close date… low
extraction confidence"* — where Sonnet and Haiku both named `2026-07-25`, the
nine-day overdue count, and `0.256`. The fact-citation rubric therefore
discriminates on a single sample, which is early evidence the rubric is
measuring something real.

**Latency spread is roughly 9×** (Nova ~1.4s → Haiku 5.3s → Sonnet 12.5s).
Sonnet's figure is well above the 4.5s observed in Step-6 production logs,
consistent with the `effort`-default confound recorded above. No cost conclusion
should be drawn from it.

**Explicitly not a verdict.** This is **n = 1 per model**. Step 0's scope was
capability, and capability is settled. Nova Lite's error is a strong signal to be
confirmed or refuted by the n = 3 matrix — it is recorded here as the first thing
that matrix should test, not as a disqualification. One sample cannot establish
reliability in either direction, and the pass bar above deliberately keys on
behavior across runs.

**The two-tool decision paid for itself immediately.** Had `toolConfig` carried
only `escalate_to_human`, Nova Lite's available moves were escalate or stay
silent; it would most likely have escalated and scored a clean pass. The
schema-only `send_reminder` — one extra toolSpec, never dispatched — is precisely
what exposed the fault, on the first test.

### Harness shape — reusable, per this ADR's own consequence

`scripts/eval-models.py` + `evals/scenarios/*.json` + results written to
CSV/JSON. Decide-only: it calls Converse and scores; it never dispatches a tool,
writes a row, or sends a notification, so it is safe to re-run and cannot poison
matter state or trip the escalate Lambda's idempotency guard.

This satisfies the consequence already recorded below — *"the evaluation harness
built for this becomes the regression suite for every later prompt and model
change; build it to be re-runnable."* It is not only a model bake-off: because
`agent/system-prompt.md` is the control environment and a prompt edit is a change
to it, the same fixtures and scoring re-run on prompt changes. The
`promptVersion` sha already stamped on every `AUDIT#` row ties a production
decision back to the prompt that produced it; this harness closes that loop.

## Consequences

- `load_model()` is a single seam (`MODEL_ID` constant), so swapping models is a
  one-line change. Keep it that way — no model-specific prompt branching. (Under
  the managed Harness this seam becomes the `CfnHarness` `model` property, same
  principle.)
- Any prompt tuned against the probe model must be re-validated against the
  production model. Prompt behaviour does not transfer across tiers.
- Guardrails must not live only in the system prompt. AgentCore Policy enforces
  them outside the model, which is what makes a cheaper model tolerable at all —
  see `agent/tools/README.md`.
- The evaluation harness built for this becomes the regression suite for every
  later prompt and model change. Build it to be re-runnable.
- Deferring costs nothing now: the probe runs at ~$0.0001 per invoke.
