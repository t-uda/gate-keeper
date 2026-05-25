# Dogfooding rules — semantic advisory pool

> Status: internal process artifact — not user-facing reference.

Seed pool of semantic rules that `gate-keeper` evaluates against its own
pull requests as advisory input. Complements
[`example-rules.md`](example-rules.md), which holds the deterministic
filesystem and GitHub examples.

## Operating contract

These rules route to `semantic_rubric` / `llm-rubric`. With no LLM
provider configured, each rule produces an `unavailable` diagnostic
carrying `provider_unconfigured` evidence. See
[`llm-rubric.md`](llm-rubric.md) for the contract details.

Promotion criteria live in [`dogfooding.md`](dogfooding.md). At time of
authoring, the three rules below are advisory only and do not meet the
trailing-10-PR criteria.

## Semantic advisory rules

- The PR description body should describe the user-visible change introduced by this PR. The description may appear anywhere in the body — for example, in a `## Summary` section, a `## What changed` bullet, or the opening sentence. Leading metadata (`Closes #N`, `Fixes #N`, `Refs #N`, badge images) is not the description. [target_kind: pr_description]
- The PR description should describe how the change was verified — for example, an explicit `## Test plan` or `## Validation` section, the test commands run, or an affirmation that the change does not require new tests with a brief reason. [target_kind: pr_description]
- Product code must not hardcode absolute filesystem locations that assume a specific user account, devcontainer user, host username, or workspace root layout. Acceptable spellings include `~`, `$HOME`, `${HOME}`, `home()` from pathlib, `platformdirs`, documented placeholders, and CI-only locations explicitly scoped to a workflow step. Test fixtures that exercise location handling are exempt when the test scope makes the intent obvious. [target_kind: pr_description]

The pool intentionally omits a commit-message rule: the dogfood workflow
([`.github/workflows/dogfooding.yml`](../.github/workflows/dogfooding.yml))
only dispatches `--artifact-kind pr_description`, so a `commit_message`
rule would short-circuit to `Status.UNSUPPORTED` on every run (see #242
for the dispatch-shape decision; the deferred commit-message dogfood
tracker lives under umbrella #63).

## Rationale per rule

The first two rules adapt the vetted "good" worked examples in
[`semantic-rules.md`](semantic-rules.md) §3.1 and §3.2. They cover two
dimensions of the benchmark fixture (#65). The third rule (env-safety)
addresses a regression class the prior pool did not cover: PRs that
land hardcoded user/container-layout paths in product code (anchor case
[#245](https://github.com/t-uda/gate-keeper/issues/245), tracking issue
[#246](https://github.com/t-uda/gate-keeper/issues/246)).

| Rule | Dimension | Source |
|---|---|---|
| PR-body describes the user-visible change (anywhere in the body) | Clarity | `semantic-rules.md §3.1` |
| PR-body describes how the change was verified | Completeness | `semantic-rules.md §3.2` |
| Product code avoids hardcoded user-specific / container-layout paths | Env-safety | #245 anchor / #246 tracking |

Each rule is target-grounded (`semantic-rules.md §1`): the model
evaluates a single observable predicate against a concrete piece of
text rather than an abstract quality such as "clear" or "good".

The v2 wording (#253) matches the real PR conventions both this repo
and `uda-lab/hermes-engineering` follow — `Closes #N` + `## Summary` +
`## Validation` template — so the predicate is satisfied by conforming
PRs instead of failing ~90% of them on the prior `first sentence` /
`how the change was tested` phrasing.

## What this document does NOT settle

- CI wiring — extending the self-gating workflow to evaluate these
  rules per-PR is out of scope for the seed pool (#72 explicitly
  excludes building new comment-posting infrastructure when none
  exists).
- Rule expansion — adding a fourth rule follows the same vetting path
  as #65 fixture entries.
