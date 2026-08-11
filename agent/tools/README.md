# Agent Tools

The agent's entire capability surface. If an action is not in this list, the
agent cannot take it — which is the point. The blast radius of the model is the
tool list, not the model.

**One of the five is built.** `escalate_to_human` is live; the other four are
designed and deliberately unbuilt. The table below is the intended surface, not
the shipped one — production passes exactly one tool to the model, and the
status column is the authority on which.

What is built is exposed through **AgentCore Gateway** (`CfnGateway` +
`CfnGatewayTarget`), so the agent never holds AWS credentials directly and every
invocation is logged by Gateway rather than being trusted from inside the loop.

| Tool | What it does | AWS mapping | Status |
|---|---|---|---|
| `send_reminder` | Emails the responsible counterparty listing exactly which documents are outstanding and when they are due. | SES → Gateway target | **DEFERRED — blocked on ADR-005 SES sending domain. See reversal trigger below.** |
| `schedule_followup` | Sets a future check on this matter, so waiting is an explicit, auditable decision rather than a dropped ball. | EventBridge Scheduler → Gateway target | Not built |
| `escalate_to_human` | Hands the matter to a named internal owner with the reason attached. The human-in-the-loop path. | SNS → Gateway target | **BUILT and live** (SNS, not SES — no verified sender needed) |
| `update_matter` | Appends the action taken and its rationale to matter state. Append-only — the agent records history, it does not rewrite extracted fields. | DynamoDB `ida-<stage>-matters` → Gateway target | Not built |
| `flag_anomaly` | Marks the matter for review when something does not add up (mismatched document, low-confidence extraction, passed due date). Does not attempt a fix. | DynamoDB + SNS → Gateway target | Not built |

## Design rules

**Narrow inputs.** Each tool takes a `matterId` plus a small, typed payload. No
free-form "do this" parameter, no raw query passthrough. The agent chooses
*which* tool and *for which matter*; it does not compose arbitrary operations.

**`update_matter` is append-only.** The agent can add to the action history. It
cannot overwrite the required-document list, a received document, or an
extracted field — those belong to the fixed pipeline. A tool that let
the agent edit extracted data would collapse the body/brain separation the whole
design rests on.

**Every tool call is one row in the audit trail.** Tool, matter, inputs,
timestamp, and the agent's stated reason. This is a compliance deliverable, not
debug logging.

**Guardrails belong outside the prompt — and today only some of them are.** The
design target is AgentCore Policy (`CfnPolicy` / `CfnPolicyEngine`) holding the
reminder caps, the never-contact-an-insured rule, and the escalate-past-due-date
rule, so they survive a bypassed prompt or a misbehaving model. **That is not
built.** Cedar is deferred until `send_reminder` gives it something with a real
abuse surface to police ([ADR-006](../../docs/decisions/ADR-006-agent-architecture.md)).

What *is* structural today is narrower, and worth stating precisely rather than
rounding up: the model is passed exactly one tool, so no other action is
reachable; the scheduled sweep skips already-escalated matters **before the
model is called at all**; and per-run caps bound both matters examined and
escalations dispatched. Everything else in this section is currently the prompt
asking nicely — belt without braces. A model that behaves is evidence; only
code that cannot do otherwise is a control. The guardrail table in
[docs/step-6-agent-design.md](../../docs/step-6-agent-design.md) tracks which is
which.

## Open questions

**Still open — does `send_reminder` render the email body, or fill approved
templates with slots?** Templates are far easier to defend in an audit;
free-form generation reads better. Leaning templates-with-slots for anything
leaving the building. This gets decided when the tool is built, not before.

**Answered — idempotency.** The original guess was a per-matter action key
checked before the tool fires. What shipped for `escalate_to_human` is stricter
and is the pattern the remaining tools will follow: **record-then-act**. The
handler does a conditional put on the action row (`attribute_not_exists(SK)`)
and only performs the side effect if that write wins. A retry loses the
condition and returns without re-sending. The key is composed from the matter
and a **normalised** document type, so it is never model-authored — an
identical decision phrased differently by the model still collides on the same
key. Ordering matters: acting first and recording after leaves a delivered
email with no row to prove it happened.


## `send_reminder` — deferred, with an explicit reversal trigger (2026-08-09)

**Status: deferred, not descoped.** Production runs with a single tool
(`escalate_to_human`), so every matter whose correct action is a reminder is
currently un-actionable by the agent.

**Why it is not built yet.** `send_reminder` emails a counterparty, which needs a
verified SES sending identity — the same gap ADR-005 tracks on its domain track,
and the reason `escalate_to_human` was deliberately routed through **SNS**
instead (SNS owns delivery and needs no verified sender). Building
`send_reminder` now would force either solving the domain question under
schedule pressure, or shipping a tool that cannot actually send. Neither is
better than waiting.

**REVERSAL TRIGGER — build it when this becomes true:**

> A verified SES sending domain exists for the stage.

At that point `send_reminder` is the **next tool to add**, ahead of
`schedule_followup`, `update_matter`, and `flag_anomaly` — it is the only one
with a matter class already waiting for it.

**What must happen at the same time, or the change is incomplete:**

1. Add its `toolSpec` to `agent/core/decide.py` (production currently passes
   `[ESCALATE_TOOLSPEC]` at both Converse call sites).
2. **Delete the blocked-capability detector** in `decide.py` and its
   `blockedCapability` plumbing in `sweep.py` / the Lambda's `SWEEP_NOTABLE`
   condition. That machinery exists only to make this gap visible while it
   exists; leaving it after the fact turns a stopgap into permanent noise.
3. Reconsider the `## When the right action has no tool` section of
   `system-prompt.md`. It stays correct in principle — the tool surface may
   always be narrower than the prompt — but the `NO TOOL AVAILABLE:` contract
   should be re-tested against the widened tool set.
4. Cedar's cadence guardrails become live-relevant: `send_reminder` is the tool
   with an actual abuse surface (wrong recipient, over-cadence, reminding when it
   should escalate). See the guardrail table in `docs/step-6-agent-design.md`.
5. Re-run the ADR-001 eval. The harness already carries a schema-only
   `send_reminder`, so its S6 scenario becomes a real end-to-end case rather than
   a selection-only one.
