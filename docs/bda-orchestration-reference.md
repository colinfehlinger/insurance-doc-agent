# BDA orchestration — extraction from the accelerator reference implementation

**Date:** 2026-07-22
**Satisfies:** the Step 5 prerequisite task in [ADR-004](decisions/ADR-004-idp-accelerator-adopt-vs-direct.md)
**Source:** `@cdklabs/genai-idp@0.3.1`, obtained via `npm pack` and read from the tarball. **No dependency was added.** Their code is MIT-0 / Apache-2.0.

Files read:

| Path in tarball | Lines | What it holds |
|---|---|---|
| `assets/lib/idp_common_pkg/idp_common/bda/bda_service.py` | 110 | the BDA client wrapper |
| `assets/lambdas/unified/bda_invoke_function/index.py` | 168 | **the retry logic** |
| `assets/lambdas/unified/bda_completion_function/index.py` | 88 | the EventBridge completion handler |
| `assets/lib/idp_common_pkg/idp_common/utils/__init__.py` | — | `calculate_backoff` |

---

## ⚠️ Correction to ADR-004's own reasoning

ADR-004 described the accelerator's async orchestration as *"battle-tested"* and
its metric surface as *"production pain already absorbed."* Having now read it,
that was **half right, and the wrong half was assumed rather than checked**.

- The **retry logic is real and worth copying** — but it lives in the invoke
  Lambda, not in the `BdaService` class. `BdaService.invoke_data_automation_async`
  is a bare API call with no error handling whatsoever.
- The **idempotency is not there at all.** See extraction 3 below. This is a
  defect, not a pattern.
- Their **polling implementation is worse than feared** — `while True` with
  `time.sleep(10)`, no timeout, no attempt cap. Being event-driven avoids it
  entirely, which further validates ADR-004's conclusion.

Net: reading the source *raised* confidence in the decision not to adopt, and
changed what we copy.

---

## Extraction 1 — submit-side throttle / retry / backoff

**Copy this.** It is sound.

```python
MAX_RETRIES     = 7
INITIAL_BACKOFF = 2      # seconds
MAX_BACKOFF     = 300    # 5 minutes

def calculate_backoff(attempt, initial_backoff, max_backoff):
    backoff = min(max_backoff, initial_backoff * (2 ** attempt))
    jitter  = random.uniform(0, 0.1 * backoff)   # 10% jitter
    return backoff + jitter
```

Exponential, capped, with jitter — textbook and correct. Jitter matters: without
it, concurrent Lambdas retry in lockstep and re-throttle each other.

### ⚠️ Do not copy the in-Lambda sleep

They call `time.sleep(backoff)` inside the Lambda handler. With
`MAX_RETRIES = 7` and `MAX_BACKOFF = 300`, the worst case is roughly
**2+4+8+16+32+64+128 ≈ 4 minutes of billed idle time**, and a cap of 300s per
attempt means a pathological case approaches Lambda's 15-minute ceiling.

Paying Lambda duration to sleep is the wrong shape. **Our approach:** keep the
exponential-with-jitter parameters, but cap in-Lambda retries at a much shorter
budget (~30s total) and let the invoking event source redrive beyond that. For a
thin slice at one document per test run this will never trigger; the point is
not to bake in a pattern that becomes expensive under load.

---

## Extraction 2 — error classification

**Copy this list verbatim.** It is the most directly reusable artefact here —
knowing exactly which BDA error codes are worth retrying is otherwise learned
the expensive way.

```python
retryable_errors = [
    'ThrottlingException',
    'ServiceQuotaExceededException',
    'RequestLimitExceeded',
    'TooManyRequestsException',
    'InternalServerException',
]
```

Three-tier handling:

| Tier | Condition | Action |
|---|---|---|
| 1 | `ClientError` with a code in the list above | retry with backoff |
| 2 | `ClientError`, any other code | **fail immediately** — no retry |
| 3 | any other `Exception` | fail, counted separately as *unexpected* |

Tier 2 matters: retrying a validation error or an access-denied just burns time
and money before failing anyway. Failing fast on non-retryable errors is the
half people usually get wrong.

**This maps cleanly onto the EventBridge detail-types.** Submit-side
classification (above) governs the `InvokeDataAutomationAsync` call; the
completion-side split is delivered by event type:

| EventBridge detail-type | Meaning |
|---|---|
| `Bedrock Data Automation Job Succeeded` | success |
| `Bedrock Data Automation Job Failed With Client Error` | **non-retryable** — bad input, bad blueprint |
| `Bedrock Data Automation Job Failed With Service Error` | **retryable** — resubmit |

