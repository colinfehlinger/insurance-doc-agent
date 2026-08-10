# Step 6 — the agent (the brain): design and decisions

**Design pass only. No code, no deploy.** Forks and recommendations for review.
**Date:** 2026-07-25 · build + first-deploy findings appended 2026-07-26

> **First-deploy findings** — see the section at the end of this doc. The build
> is done and synth-clean; the first `Ida-Dev-Agent` deploy attempt surfaced
> AgentCore resource **naming constraints** (now confirmed against the service
> model and fixed before redeploy). The two contracts confirmed pre-deploy
> (tool-type enum, IAM actions) held.

This is where `IdaAgentProbe` (Step 3 hello-world) becomes the real
Document-Chase Agent. The deterministic body (Steps 1–5) already produces the
matter state the agent reasons over. Step 6 adds the one part of the system
allowed to exercise judgment — and nothing else.

Related: [ADR-006](decisions/ADR-006-agent-architecture.md) (architecture — the
gating decision), [ADR-001](decisions/ADR-001-foundation-model.md) (model),
[ADR-005](decisions/ADR-005-document-matter-correlation.md),
[agent/system-prompt.md](../agent/system-prompt.md),
[agent/tools/README.md](../agent/tools/README.md).

---

## Tooling, re-confirmed (2026-07-25)

Checked against current docs and the pinned `aws-cdk-lib` 2.261.0, not memory:

| Capability | State | How it's used |
|---|---|---|
| **Managed Harness** | GA 2026-06-17 | Declares the agent as config: `model`, `systemPrompt`, `allowedTools`, `memory`, `maxIterations/maxTokens`. Powered by Strands. `CfnHarness` L1 in `aws-cdk-lib`. Default model Claude Sonnet 4.6. |
| **Gateway** | GA | Tools attach here; the agent never holds AWS creds, every call is logged. Lambda → tool via `CfnGatewayTarget` with a `ToolDefinition` (`name`, `description`, `inputSchema`, optional `outputSchema`). |
| **Policy** | GA 2026-03-03 | Cedar, default-deny, sits **inside the Gateway**, evaluates every tool call. Attaches via the gateway's `policyEngineConfiguration` (`CfnPolicyEngine` + `CfnPolicy`, `enforcementMode`). Now also supports Bedrock Guardrails. |
| **Observability** | GA | Every action traced automatically; unified view. This is the decision audit trail — the compliance deliverable. |
| **Memory** | GA | Short- and long-term, per session, `CfnMemory`. Consumption-priced. |

**The one finding that reframed the architecture:** all of the above are
**stable L1 constructs in the pinned `aws-cdk-lib` 2.261.0** — no alpha library.
That is what makes ADR-006's fold-in clean. See ADR-006.

---

## Fork 1 — architecture: fold in or stay separate → **ADR-006**

**Resolved: fold in, natively.** Build the agent in `infra/` with the stable
`CfnHarness`/`CfnGateway`/`CfnGatewayTarget`/`CfnPolicy*` L1s; **retire the probe
and its alpha sub-project** rather than migrate it. The alpha dependency that
made "stay separate" attractive in ADR-003 no longer exists for the real agent,
and resource-sharing (matter table, CMK, SES, EventBridge) is far cheaper with
typed references than with SSM-ARN-passing. Full reasoning in
[ADR-006](decisions/ADR-006-agent-architecture.md). This gates the rest.

---

## Fork 2 — probe → real agent: rebuild, not refactor

The probe was **Runtime + hand-coded Strands** (calculator/fetcher tools,
`agentcore deploy`, alpha `cdk/`). The real agent is the **managed Harness as
config** (`CfnHarness`). So this is a small **rebuild**, not a refactor — and
that is the right shape, because the Harness turns the agent into declaration:

| Probe (Step 3) | Real agent (Step 6) |
|---|---|
| Strands app in `app/IdaAgentProbe/main.py` | `CfnHarness` config |
| Default calculator/fetcher tools | the five real tools via Gateway targets |
| `DEFAULT_SYSTEM_PROMPT = "helpful assistant"` | `agent/system-prompt.md` → `systemPrompt` |
| Nova Micro (probe-only) | build on a strong model, eval down (Fork 5) |
| `agentcore deploy`, alpha `@aws/agentcore-cdk` | `cdk deploy`, stable `aws-cdk-lib` |
| Runtime + container/CodeZip | Harness, no custom container needed |

Discarded: the probe's Python app and generated `cdk/`. Kept: the toolchain
lesson (Step-3 notes) and `system-prompt.md`, which was written for exactly this.

---

## Fork 3 — the five tools → AWS mapping via Gateway

Each tool is a Lambda behind a `CfnGatewayTarget`. Narrow typed inputs
(`matterId` + a small payload), per [agent/tools/README.md](../agent/tools/README.md).

| Tool | Lambda does | Infra | New or reuse |
|---|---|---|---|
| `update_matter` | appends an `ACTION#<ts>` row (append-only; never rewrites extracted fields) | DynamoDB `ida-dev-matters` | **reuse** |
| `escalate_to_human` | appends `ACTION#escalated` + publishes to an escalation topic | **SNS topic** + DynamoDB | **new** (SNS) |
| `flag_anomaly` | marks the matter for review; publishes to the same topic | DynamoDB + SNS | reuse + new |
| `schedule_followup` | sets a future check on the matter | **EventBridge Scheduler** | **new** |
| `send_reminder` | emails the broker/employer the outstanding docs + due date | SES | reuse (**gated**, below) |

**New infra:** one SNS escalation topic (email subscription to the verified
gmail), and EventBridge Scheduler wiring for `schedule_followup`. **Reuse:** the
matter table, the CMK, SES.

### ⚠️ SES reality gates `send_reminder`

