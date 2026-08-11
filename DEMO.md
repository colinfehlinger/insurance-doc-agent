# Demo — the Document-Chase Agent, running for real

Everything below happened on real AWS infrastructure with synthetic test data.
Every quote is a verbatim excerpt from a live DynamoDB audit record, pulled from
the table rather than transcribed from memory — the field it came from is named
each time, because a document that claims verbatim evidence should be checkable
against the source.

## What this is

A document-chase agent for group-benefits / TPA back-office work: it tracks
which documents a matter is still missing, reads what has actually happened, and
decides — on its own, on a schedule, unsupervised — whether a human needs to get
involved.

The design principle: **the pipeline is fixed and auditable; the agent owns only
the judgment.** See [architecture](docs/architecture.md).

---

## The 30-second view

This project started from one question: *can the person who owns these matters
see where everything stands in half a minute?* Everything after this section
exists to keep that view true without anyone maintaining it by hand.

Here is that readout, run just now against the live table — a real script, real
data, nothing staged:

```console
$ cd infra && npx tsx ../scripts/readout.ts

============================================================
  DOCUMENT-CHASE AGENT — MATTER READOUT (dev)
============================================================

  >>> ACTION NEEDED: 1 document(s) awaiting triage <<<
  These arrived but could not be matched to a matter. A human
  must place them (ADR-005) — they are never auto-assigned.
      - unassociated/orphan-census.pdf   (unresolved-at-ingestion)

  MTR-2026-0142  Northwind Manufacturing  [blocked]
     target close 2026-08-01 · chase Dana Whitfield
     [in-review] census  due 2026-07-25  conf=0.255859375
     [missing]   signed-employer-application  due 2026-07-30
     2 action(s) on record

  MTR-2026-0157  Cedarline Logistics  [open]
     target close 2026-08-15 · chase Sam Okafor
     [in-review] census  due 2026-08-07  conf=0.412109375
     1 action(s) on record

  MTR-2026-0163  Harbor Point Foods  [open]
     target close 2026-08-20 · chase Lee Contreras
     [in-review] census  due 2026-08-10  conf=0.099609375
     1 action(s) on record

  MTR-2026-0184  Fairmont Dental Group  [open]
     target close 2026-08-17 · chase Rosa Ibarra
     [missing]   census  due 2026-07-30
     1 action(s) on record

  MTR-2026-0209  Bellweather Print Works  [open]
     target close 2026-09-05 · chase Imani Osei
     [missing]   signed-employer-application  due 2026-08-14

============================================================
```

Read it as a map of the rest of this document:

- **The triage queue is the first thing on the page, not a footnote.** A census
  arrived that could not be matched to any matter. It sits there until a human
  places it — the pipeline never guesses an association (ADR-005).
- **`MTR-2026-0142` is `blocked` with 2 actions** — a reminder sent back in July,
  then the escalation in §1. The second run in §2 added no third action, which is
  the point of §2.
- **`MTR-2026-0157`, `0163` and `0184` each show exactly 1 action.** `0163` and
  `0184` were escalated by the **unattended sweep**, and the table holds 4 and 5
  sweep audit records against them respectively — every subsequent run re-read
  the matter, saw it was already handled, and wrote a decision record without
  taking a second action. One action row against five runs is the pre-filter and
  the idempotent write doing their jobs, visible from the outside.
- **`MTR-2026-0209` has no action line at all** — because it has no actions. Its
  only record is a single sweep audit row from this morning. That is §4: the
  matter whose correct next step is a reminder, which the agent cannot yet send.
  The gap shows up here as a matter the readout cannot say anything reassuring
  about, which is exactly the right amount of alarming.

Confidences print raw rather than rounded (`0.255859375`), because this readout
is an operator tool and rounding is a judgment the operator should make.

---

## 1. The core loop, on a real matter

A census document lands in S3. Bedrock Data Automation classifies and extracts
it, attaching a confidence score to every field. Nothing in that path decides
what to *do* — it only decides what the document *says*, and says how sure it is.

That score is what the agent reasons over. On `MTR-2026-0142` the census came
back at **0.256** confidence — far below the acceptance threshold — while the
target close date had already passed and a second required document was still
missing. From the decision record:

