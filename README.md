# Document-Chase Agent

An AI agent for group-benefits and TPA back offices. For each **matter** — a
group renewal, a claim, an onboarding, a closing — it tracks which documents are
still missing, chases them, and gives the owner a 30-second status view, with
compliance-grade data handling throughout.

Built in the open. Each step is a working, deployable increment.

## The design principle

**The pipeline is a fixed, auditable code path. The agent owns only the judgment.**

Ingestion, classification, and extraction run as a fixed code path with
confidence-scored extraction and mandatory human review below threshold.
**There is no agentic loop in extraction** — nothing in that path chooses what
to do next; it only decides what a document *says*, and attaches a number saying
how sure it is. "How did you get this field?" is answered with the source
document, the extraction confidence, whether it crossed the review threshold,
and who approved it if it did not. Not "the model decided."

The agent receives state that has already been established and answers exactly
one question: *what should happen next on this matter?* It can remind, wait,
escalate, flag, or record. Those five tools are its entire capability surface,
so its blast radius is the tool list rather than the model.

See [docs/architecture.md](docs/architecture.md) for the full picture.

## Status

**Steps 1–3 complete.** The pipeline skeleton is deployed to dev, and the
AgentCore toolchain is proven end to end — a Runtime deployed with
`agentcore deploy` and successfully invoked.

| Stack | Contains | Status |
|---|---|---|
| `Ida-Dev-Shared` | KMS CMK, rotation on, alias `alias/ida-dev-data` | Real |
| `Ida-Dev-State` | DynamoDB `ida-dev-matters`, PK `matterId`, on-demand, CMK, PITR | Real |
| `Ida-Dev-Ingestion` | S3 raw bucket — CMK, versioned, TLS-only, all public access blocked | Real |
| `Ida-Dev-Understanding` | SSM placeholder `/ida/dev/bda/project-arn` | Stub |
| `Ida-Dev-Agent` | SSM placeholder `/ida/dev/agent/runtime-arn` | Stub |
| `ViewStack` | Defined in [infra/lib/view-stack.ts](infra/lib/view-stack.ts), not instantiated | Later |

All five are `CREATE_COMPLETE` in dev (us-east-1). Deployed with
`npx cdk deploy --all -c stage=dev`; no bootstrap was needed — the account was
already at CDK bootstrap v29.

**Agent runtime probe (Step 3).** A separate stack,
`AgentCore-IdaAgentProbe-dev`, deployed by `agentcore deploy` rather than by
`infra/`. Runtime status **READY**, invoked successfully. It shares no resources
with the `Ida-Dev-*` stacks and does not collide with them or with the unrelated
legacy workload in the same account. Kept deployed for Step 6 — AgentCore
Runtime is consumption-billed with **no idle charge**. See
[agent/runtime/README.md](agent/runtime/README.md).

## Layout

```
infra/          CDK (TypeScript) — the whole system
  bin/app.ts    entrypoint; stage from context, region pinned per stage
  lib/          config + one file per stack
agent/          system prompt and tool definitions
  system-prompt.md
  tools/        the five tools and their AWS mappings
web/            owner dashboard (later)
docs/           architecture
scripts/        deploy, synthetic data seeder
```

## Prerequisites

