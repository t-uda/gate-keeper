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

- The PR description should name the user-visible change in the first sentence. [target_kind: pr_description]
- The PR description should state how the change was tested. [target_kind: pr_description]
- The commit message should explain why the change was made, not only what was changed. [target_kind: commit_message]

## Rationale per rule

The three rules above adapt the vetted "good" worked examples in
[`semantic-rules.md`](semantic-rules.md) §3.1, §3.2, and §3.6. They
cover three dimensions of the benchmark fixture (#65):

| Rule | Dimension | Source |
|---|---|---|
| First-sentence user-visible change | Clarity | `semantic-rules.md §3.1` |
| Testing method stated | Completeness | `semantic-rules.md §3.2` |
| Why-not-only-what in commit message | Justification | `semantic-rules.md §3.6` |

Each rule is target-grounded (`semantic-rules.md §1`): the model
evaluates a single observable predicate against a concrete piece of
text rather than an abstract quality such as "clear" or "good".

## What this document does NOT settle

- CI wiring — extending the self-gating workflow to evaluate these
  rules per-PR is out of scope for the seed pool (#72 explicitly
  excludes building new comment-posting infrastructure when none
  exists).
- Rule expansion — adding a fourth rule follows the same vetting path
  as #65 fixture entries.