No domain is registered (`fehlingerops.com` still 404 as of 2026-07-25; ADR-005),
and the only verified SES identity is `test-recipient@example.com`. So
`send_reminder` in dev can only send **from and to that gmail** — it cannot yet
email a real broker. This is why the thin slice (Fork 6) deliberately starts with
`escalate_to_human`, whose SNS path **needs no verified SES sender at all** (SNS
handles delivery), sidestepping the gap entirely. `send_reminder` comes online in
the tool-expansion pass, gmail-only until the domain track (ADR-005) completes.

### Idempotency (carried from tools/README open questions)

A retried Harness invocation must not send two reminders or two escalations.
Recommendation: each tool writes its `ACTION#<deterministic-key>` row
conditionally (attribute_not_exists), and the side effect (SES/SNS/Scheduler)
fires only after the conditional write wins — the same "record-then-act" shape
the mapper uses. Deterministic key = `matterId + docType + action-type + period`.

---

## Fork 4 — guardrails: Cedar Policy vs prompt

The hard rules in `system-prompt.md` split by whether they must be
**non-probabilistic**:

| Rule | Home | Why |
|---|---|---|
| Never contact an insured/claimant directly | **Cedar** | Deny `send_reminder` unless recipient ∈ broker/employer set. A safety boundary must not depend on the model. |
| Reminder cadence cap (≤ N per doc) | **Cedar** | Deny `send_reminder` when the matter's reminder count ≥ cap (count as a Cedar entity attribute). |
| Overdue → must escalate, may not remind | **Cedar** | Deny `send_reminder` when the doc is past due; force the escalate path. This is the exact boundary the thin slice tests. |
| `update_matter` is append-only | **structural + Cedar** | Enforced by the tool's narrow write; Cedar can additionally deny any non-append shape. |
| Never fabricate receipt of a document | **structural** | The agent has no tool that can mark a doc `received` — only the pipeline's mapper writes that. Nothing to fabricate with. |
| Tone, phrasing, which doc to chase first, when waiting is wiser than acting | **prompt** | Genuine judgment — framing, not a boundary. |

**Recommendation: Cedar Policy is DEFERRED past the thin slice, reserved in the
design.** The slice has exactly one tool (`escalate_to_human`) with no dangerous
branch to guard — escalation is always safe. Cedar earns its place the moment
`send_reminder` exists, because that is the tool with an abuse surface (wrong
recipient, over-cadence, reminding when it should escalate). So: **thin slice =
prompt only; tool-expansion pass = add the Cedar policy set above at the
Gateway.** Stated plainly so it is a scheduled addition, not an omission.

### FUTURE TOOL SEQUENCING — interactions found before building (2026-08-10)

Design analysis for `schedule_followup`, `update_matter`, and `flag_anomaly`,
recorded before any of them was built. Nothing here is implemented; production
still carries `escalate_to_human` alone.

**Proposed shapes**, each mirroring `escalate_to_human`'s proven pattern (Lambda
behind a Gateway target, conditional write, record-then-act, `AUDIT#` row from
`decide_and_act`):

| Tool | Idempotency key | Side effect |
|---|---|---|
| `schedule_followup` | `ACTION#followup#<docType>#<followUpDate>` | none |
| `update_matter` | `ACTION#note#<sha256(note)[:12]>` | none |
| `flag_anomaly` | `ACTION#anomaly#<normalized type>` | SNS publish |

The keys differ deliberately. Escalation is once-per-doc; a **deferral is
inherently repeatable**, so keying it on doc alone would make a second deferral
impossible while keying it on a timestamp would write a fresh row on every daily
run. Date-keyed makes "same deferral, re-decided" a no-op. `update_matter` is
content-addressed for the same reason. It writes **only** `ACTION#note` rows and
never `DOC#`/`META`, which makes the append-only rule structural rather than a
promise.

#### THE INTERACTION THAT MATTERS MOST

**`schedule_followup` would likely mask the `blockedCapability` signal.** With a
deferral tool available, the remind case gains a *plausible* alternative: the
agent may defer instead of emitting `NO TOOL AVAILABLE:`. The signal stops
firing, and it looks like the gap closed when it did not — a matter needing a
**reminder** would instead be **deferred**, potentially forever, and the
observability built on 2026-08-09 would go quiet without anything failing.

This is the trap the whole remind-gap arc exists to avoid, reappearing one level
up: a signal that stops firing is indistinguishable from a problem that stopped
happening. Any plan to add `schedule_followup` must say how the remind signal
stays alive alongside it.

#### Four further interactions, each cheap to miss

1. **`schedule_followup` needs a `matter_state` change first.** The
   "let the agent re-decide" pattern works for escalation only because the
   escalate row lands in `actionHistory`. `matter_state` projects
   `{action, actor, reason}` — **the follow-up date would not be visible**, so the
   agent could not tell a live deferral from an expired one.
2. **`flag_anomaly` bypasses the email valve.** `MAX_ESCALATIONS_PER_RUN` counts
   escalations only, so an anomaly-heavy run could send unbounded email through a
   path the valve cannot see.
3. **Eval fixtures need re-litigating.** S5 (*missing, due in 14 days* →
   `none|remind`) has an obviously better answer once deferral exists, and the
   harness's two-tool config would have to widen to four or it would be testing a
   surface production no longer has.
4. **Infra cost is larger than it looks:** three Lambdas, three Gateway targets,
   three log groups each needing the DESTROY-policy fix (or the orphan-collision
   class reopens), and IAM per role.

#### Recommended sequencing

`update_matter` **first and alone** — no side effects, no valve interaction, no
signal interference, and it exercises the four-tool wiring path once. Then
`flag_anomaly` with the valve fix. Then `schedule_followup` last, only once the
remind-signal question above has an answer.

Building all three together would change the model's entire selection space in a
single deploy on a live system, immediately after the current behaviour was
proven end-to-end — discarding the baseline that makes a regression visible.

