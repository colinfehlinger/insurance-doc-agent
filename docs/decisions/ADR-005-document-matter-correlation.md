# ADR-005 — How a document is associated with a matter

**Status:** **Principle decided. Two mechanisms decided — thin-slice (now) and production (later) — with a migration path between them.**
**Date:** 2026-07-22 · **Revised:** 2026-07-22 (Step 5 Phase A: SES prerequisites checked, mechanisms resolved)
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

## SES prerequisites — checked 2026-07-22, and they gate the production mechanism

| Question | Answer |
|---|---|
| Does inbound receiving need a **domain** identity? | **Yes.** Receiving requires a verified domain with a TXT record; an email-address identity cannot receive. |
| MX record required? | **Yes** — `10 inbound-smtp.us-east-1.amazonaws.com` |
| Is inbound available in us-east-1? | **Yes.** |
| Our sending status | ✅ **Production access already enabled** — `ProductionAccessEnabled: True`, `SendingEnabled: True`, `EnforcementStatus: HEALTHY`, quota 50,000/day at 14/s. **Not in the sandbox.** |
| Our identities | **`test-recipient@example.com` only** — an EMAIL_ADDRESS identity. **Cannot receive.** |
| Our inbound config | **None.** Zero receipt rule sets exist; `describe-active-receipt-rule-set` returns nothing. |

**We have no domain.** `legacy-domain.example` was deleted in the account
decommission, and it was never ours to use for this anyway.

Also relevant: *"with the exception of Amazon S3 buckets, all of the AWS
resources that you use for receiving email with SES have to be in the same AWS
Region as the SES endpoint."* Everything we have is us-east-1, so no conflict.

### What acquiring a domain would involve

1. **Register a domain** (~$12–15/yr). Route 53 Domains, or any registrar with
   DNS we control. Note the account currently has **no** Route 53 hosted zones —
   that was cleaned up too, so this is from zero.
2. **Hosted zone** for DNS (~$0.50/month).
3. **Verify the domain in SES** in us-east-1 (TXT record).
4. **Publish the MX record** → `10 inbound-smtp.us-east-1.amazonaws.com`.
5. **Create and activate a receipt rule set** — there is currently no active
   rule set at all, so this is greenfield.
6. **DKIM + SPF + DMARC** for outbound deliverability. `send_reminder` emails
   land in spam without them, which quietly breaks the product's core loop.

None of this is hard, but it is **days of DNS propagation and verification, not
minutes** — and it is unrelated to proving the thin slice works. That is the
whole argument for splitting the decision below.

### ⚠️ SES inbound must run on a SUBDOMAIN if the apex is a Workspace mailbox

`fehlingerops.com` is being set up as a Google Workspace domain (`owner@example.com`), which means its **apex MX will point at Google** (`aspmx.l.google.com`, etc.). A domain can have only one set of MX records, and **SES inbound and Workspace cannot both own the apex MX** — pointing the apex at SES would break the real mailbox.

**Resolution: run SES inbound on a dedicated subdomain**, e.g. `docs.fehlingerops.com`, with its own MX (`10 inbound-smtp.us-east-1.amazonaws.com`) and its own SES domain-identity verification. Workspace mail on the apex is untouched.

Per-matter aliases then become:

```
docs+{matterToken}@docs.fehlingerops.com
```

Upside beyond avoiding the conflict: these are **visibly system addresses**, they keep the document pipeline isolated from the real inbox, and the subdomain's DNS/verification is independent of Workspace so it can be set up without touching mail that already works.

Registration status as of 2026-07-22: the `.com` registry (Verisign RDAP) still returns **404 — not yet registered**. Workspace/Squarespace registration can lag propagation; re-check before starting the subdomain track. Squarespace will be the registrar (not Route 53), so the subdomain's SES records go either in Squarespace DNS directly, or in a Route 53 hosted zone that the apex delegates `docs.` to via an NS record.

---

## Candidate mechanisms — evaluated

| Mechanism | How it works | Failure mode | Implication for ingestion |
|---|---|---|---|
| **S3 key prefix convention** — `inbound/<matterId>/<file>` | Whoever writes the object encodes the matter | Only as good as the writer. Pushes the problem upstream rather than solving it — something still has to know the matter | Trivial infra. Only viable when uploads come from a system that already knows the matter |
| **Per-matter inbound email alias** — `mtr-2026-0142@…` | SES receipt rule parses the recipient → matter | Broker replies to the wrong thread, or forwards to a colleague who mails the generic address. Address sprawl over time | SES receipt rules + a routing Lambda + alias lifecycle. Fits how brokers actually work: they reply to the email that chased them |
| **Explicit operator association at upload** | A person picks the matter in a UI | Human error, and it does not scale to inbound email — which is the dominant channel for a TPA | Needs the owner view (currently "later"). Strongest accuracy, weakest coverage |
| **Extracted-field matching** — group number, policy ID, employer name + confidence threshold | Match extracted fields against matter records | **This is the one that produces confident wrong answers.** Employer names collide and abbreviate; group numbers get transcribed wrong; a renewal and a claim for the same employer are different matters | No new infra, but it inverts the dependency: extraction would drive routing |

---

