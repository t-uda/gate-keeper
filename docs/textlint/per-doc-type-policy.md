# textlint per-doc-type config policy

**Issue:** #91 | **Parent umbrella:** #80

## Question

Should engineering-paper / SI-unit / domain-specific textlint rules live in one global
config (applied to every Markdown file), or only in per-doc-type configs (applied
selectively by file path or explicit `--config` switch)?

**Trade-off.** A global preset is operationally simple: one `.textlintrc.json`,
one CI command, no routing logic. The risk is false positives on dev docs that use
informal phrasing, shorthand units, or intentional repetition that would be
unacceptable in a formal paper. A per-doc-type split removes that noise but adds
config-management overhead, and only pays for itself when a genuinely paper-style
corpus exists alongside the dev corpus.

---

## Repo corpus survey

Surveyed `docs/`, `README.md`, and `tests/fixtures/semantic/targets/` as of main
(`9ae3318`).

| Category | Files found | Representative examples |
|---|---|---|
| Dev / process docs | `docs/dogfooding.md`, `docs/gh-aw.md`, `docs/mvp-readiness.md`, `docs/issue-plan.md`, `docs/backend-external.md`, `docs/changelog_*.md` | CI promotion rules, dev workflow prose |
| Design / IR docs | `docs/rule-ir.md`, `docs/llm-rubric.md`, `docs/mvp-spec.md`, `docs/backend-external.md` | Schema tables, architectural decisions |
| User-facing docs | `README.md`, `docs/example-rules.md` | Quickstart, rule examples |
| Test fixture targets | `tests/fixtures/semantic/targets/*.md` | Synthetic PR descriptions, changelogs, quickstart fragments |
| Paper-style writing | **None found** | — |

Findings:
- All Markdown is project-internal: contributor guides, architecture docs, user
  quickstarts, and LLM-rubric test fixtures.
- The fixture targets (`pr_description_strong.md`, `changelog_with_rationale.md`,
  `readme_quickstart_complete.md`, etc.) are intentionally short synthetic snippets
  used as rubric evaluation inputs, not prose documents.
- No engineering papers, academic preprints, technical reports, or SI-heavy content
  exist anywhere in the tree.
- The closest domain-specific content is the IR schema tables in `docs/rule-ir.md`,
  which use informal Markdown tables rather than paper-style citation or unit
  notation.

**Conclusion from survey:** the corpus is homogeneous — entirely dev/process/design
prose. There is no paper-style stratum that would benefit from a separate config.

---

## Decision

**Use global preset only.** This repo's corpus does not warrant per-doc-type splitting
at this time. All textlint rules should live in a single `.textlintrc.json` (to be
created in #82) and apply uniformly to every Markdown file.

Rationale:
1. No paper-style writing exists in the repo; any engineering-paper or SI-unit preset
   would fire exclusively against dev docs, producing only false positives with zero
   true-positive value.
2. The fixture targets under `tests/fixtures/semantic/targets/` are synthetic prose
   fragments, not formal documents; applying paper-style rules there would pollute the
   rubric eval corpus with spurious lint failures.
3. Operational simplicity matters: one config file, one CI invocation, no path-routing
   logic. Per-doc-type configs add maintenance overhead that is not justified by corpus
   heterogeneity today.

### Criteria for "global-worthy" rules (cross-reference #81)

A rule is suitable for the global preset when it meets **all** of the following:

- It applies to at least two of the three doc categories (dev/process, design, user-facing).
- It produces no false positives on existing corpus files (validated empirically in #83).
- It can be evaluated without document-type context (i.e. it does not assume paper
  structure, citation conventions, or domain-specific unit notation).
- Its false-positive rate on test fixtures is zero (the LLM rubric eval corpus must
  remain clean).

A rule that fails the first or third criterion should be **deferred** until a
paper-style corpus exists and a per-doc-type mechanism is adopted (see reopen criteria
below).

---

## Reopen criteria

This decision should be revisited when **any** of the following occurs:

1. A paper-style document (preprint, technical report, SI-unit-heavy spec) lands in
   the repo — suggested landing zone `docs/paper/` so the boundary is unambiguous.
2. A specific engineering-paper or SI-unit rule is evaluated in #83 and its
   false-positive rate on dev docs exceeds 20% of flagged occurrences while its
   true-positive rate on paper-style content is high — quantitative signal that a split
   would carry real value.
3. The repo corpus grows a new category (e.g. a user manual under `docs/guide/` with
   formal register) that is meaningfully distinct from current dev/design prose.

---

## What this decision does NOT settle

- **#82** — which specific global rules go into `.textlintrc.json` (package set and
  config file authoring).
- **#83** — empirical evaluation of candidate presets (e.g. `textlint-rule-preset-JTF-style`,
  engineering-paper rules) against the actual corpus to measure false-positive rates
  before any global inclusion.
- **#85** — severity mapping (error / warning / advisory) per rule.
- **#87** — CI workflow integration and the `--config` flag wiring for the GH Actions
  step.
- The adapter-side question of whether the textlint adapter passes a `--config` path or
  relies on a discovered config file — that is an implementation detail owned by #94.
