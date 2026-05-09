# Rule IR and Diagnostic Schema

This document describes the persisted JSON contract between
`gate-keeper compile` and `gate-keeper validate`. The Python source of truth is
`src/gate_keeper/models.py`. Reference fixtures live under `tests/fixtures/ir/`.

The schema is intentionally minimal for the 3-day MVP. Plugin abstractions,
provider configuration, and schema versioning are out of scope.

## Top-level shapes

`compile` emits a `RuleSet`:

```json
{
  "rules": [ /* Rule, ... */ ]
}
```

`validate` emits a `DiagnosticReport`:

```json
{
  "diagnostics": [ /* Diagnostic, ... */ ]
}
```

## `Rule`

Every rule includes all of the following fields:

| Field | Type | Notes |
| ----- | ---- | ----- |
| `id` | string | Stable identifier; unique within a `RuleSet`. |
| `title` | string | One-line human label. |
| `source` | `SourceLocation` | Where the rule originated in the rule document. |
| `text` | string | Verbatim normative text from the document. |
| `kind` | `RuleKind` | See enum table below. |
| `severity` | `Severity` | See enum table below. |
| `backend_hint` | `Backend` | The backend the classifier picked. See enum table below. |
| `confidence` | `Confidence` | Classifier confidence; `low` must remain visible in `compile` and `explain` output. |
| `params` | object | Kind-specific parameters. The exact key set per kind is defined by the backend issue that owns the kind (#3, #6, #10–#13). |

## `Diagnostic`

| Field | Type | Notes |
| ----- | ---- | ----- |
| `rule_id` | string | Refers to `Rule.id`. |
| `source` | `SourceLocation` | Echoed from the originating rule. |
| `backend` | `Backend` | Backend that produced this diagnostic. |
| `status` | `Status` | See enum table below. |
| `severity` | `Severity` | Echoed from the originating rule. |
| `message` | string | Compiler-style single-line message. |
| `evidence` | array of `Evidence` | Free-form per-backend records. |
| `remediation` | string \| null | Optional. Omitted from output when null. |

`Unknown` and `unavailable` evidence are represented by `status = unavailable`,
not by an absent `Diagnostic` and not by `pass`. This keeps the schema
fail-closed when the backend cannot read its required input.

## `Evidence`

```json
{ "kind": "<string>", "data": { /* arbitrary backend payload */ } }
```

`evidence` is an unstructured bucket on purpose. Each backend defines its own
`kind` namespace and the keys inside `data`. Consumers should not assume any
shape beyond those two top-level keys.

## `SourceLocation`

| Field | Type | Notes |
| ----- | ---- | ----- |
| `path` | string | Path to the rule document. |
| `line` | integer | 1-based line number. |
| `heading` | string \| null | Optional. Nearest heading scope. Omitted from output when null. |

## Enums

### `Backend`
`filesystem`, `github`, `llm-rubric`, `external`

The `external` backend is a single dispatcher for third-party tool adapters
(textlint, vale, ESLint, …). New tools register as adapters under this
backend rather than minting new `Backend` enum values; see
[`docs/backend-external.md`](backend-external.md) for the adapter contract.

### `RuleKind`
`file_exists`, `file_absent`, `path_matches`, `text_required`, `text_forbidden`,
`markdown_tasks_complete`, `github_pr_open`, `github_not_draft`,
`github_labels_absent`, `github_tasks_complete`, `github_checks_success`,
`github_threads_resolved`, `github_non_author_approval`,
`github_changed_files_absent`, `semantic_rubric`, `external_check`

### `Severity`
`error`, `warning`, `advisory`

### `Status`
`pass`, `fail`, `unavailable`, `unsupported`, `error`

### `Confidence`
`high`, `medium`, `low`

## Per-kind params

| `kind` | `params` key | Default | Notes |
| ------ | ------------ | ------- | ----- |
| `github_labels_absent` | `labels` | `["blocked","do-not-merge","needs-decision"]` | List of blocking label names (case-insensitive). Absent key uses default list; explicit `[]` means no blocking labels → always PASS. |
| `github_checks_success` | _(none)_ | — | Evaluates every entry in `statusCheckRollup` as required; only `SUCCESS` state/conclusion passes. Branch protection remains the authoritative control plane. |
| `external_check` | `tool` | _(required)_ | String adapter ID selecting which `external` adapter handles the rule (e.g. `"textlint"`). Missing → `unavailable` / `params_error`; unregistered → `unsupported` / `adapter_unknown`. All other `params` keys are forwarded verbatim to the adapter, which owns its own per-tool keys. See [`docs/backend-external.md`](backend-external.md). |
| `github_changed_files_absent` | `patterns`, `case_sensitive` | `case_sensitive=true` | `patterns` is required and must be a non-empty list of glob strings; missing or invalid → `unavailable` / `params_error`. `case_sensitive` is optional (default `true`). Glob semantics: `**` matches zero or more path segments, `*` matches anything except `/`, `?` matches one non-`/` char. See the dedicated section below for the data policy. |

## `github_non_author_approval` — formal evidence and limitations

The `github_non_author_approval` rule kind reads `gh pr view --json latestReviews,author`
and evaluates a single deterministic condition. `latestReviews` is GitHub's
precomputed slice of one review per reviewer (the latest state for that
reviewer), which avoids any pagination concern on the full reviews connection.

**PASS** when at least one reviewer satisfies ALL of:
- Login is not the PR author.
- Not a bot (`is_bot=True` or login ending in `[bot]`).
- Latest review state (as reported by `latestReviews`) is `"APPROVED"`.

**FAIL** otherwise — including when no qualifying reviewer exists.

### What this check proves

| Condition | Proven? |
| --------- | ------- |
| At least one non-author non-bot `APPROVED` review | Yes — deterministic from `gh` data. |
| Reviewer is a project member / CODEOWNER | No — `authorAssociation` not evaluated. |
| Review is semantically "substantive" | No — out of scope for MVP. |
| Comment-only reviews satisfy the rule | No — `COMMENTED` never satisfies. |
| `DISMISSED` reviews satisfy the rule | No — conservative; dismissed = not approved. |
| Branch protection CODEOWNERS rules are enforced | No — branch protection is the authoritative control plane; this check is advisory evidence. |

### Boundaries

- Bot reviews (`is_bot=True` OR login ends with `[bot]`) never satisfy the rule.
- Self-reviews (reviewer login == PR author login) never satisfy the rule.
- `DISMISSED`, `COMMENTED`, `CHANGES_REQUESTED`, and `PENDING` never satisfy the rule.
- Only the **latest** review per reviewer (by `submittedAt`) is considered.
- Comment-only and semantically substantive review judgement are out of scope.

## `github_changed_files_absent` — changed-file glob policy

The `github_changed_files_absent` rule fails when any file changed by the
target PR matches any of the configured forbidden glob patterns.  The
backend walks the GraphQL `pullRequest.files` connection page-by-page until
the connection reports `hasNextPage = false` and only then evaluates the
patterns.

### Params

```json
{
  "patterns": ["outputs/**", "**/*.xlsx", "context/researcher/raw/**"],
  "case_sensitive": true
}
```

- `patterns` — required, non-empty list of glob strings.  Each entry must
  be a non-empty string.
- `case_sensitive` — optional, defaults to `true`.

Missing or malformed `patterns` (or non-bool `case_sensitive`) produces
`status = unavailable` with `evidence.kind = params_error`.

### Glob semantics

| Token | Meaning |
| ----- | ------- |
| `**`  | zero or more path segments (matches across `/`) |
| `*`   | any sequence of characters except `/` |
| `?`   | exactly one character except `/` |
| literal | matched verbatim (regex metacharacters are escaped) |

`**/*.xlsx` matches both `data.xlsx` and `deep/nested/data.xlsx`.
`src/*.py` matches `src/a.py` but **not** `src/sub/b.py`.

### Evidence — `pr_changed_files`

| Field | Meaning |
| ----- | ------- |
| `total_changed_files` | Total count of files reported by GitHub for the PR. |
| `forbidden_patterns` | The patterns evaluated. |
| `case_sensitive` | The effective flag value. |
| `offending` | List of `{path, pattern}` for each changed file that matched at least one pattern (the pattern recorded is the first match). |
| `matched_patterns` | Sorted unique set of patterns that produced at least one offending entry. |
| `pagination_complete` | Always `true` on `pass`/`fail`; `unavailable` is emitted instead when pagination cannot complete. |
| `page_count` | Number of GraphQL pages fetched to assemble the full list. |
| `owner`, `repo`, `number`, `url` | PR coordinates (echoed from the resolver). |

### Failure modes

| Status | Evidence kind | When |
| ------ | ------------- | ---- |
| `pass` | `pr_changed_files` | No changed file matched any forbidden pattern. |
| `fail` | `pr_changed_files` | One or more changed files matched a forbidden pattern; the offending list is populated. |
| `unavailable` | `params_error` | `patterns` is missing, not a list, empty, contains a non-string or empty entry, or `case_sensitive` is not a bool. |
| `unavailable` | `gh_pagination_unavailable` | The GraphQL connection truncates without a usable cursor or the page count exceeds the safety bound. |
| `unavailable` | `gh_failure` / `gh_missing` / `gh_auth_failure` / `gh_json_error` / `gh_missing_field` / `gh_graphql_error` | Any other `gh` failure on the resolve or fetch call. |

The backend never returns `pass` on a partial page set — incomplete
pagination is treated as `unavailable` so a forbidden file added in a
later page can never be silently skipped.

## Validation policy

`from_dict` parsers in `src/gate_keeper/models.py` are strict and fail-closed:

- missing required fields raise `ValueError`;
- unknown fields raise `ValueError`;
- enum values outside the table above raise `ValueError`.

This is by design. The IR is a contract; tolerating drift here would silently
weaken every downstream backend.

## Fixtures

| File | Shape |
| ---- | ----- |
| `tests/fixtures/ir/rule-filesystem-text-required.json` | `RuleSet` with one filesystem rule. |
| `tests/fixtures/ir/rule-github-pr-open.json` | `RuleSet` with one GitHub rule. |
| `tests/fixtures/ir/diagnostic-mixed.json` | `DiagnosticReport` exercising `pass`, `fail`, `unavailable`, and `unsupported`. |