## Two mechanisms, decided

The principle is one decision; the mechanism is two, because the thin slice and
production have genuinely different constraints. Splitting them is what lets
Step 5 proceed without waiting on DNS.

### Thin-slice mechanism (Step 5, now): **S3 key prefix convention**

```
matters/{matterId}/{documentId}-{originalFilename}
```

Written by `scripts/seed-synthetic-matters.ts` when it uploads a synthetic
document. The submit Lambda parses `matterId` from the key and carries it
forward as metadata on the BDA job and into the completion event handling.

Honors the principle exactly: the association is **established at ingestion**
(the seed script knows which matter it is creating a document for), **carried as
metadata** (the key itself, then explicit fields), and **never inferred from
content**. A key that does not match the convention → `needs_triage`.

Chosen because it is the cheapest thing that is *architecturally honest*. It
requires no domain, no DNS, no SES inbound, and no new AWS resources — and it
exercises the same code path that the production mechanism will: *something
upstream decided the matter; the pipeline trusts that and never re-derives it.*

**Its real-world weakness is also its honesty:** a key prefix is only as good as
whoever wrote the key. It pushes correlation upstream rather than solving it.
That is fine when the writer is our own seed script and unacceptable when the
writer is a broker's email client — which is exactly why it is not the
production answer.

### Production mechanism (later): **per-matter email alias, operator fallback**

An inbound alias per matter, using a **non-enumerable token**:

```
docs+{matterToken}@{our-domain}        e.g. docs+7f3a9c2e5b81@…
```

`matterToken` is a random 12–16 hex-character value stored on the matter — **not
the sequential `matterId`**. Sequential or guessable aliases can be probed:
someone who receives `MTR-2026-0142@…` can trivially try `…0143@…` and post
documents into another client's matter. The token is a capability, so it must
not be derivable from anything a counterparty can see.

Requires, per the SES check above: a registered domain, a hosted zone, SES
domain verification, an MX record, an active receipt rule set, and DKIM/SPF/DMARC.

**Why it fits the workflow:** the agent's own `send_reminder` email is sent
*from* the alias, so the broker's reply lands on it. The association is created
by the act of chasing — no human has to remember to tag anything.

#### Be realistic about the fallback rate

The alias will not catch most documents, and the design must not assume it does.
Real counterparty behaviour:

- **Forwarding.** A broker forwards our reminder to the employer's HR contact,
  who replies to the broker, who forwards the attachment on from *their* address
  — often to whatever address they have on file, not the alias.
- **New threads.** People compose a fresh email with the subject "Northwind
  census" rather than replying.
- **Different addresses.** Reply from a phone, a shared mailbox, or a colleague.
- **Portals and paper.** Some documents will arrive by upload or by scan.

**Operator association is therefore a primary path, not an edge case.** Plan the
UI, the queue, and the staffing on the assumption that a substantial share of
documents — plausibly the majority early on — arrive unassociated and need a
human to place them. A design that treats operator association as an exception
will produce a triage queue nobody has time to work, and *that* is what creates
pressure to start guessing.

### Migration path

Both mechanisms feed the same internal contract, so the migration is additive:

```
      seed script ──► matters/{matterId}/…  ──┐
                                              ├─► resolve_matter(event)
  SES inbound ──► alias token ──► matterId ──┤        │
                                              │        ▼
  operator upload ──► explicit matterId ─────┘   { matterId | NEEDS_TRIAGE,
                                                    source, confidence }
```

1. **Now:** `resolve_matter()` implements only the key-prefix branch. Everything
   else returns `NEEDS_TRIAGE`.
2. **Later:** add the alias branch and the operator branch. The key-prefix branch
   stays — it remains the mechanism for programmatic and back-fill uploads.
3. **Nothing downstream changes.** The mapper and matter-state writer only ever
   see the resolved output, never the mechanism.

Committing to `resolve_matter()` as a single named seam in Step 5 — even with
one branch implemented — is what makes step 2 additive rather than a rewrite.
**Build the seam now; fill it in later.**

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
- **The triage STATE is required in the thin slice. A triage UI is not.**
  See the scope note below — this distinction is what keeps the slice thin.
- This must be settled **before** the ingestion design in Step 5, not
  discovered during it.

---

## Scope note — triage state vs. triage UI

**Required in the thin slice:**

- An explicit `needs_triage` status. A document that cannot be resolved to a
  matter gets that status recorded — it does not silently vanish, and it is
  never guessed at.
- It is **visible in the Step 5 readout**, alongside matter status. If the
  readout only shows successfully-associated documents, the failure mode is
  invisible and the design is not actually honoured.
- A count. "3 documents awaiting triage" is enough.

**Explicitly NOT required in the thin slice:**

- A triage UI, review screen, or queue-working interface.
- An assignment or ownership model.
- Notification or escalation on triage backlog.
- Bulk association tooling.

The distinction matters because the *state* is what makes the principle real —
it is the thing that gives "no association" somewhere to go other than a guess.
The *interface* is a productivity feature for humans working the queue, and
belongs with the owner view.

Building the state is a status enum and a line in the readout. Building the UI
is a project. **Conflating them is exactly how a thin slice stops being thin.**
