# Step 5 — close-out notes

**Status:** ✅ milestone met. The deterministic half of the system works end to
end on real infrastructure.
**Date:** 2026-07-25

Related: [ADR-002](decisions/ADR-002-bda-vs-textract.md) (confidence gate),
[ADR-004](decisions/ADR-004-idp-accelerator-adopt-vs-direct.md) (direct BDA),
[ADR-005](decisions/ADR-005-document-matter-correlation.md) (correlation),
[bda-orchestration-reference.md](bda-orchestration-reference.md) (the confirmed
BDA contracts).

---

## The milestone, and what it proves

A document hit S3 and became correlated, confidence-scored, human-reviewable
matter state with **zero human involvement** — and where it couldn't be trusted
(low confidence) or couldn't be placed (the orphan), it **escalated instead of
guessing.**

Final readout:

```
>>> ACTION NEEDED: 1 document(s) awaiting triage <<<
  - unassociated/orphan-census.pdf   (unresolved-at-ingestion)

MTR-2026-0142  Northwind Manufacturing  [blocked]
   [in-review] census  conf=0.2520 · [missing] signed-employer-application
MTR-2026-0157  Cedarline Logistics  [open]
   [in-review] census  conf=0.4160
MTR-2026-0163  Harbor Point Foods  [open]
   [in-review] census  conf=0.0996
```

That is the compliance thesis of the whole build, demonstrated: the deterministic
body does its job, and every point of uncertainty routes to a human.

## In-account verification (not just the readout)

Confirmed directly against DynamoDB and S3, 2026-07-25:

