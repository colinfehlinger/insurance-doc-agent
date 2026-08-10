# Eval result files

Raw output from the ADR-001 model evaluation. Each file is the evidence behind a
claim made in [DEMO.md](../../DEMO.md) and
[ADR-001](../../docs/decisions/ADR-001-foundation-model.md), committed so those
claims can be checked rather than taken on trust.

| File | promptVersion | Models | What it establishes |
|---|---|---|---|
| `runs-20260804T082945` | `cd004f7ecc2c` | all four | **The model selection.** Sonnet and Haiku 21/21; both Nova models disqualified. |
| `runs-20260809T194623` | `9ad7255d3d5b` | Sonnet, Haiku | **No regression** from the later prompt change — 21/21 again under the prompt running today. |

Two runs from the scorer-debugging sequence are deliberately **not** committed.
Their scorer had five defects (documented in `docs/step-6-agent-design.md`), and a
result file whose stored classifications contradict the current scorer is a trap
for whoever reads it later.

## A note on `harnessCommit`

Each file's `meta.harnessCommit` records the scorer revision that produced it.
**Those SHAs no longer resolve.** The repository history was rewritten with
`git filter-repo` before publication, to scrub an AWS account id, two personal
email addresses, and an access key id from every commit — which necessarily
rewrote every SHA in the repo.

The values are kept as recorded rather than edited, because the alternative is
altering evidence after the fact. What they still convey is *which* scorer
revision produced a run and that two runs used different ones; what they no
longer support is `git show`. `promptVersion` and `scenariosSha` are content
hashes and remain fully verifiable — `promptVersion` is the sha256 prefix of
`agent/system-prompt.md`, so the current file still hashes to `9ad7255d3d5b`.
