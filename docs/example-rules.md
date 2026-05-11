# Example gate-keeper Rules

Annotated walkthroughs across all four backends.
Each entry shows: rule text → classifier route → sample target → `validate` output.

Use `gate-keeper explain <rules.md>` to see how a rule is routed before running validation.

---

## 1. `README.md` must exist — filesystem `file_exists`

**Rule document:**

```markdown
## Required Files

- `README.md` must exist in the repository root.
```

**Classifier route:** `filesystem / file_exists / high` — "must exist" triggers the explicit existence predicate.

**Sample run — pass / fail:**

```
$ gate-keeper validate rules.md --target path/to/README.md   # file exists
rules.md:3: warning: [filesystem/pass] rule-rules-L3: path/to/README.md exists.
  [file_stat(path=path/to/README.md, exists=True)]

$ gate-keeper validate rules.md --target path/to/README.md   # file absent
rules.md:3: warning: [filesystem/fail] rule-rules-L3: path/to/README.md does not exist.
  [file_stat(path=path/to/README.md, exists=False)]
```

---

## 2. `FIXME.txt` must not exist — filesystem `file_absent`

**Rule document:**

```markdown
## Temporary Files

- `FIXME.txt` must not exist in the repository.
```

**Classifier route:** `filesystem / file_absent / high` — "must not exist" triggers the absence predicate.

**Sample run — pass / fail:**

```
$ gate-keeper validate rules.md --target path/to/FIXME.txt   # file absent
rules.md:3: warning: [filesystem/pass] rule-rules-L3: path/to/FIXME.txt is absent.
  [file_stat(path=path/to/FIXME.txt, exists=False)]

$ gate-keeper validate rules.md --target path/to/FIXME.txt   # file present
rules.md:3: warning: [filesystem/fail] rule-rules-L3: path/to/FIXME.txt exists but must be absent.
  [file_stat(path=path/to/FIXME.txt, exists=True)]
```

---

## 3. GitHub PR merge gates — `github_not_draft`, `github_threads_resolved`, `github_tasks_complete`

**Rule document:**

```markdown
## Merge Conditions

- The PR must not be in draft state before merging.
- All review threads must be resolved before merge.
- PR tasks and checkboxes must all be complete before merging.
```

**Classifier routes:**

- `github / github_not_draft / high` — "not be in draft state"
- `github / github_threads_resolved / high` — "review threads … resolved"
- `github / github_tasks_complete / high` — "PR tasks and checkboxes"

**Sample run (all pass):**

```
$ gate-keeper validate rules.md --target https://github.com/owner/repo/pull/42
rules.md:3: warning: [github/pass] rule-L3: PR owner/repo#42 is not a draft.
  [pr_draft(is_draft=False, ...)]
rules.md:4: warning: [github/pass] rule-L4: PR owner/repo#42: all 0 review thread(s) are resolved.
  [review_threads(total=0, unresolved_count=0, ...)]
rules.md:5: warning: [github/pass] rule-L5: PR owner/repo#42 has all 7 task(s) checked.
  [pr_tasks(checked=7, unchecked=0, total=7, ...)]
```

**Sample run (fail — draft PR with unchecked tasks):**

```
$ gate-keeper validate rules.md --target https://github.com/owner/repo/pull/43
rules.md:3: warning: [github/fail] rule-L3: PR owner/repo#43 is a draft.
  [pr_draft(is_draft=True, ...)]
rules.md:5: warning: [github/fail] rule-L5: PR owner/repo#43 has 2 unchecked task(s) of 5.
  [pr_tasks(checked=3, unchecked=2, total=5, ...)]
```

**Note:** `gh` CLI must be authenticated. Without network access the backend returns `unavailable`.

---

## 4. PR changed-file path policy — `github / github_changed_files_absent`

Forbid a PR from touching paths that match a glob (e.g. generated outputs,
binary artifacts, raw researcher data).

**Rule document:**

```markdown
## Path policy

- PRs must not change generated workbook outputs.
- PRs must not add raw Office/PDF artifacts.
- PRs must not create context/researcher/raw/ as a tracked path.
```

**Classifier route:** `github / github_changed_files_absent / high` —
explicit PR + verb-of-modification wording. Each rule needs `params.patterns`
(a list of glob strings); the classifier records the kind and backend, and
authors fill in `params` after `compile`.

**Compiled rule (JSON excerpt — patterns added by hand):**

```json
{
  "rules": [{
    "id": "no-workbook-outputs",
    "text": "PRs must not change generated workbook outputs.",
    "kind": "github_changed_files_absent",
    "severity": "error",
    "backend_hint": "github",
    "params": {
      "patterns": ["outputs/**", "**/*.xlsx"],
      "case_sensitive": true
    }
  }]
}
```

