# Getting started with gate-keeper

gate-keeper compiles a natural-language Markdown rule document into
compiler-style pass/fail diagnostics.
You write rules in plain English; gate-keeper routes each one to the right
backend (filesystem, GitHub PR, or LLM rubric) and emits structured evidence
so that failures are never ambiguous.

## Install

**From PyPI using `uv`** (recommended for end users):

```sh
uv tool install gate-keeper
gate-keeper --version
```

**From a local checkout** (recommended for contributors):

```sh
git clone https://github.com/t-uda/gate-keeper.git
cd gate-keeper
uv sync
uv run gate-keeper --version
```

Expected output:

```
gate-keeper 0.1.1
```

## Write your first rule document

Create a file called `rules.md`.
Each normative bullet is one rule.
gate-keeper classifies rules into backends automatically.

```markdown
# Project Rules

## Filesystem Checks

- `README.md` must exist in the repository root.

## GitHub PR Checks

- The PR must not be in draft state before merging.

## Quality Checks

- The commit message must clearly describe the change and its motivation.
```

The first bullet routes to the **filesystem** backend (deterministic file
check).
The second routes to the **GitHub** backend (GraphQL PR query).
The third routes to the **llm-rubric** backend (semantic judgment).
No backend configuration is required to try the filesystem path.

## Compile and inspect

Before running validation you can inspect how gate-keeper classifies each rule:

```sh
gate-keeper compile rules.md --format json
```

Excerpt of the output (fields shortened for readability):

```json
{
  "rules": [
    {
      "id": "rule-rules-L5",
      "kind": "file_exists",
      "backend_hint": "filesystem",
      "confidence": "high"
    },
    {
      "id": "rule-rules-L9",
      "kind": "github_not_draft",
      "backend_hint": "github",
      "confidence": "high"
    },
    {
      "id": "rule-rules-L13",
      "kind": "semantic_rubric",
      "backend_hint": "llm-rubric",
      "confidence": "low"
    }
  ]
}
```

`confidence: high` means the rule matched a deterministic pattern.
`confidence: low` means gate-keeper could not map the rule to a concrete
predicate and will fall back to the LLM rubric backend.

See [docs/rule-ir.md](rule-ir.md) for the full IR schema.

## Validate locally

Run `gate-keeper validate` to evaluate rules against a target file or
directory.
The `--backend filesystem` flag restricts evaluation to the filesystem
backend, which requires no credentials.

**Passing case** — `README.md` is present:

```sh
gate-keeper validate rules.md --target README.md --backend filesystem
```

Output:

```
rules.md:5: warning: [filesystem/pass] rule-rules-L5: README.md exists. [file_stat(path=README.md, exists=True)]
```

Exit code `0`.

**Failing case** — target file is absent:

```sh
gate-keeper validate rules.md --target MISSING.md --backend filesystem
```

Output:

```
rules.md:5: warning: [filesystem/fail] rule-rules-L5: MISSING.md does not exist. [file_stat(path=MISSING.md, exists=False)]
```

Exit code `1`.

Each diagnostic line has the form:

```
<source_file>:<line>: <severity>: [<backend>/<status>] <rule_id>: <message>. [<evidence>]
```

See [docs/diagnostics-guide.md](diagnostics-guide.md) for the full status and
evidence taxonomy.

## Validate against a PR

To run GitHub PR rules, pass a PR reference as the target:

```sh
gate-keeper validate rules.md --target OWNER/REPO#NUMBER
```

Replace `OWNER/REPO#NUMBER` with a real repository and PR number, for example:

```sh
gate-keeper validate rules.md --target t-uda/gate-keeper#100
```

gate-keeper calls the GitHub GraphQL API via `gh` (the GitHub CLI).
Make sure `gh auth login` has been run before using this target form.
Rules that require GitHub context (such as draft-state checks) are evaluated.
Pass `--backend github` to skip filesystem rules entirely when validating a PR;
without it, filesystem rules see the PR reference string as a local path and
return `fail` because no such path exists on disk.

## Next steps

- `gate-keeper --help` and `gate-keeper <subcommand> --help` — full flag
  reference for all subcommands (compile, explain, validate, diagnose, bench).
- [docs/diagnostics-guide.md](diagnostics-guide.md) — understand every status
  value, severity level, and evidence field in gate-keeper output.
- [docs/llm-rubric.md](llm-rubric.md) — enable and configure the LLM rubric
  backend for semantic rule evaluation.
- [docs/gh-aw.md](gh-aw.md) — integrate gate-keeper with GitHub Agentic
  Workflows (`gh aw`) for CI-level enforcement.
- [docs/example-rules.md](example-rules.md) — annotated rule examples covering
  all backends.
