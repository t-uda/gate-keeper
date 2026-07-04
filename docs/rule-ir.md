# Rule IR and Diagnostic Schema

This document describes the persisted JSON contract between
`gate-keeper compile` and `gate-keeper validate`. The Python source of truth is
`src/gate_keeper/models.py`. Reference fixtures live under `tests/fixtures/ir/`.

The schema is intentionally minimal for the 3-day MVP. Plugin abstractions,
provider configuration, and schema versioning are out of scope.

## Consuming IR with `gate-keeper validate`

`compile` emits the IR JSON shape; `validate` can consume it directly:

```sh
gate-keeper compile rules.md > rules.json
gate-keeper validate --rules-format ir rules.json --target .
```

`--rules-format ir` parses the file with the strict `RuleSet.from_dict`
loader and **bypasses the classifier**, so hand-authored `kind`,
`backend_hint`, and `params` (notably `params.tool` for the `external`
backend) survive into the validator. See
[docs/cli-reference.md#validate](cli-reference.md#validate) for the full
flag reference and the IR-specific error table.

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
| `target_kind` | `TargetKind` (optional) | Optional annotation declaring the artifact kind the rule addresses (PR description, commit message, …). Defaults to `unspecified` and is omitted from the persisted output when unset. Consumed by the `llm-rubric` backend so a rule whose premise does not apply to the artifact returns `unsupported` rather than parroting the rule's wording. See enum table below. |

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
`markdown_tasks_complete`, `markdown_evidence_block`, `github_pr_open`,
`github_not_draft`, `github_labels_absent`, `github_tasks_complete`,
`github_checks_success`, `github_threads_resolved`,
`github_non_author_approval`, `github_changed_files_absent`,
`changed_file_policy`, `semantic_rubric`, `external_check`

### `Severity`
`error`, `warning`, `advisory`

### `Status`
`pass`, `fail`, `unavailable`, `unsupported`, `error`

### `Confidence`
`high`, `medium`, `low`

### `TargetKind`
`unspecified`, `pr_description`, `commit_message`, `issue_body`,
`documentation`, `code_change`

Optional rule annotation introduced in #169. The Markdown rule extractor
(`src/gate_keeper/parser.py`) recognises a trailing `[target_kind: <value>]`
token on a normative bullet, ordered-list item, task checkbox, or
paragraph; the token is stripped from the rule text and the parsed
`TargetKind` is attached to the resulting `Rule`. Unknown values are
tolerated: the token is stripped, `target_kind` falls back to
`unspecified`, and a parse-time warning is recorded under
`params.target_kind_parse_warning`. The `llm-rubric` backend uses the
field to inject an artifact-kind sentence into the prompt and authorise
an `unsupported` verdict when the rule's premise does not apply to the
artifact.

## Per-kind params

| `kind` | `params` key | Default | Notes |
| ------ | ------------ | ------- | ----- |
| `github_labels_absent` | `labels` | `["blocked","do-not-merge","needs-decision"]` | List of blocking label names (case-insensitive). Absent key uses default list; explicit `[]` means no blocking labels → always PASS. |
| `github_checks_success` | _(none)_ | — | Evaluates every entry in `statusCheckRollup` as required; only `SUCCESS` state/conclusion passes. Branch protection remains the authoritative control plane. |
| `external_check` | `tool` | _(required)_ | String adapter ID selecting which `external` adapter handles the rule (e.g. `"textlint"`). Missing → `unavailable` / `params_error`; unregistered → `unsupported` / `adapter_unknown`. All other `params` keys are forwarded verbatim to the adapter, which owns its own per-tool keys. See [`docs/backend-external.md`](backend-external.md). |
| `github_changed_files_absent` | `patterns`, `case_sensitive` | `case_sensitive=true` | `patterns` is required and must be a non-empty list of glob strings; missing or invalid → `unavailable` / `params_error`. `case_sensitive` is optional (default `true`). Glob semantics: `**` matches zero or more path segments, `*` matches anything except `/`, `?` matches one non-`/` char. See the dedicated section below for the data policy. |
| `changed_file_policy` | `manifest_path`, `changed_files_source`, `local_git_mode`, `repo_root` | `local_git_mode=staged_and_unstaged`; `repo_root` defaults to the target string or cwd | `manifest_path` (required, non-empty string) points at a YAML policy manifest. `changed_files_source` (required) is `"github_pr"` or `"local_git"`. `local_git_mode` (relevant only for `local_git`) is one of `staged \| unstaged \| staged_and_unstaged \| untracked \| all`. `repo_root` overrides the directory used for `git` commands. Deterministic changed-file policy, **not** semantic content review. See the dedicated section below. |
| `markdown_evidence_block` | `heading` | _(required)_ | Verbatim ATX heading title (case-sensitive). Backend locates the first fenced code block following this heading and stops at the next sibling/parent heading. Missing → `unavailable` / `params_error`. |
| `markdown_evidence_block` | `format` | _(required)_ | Format of the fenced block. Currently only `"yaml"` is supported. Other values → `unsupported`. |
| `markdown_evidence_block` | `required_keys` | _(required)_ | Non-empty list of dotted-key strings (e.g. `"policy.bundle"`). Each must resolve through nested mappings. Missing or empty → `unavailable`. |
| `markdown_evidence_block` | `allowed_sentinel_values` | `[]` | Optional list of allowed lowercase-token sentinel values (e.g. `["not_applicable", "missing_blocker"]`). When non-empty, any string leaf at a required key that matches the sentinel-token shape `^[a-z][a-z0-9_]*$` must appear in this list; free-form strings (with spaces, hyphens, mixed case, slashes) are not validated. |
| `external_check` (`tool=command`) | `argv` | _(required)_ | Non-empty list of strings. The first element is the executable; remaining elements are arguments. Shell strings (a single string with spaces) are rejected. The adapter never invokes a shell. |
| `external_check` (`tool=command`) | `timeout_seconds` | `30` | Subprocess timeout in seconds. Must satisfy `0 < timeout_seconds <= 300`. Timeouts produce `error` / `cli_timeout` diagnostics. |
| `semantic_rubric` | `targets` | _(absent)_ | Optional list of artifact specs for multi-artifact semantic rules (#182, slice 1). Each entry is a mapping `{id, kind, path}`. `id` is a stable rule-author-supplied string used for per-quote attribution in evidence. `kind` is **required** and must be a valid `TargetKind` value. `path` is an optional file path (absolute or relative); when absent or unreadable the prompt records a human-readable placeholder **and** the substring-grounding check excludes that artifact, making it effectively un-quotable — the fabrication validator will reject any quote drawn from a placeholder. Path traversal (`..` components) is rejected at parse time. Maximum 5 entries; IDs must be unique. Absent / empty list preserves the legacy single-target rendering byte-for-byte. The `llm-rubric` backend consumes this key to render the `## Target artifacts (multi)` block and to surface a parallel `supporting_evidence_quote_target_ids` list on the success evidence dict so each quote can be attributed back to a specific artifact. See [`docs/llm-rubric.md`](llm-rubric.md). |

## `params.target_scope` — per-rule target scope (engine-level, all kinds)

`params.target_scope` is an **engine-level** (not per-kind) key: a list of
repo-relative glob strings declaring which candidate files the rule runs against
(#279, S3 of umbrella #277). It is a distinct key from `params.targets` above —
`targets` assembles several artifacts into one `llm-rubric` prompt (a backend
concern), while `target_scope` narrows *which* files any rule is dispatched
against (an engine concern). A rule may carry both.

When present, the CLI / validator computes a per-rule effective set

```
effective_set = scope_expansion(target_scope) ∩ run-level candidate set
```

on normalized repo-relative POSIX paths, where the run-level candidate set is the
resolved `--target` / `--target-changed` pool, and dispatches the rule against
its own `TargetSpec`. Backends are unchanged and never learn a scope was applied.

| Property | Behaviour |
| -------- | --------- |
| Absent | Rule dispatches against the run-level target byte-for-byte as before (no effective-set computation, no new evidence). |
| Non-empty effective set | Rule runs against the intersection; a `scope_effective_set` evidence record (scope / candidate / effective sizes + effective paths) rides on the rule's normal verdict. A single-file effective set dispatches as a single target; a multi-file set aggregates on the `filesystem` backend and stays `multi_target_unsupported` on `llm-rubric` until S5 (#281). |
| Empty effective set (valid scope, no intersection) | `PASS` with `scope_empty` evidence — the steady state of incremental auditing; never a run abort, never a silent pass. |
| Malformed / empty-list / non-list scope, or a scope that expands to zero files repo-wide | `UNAVAILABLE` with `scope_invalid` evidence (fail-closed). |
| Per-rule effective set exceeds `DEFAULT_FILE_LIMIT` (200) | `UNAVAILABLE` with `scope_file_limit_exceeded` evidence for that rule only — one over-broad rule never sinks the rest of the run. |

Glob semantics match the existing target machinery: `**` spans path segments,
`*` matches within a segment, `?` one non-`/` char. Scopes are author-supplied;
they are never auto-populated. The full contract is
[`docs/design/multi-target.md`](design/multi-target.md) §9.

## `markdown_evidence_block` — structured policy evidence

Validates a fenced code block embedded in a Markdown target. Useful for
checking that a PR description, issue body export, or local report carries a
structured policy-evidence block (required reads, policy bundle IDs, lists of
unresolved decisions, …).

The backend reads the local Markdown file at `--target`, locates the first ATX
heading whose trimmed title equals `params.heading`, and looks for the first
fenced code block before the next sibling/parent heading.

### PASS

- Heading found.
- A fenced code block follows it.
- Block parses as a `params.format` mapping (currently `yaml` only — parsed
  with `yaml.safe_load`; tags and arbitrary object construction are not
  evaluated).
- Every dotted key in `params.required_keys` resolves to a present mapping
  entry.
- For string leaves whose value matches the sentinel-token shape
  `^[a-z][a-z0-9_]*$` (e.g. `not_applicable`), the value is in
  `params.allowed_sentinel_values` (or the allowlist is empty).

### FAIL

- Heading missing → evidence `failure: heading_missing`.
- Heading found but no fenced block follows before the next sibling/parent
  heading → `failure: block_missing`.
- Block does not parse as the requested format → `failure: malformed`, with
  `error_line` (1-based, absolute) when the parser exposes a problem mark.
- Block parses but is not a mapping → `failure: not_a_mapping`.
- One or more required keys missing OR one or more sentinel-shaped string
  values are not in the allowlist → `failure: key_or_sentinel`, with
  `missing_keys` and `invalid_sentinels` listed in the evidence payload.

### UNAVAILABLE / UNSUPPORTED

- Missing `heading`, `format`, or `required_keys` params → `unavailable` with
  `params_error` evidence.
- `format` value other than `yaml` → `unsupported`.
- Target file missing or unreadable → `unavailable`.

### Boundaries

- Local Markdown files only. GitHub PR bodies must be saved to a Markdown
  target via a wrapper or future GitHub backend feature.
- Heading match is exact (case-sensitive, no Unicode normalisation, ignores
  heading level).
- The "first" fenced block under a heading wins; later blocks under the same
  heading are not inspected.
- Sentinel validation is shape-based, not allowlist-only: free-form strings
  bypass the check entirely. Pattern is intentionally narrow so values like
  `"spread-applicant-ai/v3"` or `"Reviewed by jdoe"` are not flagged.
- Arbitrary JSON Schema validation is out of scope. The first slice supports
  required-key presence and sentinel-value membership only.
- YAML parsing uses `yaml.safe_load`. `safe_load` evaluates a single document;
  multi-document streams cause a parser error and are reported as
  `failure: malformed`. Anchors and aliases are resolved within YAML as
  normal; the security guarantee is specifically that `!tag` constructors
  cannot instantiate arbitrary Python objects.

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
| literal | matched verbatim (regular expression metacharacters are escaped) |

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

## `changed_file_policy` — manifest-backed changed-file policy

The `changed_file_policy` rule kind (issue #230) evaluates **only changed
files** — from a GitHub PR or from a local Git working tree — against a
repository-owned YAML policy manifest. It is a **deterministic changed-file
policy check, not a semantic content review**: file contents are never
parsed, inspected, or uploaded.

It is intended for real-data intake repositories such as
`uda-lab/spread-applicant-ai`, which must prevent rights-constrained or
private artifacts from being committed to public Git history. The manifest
remains the project's source of truth; this rule only enforces the manifest.

### Params

```json
{
  "manifest_path": "policy-manifests/path-rules.yaml",
  "changed_files_source": "github_pr",
  "local_git_mode": "staged_and_unstaged",
  "repo_root": ""
}
```

- `manifest_path` — required, non-empty string; absolute or relative to the
  working directory.
- `changed_files_source` — required; one of `"github_pr"` or `"local_git"`.
- `local_git_mode` — optional, used only when `changed_files_source ==
  "local_git"`. One of `staged | unstaged | staged_and_unstaged |
  untracked | all`. Defaults to `staged_and_unstaged`.
- `repo_root` — optional. Directory used for Git commands when
  `local_git`. When absent, the target string is used if it does not look
  like a PR reference; otherwise cwd.

Missing or invalid params produce `status = unavailable` with
`evidence.kind = params_error`.

### Manifest schema

The parser accepts a small explicit schema; unknown top-level keys and
unknown entry fields are rejected (fail closed). Arbitrary YAML tags are
**not** evaluated (`yaml.safe_load`).

```yaml
version: 1                            # required, must equal 1
entries:                              # required, list of entries
  - kind: forbidden_path_pattern
    pattern: "outputs/**/*.xlsx"      # required
    stop_condition: fail_closed_never_commit  # required
    source: docs/policy.md#3          # required (manifest entry reference)
    note: "..."                       # optional

  - kind: known_allowed_exception
    pattern: "outputs/sample.xlsx"    # required
    exempts_pattern: "outputs/**/*.xlsx"  # required (the forbidden pattern exempted)
    authorising_issue: "#42"          # required
    source: docs/policy.md            # required

  - kind: generated_output_extension
    extension: .xlsx                  # required
    source: docs/policy.md            # required
    note: "..."                       # optional

  - kind: authored_text_extension
    extension: .md                    # required
    source: docs/policy.md            # required
```

Optional informational top-level keys (`source_documents`, `allowed_kinds`,
`allowed_stop_conditions`, `known_allowed_exceptions_note`,
`default_unknown_extension`, `default_unknown_path`) are accepted and
ignored.

### Glob semantics

Identical to `github_changed_files_absent` (`**`, `*`, `?` per the table
above). Matching is case-sensitive.

### Evidence — `changed_file_policy`

| Field | Meaning |
| ----- | ------- |
| `total_changed_files` | Number of changed files considered. |
| `manifest_path` | The manifest path that was evaluated. |
| `source` | `github_pr:<owner>/<repo>#<n>` or `local_git:<mode>`. |
| `violations` | List of `{path, matched_pattern, stop_condition, manifest_source, note}` per offending file. |
| `forbidden_pattern_count` | Count of forbidden patterns in the manifest. |
| `exception_count` | Count of exception entries in the manifest. |

The `manifest_source` on each violation is the `source:` field of the
matched manifest entry, so diagnostics identify the exact manifest entry
that caused each finding (per #230 done criteria).

### Failure modes

| Status | Evidence kind | When |
| ------ | ------------- | ---- |
| `pass` | `changed_file_policy` | No changed file violates a forbidden pattern (or all violations are covered by a known-allowed exception). |
| `fail` | `changed_file_policy` | One or more changed files violate the manifest; the `violations` list is populated and the diagnostic carries a `remediation` block listing the offending paths. |
| `unavailable` | `params_error` | Required params missing or malformed (`manifest_path`, `changed_files_source`, `local_git_mode`, `repo_root`). |
| `unavailable` | `manifest_error` | Manifest file missing, YAML parse error, unsupported version, unknown top-level key, unknown entry kind, missing required entry field, or unknown entry field. |
| `unavailable` | `local_git_error` | `git` binary missing or Git command failed (e.g. target is not a Git repository). |
| `unavailable` | `gh_*` | Any `gh` failure when fetching the PR file list (same surface as `github_changed_files_absent`). |

### Composition

This rule reuses the existing PR file-list machinery
(`_fetch_changed_files`) so its GitHub-side behaviour matches
`github_changed_files_absent`. The two rules can coexist; choose
`github_changed_files_absent` for a flat list of glob patterns embedded in
the rule itself, and `changed_file_policy` when the patterns live in a
repository-owned YAML manifest with stop-conditions and exception entries.

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