### PRINCIPLE — a test is trustworthy only once shown to fail on a known-bad input (2026-08-04)

Written down after the same mistake three times in one week. A test that has
never been observed to fail is not evidence that the thing under test works; it
is evidence that the test ran.

**Incident 1 — the scorer's self-test.** Three cases, all passing, and it shipped
five bugs. Every case happened to contain no negation and no citation of
`asOfDate`, which were precisely the two constructs the detector mishandled. The
suite confirmed what its author already believed and had no case it was capable
of failing.

**Incident 2 — the first matrix run.** The scorer reported all four models
disqualified. That result was implausible enough to investigate, and the
instrument turned out to be wrong. Had it disqualified only the Nova models —
the answer we half-expected — the same five bugs would have been recorded as
findings about the models. *A broken instrument that returns the expected answer
is undetectable.*

**Incident 3 — the ordering-fix test.** Verifying that filtering before capping
fixes throughput, the fake table's condition-parsing was broken, so `should_skip`
returned `None` for every matter and both orderings produced identical output.
The test "passed" for entirely the wrong reason; only an explicit assertion on
the expected value caught it, not reading the output, which looked plausible.

**The practice:**

1. **Every detector needs a known-positive** — an input it must flag. If it
   cannot be shown catching the failure that motivated it, it is decoration.
2. **Every fix needs the pre-fix behaviour asserted too.** The ordering test is
   trustworthy because it demonstrates *both* that the old order yields 0 of 5
   and the new order yields 5 of 5. Verifying only the new behaviour cannot
   distinguish "the fix works" from "the test is inert".
3. **Assert on values, never eyeball output.** All three incidents produced
   output that looked reasonable. Two were caught by assertions; the one caught
   by reading was caught only because the number was absurd.
4. **When a result matches expectations, that is not confirmation.** It is the
   condition under which a broken instrument is invisible. Suspicion is cheapest
   when the answer is convenient.

#### KNOWN-BAD CHECKS — commands that return convincing false negatives

A running list, because each of these produced an empty result that looked like
evidence of a real failure. **An empty result from any of these means the check
is wrong, not that the thing is broken.**

| Broken form | Symptom | Correct form |
|---|---|---|
| `--dimensions Name=ScheduleName,Value=ida-dev-sweep-daily` on `AWS/Scheduler` | `InvocationAttemptCount` returns **no datapoints, ever** | `--dimensions Name=ScheduleGroup,Value=default` |
| `aws logs filter-log-events` with no `--start-time` | returns **0 events** regardless of content | pass `--start-time` (epoch ms), e.g. `$(( ($(date +%s) - 86400) * 1000 ))` |
| Any `aws` command with a leading-slash argument in Git Bash (`/aws/lambda/...`, `/ida/dev/...`) | `ResourceNotFoundException` on resources that exist | prefix `MSYS_NO_PATHCONV=1`, or use boto3 |

**The `ScheduleName` dimension has now caused a false negative twice** — once in
the Day-2 handoff and again in the "the sweep did not run this morning"
diagnosis, where it contributed to a conclusion that was entirely wrong: the
sweep had fired exactly on schedule. AWS/Scheduler publishes
`InvocationAttemptCount` under `ScheduleGroup` only; a `ScheduleName` dimension
does not exist, and CloudWatch returns an empty series for a dimension that was
never published rather than an error. Silence from a metric query is
indistinguishable from a metric that is genuinely zero — which is precisely the
property that makes a wrong dimension so convincing.

The general rule these share: **a query against the wrong name, path, or window
fails silent rather than loud.** Before concluding that something did not happen,
confirm the check can see the thing when it *does* happen — the same
known-positive discipline the section above demands of detectors.

#### An alarm that FIRES and an alarm that NOTIFIES are two separate guarantees

Found 2026-08-07, on the first live unattended sweep. `ida-dev-sweep-notable`
transitioned `OK -> ALARM` exactly as designed — correct metric, correct
threshold, correct timing — and **no notification was ever sent**. CloudWatch's
own record was unambiguous once looked at:

```
describe-alarm-history --history-item-type Action
  Failed to execute action arn:aws:sns:...:ida-dev-sweep-ops
```

The ops topic showed `NumberOfMessagesPublished = 0`. **Cause:** the topic is
encrypted with the shared CMK, and the key policy granted only the root account.
An IAM-principal caller can publish to a CMK-encrypted topic because its ROLE
carries the key permissions; a SERVICE principal cannot — CloudWatch Alarms calls
KMS as `cloudwatch.amazonaws.com` and was denied. `ida-dev-escalations`, on the
same key, was unaffected, because the escalate Lambda publishes with its own role.
So the client-facing path worked while the ops path was dead, which is the
worst-ordered pair of those two outcomes.

**What made this reachable:** the alarm had been called "verified end-to-end"
after confirming metric → alarm state → *action configured*. That verification
stopped at the topic boundary. Configuring an action and executing one are
different facts, and only the second involves the topic's permissions at all.

| Guarantee | Proven by | NOT proven by |
|---|---|---|
| The alarm fires on the right condition | `describe-alarm-history --history-item-type StateUpdate`; metric datapoints | anything about delivery |
| The alarm actually notifies someone | `--history-item-type Action` showing **"Successfully executed action"**, plus the topic's own `NumberOfMessagesPublished` / `NumberOfNotificationsDelivered` | `StateValue`, `AlarmActions` being populated, or a `set-alarm-state` test |

Note `set-alarm-state` does exercise the action path and would have caught this —
but it was used only on the two AWS-native alarms, and the one alarm verified
"properly" with a real metric was the one whose delivery was never checked. The
more realistic-looking test covered less.

**Practice:** for any alarm whose purpose is to reach a human, assert on the
*Action* history item and the destination's own publish/deliver metrics.
`StateValue` is also useless here for a different reason — a short-period alarm
resets once its window clears, so by the time anyone looks it reads `OK` whether
or not it ever fired. History is the artifact; state is a snapshot.

