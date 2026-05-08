# Diagnostics guide

This guide explains every status value, severity level, and evidence shape that
gate-keeper produces. It is the companion to [docs/rule-ir.md](rule-ir.md),
which documents the rule IR contract, not the runtime output.

## Status taxonomy

Each validated rule produces exactly one `status` value.

### `pass`

The backend evaluated the rule and the target satisfies it.

- **CI exit code**: contributes `0` when every rule in the run is `pass`.
- **Typical cause**: evidence was found and matched the rule predicate.
- **What to do**: nothing; the gate is satisfied.

### `fail`

The backend evaluated the rule and the target violates it.

- **CI exit code**: the process exits `1` when any rule is `fail`,
  regardless of severity.
- **Typical cause**: a required file is absent, a required pattern was not
  found, a GitHub check failed, an LLM rubric judged the content insufficient,
  or textlint reported findings.
- **What to do**: read the `message` and `evidence` fields to understand the
  violation. Use `remediation` if present — it contains the recommended
  corrective action.

### `unavailable`

The backend could not evaluate the rule because a precondition is not satisfied.

- **CI exit code**: treated as `fail` (exit `1`) to keep the pipeline
  fail-closed. Missing evidence is never silently promoted to a pass.
- **Typical cause**: the LLM provider is not configured, the `gh` CLI is not
  authenticated, or a required external tool is absent.
- **What to do**: inspect `evidence[0].kind` for the specific sub-reason.
  See the "What to do when…" section below for the most common cases.

### `unsupported`

The backend or rule kind is not implemented for the current target type.

- **CI exit code**: exits `1` (fail-closed; the rule was declared but cannot
  run).
- **Typical cause**: a rule kind that requires a pull-request context is run
  against a local file target, or the backend is intentionally a stub.
- **What to do**: verify that the target type matches the rule kind. If the
  rule is intentionally out of scope for the current context, exclude it from
  the run.

### `error`

The backend encountered an unexpected runtime fault during evaluation.

- **CI exit code**: exits `1` (fail-closed).
- **Typical cause**: a subprocess timed out, an OS-level I/O error occurred,
  or the LLM provider returned an unrecoverable response.
- **What to do**: inspect `evidence[0].kind` — `cli_timeout`, `cli_os_error`,
  or `provider_error` indicate the fault category. Retry transient faults;
  file a bug for persistent ones.

---

## Severity

Severity is a rule-author annotation, not a runtime verdict. Every rule
specifies exactly one of `error`, `warning`, or `advisory`.

### `error`

The rule represents a hard requirement. A violation must be resolved before
the work item can proceed.

### `warning`

The rule represents a strong convention. A violation should be resolved but
does not automatically block delivery.

### `advisory`

The rule is informational. It highlights a possible concern that requires
human judgment rather than an automatic gate.

### Interaction with status

**In the current MVP, only `status` controls the CI exit code — severity does
not.** A `fail` result exits `1` regardless of whether its severity is `error`,
`warning`, or `advisory`. An `advisory`-severity rule that evaluates to `pass`
contributes `0` the same as an `error`-severity rule that passes.

This separation lets rule authors write advisory checks that appear in output
and PR comments without blocking CI. For guidance on when to promote a rule
from `advisory` to a required gate, see [docs/dogfooding.md](dogfooding.md).

---

## Evidence shapes per backend

Each `Diagnostic` carries an `evidence` list. Every entry has a `kind` string
and a `data` object. The shapes below show one realistic JSON fragment per
backend.

### Filesystem — `text_required` / `markdown_tasks_complete`

```json
{
  "kind": "text_match",
  "data": {
    "path": "AGENTS.md",
    "pattern": "uv",
    "match_count": 3
  }
}
```

```json
{
  "kind": "markdown_tasks",
  "data": {
    "path": "docs/checklist.md",
    "checked": 5,
    "unchecked": 1,
    "total": 6
  }
}
```

### GitHub — `github_threads_resolved` / `github_checks_success`

```json
{
  "kind": "review_threads",
  "data": {
    "total": 4,
    "unresolved_count": 0,
    "unresolved": [],
    "owner": "t-uda",
    "repo": "gate-keeper",
    "number": 97
  }
}
```

```json
{
  "kind": "checks_rollup",
  "data": {
    "total": 3,
    "successful": ["ci", "textlint"],
    "non_successful": [{"name": "dogfooding", "state": "FAILURE"}],
    "summary": "2/3 checks passed",
    "owner": "t-uda",
    "repo": "gate-keeper",
    "number": 97
  }
}
```

### LLM rubric — `llm_judgment`

```json
{
  "kind": "llm_judgment",
  "data": {
    "model": "gpt-4o-mini",
    "prompt_version": "v1",
    "judgment": "fail",
    "primary_reason": "The PR description does not explain the motivation for the change.",
    "supporting_evidence_quotes": [
      "PR body: 'fixes things'"
    ],
    "suggested_action": "Add a 'Why' section explaining the problem this PR solves.",
    "latency_ms": 842,
    "tokens_in": 512,
    "tokens_out": 87
  }
}
```

### External / textlint — `textlint_finding`

```json
{
  "kind": "textlint_finding",
  "data": {
    "file": "docs/getting-started.md",
    "line": 14,
    "column": 5,
    "rule_id": "prh",
    "severity_int": 2,
    "message": "TextLint -> textlint",
    "fixable": true
  }
}
```

### Failure-mode — `provider_error` / `parse_error`

```json
{
  "kind": "provider_error",
  "data": {
    "rule_text": "The README must contain a usage section.",
    "rule_kind": "semantic_rubric",
    "target": ".",
    "provider": "openai",
    "failure_mode": "APIConnectionError",
    "detail": "Connection refused while calling OpenAI completions endpoint."
  }
}
```

```json
{
  "kind": "parse_error",
  "data": {
    "stdout_excerpt": "Error: cannot find module 'textlint'",
    "stderr_excerpt": ""
  }
}
```

---

## What to do when…

### I see `unavailable` with `provider_unconfigured` evidence

The LLM rubric backend is not configured. gate-keeper cannot evaluate
`semantic_rubric` rules without a configured provider.

Follow the credential setup instructions in
[docs/llm-rubric.md](llm-rubric.md) to set `GATE_KEEPER_LLM_PROVIDER` and
the corresponding API key in the project dotenv file.

### I see `unavailable` with `gh_auth_failure` evidence

The `gh` CLI is present but not authenticated. gate-keeper cannot call GitHub
APIs.

Run `gh auth login` and follow the prompts, then retry the gate-keeper run.

### I see `unavailable` on `github_threads_resolved`

The pull request has more than 100 review threads. The GraphQL query fetches
the first 100 threads only, and gate-keeper is fail-closed when pagination is
required — the `evidence[0].kind` will be `gh_pagination_unavailable`.

This is intentional: a PR with more than 100 review threads almost certainly
has unresolved discussion that must be addressed manually. Resolve threads and
retry.

### I see `error` on a textlint finding

If gate-keeper reports `status: error` for a textlint rule, the textlint
process itself failed (timeout, binary missing, or OS error) rather than
reporting findings. This is distinct from `status: fail`, which means textlint
ran successfully and reported one or more findings.

- For `cli_timeout` evidence: increase `params.timeout` in the rule, or
  reduce the target scope.
- For `cli_missing` evidence: install textlint with `npm install textlint`.
- For a textlint finding you believe is a false positive, follow the guidance
  in [docs/textlint/exception-policy.md](textlint/exception-policy.md).
