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

## 6. textlint prose quality — `external / external_check`

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

**Notes:**

- The `gate-keeper validate` CLI re-parses and re-classifies the rule document on every call (`src/gate_keeper/cli.py` `_cmd_validate`), so a hand-edited compiled `rules.json` is not honoured. The JSON is read as text and the custom `kind` / `backend_hint` / `params` overrides are dropped. Use the IR shape above as a reference for the adapter contract; exercise the textlint adapter via the `gate_keeper.validator.validate` Python API or via the test fixtures in `tests/fixtures/external/` until a CLI flag for IR input lands.
- Requires Node and `npm install` at the repository root to install textlint.
- Run `npx --no textlint --fix <file>` to apply auto-fixable corrections.
- See `docs/backend-external.md` for the full adapter contract.
