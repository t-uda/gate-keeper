# Design: Multi-Target Evaluation (Directory / Multi-File Context)

Tracking issue: #74. Design phase only — no IR or backend changes in this doc.

---

## 1. Problem Statement

`gate-keeper validate` accepts exactly one `--target` path or PR identifier.
Every backend call signature mirrors this: `check(rule: Rule, target: str | Path) -> Diagnostic`.
The constraint is structural, not incidental.

As a result the following natural-language rules — already expressible in rule
documents today — are unevaluable or evaluate incorrectly:

| Rule | Why unevaluable today |
|------|----------------------|
| "All public Python modules expose a `__all__` export list" | Single-target can only test one `.py` file; caller must loop externally. |
| "Naming conventions (snake_case for functions, PascalCase for classes) are consistent across all source modules" | Consistency is a cross-file property; a single-file check misses inter-module drift. |
| "CHANGELOG entries match the API symbols introduced since the last tag" | Requires simultaneous access to `CHANGELOG.md` and one or more source files. |
| "Every public function in `src/` has a corresponding docstring" | Structural coverage check requires walking a directory tree. |
| "README `## Usage` section matches the CLI `--help` output captured in `docs/cli-reference.md`" | Rule spans two specific named files; neither is the same as `--target`. |
| "No module imports from a private (`_`-prefixed) sibling module" | Requires reading all files in a package to identify all import edges. |

The single-target model also interacts poorly with the LLM rubric backend:
the model currently receives only a path reference string, not content; rules
that say "doc matches code" have no content to judge against.

---

## 2. Design Questions

### 2.1 How does a rule signal it wants multi-file context?

**Recommendation: explicit IR param (`params.targets`) on the existing
`semantic_rubric` / `external_check` / filesystem rule kinds, populated by the
classifier or by the rule author; no new `RuleKind` enum value.**

Rationale:

- *Implicit classifier inference* is seductive but fragile. Deciding from free
  text whether a rule needs one file or many is itself a semantic judgment.
  Classifier confidence for multi-file rules would almost always be `low`, so
  the model would fall back to `llm-rubric` anyway — and then the LLM would
  receive no actual content. The implicit path adds a latency cliff without
  solving the content-delivery problem.

- *New `RuleKind` values* (e.g. `cross_file_semantic`) would require touching
  the strict `from_dict` parser, every backend's dispatch table, the
  classifier, and every downstream consumer. The `Backend` enum stability
  lesson from `#80` / `backend-external.md` applies equally to `RuleKind`: do
  not mint a new value when an adapter/param extension suffices.

- *Explicit `params.targets`* keeps the IR contract unchanged at the top level.
  `params` is already an unvalidated `dict[str, Any]` by design (see
  `docs/rule-ir.md` "Per-kind params"). A new optional key `targets` carries
  the file-set specification. Backends that understand it act on it; backends
  that do not treat the rule as single-target (graceful degradation). The
  classifier can populate it from obvious signals ("all `.py` files in `src/`")
  with `medium` confidence, or leave it absent for the author to supply via
  structured params (Upgrade Path item 3).

Trade-off: the rule text still uses natural language to describe scope; if the
classifier misses it, the author must add `params.targets` explicitly. This is
acceptable because fail-closed behaviour already exists: an absent `params.targets`
on a rule that needs multi-file context will produce a single-file result with
`low` confidence, which is the current baseline, not a regression.

---

### 2.2 File-set selection

**Recommendation: user-controlled glob(s) expressed in `params.targets`, with
a strict limit on files sent to LLM contexts; heuristic subset is a fallback
only for the filesystem backend.**

Two cases are in scope:

#### Filesystem multi-target (structural/text checks)

The filesystem backend walks the resolved file set and emits one `Diagnostic`
per file, then aggregates into a summary `Diagnostic` at the rule level. File
selection:

1. If `params.targets` is a list of globs, expand relative to the directory
   argument supplied at validate time.
2. If `params.targets` is absent and `--target` is a directory, apply a
   backend-default glob (`**/*` filtered to text-readable files) with a
   configurable file-count cap (default 200).
3. If `--target` is a single file and `params.targets` is absent, current
   single-file behaviour is preserved exactly.

Heuristics (e.g. "guess which files are relevant from rule text") are
explicitly **not** used in the filesystem backend; the rule must provide a
glob or the caller must pass a directory.

#### Semantic multi-target (LLM rubric / cross-file rules)