- **Node 24 LTS** (`infra/.nvmrc` pins 24; tested on 24.18.0, npm 11.16.0)

  > **Why the bump off Node 20.** The AWS SDK for JavaScript v3 requires
  > **Node ≥22 for versions published after the first week of January 2027**, and
  > the deprecation warning fired on every `cdk` and `agentcore` invocation. Node
  > 20 reaches end-of-life around the same date. Moved now rather than under
  > deadline mid-series.
  >
  > **Why 24 rather than 22.** `winget install OpenJS.NodeJS.LTS` installs
  > **24** — Node 24 is the current LTS line and 22 has moved to maintenance. 24
  > satisfies the ≥22 requirement with a longer support runway, so there was no
  > reason to pin backwards. It also happens to align `@types/node` with the
  > generated `agentcore/cdk/`, which was already on `^24`.
  >
  > ⚠️ **`.nvmrc` is inert on Windows — it is documentation, not enforcement.**
  > No version manager is installed here; Node comes from the plain
  > `C:\Program Files\nodejs` MSI. Note that **nvm-windows would not fix this**:
  > unlike nvm for bash, it has no automatic `.nvmrc` read — that needs a
  > hand-written shell hook. The file earns its keep for CI and for anyone
  > cloning on another machine, not for local switching.
  >
  > If real enforcement is ever wanted, **fnm** is the option that actually does
  > it: `winget install Schniz.fnm`, then enable `--use-on-cd` in the shell
  > profile so entering the directory switches versions automatically.
- AWS CLI v2, authenticated
- An AWS account you can bootstrap

For the agent runtime (Step 3+):

- Python 3.10+ (tested on 3.11.9)
- **`uv`** (tested on 0.11.30) — `pip install uv`
- AgentCore CLI, pinned: `npm install -g @aws/agentcore@0.24.1`

> ⚠️ **`uv` is a hard requirement, not optional.** The upstream
> `aws/agentcore-cli` README lists it as *"optional, for Python agent support"* —
> that is wrong. `agentcore create` aborts outright without it:
> `'uv' is required for Python projects`. Install it before Step 3.
>
> ⚠️ **Pin the AgentCore CLI to `0.24.1`** — not `@latest`, not `@preview`. npm
> carries a `preview` dist-tag at `1.0.0-preview.22`; a floating install can
> regenerate the agent project against a different config schema between
> sessions and break a deploy that worked yesterday. See
> [agent/runtime/README.md](agent/runtime/README.md).

## Deploy

```bash
cd infra
npm install
npx cdk synth -c stage=dev        # no AWS calls, safe to run anytime
```

First deploy — bootstraps the account, then deploys everything:

```bash
./scripts/deploy.sh dev
# or with a named profile:
AWS_PROFILE=my-profile ./scripts/deploy.sh dev
```

Equivalent by hand:

```bash
cd infra
npx cdk bootstrap                 # first run only
npx cdk deploy --all -c stage=dev
```

Tear down (dev only — everything is destroyable by design):

```bash
cd infra && npx cdk destroy --all -c stage=dev
```

## Skeleton decisions worth knowing

**Stages are config, not copies.** [infra/lib/config.ts](infra/lib/config.ts)
defines `dev` and `prod`. Only `dev` is instantiated. The meaningful difference
is `retainData`: prod keeps stateful resources on stack deletion, dev destroys
them. Adding prod later is one line in `bin/app.ts`, not a refactor.

**No account id in the repo.** The account resolves from
`CDK_DEFAULT_ACCOUNT` at synth time. The S3 bucket name interpolates it
(`ida-dev-raw-<account>`) for global uniqueness without committing it.

**Cross-stack values are passed as props, not looked up by name.** The KMS key,
bucket, and table are constructor arguments. With
`"@aws-cdk/core:defaultCrossStackReferences": "weak"` set in `cdk.json`, these
synthesize as `Fn::GetStackOutput` resolved by the CDK CLI at deploy time rather
than as hard CloudFormation `Export`/`ImportValue` pairs. On a greenfield
project whose stack boundaries are still moving, that avoids the "export cannot
be deleted as it is in use" deadlock. (This is now the default in a fresh
CDK 2.261.0 app, not a custom setting.)

**One CMK per stage, not one per service.** Simpler to audit, rotate, and revoke
— and revoking the key is the offboarding story.

**`cdk.context.json` is gitignored.** This repo is public, and cached CDK context
routinely contains account ids, AZ lists, and AMI ids. There are no `fromLookup`
calls today, so the file holds nothing worth keeping. ⚠️ **Revisit this in the
IDP step** — if that step introduces context lookups, the choice becomes: commit
a sanitized version (deterministic synth for anyone cloning, but account details
land in git history), or keep ignoring it and accept that fresh clones re-resolve
lookups against whatever account they are pointed at. Do not let it default
silently; an un-ignored `cdk.context.json` is a common way account ids leak into
a public repo.

