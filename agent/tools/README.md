# Agent Tools

The agent's entire capability surface. If an action is not in this list, the
agent cannot take it — which is the point. The blast radius of the model is the
tool list, not the model.

All five are exposed through **AgentCore Gateway** (`CfnGateway` +
`CfnGatewayTarget`), so the agent never holds AWS credentials directly and every
invocation is logged by Gateway rather than being trusted from inside the loop.

| Tool | What it does | AWS mapping | Status |
|---|---|---|---|
| `send_reminder` | Emails the responsible counterparty listing exactly which documents are outstanding and when they are due. | SES → Gateway target | Not built |
| `schedule_followup` | Sets a future check on this matter, so waiting is an explicit, auditable decision rather than a dropped ball. | EventBridge Scheduler → Gateway target | Not built |
| `escalate_to_human` | Hands the matter to a named internal owner with the reason attached. The human-in-the-loop path. | SES / SNS → Gateway target | Not built |
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

**Guardrails live outside the prompt.** Reminder caps, the never-contact-an-
insured rule, and the escalate-past-due-date rule are enforced by AgentCore
Policy (`CfnPolicy` / `CfnPolicyEngine`), so they hold even if the prompt is
bypassed or the model misbehaves. The system prompt states them too — belt and
braces — but the prompt is not the control.

## Open questions for the AgentCore step

- Does `send_reminder` render the email body itself, or does it pick from
  approved templates with slots? Templates are far easier to defend in an audit;
  free-form generation reads better. Leaning templates-with-slots for anything
  leaving the building.
- Idempotency: what stops a retried invocation from sending two reminders? Likely
  a per-matter action key checked in `update_matter` before `send_reminder` is
  allowed to fire.