> **Implication:** A document is present but the pipeline cannot reliably read
> it. This is not a "received and fine" situation — it is a data-quality problem
> that a human must resolve. I must not infer, correct, or guess at the extracted
> fields.

> ### Decision: **Escalate to human**
>
> Multiple escalation triggers are simultaneously active:
> 1. Both document due dates have passed.
> 2. The target close date has passed.
> 3. The census has a critically low extraction confidence (0.26) — a data
>    quality issue I cannot resolve.
> 4. The signed employer application is still missing.
>
> Sending another reminder would be inappropriate — I cannot imply
> business-as-usual when the matter is past close with unresolved data integrity
> issues.

*(`AUDIT#2026-08-03T19:12:32…`, `reasoning` field.)*

It escalated not because a rule said "overdue → escalate," but because it weighed
four separate facts and explained the weighing. That reasoning is written to a
permanent audit record alongside the decision, before anything is dispatched.

## 2. It re-decides, rather than repeating itself

Run again against the same matter minutes later, with nothing changed, it
declines to act:

> **Nothing in the matter state has changed since that escalation.** No documents
> have been received, no human action has been recorded, and no new information
> has arrived. Taking any further automated action — sending a reminder,
> re-escalating with identical content — would add noise without value…
>
> **The correct next action is to wait.** The matter is in human hands. The
> agent's role here is complete until the human owner records an action or the
> matter state changes (e.g., a document is received, a decision is logged, or
> the owner requests a follow-up contact).