### FINDING — the remind gap: safe, but invisible (2026-08-09)

Production carries **one** tool. `agent/core/decide.py` defines only
`ESCALATE_TOOLSPEC`, and both of its Converse call sites pass it alone. The
ADR-001 eval harness carries **two** (`escalate_to_human` + a schema-only
`send_reminder`), so the eval measures a three-way choice production cannot make.
**S6 — the remind scenario all four models passed — cannot occur in production.**

#### What the agent actually does, verbatim

Asked to decide a remind-shaped matter (census missing, due in 2 days, no prior
reminder) with only `escalate_to_human` available, Haiku 4.5 abstained 3/3 at
temperature 0. This is *before* any prompt change — unprompted:

> **Action:** Send an initial reminder to Marcus Bell.
>
> However, I notice that the available tools do not include a `send_reminder`
> function. The only tool available to me is `escalate_to_human`, which is
> designed for situations requiring human judgment…
>
> Since this is a routine first reminder with clear justification, and I lack the
> tool to send it directly, I should **not escalate** — escalation is for
> exceptions, not for routine work… **No action taken.** The matter requires a
> reminder… This task should be routed to the appropriate system or staff member.

**This is the good outcome.** It identifies the right action, declines to
substitute escalation for a missing capability, and reasons explicitly that
escalation is for exceptions rather than a fallback. None of the three failure
modes worth fearing occurred: no hallucinated tool, no JSON-in-prose tool call,
no spurious escalation.

#### Safe, but invisible — and invisible is the actual defect

The abstention recorded as `decision.action: "none"` — **indistinguishable from
"nothing needed doing."** The reasoning text preserved the difference; no metric,
alarm, or summary field did. `SWEEP_NOTABLE` fires on
`escalations || errors || valveTripped`, none of which applied. A matter needing
a chase produced a run that looked perfectly quiet.

So the failure is not wrong action — it is **latency plus silence**. The matter
ages untouched until it becomes overdue, at which point it converts into an
escalation the system *can* act on. Nothing incorrect happens; the work is just
late, and nobody is told.

#### Sequencing decision

1. **Visibility first (2026-08-09).** The prompt instructs the agent to prefix
   such a refusal with `NO TOOL AVAILABLE:`; `decide.py` detects it, `sweep.py`
   counts it, and the Lambda's `SWEEP_NOTABLE` condition includes it — reusing
   the already-verified ops alarm and delivery path. No new alarm, topic, or IAM.
2. **`send_reminder` deferred**, blocked on the ADR-005 SES sending-domain gap,
   with an explicit reversal trigger and completion checklist in
   `agent/tools/README.md`. Building it now would force solving the domain
   question under pressure, or ship a tool that cannot send.

Chose **adding** the prompt instruction over **trimming** the reminders section:
the observed behaviour was already correct and worth codifying rather than
leaving to luck; the guidance is needed again the moment `send_reminder` ships;
and the marker converts a fuzzy prose heuristic into a contract the code relies
on.

**The detector is a HEURISTIC, not a control**, labelled as such in code. It
reads model prose. Bias is deliberately toward false positives — a spurious ops
email costs one email, while a false negative restores exactly the silence it
exists to remove. **It is a stopgap and must be deleted when `send_reminder`
ships**; a temporary signal left in place becomes permanent noise. The fallback
patterns (the unprompted wordings above) stay regardless, because an instruction
is not a guarantee.

#### Verification — both required, because the system was live

- **Targeted, production single-tool config vs the remind case:** 3/3 — no tool
  called, `NO TOOL AVAILABLE:` emitted verbatim, detector fired via the marker.
  The detector was unit-tested against two known-positives (the marker; the real
  unprompted transcript) and two known-negatives (genuine nothing-to-do;
  already-escalated abstain) — a detector that fired on every `none` would merely
  relabel silence as noise.
- **Two-model regression, `promptVersion 9ad7255d3d5b`:** Sonnet 21/21, Haiku
  21/21, zero errors of any class. Its limit, stated plainly: the eval passes a
  **two-tool** config, so it **structurally cannot reach** the new prompt
  section. It proves no regression; only the targeted test proves the new
  behaviour.

#### What to expect on the next read, so it is not mistaken for a bug

Once a matter sits in the remind window — missing, due soon, **not yet overdue**,
not already escalated — the sweep will fire `sweep-notable` on it **every run**
until `send_reminder` exists. **That is the fix working, not a regression.**

**Correcting an expectation recorded at deploy time:** `MTR-2026-0184` was
assumed to be that matter. It is not. It was *escalated* on 2026-08-07, because
by then its census was 8 days overdue — an escalate trigger, not a remind one.
The pre-filter now skips it, so it never reaches the model and cannot produce the
signal. At deploy time **all four matters were escalated, `ELIGIBLE=0`, and the
signal was dormant** — deployed and tested, but with nothing exhibiting the gap.

#### CLOSING EVIDENCE — the chain proven end-to-end, unattended (2026-08-10)

The detector had been proven only in isolation, by direct Converse invocation.
That is the same shape as several failures in this project: units passing while
the assembled pipeline did not. So a matter was seeded into the genuine remind
window (`MTR-2026-0209`, signed employer application missing, due in 4 days, no
prior contact) via the seed script, and the deployed path was left to run
unattended.

The `2026-08-10` 07:00 ET scheduled run, `invokedBy=eventbridge-scheduler`,
`dryRun=False`, `promptVersion 9ad7255d3d5b`:

```
examined 5 | skipped 4 | processed 1 | escalations 0 | blockedCapability 1
MTR-2026-0209  BLOCKED  no tool for the required action (marker)
SWEEP_NOTABLE {"escalations": 0, "blockedCapability": 1, ...}
```

