# Semantic-rubric benchmark fixtures

This directory holds the test scaffold for the `llm-rubric` (semantic) backend
quality work tracked under umbrella #63. It defines a stable schema, a small
set of realistic known-pass / known-fail entries, and the targets they refer to.

This issue (#65) lands the **fixture set and loader only**. Actual LLM
evaluation against these fixtures comes in later sub-issues that depend on #51
(real provider wiring).

## Layout

- `entries/<id>.json` — one fixture entry per file. The filename stem is the
  entry id (used in test output and for stable ordering).
- `targets/` — text artefacts referenced by entries with `target.kind == "path"`.
  Inline snippets live directly in the entry JSON via `target.kind == "inline"`.

## Entry schema

Each entry is a JSON object with the following fields. Unknown fields are a
hard error (the loader fails loudly).

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `rule_text` | string | yes | Verbatim rule wording (the Markdown bullet text the rule would appear as in a rules document). |
| `target` | object | yes | What the rule is judged against. See **Target** below. |
| `expected_judgment` | `"pass"` \| `"fail"` | yes | The judgment a correctly-functioning semantic rubric should return. |
| `expected_rationale_keywords` | array of strings | yes | Soft signal: tokens that should appear in the model's rationale. Used as a fuzzy match by future tests; not an exact-match contract. |
| `category` | enum | yes | One of `clarity`, `completeness`, `justification`, `naming`, `consistency`. |
| `intended_backend` | enum | yes | Which backend *should* judge this rule under the E2.5 architecture. One of `llm-rubric`, `external+textlint`, `filesystem`, `github`. For #65's initial landing every entry is `llm-rubric`; the field is present so the schema stays stable when later trunks (#80 / #94) come online. |
| `notes` | string | no | Author note about why this entry exists. |

### Target

`target` is a discriminated object:

- `{"kind": "path", "value": "<relative/path/under/targets>"}` — load a file
  from `tests/fixtures/semantic/targets/`.
- `{"kind": "inline", "value": "<text>"}` — use the literal string as the
  target text.

## Contribution rules

Determinism matters. The benchmark is meaningful only if entries describe
artefacts a competent reviewer would judge the same way every time.

- **Realistic**: derived from `gate-keeper`-style use cases (PR descriptions,
  rule documents, code naming, doc/code consistency, justification of
  changes). No invented domains.
- **Single claim per entry**: each `rule_text` should be a single judgable
  claim. Compound rules go in separate entries.
- **Targets are minimal**: just enough text to make the judgment unambiguous.
  Long targets dilute the signal and make rationale-keyword checks brittle.
- **`expected_rationale_keywords` are observable**: pick concrete nouns or
  verbs the model would naturally use. Do not require the model to echo the
  rule text verbatim.
- **No live data**: never reference real PR URLs, real user names, or
  proprietary content. Inline snippets only, or files committed under
  `targets/`.
- **No adversarial / injection cases yet**: those belong to the deferred
  axis L work; keep this fixture honest about routine quality signal.
- **Keep `intended_backend` honest**: if a rule could plausibly be caught by
  textlint or a filesystem predicate, mark it as such even though every entry
  currently routes to `llm-rubric` in practice. The field exists so future
  routing-regression tests can detect when a rule that *should* be deterministic
  falls through to the LLM.

## Loader

Entries are consumed via `tests/semantic_fixtures.py`. The loader validates
each entry against the schema (unknown fields fail loudly) and yields typed
`FixtureEntry` objects. See `tests/test_semantic_fixtures.py` for usage.
