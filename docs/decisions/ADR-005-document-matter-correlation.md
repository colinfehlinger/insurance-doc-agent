# ADR-005 — How a document is associated with a matter

**Status:** **Principle decided. Mechanism deferred to Step 5** (candidates evaluated below).
**Date:** 2026-07-22
**Related:** [ADR-002](ADR-002-bda-vs-textract.md) (extraction), [ADR-004](ADR-004-idp-accelerator-adopt-vs-direct.md) (surfaced this gap), [docs/idp-accelerator-triage.md](../idp-accelerator-triage.md)

---

## Context

**Nothing on a document says which matter it belongs to.**

Bedrock Data Automation extracts what is *on the page*. A group census names an
employer and lists employees; it does not say "this belongs to matter
MTR-2026-0142." An SBC names a plan and a carrier. A claim form names a
claimant. None of them carry our identifiers, because our identifiers did not
exist when the document was created.

This is not a Step 5 implementation detail. **It is the product's central design
question**, for three reasons:

1. **It determines ingestion design.** Per-matter inbound email aliases, an S3
   key prefix convention, and an operator-driven upload UI are different
   products with different infrastructure. This decision picks one, and the
   ingestion stack follows from it — not the other way round.
2. **It is the first thing a TPA will ask in a demo.** "How does it know which
   renewal that belongs to?" is the question that separates a plausible demo
   from a real system. There has to be a crisp answer.
3. **Getting it wrong is credibility-ending, not merely wrong.** A
   misattributed document means the agent chases the wrong renewal — emailing a
   broker about documents they already sent, or marking a matter complete on the
   strength of another client's census. In a regulated back office that is worse
   than doing nothing, because it is confidently wrong and it is *outbound*.

The asymmetry matters: a missed association produces a matter that looks
incomplete and gets chased — annoying, self-correcting. A **wrong** association
produces a matter that looks complete when it is not, and outbound correspondence
to the wrong party. Only one of those is recoverable by a human noticing.

## Decision

**Correlation is established at ingestion and carried as metadata. It is never
inferred from document content alone.**

Every document entering the raw bucket must arrive already associated with a
matter, by construction of how it arrived. Extracted fields may **verify** that
association; they may never **create** it.

Concretely:

- The association is written by the ingestion path (key prefix, email routing,
  or upload action), not derived by the extraction step.
- If a document arrives with no association, it goes to a **human triage queue**.
  It does not get matched by guessing.
- Content-based matching (group number, policy ID, employer name) runs as a
  **cross-check**. On mismatch the document is **flagged for review**, never
  silently reassigned.

The specific mechanism is deferred — see candidates below — but the principle is
locked, because it is what makes the failure mode recoverable: unassociated
documents are visible and get triaged; misassociated ones are not.

## Candidate mechanisms — evaluated, not yet chosen

| Mechanism | How it works | Failure mode | Implication for ingestion |
|---|---|---|---|
| **S3 key prefix convention** — `inbound/<matterId>/<file>` | Whoever writes the object encodes the matter | Only as good as the writer. Pushes the problem upstream rather than solving it — something still has to know the matter | Trivial infra. Only viable when uploads come from a system that already knows the matter |
| **Per-matter inbound email alias** — `mtr-2026-0142@…` | SES receipt rule parses the recipient → matter | Broker replies to the wrong thread, or forwards to a colleague who mails the generic address. Address sprawl over time | SES receipt rules + a routing Lambda + alias lifecycle. Fits how brokers actually work: they reply to the email that chased them |
| **Explicit operator association at upload** | A person picks the matter in a UI | Human error, and it does not scale to inbound email — which is the dominant channel for a TPA | Needs the owner view (currently "later"). Strongest accuracy, weakest coverage |
| **Extracted-field matching** — group number, policy ID, employer name + confidence threshold | Match extracted fields against matter records | **This is the one that produces confident wrong answers.** Employer names collide and abbreviate; group numbers get transcribed wrong; a renewal and a claim for the same employer are different matters | No new infra, but it inverts the dependency: extraction would drive routing |

**Leading candidate: per-matter email alias as primary, operator association as
fallback, extracted-field matching as verification only.** That combination
matches the actual workflow — the agent's `send_reminder` email *is* the thread
the document comes back on, so the association is created by the very act of
chasing. It degrades safely: no alias match → triage queue, not a guess.

Not adopted yet because it needs the SES inbound path designed, and the alias
lifecycle (creation, retirement, matters that outlive an alias) worked through.

## Measurement criteria (Step 5)

| Metric | Definition | Bar |
|---|---|---|
| **Misattribution rate** | Documents associated with the wrong matter | **Zero tolerance.** Any occurrence blocks the thin slice from being called done. |
| Unattributed rate | Documents landing in triage with no association | Reported. High is acceptable early; it is the safe failure. |
| Verification catch rate | Of documents where content-matching disagreed with the ingestion-supplied matter, how many were genuinely misfiled | Validates the cross-check is worth running |

The asymmetry is deliberate: **unattributed is a cost, misattributed is a
defect.**

## Consequences

- **The ingestion stack cannot be finalised until the mechanism is chosen.**
  `ingestion-stack.ts` is currently a bare raw bucket; SES receipt rules and key
  conventions are downstream of this decision.
- Matter state needs somewhere to record *how* an association was made and with
  what confidence — the audit trail must answer "why did you think this belonged
  here", the same standard applied to extraction in `docs/architecture.md`.
- The agent must never resolve correlation. It reads matter state; it does not
  decide which matter a document belongs to. That would put judgment back into
  the pipeline and violate the body/brain split.
- A triage queue is now a required component of the thin slice, not an
  enhancement. Without it, "no association" has nowhere to go and the pressure
  to guess returns.
- This must be settled **before** the ingestion design in Step 5, not
  discovered during it.