**Sample run (pass — no offending paths):**

```
$ gate-keeper validate rules.md --target https://github.com/owner/repo/pull/42
rules.md:3: error: [github/pass] no-workbook-outputs: PR owner/repo#42: 0 of 7 changed file(s) match forbidden patterns.
  [pr_changed_files(total_changed_files=7, forbidden_patterns=['outputs/**', '**/*.xlsx'], offending=[], pagination_complete=True, ...)]
```

**Sample run (fail — generated workbook touched):**

```
$ gate-keeper validate rules.md --target https://github.com/owner/repo/pull/43
rules.md:3: error: [github/fail] no-workbook-outputs: PR owner/repo#43: 1 of 9 changed file(s) match forbidden patterns: ['outputs/results.xlsx'].
  [pr_changed_files(total_changed_files=9, offending=[{'path': 'outputs/results.xlsx', 'pattern': 'outputs/**'}], matched_patterns=['outputs/**'], page_count=1, ...)]
```

**Notes:**

- `patterns` is required and must be a non-empty list of glob strings.
  Missing or malformed params produce `unavailable` / `params_error`.
- Glob semantics: `**` matches across `/`, `*` matches within a single
  segment, `?` matches one non-`/` character. `**/*.xlsx` matches both
  top-level and nested workbooks.
- Pagination is exhaustive — the backend walks the full PR file list
  before evaluating. A truncated fetch returns `unavailable` rather than
  passing on partial data.
- Evidence includes `total_changed_files`, `forbidden_patterns`,
  `offending`, `pagination_complete`, and `page_count`. See
  [docs/rule-ir.md](rule-ir.md) for the full schema.

---

## 4b. Manifest-backed changed-file policy — `github / changed_file_policy`