**Deliberately deferred**, each tied to the step that needs it: DynamoDB
single-table design with a sort key and a missing-docs-by-due-date GSI
(recreates the table, fine in dev); VPC + PrivateLink; SES receipt rules and
EventBridge triggers; the agent container image build pipeline.

## Versions

Pinned and verified against npm on 2026-07-21:

| Package | Version | Note |
|---|---|---|
| `aws-cdk-lib` | 2.261.0 | current latest |
| `aws-cdk` (CLI) | 2.1132.0 | current latest |
| `constructs` | ^10.7.1 | |
| `typescript` | ^5.7 | resolves 5.9.3 — **not** 7.x, see below |
| `tsx` | ^4.23.1 | runner, matches the current `cdk init` template |
| `@types/node` | ^24.10.1 | matched to the Node 24 runtime (resolves 24.13.3) |

**The app command is `npx tsc && npx tsx bin/app.ts`.** `tsx` transpiles without
type-checking, so the `tsc` half is not decoration — it is the type gate. Drop it
and type errors sail straight through into a synth. Verified by breaking a type
on purpose: synth fails with exit 1 before any template is written.

**`@types/node` moved in lockstep with the runtime.** Leaving it at `^20` would
have type-checked against a Node 20 stdlib on a Node 24 runtime, making newer
APIs appear not to exist. Bumped to `^24.10.1` *after* the Node install was
confirmed, so one `npm install` picked up matching types. No `engines` field is
declared anywhere in the repo, so nothing else needed to move with it.

**TypeScript is pinned to `^5.7` (resolves 5.9.3) on purpose.** Two separate
things push toward 7.x and both are declined for now: `latest` on npm is 7.x, and
a fresh `cdk init` on CLI 2.1132.0 now generates `"typescript": "~7.0.2"`. TS 7
is GA, but its stable compiler API does not land until 7.1, so the surrounding
tooling ecosystem still lags. TS 7 is a separate gated migration, not part of
this build.

## Roadmap

1. ~~Repo scaffold + CDK skeleton~~ ✅
2. ~~Prereqs and CDK app — confirm the full toolchain, stubs deploy cleanly~~ ✅
   — all five stacks `CREATE_COMPLETE` in dev, verified live in-account
3. ~~Prove the AgentCore toolchain — minimal Runtime, invoked once~~ ✅

   Runtime deployed via `agentcore deploy` (**not** `infra/`) and invoked
   successfully; status **READY**. Model is **Amazon Nova Micro, probe-only** —
   the scaffold's Claude Sonnet 4.5 default is gated behind an access form in
   this account, and the probe tests the toolchain rather than model quality.
   The production model is deliberately undecided; see
   [ADR-001](docs/decisions/ADR-001-foundation-model.md).

   > ⚠️ **The container-image ordering gotcha has not been solved — it has been
   > sidestepped.** `CodeZip` builds upload a zip, so there is no image and no
   > ordering problem. A `Container` build reintroduces it: a `CfnRuntime` will
   > not resolve unless a valid image already exists at the referenced tag,
   > which forces CodeBuild → wait → runtime. **Expect this back when the real
   > agent needs a container** (custom system dependencies, a non-Python
   > runtime, or anything CodeZip cannot package).

4. IDP Accelerator triage — reuse `@cdklabs/genai-idp` +
   `@cdklabs/genai-idp-bda-processor` (Pattern 1 = BDA) for ingestion and
   extraction; the matter-state model and the agent stay hand-built
5. The thin slice — synthetic document lands in S3 → BDA classifies → matter
   updates in DynamoDB → agent decides and calls `send_reminder` once → basic
   readout
