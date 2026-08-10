# Repo and account hygiene — lessons kept for their own sake

These were learned while clearing this AWS account for the Document-Chase Agent
and while keeping the account id out of this repo. The work that produced them
is not part of this project and has been removed from the history; the lessons
are general, they were each paid for once, and they are recorded here so they do
not have to be paid for again.

Every one of them is a **silent** failure: the check appears to run, reports
nothing wrong, and is inert.

---

## 1. A `.gitignore` does nothing to a file git already tracks

Ignore rules are consulted **only for untracked files**. Adding a rule for an
already-committed file is a no-op — the file keeps being tracked and keeps
being committed. `git rm --cached <file>` is the missing step.

## 2. A deeper `.gitignore` outranks a parent's file-level rule

A generated `.gitignore` in a subdirectory can force-commit a file via a
negation (`!deployed-state.json`), and that negation beats a matching
file-level rule in the root `.gitignore`.

**Excluding the directory wins instead:** git does not descend into an excluded
directory, and a file beneath one cannot be re-included by any negation below it.

## 3. Verify with `git ls-files --others --exclude-standard`, not `git check-ignore`

`git check-ignore` exits 0 on *any* matching rule — including a negation. A
deliberately un-ignored file is therefore indistinguishable from an ignored one.
It misreported a tracked file here and twice nearly hid the problem. It also
says nothing at all about already-tracked files, which is exactly trap 1.

## 4. `git remote -v` gates whether a scrub is even available

The account id had reached seven commits across three files **plus two commit
messages**, including a subject line that a content-only rewrite would have
missed. Removing it required rewriting history, which was only safe because
nothing had ever been pushed.

Had the repo been published first, the id would have been unrecoverable. So the
question *"has this ever been pushed?"* is not a detail of the cleanup — it
decides whether a clean cleanup exists at all. Check it first.

> Sequel, learned later: once a repo **is** public, a rewrite is still possible
> but it is now a force-push over history other people may already hold. That is
> a deliberate act with a blast radius, not a tidy-up.

## 5. A scrubbed placeholder inside an assertion makes the guard inert

A pre-flight script asserted it was running against the intended AWS account.
When the real account id was scrubbed from this repo and replaced with
`000000000000`, the assertion kept its shape and lost its meaning: it now
compared against an account that cannot exist, so it could never match and
never protect anything — while still appearing to run.

**Fix:** read the expected value from the environment with a hard failure on
unset (`${EXPECTED_ACCOUNT:?}`), never from a redacted literal.

**General form:** redaction can silently disarm a control that quotes the
redacted value. After any scrub, re-check every assertion that referenced what
was scrubbed.

## 6. The Resource Groups Tagging API is not an inventory

It enumerates **tagged** resources. An untagged resource is returned nowhere —
no error, no gap indicator, the whole service simply absent from the results.

A breadth-first pass built on it here reported a service as covered when it was
not, and a table survived a decommission unreviewed. The consequence was nil
because that table was empty. The failure mode it *can* produce is a
data-bearing resource surviving a decommission unreviewed.

**Fine for orientation; unsafe as the basis for a deletion.** Any inventory that
gates destruction must use per-service `list-*` / `describe-*` enumeration.

---

## The shape they share

Each of these is a control that **still reports success after it has stopped
working** — an ignore rule that never applied, a check that answers a different
question than the one asked, an assertion comparing against an impossible value,
an inventory that omits without saying so.

That is the same failure mode as the dead-man's switch in
[step-6](step-6-agent-design.md): its first version watched whether the schedule
*fired*, which stayed healthy while the kill switch silently blocked every run.

**A signal that stops firing is indistinguishable from a problem that stopped
happening.** The only defence is to test the control against a known-bad input
and confirm it fails.