The agent's own reasoning, verbatim, reaching the remind conclusion by argument
rather than by hitting an engineered trigger:

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

Every link, with its artifact:

| Link | Evidence |
|---|---|
| Scheduled, unattended | `invokedBy=eventbridge-scheduler`, 11:00:05Z, cold start |
| Reached the model | `examined 5, skipped 4, processed 1` — pre-filter let 0209 through |
| Marker emitted | `NO TOOL AVAILABLE:` verbatim in the reasoning |
| `AUDIT#` row | `decision.action: none`, `blockedCapability.via: marker`, `gatewayCall: null` |
| No wrong action | `ACTION#escalate` on 0209 stayed **0**; `escalations: 0` |
| Token → metric | `SweepNotable = 1` at 10:56Z |
| Metric → alarm | `OK → ALARM` at 11:01:10Z |
| Alarm → delivery | `[EXECUTED] Successfully executed action …ida-dev-sweep-ops`; topic `Published 1 / Delivered 1 / Failed 0` |
| Nothing else touched | 0142/0157/0163/0184 all unchanged, zero rows dated today |

Note the alarm had already reset to `OK` by 11:16:10Z when its 5-minute window
cleared — which is why **alarm history, not current `StateValue`, is the artifact
that proves a transition happened.**

**The window is finite.** `MTR-2026-0209`'s document is due `2026-08-14`, so runs
on 08-11 through 08-14 remain in the remind window; from 08-15 it is overdue and
the matter converts to the escalate branch, at which point the signal stops and
the pre-filter takes over permanently. Re-seeding a fresh remind-window matter is
what keeps this branch observable — the branch is a *state*, not a fixture, and
it expires.

### PRINCIPLE — a probabilistic guard is not a structural guarantee (2026-08-04)

The generalizable lesson from the Step-6 → sweep arc, stated once here because it
has now come up four separate times and will come up again.

**A model behaving correctly is evidence, not a control.** The two are routinely
confused because they produce identical output right up until they don't:

| Behaviour | What we have | What it is |
|---|---|---|
| Agent abstains on an already-escalated matter | ADR-001 S3: 3/3, both eligible models | Evidence. The model *chose* well. |
| Agent cannot double-escalate | Conditional put on `ACTION#escalate#<docType>` | Control — but only as strong as its key |
| Agent respects the reminder cap | ADR-001 S7: all candidates correct | Evidence. Nothing *prevents* a fourth reminder. |
| Agent never sends in a dry run | `dispatch` must be explicitly `True` | Control. A missing key cannot dispatch. |

The distinction is invisible at one invocation a day and decisive at N per night.
A 3/3 result means "did not fail three times", which is a different claim from
"cannot fail" — and the gap between them is exactly the volume you plan to run at.

Three concrete applications, all now in the code:

1. **Pre-filter before the model, not instead of it.** The sweep skips matters
   already carrying an `ACTION#escalate` row *before* spending a model call.
   The agent would very likely have abstained; the point is that it no longer
   has to be right for the system to be safe. Structural check first, model
   second — and it saves the tokens as a side effect, not as the motivation.
2. **Idempotency keys must not be model-authored.** `ACTION#escalate#<docType>`
   was a real control whose key came from the model's phrasing, so `"census"` and
   `"census, signed-employer-application"` silently keyed different rows. The
   escalate Lambda now canonicalises `docType`, which is what turns the
   conditional put back into a guarantee rather than a usually-works.
3. **Safety switches fail safe, and default to opt-in.** The dispatch gate reads
   `cfg.get("dispatch") is True`, not falsiness on an opt-out key. The earlier
   form meant an *omitted* key fell through to dispatch — and a missing key is
   precisely what a new caller or a half-finished refactor produces. The failure
   mode of "forgot to set a flag" must be "did nothing", never "sent email".

The heuristic: **when a check protects something irreversible, ask what happens
if the model is wrong, and then what happens if the config is wrong.** If either
answer is "it acts anyway", the guard is advisory.

#### A guard that cannot be made structural, named as such (2026-08-04)

The sweep Lambda's role grants `dynamodb:PutItem` on the matter table so it can
write `AUDIT#` decision rows, including the error rows that keep a failing matter
visible. It should only ever write those. **That constraint cannot be expressed
in IAM:** DynamoDB's `dynamodb:LeadingKeys` condition scopes by *partition* key,
and there is no sort-key-prefix equivalent, so "may write `AUDIT#` rows and
nothing else" is enforced by the sweep's code and by nothing else.

By the principle above, that makes it **advisory, not structural** — the role
would permit the sweep to overwrite a `META` or `DOC#` row if a future change
told it to. Recorded here rather than left implicit, because the honest version
of a least-privilege claim includes the privilege that could not be narrowed.

What *is* structural alongside it: the role carries no `sns:Publish`, no
`lambda:InvokeFunction`, no `UpdateItem`/`DeleteItem`/`BatchWriteItem`, and no
`Scan` — asserted against the synthesized CloudFormation template by
`scripts/verify-sweep-iam.py`, which is itself self-tested against an injected
forbidden action so it is known capable of failing. The design doc and the
deployed role cannot drift apart silently.

If the `PutItem` breadth ever justifies the cost, the fix is a separate audit
table with its own role, not a cleverer IAM condition — the condition does not
exist.

### TRACKED GAP — the reminder cap is prompt-only and unvalidated (2026-08-04)

Found while authoring the ADR-001 eval's S7 scenario: `system-prompt.md` stated
the cadence rule as *"the configured number of reminders"* — **and no number
existed**, in the prompt or in matter state. The agent could not follow its own
rule. Fixtures injected `reminderCap: 3` to make S7 testable, which meant S7 was
testing the fixture rather than the prompt.

Closed halfway on 2026-08-04: the prompt now names **three** explicitly, and the
fixture injection was removed so the eval tests the real rule. Two parts remain
open, and they must be picked up when `send_reminder` is built:

