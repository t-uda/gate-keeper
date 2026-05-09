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
| `2`  | `EXIT_USAGE` | Bad arguments, unreadable input file, or other usage error. |

Defined in `src/gate_keeper/diagnostics.py`.

**`bench` note:** `bench` exits `0` even when accuracy is below expectation —
accuracy is observational and does not constitute a CLI-level failure. It exits
`2` only on bad arguments or an unreadable entries directory.

---

## compile

Extract rules from a Markdown rule document and emit them as the rule IR (JSON).

### Synopsis

```
gate-keeper compile [--format {json}] <document>
```

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `document` | positional | — | Path to a Markdown rule document. |
| `--format {json}` | option | `json` | Output format. Only `json` is supported. |
| `-h, --help` | flag | — | Show help and exit. |

### Sample invocation

```sh
uv run gate-keeper compile docs/dogfooding-rules.md
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

Validate an artifact (local directory, file, or GitHub PR) against a rule
document and report pass/fail with evidence.

### Synopsis

```
gate-keeper validate [--rules-format {markdown,ir}]
                     [--backend {auto,filesystem,github,llm-rubric,external}]
                     [--format {text,json}] [--verbose]
                     [--reproducibility N]
                     --target <TARGET> <rules>
```

### Arguments and options

| Argument / option | Type | Default | Description |
|---|---|---|---|
| `rules` | positional | — | Path to a Markdown rule document, or to a precompiled Rule IR JSON file when `--rules-format ir` is given. |
| `--target TARGET` | option | **required** | Artifact or PR to validate. Pass a local path for filesystem rules; a GitHub PR URL or `owner/repo#N` for GitHub rules. |
| `--rules-format {markdown,ir}` | option | `markdown` | How to interpret `rules`. `markdown` (default) parses and classifies a Markdown rule document — the historical behaviour. `ir` loads a precompiled `RuleSet` JSON file (the same shape `compile` emits) using the strict IR parser and **bypasses the classifier**, so hand-authored `kind`, `backend_hint`, and `params` fields reach the validator unchanged. |
| `--backend {auto,filesystem,github,llm-rubric,external}` | option | `auto` | Validation backend. `auto` delegates each rule to the backend the classifier (or the IR file) selected. |
| `--format {text,json}` | option | `text` | Output format. |
| `--verbose, -v` | flag | off | Expand structured LLM-rubric rationale (judgment, reason, evidence quotes, suggested action, model) as indented lines below each diagnostic. Has no effect for non-LLM backends. |
| `--reproducibility N` | option | `1` | Run each LLM-rubric rule N times and record an agreement-rate `reproducibility_score` evidence entry. **No-op for non-LLM backends** (filesystem, GitHub, external ignore this flag). |
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

### Sample invocation

```sh
# Markdown rules (default; --rules-format markdown is implicit)
uv run gate-keeper validate docs/dogfooding-rules.md --target .

# Precompiled IR JSON
uv run gate-keeper compile docs/dogfooding-rules.md > rules.json
uv run gate-keeper validate --rules-format ir rules.json --target .

# JSON output
uv run gate-keeper validate docs/dogfooding-rules.md --target . --format json

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
