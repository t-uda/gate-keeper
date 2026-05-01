# gate-keeper and gh aw

`gh aw` (GitHub Agentic Workflows) is an **actuation plane** — it orchestrates
agents and dispatches tasks. `gate-keeper` is a **validation tool** — it
compiles rule documents and emits pass/fail evidence.

These two tools compose well, but `gate-keeper` does not depend on `gh aw` at
runtime. It works as a standalone CLI.

## Relationship summary

| Concern | Tool |
|---|---|
| Orchestrate agents and steps | `gh aw` |
| Compile and validate rules | `gate-keeper` |
| Emit pass/fail evidence | `gate-keeper` |
| Act on that evidence | `gh aw` (or any shell) |

## Using gate-keeper from a workflow step

A `gh aw` workflow step can invoke `gate-keeper` as a subprocess:

```yaml
# Conceptual gh aw snippet — not a working workflow definition.
steps:
  - name: Validate PR against project rules
    run: |
      uv run gate-keeper validate docs/rules.md \
        --target "$PR_URL" \
        --backend auto \
        --format text
    # Non-zero exit code stops the workflow.
```

`gate-keeper` exits `0` when all rules pass, `1` when any rule fails or is
unavailable. The workflow step treats a non-zero exit as a blocking signal.

## Advisory PR comment usage

For advisory (non-blocking) usage, capture the output and post it as a PR
comment:

```bash
# Conceptual shell snippet — adapt to your runner environment.
OUTPUT=$(uv run gate-keeper validate docs/rules.md \
    --target "$PR_URL" --backend auto --format text 2>&1) || true
gh pr comment "$PR_URL" --body "$(printf 'gate-keeper result:\n\`\`\`\n%s\n\`\`\`' "$OUTPUT")"
```

This posts a comment regardless of the exit code. The word "advisory" here
means the PR can still be merged without the check passing — do not use this
pattern as a substitute for required status checks.

## Fail-closed behavior and branch protection

`gate-keeper` is fail-closed: unavailable evidence (missing `gh` auth, PR not
found, pagination error) always produces a `fail` or `unavailable` diagnostic,
never a silent pass.

However, `gate-keeper` **cannot enforce branch protection rules**. It does not
configure GitHub settings. Required status checks and required reviewers must
be set directly in the repository's branch protection configuration.

Use `gate-keeper` to:
- Give agents structured evidence before they take an action.
- Produce machine-readable diagnostics for downstream decision logic.
- Run local pre-flight checks without network access (filesystem backend).

Do not use `gate-keeper` to:
- Replace required CI status checks.
- Replace required human reviewer policies.
- Enforce merge gates that must be tamper-proof.

## Project safe-output composition (optional)

`gate-keeper compile` emits a JSON IR that downstream tools can consume:

```bash
# Emit IR; pipe to another tool or store as an artifact.
uv run gate-keeper compile docs/rules.md --format json > /tmp/ruleset.json
```

A `gh aw` workflow can treat this artifact as structured context when deciding
what validation to run. The IR schema is documented in
[docs/rule-ir.md](rule-ir.md).

## Non-goals

- `gate-keeper` does not call `gh aw` APIs.
- `gate-keeper` does not require `gh aw` to be installed.
- Workflow orchestration, retry logic, and agent dispatch remain in `gh aw`.