1. **Three is a placeholder, not a policy.** It was chosen as a conventional
   default, not derived from any TPA's actual cadence. It needs real business
   input. Cheap to change — one word in a version-controlled prompt — but until
   someone confirms it, no compliance claim should rest on the specific number.
2. **A prompt sentence is not a control.** It makes the agent *able* to comply,
   not *unable* to violate, and this project's own principle is that guardrails
   must not live only in the system prompt. Enforcement belongs in the guardrail
   table above (Cedar: deny `send_reminder` at count ≥ cap) or, available sooner
   and without Cedar, in the `send_reminder` Lambda counting prior
   `ACTION#reminder` rows — the same record-then-act shape that already makes
   `escalate_to_human` idempotent via its conditional write. The Lambda-side
   check is the cheaper first move and does not block on the Policy pass.

Until one of those lands, the cap is advisory. The ADR-001 eval confirms models
*follow* it when told (S7, all candidates) — which is evidence about the models,
not about the system being unable to over-remind.

There is a subtlety worth surfacing: the thin slice is *testing whether the model
makes the escalate-vs-remind call correctly*. If Cedar forced that call, the test
would be meaningless. Leaving it to the model for the slice — then backstopping
it with Cedar once `send_reminder` is real — is deliberate, not lax.

---

## Fork 5 — model (ADR-001): build strong, eval down

**Sequencing (recommendation):** build the agent on a **strong** model so any
tool-calling bug is in the wiring, not the model — the Harness default is Claude
Sonnet 4.6; use it (or Haiku 4.5) while wiring. **Then** run the ADR-001 eval to
find the cheapest model that holds the bar. Picking the cheap model first would
conflate "the wiring is wrong" with "the model is weak."

**The eval needs the agent + tools built to be meaningful**, so it is a
**Step-6-tail / Step-7 task, not a prerequisite.** Design (detail in the ADR-001
update):

- **Inputs:** the synthetic matter set, expanded to cover every branch —
  send-reminder, escalate (overdue), do-nothing (not yet due), flag-anomaly
  (low confidence / mismatch), and the boundary cases.
- **Candidates:** Nova Micro, Nova Lite, Claude Haiku 4.5, Claude Sonnet — with
  **prompt caching on for all** (the system prompt is static and long, so cached
  input dominates and its ~90% discount materially changes cost/decision).
- **Metrics:** correct-tool-selection %, **guardrail adherence (100%, no
  tolerance — one breach disqualifies)**, escalation-boundary accuracy (tracked
  separately — the deciding metric), cost/decision (measured, with caching).
- **Tiering hypothesis:** cheap model for routine, escalate to a stronger model
  near the judgment boundary — validated only if the cheap model matches except
  at the boundary.

---

## Fork 6 — the thin slice: agent → one matter → one tool → Observability

**Confirmed: `escalate_to_human` on the overdue MTR-2026-0142 census.** Your lean
is right, and it is right for three reasons that happen to align:

1. **It is the correct action.** MTR-2026-0142's census is `in-review` and its
   due date is in the past → the right move is escalate, not remind. The agent
   choosing escalate is a *true* decision, not a contrived one.
2. **It exercises the ADR-001 escalate-vs-remind boundary** — the single most
   important thing to prove the brain can do, and the deciding metric of the
   model eval.
3. **It sidesteps the SES sender-identity gap** — escalation goes to an SNS
   topic, which needs no verified SES sender.

**One push-back, on the destination.** `escalate_to_human` needs somewhere to
go. Options: (a) SNS topic with an email subscription to the verified gmail; (b)
just an `ACTION#escalated` row on the matter. Recommend **(a) SNS + the action
row** — SNS makes the escalation a real, observable outbound signal (proving the
tool *does something*, not just writes state), while the action row keeps the
audit trail complete. One new SNS topic; email sub to the gmail; no SES sender
needed.

**Minimum scope that proves the brain:**

```
manual single-matter invoke (MTR-2026-0142)
  → Harness reasons over the matter's REAL state (read from ida-dev-matters)
  → selects escalate_to_human   (over send_reminder / do-nothing)
  → Gateway target Lambda: append ACTION#escalated + SNS publish
  → the decision + reasoning captured in Observability (the audit trail)
```

**Explicitly out of the slice:** the other four tools wired live; Cedar Policy;
Memory; the scheduled sweep; `send_reminder`'s SES path. Each is a named later
addition, not a gap.

**Definition of done:** invoking the agent on MTR-2026-0142 produces an
escalation (SNS message + `ACTION#escalated` row visible in the readout) and a
traced decision in Observability explaining *why* — citing the overdue date and
the in-review status. If it reminds instead of escalates, the slice fails, and
that is exactly the signal we want the test to be able to give.

---

## Fork 7 — invocation, Observability, Memory

**Invocation.** Single-matter **manual invoke** for the slice (invoke the Harness
endpoint with `{matterId}`). The **GSI-backed scheduled sweep** — already proven
queryable in Step 5 (`STATUS#missing` by due date) — is the later expansion that
makes it autonomous. Building the manual path first keeps the slice thin and
makes the agent's decision reproducible on demand.

**Observability — wired from the start, non-negotiable.** The decision audit
trail *is* the compliance story ("why did the agent do that?"). The Harness
traces every action automatically, so this is on by default; the slice's DoD
explicitly requires the reasoning to be inspectable, not just the outcome.

**Memory — defer, with reason.** The thin slice is a single stateless decision
on one matter, and the durable per-matter state the agent needs **already exists
in the matter table** — that is what it reads. AgentCore Memory
(per-matter/counterparty continuity, so a second reminder doesn't read like the
first) matters once there are multi-touch sequences over time; it adds state and
per-event cost without changing whether the brain can make a correct decision.
Defer to the multi-touch / cadence pass.

---