| Check | Result |
|---|---|
| Table item count | 9 — 3 matters (META + DOC# + one ACTION) + 1 triage row |
| `DOC#census` rows | all 3 `in-review`, `conf` 0.2520 / 0.4160 / 0.0996, `sourceKey` set |
| `DOC#signed-employer-application` | still `missing` (never uploaded) |
| Triage row | `TRIAGE` / `DOC#orphan-census.pdf`, `needs_triage`, sourceKey set |
| **GSI1 queryable** | `STATUS#in-review`→3, `STATUS#needs_triage`→1, `STATUS#missing`→1 |
| BDA output manifests | present under `bda-output/` for all 4 documents |

The GSI was built ahead of need (for the Step 6 scheduled sweep); it is
confirmed queryable now, so the sweep is a query away, not a schema change.

## The confidence finding — expected, not a bug

On the synthetic hand-built PDFs, `employer_name` extracts at **0.10–0.42**
confidence, below the 0.8 gate, so the conservative min-over-all-fields rule
(ADR-002) routes **every census to `in-review`, not `received`.**

The extraction itself is correct — every group number came through
(`GRP-88213`, `GRP-90455`, empty for the deliberately-ambiguous MTR-2026-0163,
`GRP-70011` for the orphan). BDA is simply, and correctly, unsure about a minimal
plain-text PDF. Real documents would score higher and some would land
`received`.

**Deferred to the ADR-002 measurement pass**, with realistic documents: the
threshold value, and whether to gate on *all* fields or only the identity fields
(`group_number`, `employer_name`). Both are product/policy tuning, not code
fixes, so they are not changed here.

---

## Integration reality — six first-run wrinkles

The pipeline design was sound from synth; every failure below was an integration
wrinkle with a service or the environment, not a design flaw. This is the honest
part of the build-in-public story, and several lessons are reusable.

### 1. BDA blueprint schema — `inferenceType` on an array is rejected

First deploy rolled back at `AWS::Bedrock::Blueprint`:
`Request has invalid blueprint schema` (400). Isolated by bisecting candidate
schemas against `create-blueprint` **directly** (seconds each) rather than
through CloudFormation (minutes each): `inferenceType` must appear only on leaf
fields, never on an `array` or `$ref` property. Also confirmed in passing that
the `DataAutomationProject` API **requires `standardOutputConfiguration`** even
though the CDK L1 marks it optional — which would have been a *second* rollback,
caught before it happened. **Lesson: iterate service-resource contracts against
the API, not the 2-minute CFN round-trip.** (Details:
[bda-orchestration-reference.md](bda-orchestration-reference.md) §1–2.)

### 2. The `GetDataAutomationStatus` contract — a fetch that was never needed

The mapper crashed on a real completion event calling
`get_data_automation_status(invocationArn=job_id)`: `detail.job_id` is a **bare
UUID**, but the API wants a full invocation ARN. The fix was not to build the
ARN — it was to delete the call. The completion event **already carries**
`output_s3_location`, which is the only thing that call was fetching. **Lesson:
read the real event before writing code against it; a defensively-coded guess at
an undocumented contract can invent work that the event makes unnecessary.**
(§3–4 of the BDA reference; the event and manifest shapes are now pinned.)

### 3. The confidence finding — see above

Not a failure so much as a truth the first real run surfaced: synthetic input
produces low extraction confidence, so everything routes to review. Worth
recording because it would otherwise read as "the pipeline didn't work" when in
fact the gate worked exactly as designed.

### 4. Orphaned log-group collision on failed-redeploy

With the `useCdkManagedLogGroup` feature flag, CDK creates explicit
`AWS::Logs::LogGroup` resources for the Lambdas, and their default removal policy
is **RETAIN**. When `Ida-Dev-Understanding` rolled back and was destroyed then
redeployed, the retained log groups collided with the recreate. Logged as a
deferred fix below.

### 5. Clock drift across a multi-session build

The wall clock advanced 2026-07-21 → 2026-07-25 across sessions, which showed up
in resource `CreationTime`s and in seeded due dates. Not a bug, but with a real
downstream implication for Step 6: **MTR-2026-0142's census was due 2026-07-24,
now in the past** — so the agent should treat it as *overdue* (escalate), not
merely *missing* (remind). Any due-date/SLA logic must read real current time,
and the synthetic due dates are now partly historical. Worth a refresh of the
seed dates, or deliberately keeping one overdue to exercise the escalation path.

### 6. Tooling and environment friction

Several non-design stumbles worth noting for anyone reproducing this on Windows:

- **Inline JSON to `aws` breaks in PowerShell.** `--messages '[...]'` and
  `--item '{...}'` get mangled by the shell parser; pass `file://` paramfiles
  instead. (Also why the seed/readout scripts shell out with argument arrays,
  not string interpolation.)
- **Windows path vs Git Bash path for `aws.exe` paramfiles.** `aws.exe` needs
  `file://C:\...`, not `file:///c/...`, even when invoked from Git Bash.
- **Pasting a multi-step run block as one unit** ran the seed against a
  half-deployed stack (the S3 events fired with no rule to catch them). Run
  deploy sequences step by step when a mid-sequence failure changes what the
  later steps should do.
- **Transient network/tooling blips** — an RDAP lookup timeout, a couple of
  classifier "temporarily unavailable" pauses, a `cdk synth` that exceeded a
  300s foreground window — none consequential, all worth expecting.

---

## Deferred fixes (logged so they are not lost)

### (a) CDK-managed Lambda log groups retain and collide on redeploy

**Problem:** the `useCdkManagedLogGroup` flag creates explicit log groups with a
default **RETAIN** removal policy. On any destroy/redeploy cycle in dev (e.g.
after a rollback), the retained `/aws/lambda/ida-dev-bda-*` groups collide with
the recreate.

**Fix (dev):** set the log groups' `removalPolicy` to `DESTROY` in dev — either
by constructing the `LogGroup` explicitly and passing it to the function, or by
tying its removal policy to `config.retainData` like the other stateful
resources. Keep RETAIN in prod. Not done this step to avoid touching the
Understanding stack again mid-milestone.

### (b) BDA failure-event mapper branches are unconfirmed

Only `Bedrock Data Automation Job Succeeded` fired on this run. The
`Job Failed With Client Error` / `Job Failed With Service Error` branches in the
mapper are still **defensively coded and log-only** — their `detail` shape has
not been seen. The client/service split is expected to map to
non-retryable/retryable, but that must be confirmed against a real failure event
before those branches do anything beyond logging and emitting a metric. Capture
the first real failure event and pin it like the success event was pinned.