A **deterministic changed-file policy check, not a semantic content
review**. Evaluates changed files from a GitHub PR *or* a local Git working
tree against a repository-owned YAML manifest (issue #230). Useful for
real-data intake repositories such as `uda-lab/spread-applicant-ai` that
must prevent rights-constrained or private artifacts from being committed
to public Git history.

**Rule document (compile produces the IR; params are author-supplied):**

```markdown
## Path policy

- PRs must not change paths forbidden by the project policy manifest.
```

**Compiled rule (JSON — params hand-authored against
`policy-manifests/path-rules.yaml`):**

```json
{
  "rules": [{
    "id": "manifest-path-policy",
    "text": "PRs must not change paths forbidden by the project policy manifest.",
    "kind": "changed_file_policy",
    "severity": "error",
    "backend_hint": "github",
    "params": {
      "manifest_path": "policy-manifests/path-rules.yaml",
      "changed_files_source": "github_pr"
    }
  }]
}
```

**Sample run (PR target — fail on a forbidden output):**

```
$ gate-keeper validate rules.md --target owner/repo#42
rules.md:3: error: [github/fail] manifest-path-policy: changed_file_policy: 1 of 9 changed file(s) violate manifest policy (policy-manifests/path-rules.yaml): ['outputs/foo.xlsx'].
  remediation: Remove or move the following files before committing:
    'outputs/foo.xlsx': matched forbidden pattern 'outputs/**/*.xlsx' (source: docs/policy.md#3)
```

**Sample run (local preflight — staged file is forbidden):**

```json
{
  "params": {
    "manifest_path": "policy-manifests/path-rules.yaml",
    "changed_files_source": "local_git",
    "local_git_mode": "staged_and_unstaged"
  }
}
```

```
$ gate-keeper validate rules.md --target .
rules.md:3: error: [github/fail] manifest-path-policy: changed_file_policy: 1 of 3 changed file(s) violate manifest policy (...): ['source/official/call.pdf'].
```

**Notes:**

- `changed_files_source` selects `"github_pr"` (uses the existing PR
  file-list machinery, identical to `github_changed_files_absent`) or
  `"local_git"` (collects changed paths via `git diff` /
  `git ls-files --others`).
- `local_git_mode` (only used with `local_git`) chooses
  `staged | unstaged | staged_and_unstaged | untracked | all`. Default
  is `staged_and_unstaged`.
- Manifest schema is strict: unknown top-level keys, unknown entry kinds,
  and unknown entry fields all fail closed with `manifest_error`
  evidence. Arbitrary YAML tags are not evaluated (`yaml.safe_load`).
- The diagnostic identifies the exact manifest entry per finding
  (`matched_pattern`, `manifest_source`, `stop_condition`).
- This rule does **not** classify file contents, decide
  rights/publication policy, or inspect file bodies. It enforces only
  the path-level policy that the manifest already states.

See [docs/rule-ir.md](rule-ir.md#changed_file_policy--manifest-backed-changed-file-policy)
for the full schema and failure modes.

---

## 5. Semantic rubric — `llm-rubric / semantic_rubric`

**Rule document:**

```markdown
## Advisory Semantic Checks

- The description should summarise the user-visible change.
```

**Classifier route:** `llm-rubric / semantic_rubric / low` — no deterministic predicate matches;
falls back to the LLM rubric backend.

**Sample run (pass — clear description):**

```
$ gate-keeper validate rules.md \
    --target "Add docs/getting-started.md with install instructions and quickstart examples"
rules.md:3: warning: [llm-rubric/pass] rule-L3: The description includes install instructions and quickstart examples.
```

**Sample run (fail — vague description):**

```
$ gate-keeper validate rules.md --target "Fix typo in docs" --verbose
rules.md:3: warning: [llm-rubric/fail] rule-L3: The description does not summarize a user-visible change.
  [llm-rubric]
    judgment  : fail
    reason    : The description does not summarize a user-visible change.
    evidence  : "Fix typo in docs"
    action    : Provide a detailed explanation of how the change affects user experience.
    model     : gpt-4o-mini
```

**Notes:**

- Use `severity: advisory` for semantic rules so they never block CI alone.
- The `--target` string is passed verbatim to the LLM as the artifact reference.
  For best results pass a descriptive text excerpt rather than a bare file path.
- Use `--verbose` to see the full structured rationale (judgment, evidence, action).
- Run `gate-keeper diagnose` to verify credentials; see `docs/llm-rubric.md`.

---

## 6. Structured policy evidence — filesystem `markdown_evidence_block`

`markdown_evidence_block` rules check that a Markdown target carries a structured
fenced code block under a named heading. Useful for PR descriptions, issue body
exports, or local reports that must declare a policy bundle, required reads,
and a list of unresolved decisions in machine-readable form.

The classifier does not auto-infer the structured params (`heading`, `format`,
`required_keys`, `allowed_sentinel_values`) from natural-language bullets. Build
the rule JSON directly or compile + edit:

**IR shape:**

```json
{
  "rules": [{
    "id": "policy-evidence-required",
    "title": "PR description carries a Policy evidence block",
    "kind": "markdown_evidence_block",
    "severity": "error",
    "backend_hint": "filesystem",
    "params": {
      "heading": "Policy evidence",
      "format": "yaml",
      "required_keys": ["policy_bundle", "required_reads", "unresolved_decisions"],
      "allowed_sentinel_values": [
        "not_applicable",
        "not_defined_yet",
        "missing_blocker",
        "unavailable"
      ]
    }
  }]
}
```

**Passing target (Markdown):**

````markdown
## Policy evidence

```yaml
policy_bundle: spread-applicant-ai/v3
required_reads:
  - docs/policy.md
  - docs/checklist.md
unresolved_decisions: not_applicable
```
````

**Failing target — invalid sentinel:**

````markdown
## Policy evidence

```yaml
policy_bundle: spread-applicant-ai/v3
required_reads:
  - docs/policy.md
unresolved_decisions: tbd
```
````

```
filesystem/fail — policy-evidence-required: target.md: evidence block at line 4: invalid sentinel value(s): ["unresolved_decisions='tbd'"]
  [evidence_block(path=target.md, heading=Policy evidence, format=yaml, fence_line=4,
      failure=key_or_sentinel, missing_keys=[], invalid_sentinels=[{'key': 'unresolved_decisions', 'value': 'tbd'}])]
```

**Notes:**

- Heading match is exact (case-sensitive). Heading level is not constrained.
- "First fenced block after the heading" wins; later blocks under the same
  heading are not inspected.
- Sentinel validation is shape-based: only string values matching
  `^[a-z][a-z0-9_]*$` (e.g. `tbd`, `not_applicable`) are checked against the
  allowlist. Free-form strings like `"spread-applicant-ai/v3"` or
  `"Reviewed by jdoe"` are not flagged.
- YAML parsing uses `yaml.safe_load` only; tags and constructors are never
  evaluated as Python objects.
- Local Markdown files only. To check a GitHub PR body, save it to a local
  `.md` file first.
- See `docs/rule-ir.md#markdown_evidence_block--structured-policy-evidence`
  for the full PASS/FAIL contract and evidence shape.

---

## 7. textlint prose quality — `external / external_check`

textlint rules use `kind: external_check` with `params.tool: textlint`.
The textlint adapter is registered automatically at CLI entry.

The classifier does not auto-route natural-language bullets to `external_check`.
Compile first, then edit the JSON to set `kind`, `backend_hint`, and `params.tool`:

```sh
gate-keeper compile rules.md --format json > rules.json
# edit rules.json: kind→external_check, backend_hint→external, params.tool→textlint
```

**Compiled rule (JSON excerpt):**

```json
{
  "rules": [{
    "id": "prose-textlint",
    "text": "Repository prose should be free of textlint findings.",
    "kind": "external_check",
    "severity": "warning",
    "backend_hint": "external",
    "params": { "tool": "textlint" }
  }]
}
```

**Diagnostic shape (when the textlint adapter is exercised):**

```
external/pass — prose-textlint: textlint reported no findings against docs/clean-file.md
external/fail — prose-textlint: textlint reported 2 finding(s)
  [textlint_finding(file=docs/draft.md, line=3, column=20,
      rule_id=terminology, message=Incorrect term: "javascript", use "JavaScript" instead, fixable=True);
   textlint_finding(file=docs/draft.md, line=3, column=35,
      rule_id=terminology, message=Incorrect term: "github", use "GitHub" instead, fixable=True)]
```

**Running the IR file through `validate`:**

```sh
# After compile + edit (or hand-authoring the JSON directly):
uv run gate-keeper validate --rules-format ir rules.json --target docs/draft.md
```

`--rules-format ir` parses the JSON with the strict `RuleSet` loader and
**bypasses the classifier**, so the hand-authored `kind: external_check`,
`backend_hint: external`, and `params.tool: textlint` reach the textlint
adapter unchanged. The default (`--rules-format markdown`) re-parses and
re-classifies a Markdown rule document on every call and is **not** the
right path for hand-authored IR — custom `kind` / `backend_hint` / `params`
overrides would be dropped.

**Notes:**

- Requires Node and `npm install` at the repository root to install textlint.
- Run `npx --no textlint --fix <file>` to apply auto-fixable corrections.
- See `docs/backend-external.md` for the full adapter contract and
  `docs/cli-reference.md#validate` for the `--rules-format` flag reference.

---

## 8. Project-local validator script — `external / external_check` (`tool=command`)

The `command` external adapter delegates a check to a project-local
executable. The rule document supplies `params.argv`; the adapter spawns
that command directly (no shell), passes a JSON `{rule, target}` object on
stdin, and parses a Diagnostic from stdout.

> **Security:** disabled by default. Pass `--allow-command-adapter` ONLY
> for rule documents you fully trust — the rule document supplies the
> argv. See the trust-model callout in `docs/cli-reference.md`.

**Compiled rule (JSON excerpt):**

```json
{
  "rules": [{
    "id": "policy-path-allowlist",
    "text": "Repository paths must satisfy the project's path-allowlist policy.",
    "kind": "external_check",
    "severity": "error",
    "backend_hint": "external",
    "params": {
      "tool": "command",
      "argv": ["python", "tools/policy/validate_path_policy.py"],
      "timeout_seconds": 30
    }
  }]
}
```

**Sample command (`tools/policy/validate_path_policy.py`):**

```python
#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
target = payload["target"]
# project-specific check ...
ok = True
print(json.dumps({
    "status": "pass" if ok else "fail",
    "message": f"path policy {'satisfied' if ok else 'violated'} for {target}",
    "evidence": [{"kind": "policy_check", "data": {"target": target}}],
}))
sys.exit(0)  # exit 0 on both pass and fail
```

**Sample run (disabled by default):**

```
$ gate-keeper validate rules.md --target .
rules.md:3: error: [external/unavailable] policy-path-allowlist: command adapter is disabled by default for security; pass --allow-command-adapter to enable it for trusted rule documents
```

**Sample run (explicitly enabled):**

```
$ gate-keeper validate rules.md --target . --allow-command-adapter
rules.md:3: error: [external/pass] policy-path-allowlist: path policy satisfied for .
```

**Notes:**

- `argv` is required and must be a non-empty list of strings. Shell strings
  (e.g. `"python tools/check.py"`) are rejected.
- `timeout_seconds` defaults to `30`, hard maximum `300`. Timeouts produce
  `error` / `cli_timeout`.
- The command must print exactly one Diagnostic JSON to stdout and exit `0`
  for both pass and fail. Any non-zero exit collapses to
  `unavailable` / `command_failure` regardless of stdout content — that
  channel is reserved for adapter-detected faults.
- See `docs/backend-external.md` and `docs/cli-reference.md` for the full
  contract.
