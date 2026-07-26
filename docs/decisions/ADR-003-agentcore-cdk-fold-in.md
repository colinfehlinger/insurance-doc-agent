# ADR-003 — Generated `agentcore/cdk/`: fold into `infra/`, or keep separate?

**Status:** **RESOLVED by [ADR-006](ADR-006-agent-architecture.md) (2026-07-25) — fold in.** Deferred through Steps 3–5; the "real agent needs `infra/` resources" trigger fired at Step 6.
**Date:** 2026-07-22
**Related:** [ADR-006](ADR-006-agent-architecture.md), [agent/runtime/README.md](../../agent/runtime/README.md)

---

> **Resolution (ADR-006):** fold in — but **natively**, not by integrating the
> probe's generated `cdk/`. The decisive change since this ADR: the managed
> Harness and the whole AgentCore surface (`CfnHarness`, `CfnGateway`,
> `CfnGatewayTarget`, `CfnPolicy*`, `CfnMemory`) ship as **stable L1 constructs
> in the already-pinned `aws-cdk-lib` 2.261.0**. The alpha-dependency cost that
> justified "keep separate" in this ADR no longer exists for the real agent, so
> the divergence table below is moot — the generated `cdk/` and its alpha deps
> are **retired**, not folded. See ADR-006 for the full reasoning.

---

## Context

`agentcore create` emits its own CDK app at
`agent/runtime/IdaAgentProbe/agentcore/cdk/`, and `agentcore deploy` runs it.
That gives the repo **two independent CDK toolchains**: `infra/` (hand-written,
pinned, deployed with `npx cdk deploy --all -c stage=dev`) and the generated one
(deployed with `agentcore deploy`).

The two have already drifted:

| | `infra/` | generated `cdk/` |
|---|---|---|
| `aws-cdk-lib` | **2.261.0** (pinned exact) | `^2.248.0` (range) |
| `aws-cdk` CLI | **2.1132.0** | **2.1126.0** (older) |
| `@types/node` | `^24.10.1` | `^24.10.1` — *converged 2026-07-22 by the Node 24 bump, no longer a divergence* |
| TypeScript | `^5.7` → 5.9.3 | `~5.9.3` → 5.9.3 *(these match)* |
| `moduleResolution` | `NodeNext` | **`Node`** (node10) |
| L3 constructs | none | `@aws/agentcore-cdk ^0.1.0-alpha.19` — **alpha** |
| Account id | resolved from `CDK_DEFAULT_ACCOUNT` | **hardcoded** in `aws-targets.json` (schema requires `^[0-9]{12}$`) |
| Stack naming | ours (`Ida-Dev-*`) | **CLI-enforced** `AgentCore-<project>-<target>` |

**Friction already encountered, recorded so this is evidence and not vibes:**

- **`moduleResolution: "Node"` is node10, removed in TypeScript 6/7.** It is not
  erroring today (verified: local `tsc` 5.9.3 exits 0, no `TS5101`), but it is a
  dated liability on a file we do not own.
- **Regeneration overwrites hand-edits.** Both project conventions we applied —
  the `Tags.of(app)` block and the stack-name comment — live in
  `cdk/bin/cdk.ts`, a generated file. A future `agentcore create` silently
  reverts them. They carry `PROJECT CONVENTION` comments so the loss shows up in
  a diff, which is mitigation, not a fix.
- **Stack naming is not negotiable.** Renaming to the project's `Ida-Dev-*`
  convention was implemented and rejected at synth: *"Synthesized stacks
  [Ida-Dev-AgentProbe] do not include the stack for target 'dev' (expected
  'AgentCore-IdaAgentProbe-dev')."* The CLI recomputes and asserts the name.
- **The account id must be committed or ignored.** The target schema accepts no
  token indirection, so `aws-targets.json` is gitignored with an
  `aws-targets.example.json` tracked in its place — a workaround `infra/` does
  not need.
- **An alpha dependency sits in the deploy path.** `@aws/agentcore-cdk` is
  `0.1.0-alpha.19`.

**The counterweight, and it is substantial.** `agentcore deploy` is a **CDK app
generator**, not a parallel provisioning system — it supports `--diff` and
`--dry-run`, and emits a normal CDK app depending on `aws-cdk-lib`. Folding it
into `infra/` is therefore tractable in a way it would not be if the CLI drove
Terraform or raw API calls. This is the single most important fact in this ADR:
**the door stays open.**

## Decision

**Deferred. Keep the generated `cdk/` as a separate sub-project with its own
deploy path.**

Reasons to wait rather than fold in now:

1. Step 3 is a throwaway probe. Coupling a disposable artefact into the
   permanent infrastructure is the wrong direction of dependency.
2. `@aws/agentcore-cdk` is alpha and `@aws/agentcore` has a `1.0.0-preview` in
   flight. Both will churn. Folding in now means absorbing that churn into
   `infra/`.
3. The probe shares **no resources** with `infra/` — no matter table, no CMK, no
   bucket. There is no coupling to justify the cost of merging.
4. We do not yet know what the real agent stack needs. Designing the seam before
   the requirement is premature.

## Measurement criteria — what would flip this

Not a metric so much as a set of **explicit triggers**. Revisit when **any**
fires:

| Trigger | Why it flips the decision |
|---|---|
| **The real agent needs an `infra/` resource** — the matter table ARN, the shared CMK, the raw bucket | This is the primary trigger. Cross-stack wiring across two toolchains with two `aws-cdk-lib` versions means passing ARNs via SSM parameters as strings, losing CDK's typed references and grant helpers. At that point sharing one app is cheaper than not. |
| **`node10` actually breaks** — the sub-project's TypeScript reaches 6.x and `moduleResolution: "Node"` becomes an error | Forces a hand-edit to a generated file, which regeneration then reverts. Recurring toil is the signal. |
| **A regeneration silently reverts a convention and it reaches an environment** | Proves the `PROJECT CONVENTION` comment mitigation is insufficient. |
| **`@aws/agentcore-cdk` reaches stable (1.x, non-alpha)** | Removes the strongest argument for keeping it at arm's length. Re-evaluate rather than auto-fold. |
| **A second AgentCore project appears** | Two generated CDK apps drifting independently is worse than one shared toolchain. |

If none fire by the end of the series, the correct outcome is to **leave it
separate and document why** — separateness is not a failure state, it is the
lower-coupling option.

## Consequences

- **Do not hand-edit `agent/runtime/IdaAgentProbe/agentcore/cdk/`** beyond the
  two `PROJECT CONVENTION` blocks already there. Anything added is lost on
  regeneration. If a change feels necessary, that is itself evidence for
  folding in — record it here rather than making it.
- `infra/` stays clean: it does not depend on the alpha construct library, and
  its pins are unaffected.
- Two deploy commands, permanently, until this is resolved:
  `npx cdk deploy --all -c stage=dev` and `agentcore deploy --target dev`. The
  README must keep saying so.
- The `aws-targets.json` / `.cli/deployed-state.json` gitignore workarounds stay
  in place while the sub-project does.
- Cost of deferring is near zero; cost of folding in prematurely is absorbing
  alpha-library churn into the fixed pipeline's toolchain.