*(`MTR-2026-0142`, `AUDIT#2026-08-03T19:15:14…` — the same matter's second run.)*

That behaviour is also enforced structurally, not just relied upon: the scheduled
sweep skips already-escalated matters **before the model is called at all**. The
model getting it right is evidence; the pre-filter is the guarantee.

## 3. It's judgment, not pattern-matching

A different matter, `MTR-2026-0157`: the census **arrived**, nothing is overdue —
but its extraction confidence was **0.41**. Same tool, entirely different reason:

> Did not send a reminder to the broker, as the document has been received — the
> issue is extraction quality, not document absence.

*(`AUDIT#2026-08-03T21:07:42…`, `closingSummary` field.)*

A system pattern-matching "overdue → escalate" never produces that distinction.
This one escalated a matter that was **not** overdue, because the problem was
data it could not trust rather than a document it was waiting on.

## 4. It's honest when it can't act

Production currently wires exactly one action: escalate. When a matter genuinely
needs a *reminder* instead — `MTR-2026-0209`, a document due in four days and not
yet overdue — the agent does not force the wrong tool into the gap:

> **Urgency:** Due date is approaching but not yet passed. Four days is near
> enough to warrant immediate contact, but not so imminent that escalation is the
> first move.
>
> **Decision:** Send a first reminder to the broker… However, I do not have a
> `send_reminder` tool available in my function set. The only tool I have is
> `escalate_to_human`.
>
> **NO TOOL AVAILABLE:** this matter needs a reminder to the broker (Imani Osei)
> for the signed employer application, due 2026-08-14, and no send_reminder tool
> is available.

*(`AUDIT#2026-08-10T11:00:05…`, `reasoning` field — produced unattended by the
07:00 scheduled run.)*

It reasoned its way to the *right* action, found it lacked the tool, and said so
explicitly rather than escalating as a workaround or silently doing nothing. That
statement is detected and routed to an operational alarm, so **a capability gap
becomes visible instead of silent** — the failure mode here is latency, not a
wrong action, and latency with nobody watching is how work quietly rots.

## 5. The model was chosen on evidence

Before picking a production model I built an evaluation harness and ran four
candidates — Claude Sonnet 4.6, Claude Haiku 4.5, Amazon Nova Lite, Amazon Nova
Micro — against seven pinned scenarios, three runs each, scored by a mechanical
(non-LLM) rubric that disqualifies on a single missed escalation. No averaging: a
compliance system does not get to average away a miss.

| Model | Correct | Median latency | p90 | Verdict |
|---|---|---|---|---|
| Claude Sonnet 4.6 | **21/21** | 7,035 ms | 9,921 ms | eligible |
| Claude Haiku 4.5 | **21/21** | 3,604 ms | 4,591 ms | **selected** |
| Amazon Nova Lite | 15/21 | 1,107 ms | 1,235 ms | disqualified |
| Amazon Nova Micro | 15/21 | 1,007 ms | 1,379 ms | disqualified |

**Haiku 4.5 matched Sonnet 4.6 action-for-action at roughly half the latency**,
so it is the production model. Both Nova models failed on three counts:

- **Missed the overdue escalation** (S1) on every run — the single disqualifying
  error class, and precisely the boundary the eval was built to probe.
- **Spuriously re-escalated an already-escalated matter** (S3) on every run —
  neither reliably read the action history.
- **Nova Lite additionally asserted false statements in dispatched content**,
  including telling a broker a document was *"still upcoming"* when it was four
  days overdue, and calling another *"overdue"* when it was four days away.

That last one is why content is scored separately from tool choice: selecting the
right tool and populating it with a falsehood is not a partial success. The
decision and the failure modes that ruled out the alternatives are in
[ADR-001](docs/decisions/ADR-001-foundation-model.md).

**On which prompt these numbers come from.** The table above is the four-model
selection run (`evals/results/runs-20260804T082945.json`), made under
`promptVersion cd004f7ecc2c` — *before* the `NO TOOL AVAILABLE` instruction in §4
was added. The system prompt is the control environment, so a change to it
invalidates a comparison rather than extending it. The later two-model regression
(`runs-20260809T194623.json`, `promptVersion 9ad7255d3d5b`, the version running
today) re-ran all seven scenarios under the current prompt and returned **21/21
for both models again, with zero errors of any class** — evidence that the prompt
change did not regress action selection. Both result files are committed, so
neither number has to be taken on trust.

## 6. It runs unattended, safely

The agent runs daily with no human in the loop — see
[the sweep architecture](docs/architecture.md#the-unattended-sweep). Getting
there took real safety engineering, not a cron job:

- **A pre-filter** that structurally prevents re-escalating a handled matter, run
  *before* the cap so a backlog cannot starve the sweep.
- **Per-matter error isolation** — one failing matter cannot abort the batch, and
  its failure is written as an audit row rather than vanishing.
- **A hard-floored dry-run flag** no payload can override, plus a kill switch
  (reserved concurrency 0) tested against both hand-invoked *and* scheduled
  executions.
- **Three CloudWatch alarms**, including a dead-man's switch that watches for a
  *completion* signal — not merely that the schedule fired.
- **Caps and a valve** — bounded matters per run, bounded escalations per run.

The rollout was staged deliberately: dry-run by hand, dry-run on schedule, live
by hand, then live on schedule — each stage verified against the DynamoDB record
and the real inbox rather than a script's self-report.

That last distinction is not incidental. A verification script once reported a
successful escalation that never happened, by formatting the model's *intent* as
though it were a persisted row. The bug was caught by querying the table
directly. The resulting rule — *confirm a distributed write only by reading the
persisted artifact; a verifier must not be able to see the actor's output* — is
recorded in the design notes, and the dead-man's switch above exists in its
corrected form for the same reason: its first version watched whether the
schedule *fired*, which stayed healthy while the kill switch silently blocked
every execution.

## What's next

Three things, in the order that unblocks the most. Each is deferred on a reason,
not on effort.

- **`send_reminder`** — designed, schema-tested, waiting on exactly one thing: a
  verified SES sending domain
  ([ADR-005](docs/decisions/ADR-005-document-matter-correlation.md)). It is the
  highest-leverage change left, because a reminder is the agent's most frequent
  correct answer and today it can only say so. Shipping the tool without a
  sending identity would mean shipping something that cannot send. So it waits —
  and §4 is what waiting looks like when you refuse to paper over it.
- **Cedar policy at the Gateway** — lands with the first tool whose misuse costs
  something. One SNS publish to a named internal owner does not need a policy
  engine; an agent that emails counterparties on a cadence does. Enforcement
  arrives with the thing worth enforcing, and the Gateway is already the place it
  attaches.
- **A read-only owner view.** The 30-second readout at the top of this document
  is real, and it is a CLI. The work left is putting it in front of the person
  who owns the matters rather than the person who owns the terminal.