The LLM rubric backend must receive file content, not just path references.
File selection:

1. `params.targets` lists globs. The backend expands, reads, and concatenates
   content with per-file headers (`--- path/to/file.py ---`).
2. `params.token_budget` overrides the default cap (see §2.3).
3. If the expanded content exceeds the cap, the backend truncates to the
   highest-priority files (lexicographic by path, deterministic) and adds a
   `truncation_warning` evidence item. It does not silently discard — callers
   can see exactly what was sent.
4. If `params.targets` is absent and `--target` is a single file, current
   behaviour is preserved.

The key invariant: **content selection is always deterministic and auditable**
via evidence records. No heuristic file ranking is applied without a
`truncation_warning` evidence item.

---

### 2.3 Token budget per rule

**Recommendation: default cap of 32 000 tokens (≈ 128 KB of UTF-8 text) per
rule evaluation; override via `params.token_budget` (integer, tokens); global
override via `GATE_KEEPER_TOKEN_BUDGET` in the per-project dotenv file (applies
to the `llm-rubric` backend only — same file read by `_load_env_file()` in
`llm_rubric.py`; `os.environ` is intentionally not consulted, per
hermes-engineering#149 / `docs/auth-matrix.md`).**

Rationale for 32 K:

- Fits comfortably within all current supported models (Haiku 4.5: 200 K,
  GPT-4o-mini: 128 K). The cap is conservative to keep latency and cost
  predictable at the default.
- Large repos easily hit 10 M tokens for a full source tree; an uncapped
  default would produce surprise bills and timeouts.
- 32 K is roughly 1 000 lines of source — enough for a typical medium-sized
  module plus its documentation.

Override precedence (highest to lowest):

1. `params.token_budget` on the rule (per-rule, set by rule author or
   classifier).
2. `GATE_KEEPER_TOKEN_BUDGET` in the per-project dotenv file (`llm-rubric`
   backend only — read via `_load_env_file()`, not `os.environ`).
3. Compiled-in default (32 000).

Token counting: use a character-based approximation (1 token ≈ 4 chars) unless
the active provider SDK exposes a `count_tokens` API. The raw approximation
can underestimate real token counts (dense code, Unicode). To compensate,
apply a 0.8× safety factor: treat `len(text) / (4 × 0.8)` = `len(text) / 3.2`
as the estimated token count. This errs toward overestimation, so truncation
triggers before the real provider limit is reached.

When the resolved file set exceeds the budget, the backend must:
- truncate to the budget (not error);
- append a `truncation_warning` evidence record listing omitted files and
  `tokens_estimated`; this record is the sole signal that truncation occurred.

Note: `Diagnostic` has no `confidence` field — confidence lives on `Rule`
(see `src/gate_keeper/models.py`). Adding `Diagnostic.confidence` would be a
schema change outside the scope of this design. The `truncation_warning`
evidence record and/or a `status` of `UNAVAILABLE` (when no files survive
truncation) are the correct mechanisms for communicating degraded evaluation
quality.

---

## 3. Relationship to `mvp-readiness.md` Upgrade Path Item 2

`mvp-readiness.md` §"Upgrade Path After MVP", item 2:

> **Multi-file filesystem target**: Support passing a directory as `--target`
> and resolving per-rule file patterns from the rule text or params.

**Position: shared mechanism, not two separate mechanisms.**

The mvp-readiness item describes exactly the filesystem multi-target case
(§2.2 above). This design generalises it by:

- using the same `params.targets` field for both filesystem and LLM rubric
  backends;
- using the same `--target <directory>` CLI extension for both backends;
- sharing the glob-expansion utility between backends.

The item is explicitly scoped to the filesystem backend ("per-rule file
patterns from the rule text or params"), but there is no reason to build a
parallel path for LLM rubric. A single `params.targets` key that both backends
consume is simpler, avoids IR proliferation, and means a rule doc written for
filesystem validation does not need to change if it is later reclassified to
`llm-rubric`. The only backend-specific divergence is _what is done with the
expanded file set_ (structural check per file vs. content concatenation for LLM
prompt), which is an implementation detail inside each backend, not a schema
difference.

Diverging into two mechanisms would require two new param keys, two CLI paths,
and two documentation surfaces for essentially the same caller-facing concept.
The shared mechanism is the right call.

---

## 4. CLI Surface Sketch

Current `validate` signature:

```
gate-keeper validate RULES --target TARGET [--backend auto] [--format text|json] [--verbose]
```

`--verbose` expands structured LLM-rubric rationale (judgment, reason, evidence quotes,
suggested action, and model name) as indented lines beneath each diagnostic in text
output; no effect on JSON format.

`--target` today accepts a single path or a PR identifier (e.g., `1234`).

### Proposed extensions

1. **Directory**: `--target path/to/dir` — passed as-is; backends detect
   `Path.is_dir()` and apply their file-selection logic.

2. **Glob**: `--target 'src/**/*.py'` — the CLI shell-expands for filesystem
   rules, but the string is also accepted literally so callers can quote-protect
   it for the LLM rubric backend (which must control expansion).

3. **File list**: `--target file1.py --target file2.py` — `--target` becomes
   `append` action; backends receive `list[str | Path]`. When a single value is
   passed the list has one entry, preserving backward compatibility.

Alternative considered: a separate `--targets` flag (plural). Rejected: it
duplicates the flag namespace and breaks the current positional semantics for
callers that already use `--target`.

### Concrete invocation examples

```sh
# Example 1: filesystem multi-target — check all Python files in src/
gate-keeper validate rules/style.md --target src/ --format text

# Example 2: LLM rubric cross-file rule — compare CHANGELOG to two source files
gate-keeper validate rules/consistency.md \
  --target CHANGELOG.md --target src/api.py \
  --backend llm-rubric --format json

# Example 3: glob-specified target set — all Markdown docs
gate-keeper validate rules/docs.md --target 'docs/**/*.md' --format text
```

---

## 5. IR Sketch

No changes to `src/gate_keeper/models.py` in this design phase. The extension
is purely additive within the existing `params: dict[str, Any]` field on
`Rule`, which is already unvalidated at the model layer.

```python
# Proposed params keys for multi-target rules (illustration only;
# not modifying models.py).

# In Rule.params for a filesystem or semantic_rubric rule:
{
    # Existing keys (backend-specific):
    "pattern": "...",        # text_required, text_forbidden, path_matches
    "tool": "...",           # external_check

    # New optional keys (multi-target extension):
    "targets": [             # list[str] of globs, relative to --target base dir
        "src/**/*.py",
        "docs/*.md",
    ],
    "token_budget": 32000,   # int; tokens cap for LLM rubric context assembly
                             # overrides project-level GATE_KEEPER_TOKEN_BUDGET
}

# Corresponding validator.validate() signature extension (sketch):
# def validate(
#     ruleset: RuleSet,
#     targets: str | Path | list[str | Path],  # was: target: str
#     *,
#     backend: str = "auto",
# ) -> DiagnosticReport: ...

# TargetSpec (proposed internal helper, not in models.py):
# @dataclass
# class TargetSpec:
#     paths: list[Path]       # resolved, deduplicated
#     is_multi: bool          # True when >1 path or a directory was passed
#     base_dir: Path | None   # directory root for glob resolution
```

The `Rule.params` contract for `targets` and `token_budget` will be formally
documented in `docs/rule-ir.md` as part of sub-issue 74a (target schema
extension).

---

## 6. Backend Impact

### `filesystem.py`

`check(rule, target)` becomes `check(rule, targets: TargetSpec) -> Diagnostic`.
The "exactly one `Diagnostic` per `Rule`" contract (see `src/gate_keeper/validator.py`
and `docs/rule-ir.md`) is preserved: per-file details are embedded as
`Evidence` items inside the single returned `Diagnostic`, not emitted as
separate diagnostics.
Internal changes required:

- `_dispatch` receives a `TargetSpec`; for `is_multi=False` it falls through to
  the existing per-file logic unchanged.
- For `is_multi=True`, `_dispatch` iterates over `targets.paths`, calls the
  existing per-file helpers, and aggregates results into one `Diagnostic`:
  PASS only when all files pass; FAIL with one `file_result` evidence item per
  failing file; UNAVAILABLE if the file set is empty (fail-closed — do not
  paper over with PASS). Per-file pass/fail details are carried in
  `Evidence(kind="file_result", data={"path": ..., "status": ..., ...})` items.
- Glob expansion: a new `_expand_globs(base: Path, patterns: list[str]) -> list[Path]`
  utility, capped at a file-count limit (default 200, overridable via
  `GATE_KEEPER_TARGET_FILE_LIMIT` in the dotenv file). Exceeding the cap is an
  error, not a silent truncation.
- No new `Backend` enum value; no new `RuleKind` value.

### `llm_rubric.py`

`check(rule, target)` becomes `check(rule, targets: TargetSpec) -> Diagnostic`.
Internal changes required:

- `_build_rubric_input` gains a `files` key listing the resolved paths.
- `_build_prompt` gains a content assembly step: iterate `targets.paths`, read
  each file, prepend `--- <path> ---` headers, concatenate, apply `token_budget`
  cap with deterministic truncation (lexicographic, from the end of the list).
- `truncation_warning` evidence item appended when content is truncated
  (lists omitted files and `tokens_estimated`). This is the authoritative
  signal for degraded evaluation quality; `Diagnostic` carries no `confidence`
  field (that field lives on `Rule` — adding it to `Diagnostic` would be a
  schema change out of scope here).
- System prompt updated: "You are a reviewer applying a single rule to one or
  more artifacts provided as file contents below." The judgment schema
  `{"judgment": ..., "explanation": ...}` does not change.
- `_build_rubric_input` updated to emit `{"rule_text", "rule_kind", "targets": [...]}`.
- Evidence `llm_judgment` gains `files_sent` and `tokens_estimated` fields.

### Adapter pattern (referencing `docs/backend-external.md`)

External adapters (`ExternalAdapter.check(rule, target)`) follow the same
`TargetSpec` migration path as the two first-party backends. The dispatcher in
`backends/external.py` expands the target spec before forwarding to the adapter,
so existing adapters that accept a single `Path` continue to work unchanged when
`is_multi=False`. Adapters that want multi-target support opt in by inspecting
`TargetSpec.is_multi` and `TargetSpec.paths`. This mirrors the `external`
backend philosophy: the dispatcher handles routing hygiene; adapters own their
evaluation logic.

---

## 7. Out of Scope / Non-Goals

- **Cross-repo context**: fetching files from remote repositories or Git
  objects is out of scope. All paths are local filesystem paths after checkout.
- **Streaming / chunking for very large file sets**: the design caps at
  `token_budget` and truncates deterministically. Streaming the file set in
  chunks across multiple LLM calls (to handle arbitrary repo sizes) is deferred
  until real budget pressure is observed. The truncation_warning evidence item
  makes it visible when this matters.
- **Heuristic file ranking / relevance scoring**: no ML-based or TF-IDF
  ranking of which files to include. Selection is always deterministic
  (caller-specified globs or lexicographic order).
- **Parallel / async backend execution**: out of scope per mvp-readiness.md
  Non-Goals ("Parallel backend execution or async I/O").
- **Cross-rule correlation**: this design evaluates each rule independently
  against its target set. Global analysis (e.g., "is property X true across
  all rules") is not in scope.
- **`--target` accepting remote URIs or S3 paths**: local filesystem only.

---

## 8. Implementation Roadmap

Proposed follow-up sub-issues (do not create these — list only):

**74a — Target schema extension**
Formally add `params.targets` (list of globs) and `params.token_budget`
(integer) to `docs/rule-ir.md`. Update `docs/rule-ir.md` per-kind params table.
Add `TargetSpec` dataclass to `models.py` (read-only; no change to `Rule`
serialization). Update classifier to populate `params.targets` from obvious
glob signals in rule text (`"all .py files"`, `"every module in src/"`).
Acceptance: `uv run pytest` passes; `gate-keeper compile` emits `params.targets`
for appropriate rules.

**74b — Filesystem multi-target**
Implement `_expand_globs`, multi-target dispatch, and per-file aggregation in
`backends/filesystem.py`. Update `validator.validate()` to accept
`list[str | Path]`. Update CLI `--target` to `append` action. File-count cap
enforced. Acceptance: directory `--target` works for all six filesystem rule
kinds with fixture tests.

**74c — LLM rubric multi-target**
Implement content assembly, token budget enforcement, truncation with evidence,
and updated prompt in `backends/llm_rubric.py`. Acceptance: `semantic_rubric`
rules with `params.targets` receive concatenated file content; truncation
produces `truncation_warning` evidence; tests cover both under-budget and
over-budget cases without live API calls.

**74d — CLI surface and external adapter contract**
Finalize the `--target` append-action CLI, update `--help` text and
`docs/example-rules.md` with multi-target invocation examples. Update
`docs/backend-external.md` adapter contract to document `TargetSpec` and
opt-in multi-target support. Acceptance: three concrete invocation examples
from §4 work end-to-end.