Their own status polling used the same vocabulary
(`["Success", "ServiceError", "ClientError"]`), which confirms the taxonomy is
consistent between the sync and async surfaces.

---

## Extraction 3 — idempotency: **they do not have it. Do not copy.**

This is the most valuable finding, and it is a negative one.

```python
# bda_service.py
def invoke_data_automation_async(self, input_s3_uri, blueprintArn=None):
    client_token = str(uuid.uuid4())        # <-- fresh random UUID, every call
```

`clientToken` exists precisely so a repeated request is recognised as the same
request. Generating a **new UUID on every invocation** satisfies the API's
requirement for the field while providing **zero deduplication**.

It is worse than it looks, because the retry loop calls
`bda_service.invoke_data_automation_async(...)` again on each attempt — so
**every retry generates a new token**. The exact scenario `clientToken` exists
to prevent — the request succeeded server-side but the response was lost, so the
client retries — creates a **duplicate BDA job**, duplicate cost, and duplicate
completion events.

### What we do instead

Derive the token deterministically from the work item, so a retry of the same
document produces the same token:

```
clientToken = sha256(f"{bucket}/{key}#{versionId or etag}")[:64]
```

Including the S3 `versionId` (our raw bucket is versioned) or the ETag means a
genuinely re-uploaded document gets a new token and is legitimately reprocessed,
while a retried or duplicated *event* for the same object version does not.

This also protects against duplicate S3 event delivery, which is a documented
at-least-once behaviour and a real occurrence — not a theoretical one.

---

## The metric set we will emit

Exact names from their implementation. Emitting the same names keeps ADR-004's
primary reversal trigger — *"our orchestration proves unreliable"* — measurable
from day one, and comparable to theirs if we ever do adopt.

| Metric | Unit | Emitted when |
|---|---|---|
| `BDARequestsTotal` | count | once per submit attempt sequence |
| `BDARequestsSucceeded` | count | submit accepted |
| `BDARequestsFailed` | count | submit ultimately failed |
| `BDARequestsLatency` | ms | per successful attempt |
| `BDARequestsTotalLatency` | ms | across all attempts incl. backoff |
| `BDARequestsThrottles` | count | each retryable error encountered |
| `BDARequestsRetrySuccess` | count | succeeded on attempt > 1 |
| **`BDARequestsMaxRetriesExceeded`** | count | **retry budget exhausted — the trigger metric** |
| **`BDARequestsNonRetryableErrors`** | count | **classification says stop — watch for misclassification** |
| `BDARequestsUnexpectedErrors` | count | exception outside the two `ClientError` tiers |
| `BDAJobsTotal` / `BDAJobsSucceeded` / `BDAJobsFailed` | count | completion side, from EventBridge |

The two bolded metrics are the reversal trigger in practice. A rising
`MaxRetriesExceeded` means our backoff is inadequate; a rising
`NonRetryableErrors` means either our classification is wrong or our inputs are.

---

## The EventBridge event shape

Read from their completion handler — this is the payload we will parse:

```python
detail      = event['detail']
object_key  = detail['input_s3_object']['name']
job_status  = detail['job_status']
```

So `detail` carries at least `input_s3_object.name` and `job_status`. **The
input object key comes back on the event**, which is what lets the completion
Lambda correlate the result to the originating document — and, via the ADR-005
key convention, to the matter.

---

## What we are explicitly NOT copying

| Their component | Why not |
|---|---|
| `wait_data_automation_invocation` — the poll loop | `while True` + `sleep(10)`, no timeout, no attempt cap. EventBridge makes it unnecessary. |
| Step Functions task tokens (`track_task_token`, `send_task_success/failure`) | No Step Functions in the thin slice. |
| Tracking-table writes (`PK = tasktoken#{key}`) | The ADR-004 boundary — processing status never touches matter state. |
| `uuid4()` client token | Actively wrong. See extraction 3. |
| In-Lambda `time.sleep` up to 300s | Pays Lambda duration to wait. See extraction 1. |
| The "intelligent skip" HITL-reprocessing branch | No HITL workflow yet. |

---

## Summary

Reading the source took under an hour and changed three things: it gave us a
concrete retryable-error list we would otherwise have guessed at, it supplied
tuned backoff parameters, and it surfaced an idempotency bug we would very
likely have copied had we merely imitated the shape of their code.

**Reading it was worth it, and reading it critically rather than reverently was
what made it worth it.**