## Fork 8 — seed change: keep MTR-2026-0142 deliberately overdue

Per the Step-5 clock-drift finding, the census due date was `2026-07-24` and is
now in the past — which is what makes the escalate path correct. Make that
**robust**, not accidental: set the census due date **relative to now**
(e.g. `now − 2 days`) in the seed so the escalate-vs-remind boundary is *always*
exercised whenever the seed runs, and keep another matter's document due in the
**future** (`now + N days`) so the *remind* / *do-nothing* branch also exists.
This turns the clock-drift wrinkle into a permanent test fixture for both sides
of the boundary.

---

## Build-plan summary (for the build pass, after review)

1. Retire the probe: destroy `AgentCore-IdaAgentProbe-dev`, remove
   `agent/runtime/IdaAgentProbe/` (ADR-006).
2. `escalate_to_human` tool Lambda + SNS escalation topic (email sub → gmail) +
   append `ACTION#escalated`, in `infra/`.
3. `Ida-Dev-Agent` stack: real `CfnGateway` + `CfnGatewayTarget` (escalate) +
   `CfnHarness` (model, `system-prompt.md`, the one tool), replacing the stub.
   Typed references/grants to the matter table, CMK, SNS.
4. Seed change (Fork 8): relative overdue + one future-due doc.
5. Invoke on MTR-2026-0142; confirm escalation + traced reasoning; extend the
   readout to show the escalation action.
6. Deferred/next: remaining four tools, Cedar Policy set, model eval, scheduled
   sweep, Memory, `send_reminder` SES path.

**Thin-slice discipline check:** one new tool, one new SNS topic, one Harness,
one Gateway, one manual invoke. No Policy, no Memory, no sweep, no multi-tool
wiring. Everything else is named and deferred.

---

## First-deploy findings (2026-07-26)

The first `Ida-Dev-Agent` deploy failed at **early validation with no resources
created** (clean fail — the account's v30 bootstrap upgrade had cleared the
earlier version block). Every failure was an **AgentCore resource naming
constraint**, not a logic error. Confirmed against the bundled
`bedrock-agentcore-control` service model (`service-2.json`, authoritative — the
same min/max/pattern the API enforces) and fixed before redeploy, so the whole
class is closed in one pass rather than one rollback at a time.

### The constraints, from the service model

| Field | Pattern / limit | Underscores? | Hyphens? |
|---|---|---|---|
| `GatewayName` | `([0-9a-zA-Z][-]?){1,100}` | ❌ no | ✅ yes |
| `TargetName` | `([0-9a-zA-Z][-]?){1,100}` | ❌ no | ✅ yes |
| `GatewayDescription` / `TargetDescription` | `min 1, max 200` | — | — |
| `HarnessName` | `[a-zA-Z][a-zA-Z0-9_]{0,39}` | ✅ yes | ❌ no |
| `ToolDefinition.name` (agent-visible tool) | `String`, unconstrained | ✅ yes | ✅ yes |

### The trap: Gateway/Target and Harness have OPPOSITE rules

The gateway family (`GatewayName`, `TargetName`) **forbids underscores, allows
hyphens**. The harness (`HarnessName`, and by extension harness-side tool config
names) **forbids hyphens, allows underscores**. So they need *different* naming
conventions, not a shared one — a single project-wide convention would violate
one of them. Fixes applied:

- `TargetName` `escalate_to_human` → **`escalate-to-human`** (hyphens)
- `TargetDescription` 224 → **≤200 chars** (trimmed)
- `HarnessName` `ida-dev-doc-chase-agent` → **`ida_dev_doc_chase_agent`** (underscores, 23 chars)
- HarnessTool config `name` `gateway-tools` → **`gateway_tools`** (proactive — same harness no-hyphen rule; not flagged by the failed deploy but the same class)

### The name the agent invokes is unchanged

Only *resource* names changed. The tool the agent actually calls is the
`ToolDefinition.name` — unconstrained, so it stays **`escalate_to_human`**, and
the Harness `allowedTools: ['escalate_to_human']` matches it. Verified in the
synthesized template: the agent-visible tool name is `escalate_to_human`.

### Method note

Rather than fix the three the deploy reported and risk a fourth, every AgentCore
`name`/`description` in the stack was validated against the service-model
patterns programmatically at synth time. All pass. This is the same
"confirm the contract against the authoritative source, not the rollback"
discipline used for the Step-5 BDA blueprint schema and the tool-type enum —
applied to a whole constraint class at once.

### Still an honest post-deploy unknown

How the Gateway namespaces the tool name it exposes to the agent (bare
`escalate_to_human`, or a `<target>___<tool>` composite). If it composes,
`allowedTools` may need the composite form. `allowedTools` is optional, so the
fallback is to drop it (the gateway has one tool anyway). Confirmed on the first
successful invoke, not guessed now.

---

## First-run findings (2026-07-27) — getting the agent to actually act

The build synthesized clean and deployed, but the agent did not act until four
distinct problems were peeled back, each hidden behind the previous one. The
decision leg (correct reasoning) worked from the start; the **execution leg** did
not. Verifying against the **DynamoDB row** — not the invoke's output — is what
exposed each one. These are the reusable lessons.

### The headline lesson: the managed Harness has an implicit base-permission contract

**Symptom (the "silent empty-toolConfig" failure):** the agent reasons correctly,
returns `stopReason: end_turn` with **no tool call**, the tool Lambda has **zero
invocations**, and nothing errors. It looks like the model "chose" not to act.

**Cause:** the managed Harness authenticates to the Gateway — to *discover and
call* its tools — using a **Workload Identity token** (`bedrock-agentcore:GetWorkloadAccessToken`).
The Harness execution role, hand-built in CDK, was missing that permission (and
the rest of the base contract: X-Ray, logs, metrics, ECR-public). With no
workload token, tool discovery **fails silently**, the Harness builds the
ConverseStream request with an **empty `toolConfig`**, and the model has no tool
to call. Everything downstream is a consequence.

