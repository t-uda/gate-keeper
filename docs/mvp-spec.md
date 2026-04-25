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
