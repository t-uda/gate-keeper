# CLI reference

`gate-keeper` is a local-first CLI that compiles natural-language Markdown rule
documents into compiler-style pass/fail output with evidence.

```
gate-keeper [--version] <subcommand> [options]
```

Subcommands: [`compile`](#compile), [`explain`](#explain),
[`validate`](#validate), [`diagnose`](#diagnose), [`bench`](#bench).

---

## Exit codes

| Code | Constant | Meaning |
|------|----------|---------|
| `0`  | `EXIT_OK`   | All rules passed (or `bench` ran without a usage error). |
| `1`  | `EXIT_FAIL` | One or more rules produced a non-passing status (`fail`, `unavailable`, `unsupported`, or `error`). |
| `2`  | `EXIT_USAGE` | Bad arguments, unreadable input file, an unmatched `--include` glob, or a duplicate rule ID across composed documents. |

Defined in `src/gate_keeper/diagnostics.py`.

**`bench` note:** `bench` exits `0` even when accuracy is below expectation —
accuracy is observational and does not constitute a CLI-level failure. It exits
`2` only on bad arguments or an unreadable entries directory.

---

## compile

Extract rules from one or more Markdown rule documents and emit them as the
rule IR (JSON).

### Synopsis

```
gate-keeper compile [--format {json}] (<document> | --include GLOB [--include GLOB ...])
```

Provide either a single positional `document` **or** one or more `--include`
globs — not both.

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `document` | positional | — | Path to a single Markdown rule document. Omit when using `--include`. |
| `--include GLOB` | option (repeatable) | — | Compose a policy bundle from every Markdown document matching `GLOB`. May be passed multiple times to combine multiple glob patterns. Mutually exclusive with the positional `document`. |
| `--format {json}` | option | `json` | Output format. Only `json` is supported. |
| `-h, --help` | flag | — | Show help and exit. |

### `--include` semantics

- Each `--include` glob is expanded with Python's `glob.glob(..., recursive=True)`,
  so `**` matches across directory boundaries when used.
- The combined set of matched paths is **sorted lexicographically** before
  parsing. Iteration order is therefore deterministic across platforms and
  independent of the order in which `--include` flags appear on the command
  line.
- Each matched document is parsed with its own source path, then all rules
  are merged into a single RuleSet. Per-rule `source.path` and `source.line`
  point back to the originating document.
- If any `--include` glob matches no files, the command exits `2` and prints
  the unmatched pattern.
- Rule IDs must be unique across the composed RuleSet. If two documents
  produce the same rule ID (the default scheme is `rule-<stem>-L<line>`,
  so two files with the same stem and a rule on the same line collide),
  the command exits `2` and reports both source path/line locations. Rule
  IDs are never silently renamed.

### Sample invocation

```sh
# single document (positional)
uv run gate-keeper compile docs/dogfooding-rules.md

# policy bundle composed from one glob
uv run gate-keeper compile --include 'rules/gatekeeper/*.md'

# policy bundle composed from multiple globs
uv run gate-keeper compile \
    --include 'rules/repo-structure/*.md' \
    --include 'rules/pr-governance/*.md'
```

### Sample output

```json
{
  "rules": [
    {
      "id": "rule-dogfooding-rules-L23",
      "title": "The PR description should name the user-visible change in the first sentence.",
      "source": {
        "path": "docs/dogfooding-rules.md",
        "line": 23,
        "heading": "Dogfooding rules — semantic advisory pool > Semantic advisory rules"
      },
      "text": "The PR description should name the user-visible change in the first sentence.",
      "kind": "semantic_rubric",
      "severity": "warning",
      "backend_hint": "llm-rubric",
      "confidence": "low",
      "params": {}
    }
  ]
}
```

### Notes

- Output is printed to stdout. Redirect with `> rules.json` to save to disk.
- The `backend_hint` and `confidence` fields are assigned by the classifier;
  they can be overridden at validation time with `--backend`.
- `compile` never makes network or LLM calls — it is a pure parse + classify
  step.

---

## explain

Show how each rule in a document maps to a backend, including the classifier's
confidence and rationale.

### Synopsis

```
gate-keeper explain [--format {text}] <document>
```

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `document` | positional | — | Path to a Markdown rule document. |
| `--format {text}` | option | `text` | Output format. Only `text` is supported. |
| `-h, --help` | flag | — | Show help and exit. |

### Sample invocation

```sh
uv run gate-keeper explain docs/dogfooding-rules.md
```

### Sample output

```
docs/dogfooding-rules.md:23: (Dogfooding rules — semantic advisory pool > Semantic advisory rules) [llm-rubric/low] rule-dogfooding-rules-L23: semantic_rubric
  The PR description should name the user-visible change in the first sentence.
  reason: no explanation available
```

Each block contains:

- `path:line:` source location and section heading.
- `[backend/confidence]` — backend the classifier selected and its confidence
  (`high`, `medium`, `low`).
- `rule_id: kind` — extracted rule identifier and kind.
- The rule text.
- `reason:` — classifier explanation (may be `no explanation available` for
  rules with a deterministic match pattern).

### Notes

- Like `compile`, `explain` never makes network or LLM calls.
- Use `explain` before `validate` to verify that rules are routed to the
  expected backends.

---

## validate

Validate an artifact (local directory, file, or GitHub PR) against one or more
Markdown rule documents and report pass/fail with evidence.

### Synopsis

```
gate-keeper validate [--rules-format {markdown,ir}]
                     [--backend {auto,filesystem,github,llm-rubric,external}]
                     [--format {text,json}] [--verbose]
                     [--reproducibility N] [--concurrency N]
                     [--allow-command-adapter]
                     [--artifact-kind {pr_description,commit_message,issue_body,documentation,code_change}]
                     --target <TARGET> [--target <TARGET> ...]
                     (<rules> | --include GLOB [--include GLOB ...])
```

Provide either a single positional `rules` document **or** one or more
`--include` globs — not both.

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `rules` | positional | — | Path to a Markdown rule document, or to a precompiled Rule IR JSON file when `--rules-format ir` is given. Omit when using `--include`. |
| `--include GLOB` | option (repeatable) | — | Compose a policy bundle from every Markdown document matching `GLOB`. May be passed multiple times. Mutually exclusive with the positional `rules`. Not compatible with `--rules-format ir`. |
| `--target TARGET` | option (repeatable) | — | Artifact or PR to validate. May be specified multiple times for filesystem multi-target evaluation (see [Multi-target evaluation](#multi-target-evaluation)). Each value is a local path, directory, quoted glob, GitHub PR URL, or `owner/repo#N` shorthand. Required unless `--target-changed` is given. |
| `--target-changed` | flag | off | Use the set of files changed since `--base-ref` as the initial candidate target pool (#278). The pool is filtered through the same text-readable and 200-file-cap machinery as an explicit `--target` expansion. When combined with `--target`, the result is the **intersection** of the two resolved sets (changed files that also satisfy the explicit target constraint). An empty changed set after filtering emits `evidence.kind=changed_set_empty` and exits 1 — never a silent exit 0. Requires a Git repository; a bad `--base-ref` exits 2. See [Changed-set target selection (--target-changed)](#changed-set-target-selection---target-changed) below. |
| `--base-ref REF` | option | `$GATE_KEEPER_BASE_REF`, then `origin/main` | Base Git ref for `--target-changed`. Ignored when `--target-changed` is not set. |
| `--rules-format {markdown,ir}` | option | `markdown` | How to interpret `rules`. `markdown` (default) parses and classifies a Markdown rule document — the historical behaviour. `ir` loads a precompiled `RuleSet` JSON file (the same shape `compile` emits) using the strict IR parser and **bypasses the classifier**, so hand-authored `kind`, `backend_hint`, and `params` fields reach the validator unchanged. |
| `--backend {auto,filesystem,github,llm-rubric,external}` | option | `auto` | Validation backend. `auto` delegates each rule to the backend the classifier (or the IR file) selected. |
| `--format {text,json}` | option | `text` | Output format. |
| `--verbose, -v` | flag | off | Expand structured LLM-rubric rationale (judgment, reason, reconstructed evidence quotes, suggested action, model) as indented lines below each diagnostic. Has no effect for non-LLM backends. |
| `--reproducibility N` | option | `1` | Run each LLM-rubric rule N times and record an agreement-rate `reproducibility_score` evidence entry. **No-op for non-LLM backends** (filesystem, GitHub, external ignore this flag). |
| `--concurrency N` | option | `1` | Maximum number of rule checks executed in parallel via a bounded `ThreadPoolExecutor` (#249, Slice 1). Default `1` preserves strict sequential dispatch. Deterministic prechecks (target-kind mismatch, registry miss) short-circuit before scheduling, so no provider call is made for short-circuited rules. Diagnostics are emitted in `ruleset.rules` order regardless of completion order. See [Bounded rule-level concurrency (#249)](#bounded-rule-level-concurrency-249) for the cost-rate warning. |
| `--allow-command-adapter` | flag | off | Enable the project-local `command` external adapter for this run only. **Security: pass this only for rule documents you fully trust** — the adapter executes whatever `params.argv` the rule document defines. Without this flag, every `external_check` rule whose `params.tool == "command"` returns `unavailable` / `command_adapter_disabled` and no subprocess runs. See [Project-local `command` adapter (#149)](#project-local-command-adapter-149) below. |
| `--artifact-kind {pr_description,commit_message,issue_body,documentation,code_change}` | option | — | Declare the kind of artifact passed via `--target` (#178). When set, every rule routed to the `llm-rubric` backend whose `target_kind` annotation differs from this value short-circuits to `unsupported` with `evidence.kind=target_kind_mismatch` (carrying `dispatch=deterministic_precheck` and `llm_called=false`) **before any provider call**. Rules with `target_kind=unspecified` ignore this flag (the prompt-level fallback still applies). Omit the flag to preserve the prior compatibility behaviour. See [Deterministic target_kind mismatch (#178)](#deterministic-target_kind-mismatch-178) below. |
| `-h, --help` | flag | — | Show help and exit. |

### When to use `--rules-format ir`

- You hand-author or generate a `RuleSet` JSON file directly (e.g. for the
  `external` backend with `params.tool: textlint`) and need `kind`,
  `backend_hint`, and `params` to survive verbatim.
- You want to validate a snapshot produced by `gate-keeper compile`
  (`compile rules.md > rules.json`) without re-parsing the source document.
- You are wiring `validate` into a pipeline where the classifier has already
  been run upstream and re-classification would erase intent.

The default Markdown path remains the right choice when authoring rules from
natural-language documents — the classifier turns prose into routed rules.

### `--include` semantics

`--include` follows the same semantics as in [`compile`](#compile): globs are
expanded with `glob.glob(..., recursive=True)`, the matched paths are sorted
lexicographically before parsing, an unmatched glob exits `2`, and duplicate
rule IDs across documents exit `2` with both source locations reported.
`--include` is Markdown-only and not compatible with `--rules-format ir`.

### Sample invocation

```sh
# Markdown rules (default; --rules-format markdown is implicit)
uv run gate-keeper validate docs/dogfooding-rules.md --target .

# Precompiled IR JSON
uv run gate-keeper compile docs/dogfooding-rules.md > rules.json
uv run gate-keeper validate --rules-format ir rules.json --target .

# JSON output
uv run gate-keeper validate docs/dogfooding-rules.md --target . --format json

# Validate against a policy bundle composed from a glob
uv run gate-keeper validate --include 'rules/gatekeeper/*.md' --target .

# With verbose LLM rationale
uv run gate-keeper validate docs/dogfooding-rules.md --target . --verbose

# Force LLM backend, run each rule 3 times for reproducibility
uv run gate-keeper validate docs/dogfooding-rules.md --target . \
    --backend llm-rubric --reproducibility 3
```

### Errors specific to `--rules-format ir`

| Condition | Exit code | Message shape |
|---|---|---|
| Missing IR file | `2` | `error: <path>: No such file or directory` |
| Not valid UTF-8 | `2` | `error: <path>: not valid UTF-8 (...)` |
| Invalid JSON | `2` | `error: <path>: invalid JSON (<reason> at line N column M)` |
| JSON shape fails strict `RuleSet` parsing | `2` | `error: <path>: invalid rule IR (<reason>)` |

The IR parser is strict: unknown fields and enum values outside the schema are
errors, not warnings. See [docs/rule-ir.md](rule-ir.md) for the contract.

### Sample output — `--format text`

```
docs/dogfooding-rules.md:23: warning: [llm-rubric/fail] rule-dogfooding-rules-L23: The PR description does not name the user-visible change in the first sentence.
docs/dogfooding-rules.md:24: warning: [llm-rubric/fail] rule-dogfooding-rules-L24: The PR description does not mention how the change was tested.
docs/dogfooding-rules.md:25: warning: [llm-rubric/fail] rule-dogfooding-rules-L25: The commit message does not explain why the change was made.
```

Each line: `path:line: severity: [backend/status] rule_id: message [evidence]`.

### Sample output — `--format json`

```json
{
  "diagnostics": [
    {
      "backend": "llm-rubric",
      "evidence": [
        {
          "data": {
            "cost_estimate_usd": 0.00010305,
            "judgment": "fail",
            "latency_ms": 1525,
            "model": "gpt-4o-mini",
            "primary_reason": "The PR description does not specify the user-visible change.",
            "prompt_version": "v1",
            "suggested_action": "Rewrite the first sentence to state the user-visible change.",
            "supporting_evidence_refs": [
              {"target_id": null, "path": "README.md", "line_start": 12, "line_end": 18}
            ],
            "supporting_evidence_quotes": ["..."],
            "tokens_in": 383,
            "tokens_out": 76
          },
          "kind": "llm_judgment"
        }
      ],
      "failure_mode": "llm_fail",
      "message": "The PR description does not specify the user-visible change.",
      "remediation": "Rewrite the first sentence to clearly state the user-visible change.",
      "rule_id": "rule-dogfooding-rules-L23",
      "severity": "warning",
      "source": {
        "heading": "Dogfooding rules — semantic advisory pool > Semantic advisory rules",
        "line": 23,
        "path": "docs/dogfooding-rules.md"
      },
      "status": "fail"
    }
  ]
}
```

### Notes

- `--backend auto` (default) routes each rule individually; `--backend
  llm-rubric` overrides the classifier and sends every rule to the LLM, which
  may produce `unsupported` diagnostics for non-`semantic_rubric` kinds.
- `markdown_evidence_block` rules require structured params (`heading`,
  `format`, `required_keys`, optional `allowed_sentinel_values`) that cannot
  be inferred from natural-language Markdown bullets. Compile a rule, edit the
  resulting JSON IR to set the params, and feed it back through the Python API
  (the CLI reparses the rule document on every run; see the textlint note in
  [docs/example-rules.md](example-rules.md) for the same constraint). See
  [docs/rule-ir.md](rule-ir.md#markdown_evidence_block--structured-policy-evidence)
  for the full PASS/FAIL contract.
- `--reproducibility N` only affects the `llm-rubric` backend. Passing it with
  `--backend filesystem`, `--backend github`, or `--backend external` is
  accepted but has no effect — reproducibility entries will not appear in
  evidence, and the run uses a single evaluation.
- When the LLM provider is unconfigured, `llm-rubric` rules return
  `unavailable` status (exit code `1`). Run `gate-keeper diagnose` to inspect
  provider configuration.
- See [docs/llm-rubric.md](llm-rubric.md) for provider setup and evidence
  shape details.
- The `github_changed_files_absent` rule kind requires per-rule `params`
  (`patterns`, optional `case_sensitive`). The natural-language classifier
  routes only explicit PR/path forbid wording (e.g. *"PRs must not change
  generated workbook outputs"*) to this kind; ambiguous filesystem rules
  remain on the filesystem or semantic backends. See
  [docs/rule-ir.md](rule-ir.md) for the full params and evidence shape.
- The `changed_file_policy` rule kind (issue #230) is a **deterministic
  changed-file policy check, not a semantic content review**. It evaluates
  changed files — from a GitHub PR or from a local Git working tree —
  against a repository-owned YAML manifest of forbidden path globs and
  exception entries. File contents are never inspected or uploaded.
  Required params: `manifest_path`, `changed_files_source`
  (`"github_pr"` or `"local_git"`); for local mode, optional
  `local_git_mode` and `repo_root`. See [docs/rule-ir.md](rule-ir.md) for
  the full manifest schema and failure modes.

### Multi-target evaluation

`--target` accepts multiple occurrences for filesystem rules; the resolved
file set is deduplicated, sorted lexicographically, and evaluated as a single
aggregated diagnostic per rule.

| Form | Example | Behaviour |
|---|---|---|
| Single file | `--target README.md` | Unchanged from earlier releases — the path is forwarded verbatim. |
| Repeated flag | `--target a.md --target b.md` | Both files are evaluated; one diagnostic per rule. |
| Directory | `--target docs/` | Recursively walks the directory; only text-readable files are included. |
| Quoted glob | `--target 'docs/**/*.md'` | Expanded by the CLI with `recursive=True`; matched directories are walked. **Quote the pattern** so your shell does not expand it before `gate-keeper` sees it — `argparse` only accepts a single value per `--target`, so an unquoted glob expanded by the shell will produce an "unexpected positional arguments" error. |

```sh
# All three filesystem rules, evaluated against every text-readable file
# under docs/:
uv run gate-keeper validate rules.md --target docs/ --format json

# Two specific files:
uv run gate-keeper validate rules.md \
    --target README.md --target docs/cli-reference.md

# Recursive Markdown glob:
uv run gate-keeper validate rules.md --target 'docs/**/*.md'
```

**Aggregation rules** (filesystem backend):

- `pass` only when every resolved file passes.
- `fail` when at least one file fails; the diagnostic message names the
  first few failing paths and the `multi_target_summary` evidence record
  carries `pass_count` / `fail_count` / `file_count`.
- `unavailable` when the resolved file set is empty (fail-closed) or when
  any per-file evaluation surfaces `unavailable` / `unsupported` / `error`.
- Per-file outcomes appear as `Evidence(kind="file_result", ...)` items,
  capped at 50; the `multi_target_summary` record reports
  `evidence_truncated` when the cap clips the list.

**File-count cap.** Expansion is bounded at **200 files** by default. When a
directory or glob expands to more than the cap, the CLI exits with
`EXIT_USAGE` (`2`) and prints `error: --target: target expansion produced N
files; exceeds limit of 200.` Narrow the target or split the run.

**Non-filesystem backends.** GitHub and external backends do *not* support
multi-target invocation: they fail closed with `unsupported` and a
`multi_target_unsupported` evidence record listing the raw targets and the
resolved file count, rather than silently using only one of the resolved paths.
LLM-rubric assembles a multi-file set into one narrowed prompt under a token
budget (#281, S5 — see [Narrowed affected-context assembly](#narrowed-affected-context-assembly-281)
below); the sole exception is a rule that declares the literal `params.targets`
list, which keeps the `multi_target_unsupported` decline.

See [docs/design/multi-target.md](design/multi-target.md) for the design
background; this section documents the first implemented slice.

### Deterministic target_kind mismatch (#178)

The `--artifact-kind` flag declares the kind of artifact passed via
`--target` so the validator can decide deterministically — before any
LLM call — whether each rule's annotated `target_kind` applies. When a
rule routed to the `llm-rubric` backend carries an explicit
`target_kind` annotation that differs from `--artifact-kind`, the rule
short-circuits to `unsupported` with a `target_kind_mismatch` evidence
record:

```json
{
  "kind": "target_kind_mismatch",
  "data": {
    "rule_target_kind": "pr_description",
    "artifact_kind": "commit_message",
    "dispatch": "deterministic_precheck",
    "llm_called": false
  }
}
```

The `dispatch=deterministic_precheck` and `llm_called=false` markers
distinguish this short-circuit from a model-returned `unsupported`
verdict (which carries the same evidence kind plus provider telemetry).

Vocabulary matches `TargetKind`: `pr_description`, `commit_message`,
`issue_body`, `documentation`, `code_change`. `unspecified` is rejected
by argparse — users who want the legacy prompt-level behaviour omit the
flag entirely. Rules whose `target_kind` is `unspecified` are
unaffected: they preserve the prompt-level fallback (the `llm-rubric`
prompt still names the rule's annotated kind in the model's
instructions for defence-in-depth, see `docs/llm-rubric.md`). Rules
routed to non-LLM backends (filesystem, GitHub, external) ignore this
flag.

Sample invocations:

```sh
# PR-description rule + PR body → reach the model normally.
gate-keeper validate docs/dogfooding-rules.md \
  --target "$(gh pr view 178 --json body --jq .body)" \
  --artifact-kind pr_description

# Commit-message rule + commit message → reach the model normally.
gate-keeper validate docs/dogfooding-rules.md \
  --target "$(git log -1 --format=%B)" \
  --artifact-kind commit_message

# Mismatch: PR-description rule + commit message → short-circuit, no
# model call. The rule's diagnostic carries
# evidence.kind = target_kind_mismatch.
gate-keeper validate docs/dogfooding-rules.md \
  --target "$(git log -1 --format=%B)" \
  --artifact-kind pr_description
```

#### Path targets — file content is rendered, not the path (#191)

When `--artifact-kind` is declared **and** `--target` resolves to a real
file on disk, the `llm-rubric` backend renders the file *content* into
the prompt's `Target reference` slot rather than the path string. This
prevents the model from latching onto the filename as a substitute for
the declared kind: at v4, gpt-4o-mini was observed returning
`judgment=unsupported` with `primary_reason: "The rule is annotated
'commit_message' but the artifact provided is a target-03-commit.txt."`
on positive controls (rule's `target_kind` matched the declared
`--artifact-kind`, so the deterministic precheck above correctly did NOT
fire) — citing the filename instead of evaluating the body. With the
content rendered, the model has no filename to parrot.

```sh
# Path target + matching --artifact-kind → the model sees the file body,
# not the path. The substring fabrication validator (#172) checks
# quotes against the same content, so content-grounded quotes resolve
# to spans (#179) normally.
gate-keeper validate docs/dogfooding-rules.md \
  --target /tmp/scratch/target-03-commit.txt \
  --artifact-kind commit_message
```

Omitting `--artifact-kind` preserves the legacy v4 byte-for-byte
behaviour (the `Target reference` slot carries `str(target)` and the
prompt-level fallback in #169 / #175 is responsible for declining
mismatches). Inline-string targets (`--target "<commit message body>"`)
and non-existent paths are forwarded unchanged regardless of the flag.

### Changed-set target selection (`--target-changed`)

`--target-changed` tells `validate` to derive the candidate target pool from
the files that changed between `--base-ref` (default: `$GATE_KEEPER_BASE_REF`,
then `origin/main`) and `HEAD`, computed via `git diff --name-only
BASE...HEAD`. The changed set is then filtered through the same text-readable
and 200-file-cap machinery as an ordinary `--target` expansion: binary files,
deleted files, and files that exceed the cap are excluded or raise a usage
error exactly as they would with an explicit `--target .`.

**Combining with `--target`:** when both flags are given the result is the
*intersection* — only files that appear in the changed set **and** also match
the explicit target constraint. This is useful for auditing changed Markdown
docs against a documentation rule set:

```sh
uv run gate-keeper validate docs/rules.md \
    --target-changed --base-ref origin/main \
    --target 'docs/**/*.md'
```

**Empty changed set:** if no changed text file survives the filter, `validate`
emits a single diagnostic with `evidence.kind=changed_set_empty` (carrying
`base_ref` and `resolved_count=0`) and exits 1. This is an explicit failure
signal — never a silent success.

**Path normalisation (R4):** `git diff --name-only` always returns
repository-root-relative POSIX paths regardless of the current working directory.
Gate-keeper resolves them to absolute paths before intersecting with
`--target` globs, so invoking `validate` from a subdirectory produces the
same result as running it from the repository root.

**LLM-rubric + multi-file changed set (R5):** a changed set (narrowed per rule
by S3 scope) that resolves to several files for an `llm-rubric` rule is assembled
into one narrowed affected-context prompt under a token budget (#281, S5 — see
[Narrowed affected-context assembly](#narrowed-affected-context-assembly-281)
below). Per-rule narrowing of the changed set landed as S3 — see
[Per-rule target scope](#per-rule-target-scope-paramstarget_scope) below.

**Non-goals:** `--target-changed` selects which files to evaluate; it does not
change *how* each rule evaluates them, and it never assembles the whole reference
closure — only the narrowed affected context reaches the model.

### Per-rule target scope (`params.target_scope`)

A rule may declare `params.target_scope` — a list of repo-relative glob strings
— in its IR to restrict which candidate files it runs against (#279, S3). This
is a **rules-IR key, not a CLI flag**: the `validate` command surface is
unchanged. The engine computes, per rule,

```
effective_set = scope_expansion(target_scope) ∩ run-level candidate set
```

where the run-level candidate set is the resolved `--target` / `--target-changed`
pool, and dispatches each rule against its own effective set. This makes a
ruleset self-contained for incremental auditing: `validate rules.md
--target-changed` becomes a full incremental audit without the caller enumerating
which rules care about which files.

```sh
# Two rules scoped to docs and src respectively; each evaluates only its subset
# of the current directory.
uv run gate-keeper validate rules.md --rules-format ir --target .

# Scope narrows the changed set: a docs-scoped rule audits only changed docs.
uv run gate-keeper validate rules.md --rules-format ir \
    --target-changed --base-ref origin/main
```

Outcomes (full contract: [docs/rule-ir.md](rule-ir.md) `params.target_scope`
and [docs/design/multi-target.md](design/multi-target.md) §9):

- **Non-empty effective set** — the rule runs against the intersection and its
  verdict carries a `scope_effective_set` evidence record.
- **Empty effective set** (valid scope, nothing in scope this run) — `PASS` with
  `scope_empty` evidence; never a run abort, never a silent pass.
- **Malformed scope or zero repo-wide matches** — `UNAVAILABLE` with
  `scope_invalid` evidence (fail-closed).
- **Per-rule effective set over the 200-file cap** — `UNAVAILABLE` with
  `scope_file_limit_exceeded` evidence for that rule only; other rules are
  unaffected.
- **Rules without `target_scope`** are dispatched byte-for-byte as before.

An `llm-rubric` rule whose effective set is multi-file is assembled into one
narrowed affected-context prompt (#281, S5 — see below); a single-file effective
set evaluates normally.

### Narrowed affected-context assembly (#281)

When a multi-file effective set (S3 scope ∩ the `--target` / `--target-changed`
candidate pool) is routed to an `llm-rubric` rule, the backend assembles the
files into a single prompt with `--- <path> ---` per-file headers and evaluates
them in **one** provider call per strategy iteration. The affected context is
always the narrowed set — never the whole reference closure.

- **Token budget.** Assembly is bounded by a per-rule token budget: default
  `32000`, overridable via `params.token_budget` in the rule IR or the dotenv key
  `GATE_KEEPER_TOKEN_BUDGET`. Token count is estimated from character length
  (`ceil(len / 3.2)`), biased high so truncation fires before the real limit.
- **Deterministic truncation.** An over-budget set is truncated to a
  lexicographic prefix of its files; every omitted file is named in a
  `truncation_warning` evidence record. No heuristic ranking is applied.
- **Assembled-set evidence.** An `affected_context` record lists the included
  files, any omitted / unreadable files, and the token estimate (narrowed and
  whole-set) so every dynamic verdict is auditable.
- **Fail-closed empties.** If no file fits the budget (or the set is empty), the
  rule is `UNAVAILABLE` with `affected_context_empty` evidence rather than sending
  an empty prompt.
- **Exceptions.** Rules that declare the literal `params.targets` list keep the
  `multi_target_unsupported` decline (that ≤5-entry mechanism is a separate axis);
  `github` / `external` backends are unaffected.

Full contract: [docs/design/multi-target.md](design/multi-target.md) §9.12.

### Bounded rule-level concurrency (#249)

`--concurrency N` schedules independent rule checks onto a bounded
`concurrent.futures.ThreadPoolExecutor` with `max_workers=N`. It targets
the common case where most of the wall-clock cost of a `validate` run is
spent waiting on the LLM-rubric provider — running several rules in
parallel collapses that wait into a single window without changing the
provider call count.

**Default behaviour is unchanged.** `--concurrency 1` (the default)
exercises the original strictly sequential dispatch path bit-for-bit;
existing scripts that omit the flag see no behavioural change.

**Ordering guarantee.** Diagnostics are emitted in `ruleset.rules` order
regardless of completion order. Internally, rules are submitted to the
executor in rule order and their futures are collected in submission
order, so stub backends or providers that respond out of order cannot
perturb the report.

**Deterministic prechecks are not scheduled.** The target-kind-mismatch
short-circuit (#178, see [Deterministic target_kind mismatch
(#178)](#deterministic-target_kind-mismatch-178)) and the
unregistered-backend (`registry_miss`) fallback both produce their
diagnostics inline in the dispatch loop before any executor submission.
This preserves the `llm_called=false` contract: a rule that the
precheck rejects never reaches the provider, even at high concurrency.

**Exception handling matches the sequential contract.** A backend that
raises inside a worker still surfaces as a `Status.ERROR` diagnostic at
the failing rule's position, with the same `exception` evidence record
the sequential path would produce.

**Cost rate caveat — read before raising the bound.** `--concurrency`
is **not** the provider Batch API. It does not reduce the per-request
token cost or the number of requests; it only overlaps requests in
time. A run with `--concurrency 8` consumes provider quota roughly 8×
faster than the same run at `--concurrency 1` and is therefore far more
likely to trip provider rate limits or burn through a quota window in
the same wall-clock interval. Provider-aware rate limiting, retry, and
backoff are out of scope for this slice; pick `N` to fit your provider
plan, and lower it if you see rate-limit errors.

**Scope (this slice).** Only the rule-level dispatch is parallelised.
Reproducibility-trial concurrency (running each rule's `N` reproducibility
attempts in parallel) and filesystem multi-target concurrency are not
included in this slice and are tracked separately.

```sh
# Default — strict sequential dispatch (no change vs. earlier releases)
uv run gate-keeper validate docs/dogfooding-rules.md --target .

# Overlap up to 4 rule checks at once for an LLM-heavy ruleset
uv run gate-keeper validate docs/dogfooding-rules.md --target . \
    --backend llm-rubric --concurrency 4

# Reject N<1 with a clear usage error
uv run gate-keeper validate docs/dogfooding-rules.md --target . --concurrency 0
# error: --concurrency must be >= 1, got 0
```

### Project-local `command` adapter (#149)

The `command` external adapter lets a trusted local rule document delegate a
check to a project-local executable. It runs only when `--allow-command-adapter`
is passed; otherwise every `external_check` rule with `params.tool == "command"`
returns `unavailable` and no subprocess is spawned.

> **Trust model — read this before passing the flag.**
>
> The rule document supplies `params.argv` (a list of strings). The adapter
> spawns that command directly (no shell), so passing
> `--allow-command-adapter` against a rule document you do not fully trust is
> a remote-code-execution vector equivalent to running the document's argv
> by hand. Never pass this flag against rule documents fetched from the
> network or authored by an untrusted party.

**Required IR shape (rule excerpt):**

```json
{
  "kind": "external_check",
  "backend_hint": "external",
  "params": {
    "tool": "command",
    "argv": ["python", "tools/policy/validate_path_policy.py"],
    "timeout_seconds": 30
  }
}
```

**Behaviour:**

- `argv` must be a non-empty list of strings; shell strings are rejected.
- `subprocess.run` is invoked with `shell=False` — no shell expansion,
  globbing, or environment-variable interpolation into argv.
- The adapter writes a JSON object to the command's stdin:

  ```json
  {
    "rule": { "id": "...", "text": "...", "params": { "tool": "command", "argv": [...] } },
    "target": "..."
  }
  ```

- The command must print a single Diagnostic JSON object (or a
  `DiagnosticReport` with exactly one diagnostic) to stdout and exit `0`
  for both pass and fail outcomes. Any non-zero exit is treated as a
  fail-closed `unavailable` / `command_failure` diagnostic.
- `timeout_seconds` defaults to `30`, hard maximum `300`. Timeouts
  produce `error` / `cli_timeout`. Missing executables produce
  `unavailable` / `cli_missing`.
- The adapter is the source of truth for `rule_id`, `source`, `severity`,
  and `backend` on the returned Diagnostic — a misbehaving command cannot
  rebrand another rule's result.

See [`docs/backend-external.md`](backend-external.md) for the full adapter
contract and full set of evidence kinds.

---

## diagnose

Report LLM-rubric provider credential state without making any LLM calls.
Useful for debugging `unavailable` diagnostics.

### Synopsis

```
gate-keeper diagnose
```

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `-h, --help` | flag | — | Show help and exit. |

`diagnose` takes no positional arguments.

### Sample invocation

```sh
uv run gate-keeper diagnose
```

### Sample output

```
dotenv path:        /home/user/.config/hermes-projects/gate-keeper.env
dotenv exists:      yes
GATE_KEEPER_LLM_PROVIDER: openai
OPENAI_API_KEY: <164 chars>
provider configured: yes
```

Fields:

| Field | Meaning |
|---|---|
| `dotenv path` | Absolute path to the per-project dotenv file gate-keeper reads. |
| `dotenv exists` | Whether the file exists on disk. |
| `GATE_KEEPER_LLM_PROVIDER` | Value of the provider key, or `<unset>` if absent. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Character count of the active key. **The key-value is never printed.** `<unset>` if the key is absent. |
| `provider configured` | `yes` when a supported provider and a non-empty key are both present; `no` otherwise. |

### Notes

- `diagnose` reads exactly the same dotenv file as the `llm-rubric` backend —
  `~/.config/hermes-projects/gate-keeper.env`. `os.environ` is intentionally
  not consulted.
- Always exits `0` regardless of configuration state (unconfigured is not a
  usage error).
- See [docs/llm-rubric.md](llm-rubric.md) for provider setup instructions.

---

## bench

Evaluate a fixture-corpus benchmark against the LLM-rubric backend and report
accuracy, reproducibility, and token metrics.

### Synopsis

```
gate-keeper bench [--reproducibility N] [--format {text,json}]
                  [--baseline PATH]
                  <entries_dir>
```

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `entries_dir` | positional | — | Directory of JSON benchmark entries (e.g. `tests/fixtures/semantic/entries/`). |
| `--reproducibility N` | option | `1` | Evaluate each entry N times and aggregate via majority vote. Ties break toward `fail`. |
| `--format {text,json}` | option | `text` | Output format. |
| `--baseline PATH` | option | none | Compare the current run against a baseline JSON file produced by `bench --format json`. Prints regressions and fixes; does **not** change the exit code. |
| `-h, --help` | flag | — | Show help and exit. |

### Sample invocation

```sh
# Quick run, text output
uv run gate-keeper bench tests/fixtures/semantic/entries/

# Full reproducibility run with JSON output
uv run gate-keeper bench tests/fixtures/semantic/entries/ \
    --reproducibility 3 --format json

# Compare against a saved baseline
uv run gate-keeper bench tests/fixtures/semantic/entries/ \
    --baseline tests/fixtures/semantic/baseline.json
```

### Sample output — `--format text`

```
entries: 24
correct: 19    accuracy: 79.2%
reproducibility (avg): 1.00  (n=1)
per-rule:
  clarity-01-pr-description-named-change     FAIL  rep=1.00  (expected pass, got fail)
  clarity-02-pr-description-vague            PASS  rep=1.00
  clarity-03-rule-text-single-claim          PASS  rep=1.00
  ...
```

### Sample output — `--format json` (summary fields)

```json
{
  "summary": {
    "accuracy": 0.792,
    "correct": 19,
    "entries": 24,
    "errors": 0,
    "latency_ms": 44249,
    "model": "gpt-4o-mini",
    "prompt_version": "v1",
    "reproducibility_avg": 1.0,
    "reproducibility_n": 1,
    "tokens_in": 10360,
    "tokens_out": 1554,
    "unavailable": 0
  },
  "per_rule": [
    {
      "id": "clarity-01-pr-description-named-change",
      "status": "FAIL",
      "expected": "pass",
      "actual": "fail",
      "reproducibility": 1.0,
      "primary_reason": "...",
      "latency_ms": 1782,
      "tokens_in": 553,
      "tokens_out": 68,
      "model": "gpt-4o-mini",
      "prompt_version": "v1"
    }
  ]
}
```

### Notes

- `bench` always exits `0` on a successful run. Low accuracy is not treated as
  a CLI-level failure — use `--baseline` to track regressions in CI instead.
- `--reproducibility N` applies the same majority-vote aggregation as
  `validate --reproducibility N`. With N=3 it runs 72 LLM calls for the
  default 24-entry corpus.
- `--baseline` comparison is advisory; it never changes the exit code.
- The canonical baseline file is `tests/fixtures/semantic/baseline.json`
  (generated by the `#132` issue). Produce your own with
  `bench --format json > baseline.json`.