**Why it bites CDK specifically:** the AgentCore **CLI auto-provisions** this base
role; a **self-built role does not get it**. The AWS docs say so explicitly
(harness-security.html: *"The AgentCore CLI creates a role with these permissions
automatically… The policy above is for cases where you create the role
yourself."*). This is the exact seam a "build it natively in CDK" decision
(ADR-006) lands on, and it is invisible until you notice the tool never runs.

**The base contract (least-privilege scoped in `agent-stack.ts`):**
`GetWorkloadAccessToken`/`…ForJWT` on the workload-identity resources (the tool
fix), `xray:PutTraceSegments…` (the missing-trace fix), `logs:*` on
`/aws/bedrock-agentcore/runtimes/*`, `cloudwatch:PutMetricData` in the
`bedrock-agentcore` namespace, and `ecr-public:GetAuthorizationToken` +
`sts:GetServiceBearerToken`.

**Layer confirmed before fixing (ADR-006 held):** the AWS "Connect to tools" doc
states the managed Harness *auto-injects* gateway tools (*"Reference a gateway
ARN and every tool configured on that gateway becomes available"*). So this was a
config/permission bug, **not** an architecture where the client must supply
tools — the client stays thin, the managed orchestration stays intact.

### The other three, and the meta-lesson

1. **Auto-provisioned memory needed permissions too.** Leaving the harness
   `memory` unset made it auto-create a session-memory resource whose events the
   role couldn't read (`ListEvents` AccessDenied), breaking the loop at start-up.
   Fixed by `memory: { disabled: {} }` — which also honored the design's
   defer-Memory decision.
2. **A fabricated model id.** `anthropic.claude-sonnet-4-6-20260514-v1:0` was
   guessed and invalid; Sonnet 4.6 has no date suffix and is inference-profile-
   only (`us.anthropic.claude-sonnet-4-6`). Should have been confirmed against
   Bedrock, as the Step-3 probe model was.
3. **AgentCore resource naming constraints** (GatewayTarget forbids underscores,
   Harness forbids hyphens, description ≤200) — caught pre-deploy against the
   service model.

**The meta-lesson — metrics lie, rows don't.** Multiple times an invoke printed
`TOOL CALLED` (an *emitted* tool-use block) and a CloudWatch datapoint appeared,
while the DynamoDB row — the thing the tool actually writes — was absent. The
Lambda **log stream** (created synchronously on first execution, lag-free) and
the **matter row** were the only reliable artifacts. A verification bar of
"the row exists AND the email arrived," never "toolConfig is present now" or
"should work," is what kept four hollow passes from being recorded as a milestone.
The verification path queries the row after invoking, so its own output can't
repeat the false pass — the discipline carried into `scripts/agent-loop.py`
(`invoke-agent.py`, which first carried it, was retired with the Harness; ADR-007).

## Ghost-reading reconciliation (2026-07-30) — the reusable lesson

A long tail of "the row is there / no it isn't" readings turned out to have
**two mundane causes**, and every exotic theory we reached for was disproven by a
direct check. Recording it because the disproven theories are the seductive ones.

**What actually happened:**

1. **A verification script that fabricated a row-shaped confirmation.** An earlier
   `invoke-agent.py` built its `>>> EXECUTION CONFIRMED` block out of the agent's
   **emitted tool-use arguments** (the model's *intent* — `status`, `notified`, an
   invented `escalationId`), formatted to look like a persisted DynamoDB item. It
   printed a convincing "row" for a write that never happened. The tell was the
   **schema**: those fields are not what this account's escalate Lambda writes
   (`action` / `actor` / `reason` / `docType` / `escalatedAt`, no `escalationId`),
   so a "row" carrying them could not have come from the table.
2. **CloudWatch metric lag.** Emitted-tool-use metrics and datapoints appeared
   while the persisted artifact did not, and lag made the two look inconsistent
   over minutes.

**What it was NOT** — each theorized, each killed by a direct check:

- **NOT two accounts.** Confirmed against the machine: one credential source,
  `[default]` → 000000000000; the `legacy-profile` profile's keys are dead
  (`InvalidClientTokenId`), so nothing could have run there. No stray profile.
- **NOT a DynamoDB TTL expiring rows.** `describe-time-to-live` → `DISABLED`;
  the Lambda writes no expiry attribute. Rows were never disappearing.
- **NOT a read-consistency race.** The reads were already strongly consistent;
  they returned `Count 0` because the tool had **never executed here** — the
  escalate Lambda's log group had **zero streams**, the lag-free proof.

**The lesson (reusable):** *confirm a distributed write only by reading the
persisted artifact with a strongly-consistent read — never by the tool's
self-report, the emitted tool-use block, or the actor's stated intent.* A
verification path must not be able to see the actor's output at all; if it can,
it will eventually echo intent as fact. The client loop's `confirm()` in
`scripts/agent-loop.py` enforces this structurally: it takes only a `matter_id`,
does a strongly-consistent read, and counts a row only if it carries this
account's Lambda schema — there is no code path from the model's output to the
confirmation. (`invoke-agent.py`, which originally carried `confirm_execution()`,
was retired with the managed Harness; see ADR-007.)

(The genuine open defect underneath all the noise was never a read problem — the
model request simply carried no tools. The first theory here — an empty toolConfig
*cached* at a pre-fix deploy, to be fixed by the workload-identity role grant and
a clean redeploy — was itself **disproven**: with the role correct and the harness
freshly created, the request *still* carried no `toolConfig`, for the configured
gateway tool, an inline tool, and even the built-in tools. The managed Harness
runtime does not inject tools for this version; resolution was to retire the
Harness and orchestrate client-side over the Gateway. Full evidence — the
three-invocation table read from the model-invocation logs — and the decision are
in ADR-007.)
