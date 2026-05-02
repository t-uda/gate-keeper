# MVP Readiness Checklist

This checklist defines the three-day MVP completion line. Every item must be
verifiable without hidden context. Check off items with `uv run` commands and
`gh pr view` output.

## CLI Commands

- [x] `gate-keeper compile DOCUMENT --format json` emits a JSON RuleSet with
  all required fields (`id`, `title`, `source`, `text`, `kind`, `severity`,
  `backend_hint`, `confidence`, `params`).
- [x] `gate-keeper validate DOCUMENT --target TARGET --backend auto --format text`
  emits compiler-style diagnostics and exits `0` (all pass) or `1` (any
  non-pass).
- [x] `gate-keeper validate DOCUMENT --target TARGET --backend auto --format json`
  emits a machine-readable DiagnosticReport with all required fields
  (`rule_id`, `source`, `backend`, `status`, `severity`, `message`, `evidence`).
- [x] `gate-keeper explain DOCUMENT --format text` shows per-rule backend hint,
  confidence, source line, and classifier reason phrase.
- [x] Exit code `2` is returned for CLI usage errors and unreadable input.

## Deterministic Backends

- [x] Filesystem backend handles: `file_exists`, `file_absent`, `path_matches`,
  `text_required`, `text_forbidden`, `markdown_tasks_complete`.
- [x] Filesystem backend requires no network access or `gh` auth.
- [x] All six filesystem rule kinds have passing-fixture and failing-fixture tests.
- [x] Classifier routes file/text/taskbox predicates to `filesystem` with `high`
  confidence.
- [x] Low-confidence routing (`llm-rubric`) is visible in `explain` and `compile`
  output.

## GitHub Backend

- [x] GitHub backend handles: `github_pr_open`, `github_not_draft`,
  `github_labels_absent`, `github_tasks_complete`, `github_checks_success`,
  `github_threads_resolved`, `github_non_author_approval`.
- [x] Missing `gh` binary produces `unavailable` (not a crash).
- [x] Failed `gh` auth produces `unavailable` (fail-closed).
- [x] GraphQL review-thread pagination produces `unavailable` when a second page
  exists (fail-closed).
- [x] Status checks that are pending or non-successful produce `fail`.
- [x] Independent review check ignores self-reviews and bot reviews.
- [x] All GitHub rule kinds have tests that do not call GitHub live.

## LLM Rubric Backend

- [x] Unconfigured LLM rubric backend returns `unavailable` (fail-closed stub).
- [x] Backend interface and advisory behavior documented in `docs/llm-rubric.md`.

## Documentation

- [x] `docs/gh-aw.md` describes composition with GitHub Agentic Workflows without
  coupling `gate-keeper` to `gh aw` as a runtime dependency.
- [x] `docs/rule-ir.md` documents the full IR schema (Rule, Diagnostic, Evidence,
  SourceLocation).
- [x] `docs/llm-rubric.md` explains the stub and its fail-closed behavior.
- [x] `docs/example-rules.md` demonstrates filesystem and GitHub rule types.
- [x] README documents Python 3.11 requirement and development commands.
- [x] README links to this checklist.

## Tests and CI

- [x] `uv run pytest` passes with no failures from a fresh `uv sync`.
- [x] End-to-end CLI smoke test: local passing fixture exits `0`.
- [x] End-to-end CLI smoke test: local failing fixture exits `1` with diagnostics.
- [x] GitHub command-construction tests cover all seven rule kinds without network.
- [x] GitHub tests verify fail-closed behavior for missing binary, auth failure,
  and pagination.
- [x] CI workflow (`.github/workflows/ci.yml`) runs `uv sync` and `uv run pytest`
  on Python 3.11 for every push and pull request.

## Known Limitations

- **Semantic review limits**: The classifier uses pattern matching only. Rules with
  ambiguous phrasing fall back to `semantic_rubric` / `llm-rubric` / `low`
  confidence. These rules return `unavailable` until an LLM provider is configured.
- **LLM advisory status**: LLM rubric results (when implemented) are informational;
  they cannot currently be promoted to required checks within gate-keeper. Promotion
  would need a per-rule severity override and CI integration.
- **Single-file target model**: The filesystem backend evaluates each rule against a
  single target path. Rules referencing specific filenames (e.g. "README.md must
  exist") require the caller to supply the correct path as `--target`.
- **GitHub rate limits**: The GitHub backend shells out to `gh` and is subject to
  API rate limits. No retry logic is implemented in the MVP.
- **Review thread pagination**: If a PR has more than 100 review threads, the
  `github_threads_resolved` rule returns `unavailable` (safe-fail, not a skip).
- **No direct GitHub writes**: gate-keeper cannot post PR comments, set labels, or
  update statuses. Output is a read-only audit of the current PR state.

## Non-Goals for MVP

- Hosted service or SaaS.
- Full natural-language understanding across arbitrary policies.
- Replacing GitHub branch protection rules, required status checks, or human review.
- Direct writes to GitHub objects.
- Deep integration with GitHub Agentic Workflows internals.
- Multi-repo orchestration or queue management.
- LLM provider integration (stub only).
- Parallel backend execution or async I/O.
- Plugin architecture for third-party backends.

## Upgrade Path After MVP

> **Note**: The list below is an idea dump from the MVP cut, **not** a prioritized
> roadmap.  The project's actual post-MVP trunk is *semantic rubric backend
> quality* (umbrella tracker: #63, gateway: #51); see `docs/llm-rubric.md`
> "Project trunk" section.  Items below may or may not become work items.
> Provider selection is not fixed (Anthropic and OpenAI are both viable); the
> implementation contract for credential transport is explicit dotenv loading —
> see issue #51 for details.

1. **LLM rubric backend**: Wire in an Anthropic or OpenAI provider to enable
   semantic rule evaluation. Add a `--model` or environment-variable configuration
   path. Promote high-confidence LLM results to `warning` severity; keep `advisory`
   for low-confidence output.
2. **Multi-file filesystem target**: Support passing a directory as `--target` and
   resolving per-rule file patterns from the rule text or params.
3. **Structured params**: Allow the rule document to specify `params` explicitly
   (e.g., `pattern: "*.py"`) so text/path rules do not need the caller to supply
   them at validate time.
4. **Retry and rate-limit handling**: Add exponential backoff for GitHub API calls
   that hit rate limits or transient errors.
5. **PR comment posting**: Add an optional `--post-comment` flag that writes a
   summary comment back to the PR via `gh pr comment`.
6. **Configurable required vs. advisory per rule**: Allow a rule document to declare
   `severity: error` to block merge vs. `severity: advisory` for informational
   output only.
