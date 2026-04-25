# Task

Build `gate-keeper`, a small rule compiler that turns natural-language rule
documents into machine-verifiable checks for local files and GitHub pull
requests.

# Goal

In three development days, produce a working CLI that can:

- parse Markdown rule documents into a structured rule IR;
- validate local filesystem/text artifacts deterministically;
- validate common GitHub PR merge-gate rules through `gh`;
- fail closed when required evidence is unavailable;
- produce compiler-style diagnostics suitable for human review or CI logs;
- document how `gh aw` workflows can call or complement the CLI without making
  `gate-keeper` depend on `gh aw`.

# Planning Authority

Use this precedence order for MVP implementation:

1. explicit user instruction in the current task;
2. `AGENTS.md` repository guidance;
3. this MVP spec;
4. the active GitHub issue body;
5. `docs/issue-plan.md` and README.

When artifacts conflict, update the lower-precedence artifact instead of
inventing behavior during implementation.

# Scope

## In

- `uv` Python package and CLI named `gate-keeper`.
- Markdown rule parser for headings, bullets, checklists, and requirement words.
- Rule IR with stable JSON output.
- Backend routing: `filesystem`, `github`, `llm-rubric`.
- Filesystem/text backend for existence, naming, required text, forbidden text,
  checked/unchecked task boxes, and simple structured Markdown checks.
- GitHub backend for PR state, draft state, status checks, blocking labels,
  unchecked PR body tasks, unresolved review threads, and independent review.
- LLM rubric backend interface with explicit "not configured" fail-closed
  behavior for the MVP.
- CLI commands: `compile`, `validate`, and `explain`.
- Fixtures and smoke tests for local and GitHub command construction.
- Documentation for local CLI use, CI use, and `gh aw` adjacency.

## Out

- A hosted service.
- Full natural-language understanding across arbitrary policies.
- Direct writes to GitHub objects.
- Replacing branch protection, required checks, or human review.
- Deep integration with GitHub Agentic Workflows internals.
- Multi-repo orchestration or queue management.

# Constraints

- Keep the core package usable without network access.
- Use deterministic backends whenever enough structured context exists.
- Treat unavailable evidence as a failed or unknown result, not as a pass.
- Keep GitHub support behind a backend boundary that shells out to `gh`.
- Keep `gh aw` integration as documented composition: workflows may call the CLI,
  consume its output, or use it as a preflight step.
- Do not require an LLM provider for the MVP to pass local deterministic checks.

# MVP CLI Contract

For the MVP, commands consume Markdown rule documents directly. Compiled JSON as
an input format is out of scope.

```bash
gate-keeper compile DOCUMENT --format json
gate-keeper validate DOCUMENT --target TARGET --backend auto --format text
gate-keeper validate DOCUMENT --target TARGET --backend auto --format json
gate-keeper explain DOCUMENT --format text
```

Exit codes:

- `0`: all evaluated rules pass.
- `1`: at least one rule fails, is unavailable, unsupported, or indeterminate.
- `2`: CLI usage error or unreadable input document.

`DOCUMENT` is a Markdown file. `TARGET` is a local path for filesystem rules or a
GitHub PR target for GitHub rules. MVP GitHub target formats are PR URLs and
`OWNER/REPO#NUMBER`.

# MVP Data Contract

Required backends:

- `filesystem`
- `github`
- `llm-rubric`

Required diagnostic statuses:

- `pass`
- `fail`
- `unavailable`
- `unsupported`
- `error`

Required severities:

- `error`
- `warning`
- `advisory`

Required rule kinds:

- `file_exists`
- `file_absent`
- `path_matches`
- `text_required`
- `text_forbidden`
- `markdown_tasks_complete`
- `github_pr_open`
- `github_not_draft`
- `github_labels_absent`
- `github_tasks_complete`
- `github_checks_success`
- `github_threads_resolved`
- `github_non_author_approval`
- `semantic_rubric`

Every compiled rule must include `id`, `title`, `source`, `text`, `kind`,
`severity`, `backend_hint`, `confidence`, and `params`. Every diagnostic must
include `rule_id`, `source`, `backend`, `status`, `severity`, `message`, and
`evidence`.

# Parser and Classifier Policy

Use a line-oriented Markdown parser for the MVP. Extract candidate rules from
heading-scoped list items, ordered-list items, task checkboxes, and paragraphs
that contain normative keywords. Preserve source line numbers and heading
context. Ignore narrative text that does not contain normative wording.

Normative keywords for MVP are `must`, `must not`, `should`, `should not`,
`never`, `required`, `forbidden`, `fail`, `block`, `ensure`, and `require`.

Classification must use deterministic pattern matching first:

- file/path/text/taskbox rules route to `filesystem`;
- PR state, draft, label, status, tasklist, review, and thread rules route to
  `github`;
- unclear semantic judgement routes to `llm-rubric`.

Confidence values are `high`, `medium`, and `low`. A low-confidence route must
remain visible in compile JSON and `explain` output.

# GitHub Backend Policy

GitHub support shells out through `gh` behind a backend boundary. The MVP does
not directly write to GitHub.

For `statusCheckRollup`, treat every returned check as required evidence. Only
successful conclusions pass. Failure, pending, cancelled, timed out, skipped,
missing, or unknown states fail closed.

For review threads, fetch the first 100 threads. If GraphQL reports another
page, return an `unavailable` diagnostic instead of passing.

For independent review, use the latest review state per non-bot, non-author
reviewer. Only `APPROVED` satisfies the MVP rule. Comment-only or semantically
"substantive" reviews are out of scope.

For the LLM rubric backend, an unconfigured provider returns `unavailable` and
exits non-zero. Provider-specific implementation is out of scope.

# Risks

- Rule extraction may overfit to one document style.
- Some GitHub review requirements are partly semantic and cannot be fully
  determined from API fields.
- `gh` output and GraphQL pagination can create false confidence if not handled
  fail-closed.
- LLM rubric evaluation can look authoritative despite being advisory.
- A 3-day span requires avoiding plugin architecture over-design.

# Implementation Outline

Day 1:

- Define the rule IR and diagnostic model.
- Implement Markdown extraction and JSON compile output.
- Implement CLI command skeleton and exit codes.
- Implement filesystem/text backend with fixtures.

Day 2:

- Implement backend routing and validation orchestration.
- Implement GitHub PR backend using `gh pr view` and GraphQL.
- Add fail-closed handling for missing `gh`, auth failures, and pagination.
- Add compiler-style output formats: text and JSON.

Day 3:

- Add LLM rubric backend interface and advisory docs.
- Add `gh aw` composition guide and examples.
- Add end-to-end examples and smoke tests.
- Harden README, packaging metadata, and issue acceptance criteria.

# Done Criteria

- `uv run gate-keeper compile docs/example-rules.md --format json` emits rule IR.
- `uv run gate-keeper validate docs/example-rules.md --target fixtures/pass`
  exits `0`.
- A failing fixture exits non-zero and reports rule id, source location, backend,
  severity, and evidence.
- GitHub backend command construction is covered by tests.
- Live GitHub validation fails closed when `gh` auth or required context is
  unavailable.
- Docs explain local use, CI use, and `gh aw` composition.
