# ADR-001 — Foundation model for the Document-Chase Agent

**Status:** Partially decided. Probe model chosen; **eval re-sequenced to the Step-6 tail / Step 7** (updated 2026-07-25 — it needs the real agent + tools to be meaningful).
**Date:** 2026-07-22 · **Updated:** 2026-07-25 (Step 6 design: build-then-eval sequencing + harness design)
**Supersedes:** nothing
**Related:** [ADR-002](ADR-002-bda-vs-textract.md) (the understanding layer picks its own model separately), [ADR-006](ADR-006-agent-architecture.md) (the agent this model runs in)

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
