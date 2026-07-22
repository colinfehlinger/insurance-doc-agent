# ADR-002 — Bedrock Data Automation vs Textract for the understanding layer

**Status:** Decided for Steps 4–5. **Revisit after Step 5 measurement.**
**Date:** 2026-07-22
**Related:** [ADR-001](ADR-001-foundation-model.md), [docs/architecture.md](../architecture.md)

---

## Context

The understanding layer classifies inbound documents and extracts fields from
them. Two credible paths on AWS:

1. **Bedrock Data Automation (BDA)** — managed, GenAI-powered. Blueprints per
   document type; classification and extraction in one service; confidence
   scores per field. Reused via `@cdklabs/genai-idp` +
   `@cdklabs/genai-idp-bda-processor` (Pattern 1).
2. **Textract + Bedrock** — Textract for OCR/forms/tables, a foundation model
   for classification and field mapping, composed by us. Available as the
   sibling construct `@cdklabs/genai-idp-bedrock-llm-processor` (Pattern 2).

**The tension worth naming up front.** This project's stated principle is that
the pipeline is deterministic and only the agent exercises judgment. BDA is
GenAI-powered — a foundation model sits inside the pipeline. Calling that half
"deterministic" without qualification overclaims, and a compliance reviewer
would catch it. The honest framing, adopted in `docs/architecture.md`, is:

> a **fixed, auditable pipeline** with confidence-scored extraction and
> mandatory human review below threshold — versus an agent that owns only the
> judgment.

That is defensible and still meaningfully different from an agentic pipeline.
The pipeline's *shape* does not change per document; the same input follows the
same path, produces a confidence score, and routes to a human when uncertain.
Nothing in it decides what to do next. **Note this is a weaker claim than
"deterministic" in the strict sense** — see the reproducibility metric below,
which is the test of how much weaker.

**AWS's own guidance** favours BDA for managed setup and rapid time-to-value,
and Textract + composable components for fine-grained control, volumes above
~50K pages/month, or where 95%+ accuracy must be demonstrated for compliance.

**Cost is genuinely ambiguous** at our stage. Textract basic OCR is ~$1.50 per
1,000 pages, but forms and tables — which is what a benefits document actually
is — run ~$50–65 per 1,000 pages. BDA prices flat per document. Which wins
depends on pages-per-document and how many Textract feature types are needed,
neither of which is known until real documents exist. **Do not model this on
assumed inputs.**

**Textract does not classify natively.** Document classification would have to
be built — a Bedrock call, or a trained classifier — so Pattern 2 is not merely
"swap the OCR engine". It is more moving parts we own.

## Decision

**Stay on BDA (Pattern 1, `@cdklabs/genai-idp-bda-processor`) through Steps 4–5.**

Rationale: it is the faster path to a working thin slice, classification is
included rather than built, and confidence scores — which the human-review
threshold depends on — come out of the box. At Step 4/5 volumes the cost
question is unanswerable anyway.

**This is explicitly reversible.** The accelerator ships Pattern 2 as a sibling
construct with the same composition model, so switching is closer to a package
swap than a rewrite. That reversibility is the reason it is safe to decide now
on incomplete cost information.

## Measurement criteria (Step 5)

Assemble a fixed evaluation set of **at least 50 documents** spanning the real
mix — signed applications, census spreadsheets, prior-carrier billing
statements, scanned/faxed pages, at least a few deliberately poor scans.
Hand-label the ground truth once; reuse it for every future comparison.

| Metric | Definition | Bar |
|---|---|---|
| **Extraction accuracy** | Per-field exact match against hand-labelled ground truth, reported per document type (not one blended number — a 90% average can hide a 40% document type). | ≥ 95% per type. Below that, evaluate Pattern 2. |
| **Classification accuracy** | Correct document type assigned. | ≥ 98% |
| **Cost per page, at our real mix** | Measured BDA spend on the eval set ÷ pages. Compare against a Textract quote computed from the *actual* feature types those documents need (forms/tables, not basic OCR). | reported — informs, does not decide |
| **Reproducibility** | Same document submitted **5 times**; compare extracted field values and confidence scores. | **Byte-identical field values.** Confidence-score drift is tolerable; value drift is not. |
| **Confidence calibration** | Of fields scored below threshold, what fraction were genuinely wrong? Of fields above, what fraction were wrong anyway? | False-confidence rate (wrong but above threshold) is the number that matters — it is the one that silently corrupts matter state. |

**The reproducibility metric is the one that tests this ADR's central claim.**
If the same document yields different field values across runs, the "fixed,
auditable pipeline" framing is not defensible and the architecture doc needs
weakening again — or Pattern 2 with a temperature-0 extraction step becomes the
compliance-driven answer regardless of cost.

## Consequences

- **Keep the processor behind an interface.** The understanding stack must
  depend on a narrow internal contract (document in → classified type +
  extracted fields + per-field confidence out), not on BDA types leaking into
  the matter-state writer. This is what preserves the package-swap property.
- Blueprints are BDA-specific and would not survive a switch. Treat time spent
  on them as investment that Pattern 2 would forfeit.
- BDA runs through the US cross-region inference profile
  (`us.data-automation-v1`): documents are stored in us-east-1 but inference may
  traverse us-east-1/us-east-2/us-west-1/us-west-2. Already recorded in
  `docs/architecture.md`; it must appear in any BAA before real PHI/PII lands.
- The human-review threshold is a product decision, not a technical default. It
  should be set from the confidence-calibration data above, not guessed.
- Revisit this ADR when any of: extraction accuracy misses the bar for a
  document type, reproducibility fails, volume approaches 50K pages/month, or a
  client contractually requires demonstrated 95%+ accuracy.
