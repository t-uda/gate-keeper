# Semantic Rule Authoring Guide

This guide covers **judgment-level** rule authoring for the `llm-rubric` backend —
the phrasing choices that determine whether the LLM judge produces stable, reproducible
verdicts. Backend routing (deciding which rules belong to `llm-rubric` vs.
`external+textlint` vs. deterministic backends) is a separate concern tracked in #95.

---

## 1. Phrasing patterns that produce stable LLM judgments

A well-formed semantic rule gives the model a single, bounded evaluation task
with a concrete pass/fail criterion. Four properties correlate with stability:

**Positive observable nouns, not abstract qualities.**
State what the artefact *contains* or *exhibits*, not what quality it *has*.
Abstract nouns (`quality`, `clarity`, `appropriateness`) require the model to
supply its own unstated standard; concrete observable nouns (`sentence`,
`section`, `flag name`, `test invocation`) anchor the claim to something present
in the text.

| Unstable | Stable |
|----------|--------|
| The PR description has good clarity. | The PR description names the user-visible change in the first sentence. |
| Error messages are high quality. | Each error message states the file path and the failing predicate. |

**Explicit scope: which artefact, which property.**
The rule must identify both the artefact being judged (`The PR description`,
`Each changelog entry`, `Function names in the public API`) and the specific
property being checked (`contains a Verification section`, `ends with the class
suffix Error`). Unscoped rules force the model to guess both dimensions.

**Single claim per bullet.**
Each rule text evaluates one predicate. Compound rules linked by `and`, `or`,
or an implicit conjunction produce indeterminate verdicts when one clause passes
and the other fails. Split them into separate rules at authoring time; each
becomes a separate `Rule` in the IR with its own `status`.

**Target-grounded phrasing.**
The model receives only `rule_text`, `rule_kind`, and `target` (see
`docs/llm-rubric.md` — Rubric input/output shape). The rule must be evaluable
from that triple alone. Do not rely on external context (`as specified in our
style guide`, `per the team decision from last sprint`). If the criterion cannot
be stated in the rule text without a cross-reference, the rule is not suitable
for the `llm-rubric` backend in its current form.

---

## 2. Anti-patterns (judgment-level)

The anti-patterns below cause the LLM to produce unreliable or
non-reproducible verdicts. Mechanical anti-patterns (style, punctuation, line
length) that belong to a textlint-style rule are tracked in #95.

**Unbounded subject (`code`, `the project`, `everything`).**
Rules whose subject is an unbounded collection have no clear stopping condition.
The model cannot evaluate "the codebase is well-organized" against a single
target. Scope the subject to the specific artefact in scope.

**Multi-clause rules with implicit conjunctions.**
`The PR description is clear and complete and consistent with the title` contains
three independent claims. Even if the model returns a verdict, it is ambiguous
which clause drove it. Downstream remediation is also ambiguous. Split these.

**Indirect and inferential requirements.**
`Ensure consistency with our values` and `reflect the team's coding philosophy`
cannot be evaluated without information the model doesn't have. Rewrite as a
concrete observable: `The PR description does not use first-person pronouns`, or
route to a deterministic backend if a file-based predicate is available.

**Judgments requiring information the LLM cannot see.**
The rubric context (see `docs/llm-rubric.md`) contains a single `target` value —
one artefact. Rules that require cross-file comparison, historical state, or
external system data cannot be evaluated from that context:

- Cross-file: `Function names are consistent across all source files.` —
  the model sees one target, not all files.
- Historical: `The PR description is more detailed than last month's average.`
- External: `The changelog entry matches the ticket title in the issue tracker.`

These rules are either unroutable to `llm-rubric`, or must be rewritten to
evaluate a single in-scope artefact.

---

## 3. Worked examples

Each pair shows a bad rule, the stable rewrite, and a short diagnosis.

### 3.1 Clarity — PR description

| | Rule text |
|--|-----------|
| Bad | `The PR description is clear.` |
| Good | `The PR description names the user-visible change in the first sentence.` |

**Why the bad version is hard:** `clear` is an abstract quality with no
measurable referent. Two runs of the same model may disagree depending on
prompt temperature and context window position. The good version binds
`clarity` to a concrete predicate — *names user-visible change* — that is
present or absent in the first sentence.

### 3.2 Completeness — test coverage evidence

| | Rule text |
|--|-----------|
| Bad | `The PR description covers testing.` |
| Good | `The PR description states how the change was tested.` |

**Why the bad version is hard:** `covers` is vague — does mentioning the
word "test" count? The good version asks whether a *method* of testing is
stated, which is a concrete observable the model can locate in the text.

### 3.3 Justification — changelog entries

| | Rule text |
|--|-----------|
| Bad | `Changelog entries are good.` |
| Good | `Each changelog entry explains the user-visible reason for the change, not just what changed.` |

**Why the bad version is hard:** `good` has no grounding. The good version
contrasts *why* (user-visible reason) against *what* (the change itself),
giving the model a concrete comparative task anchored to the `justification`
category.

### 3.4 Naming — function names

| | Rule text |
|--|-----------|
| Bad | `Functions are well named and reflect good software design.` |
| Good | `Function names describe what the function returns or does, not how it is implemented.` |

**Why the bad version is hard:** compound claim (`well named` + `good software
design`) with two abstract qualities and an implied conjunction. The good
version reduces to a single observable binary: does the name describe
purpose/output, or does it leak implementation detail?

### 3.5 Consistency — documentation drift

| | Rule text |
|--|-----------|
| Bad | `The README is consistent with the code.` |
| Good | `The README's documented CLI flags match the flags the CLI actually accepts.` |

**Why the bad version is hard:** unbounded cross-file comparison —
`the code` is an unconstrained collection, and the model cannot see all of it.
The good version scopes the consistency check to a single pair of lists
(documented flags vs. accepted flags) that can both appear inline in the target.

### 3.6 Justification — commit messages

| | Rule text |
|--|-----------|
| Bad | `Commit messages follow best practices.` |
| Good | `The commit message contains a sentence explaining why the change was made, not only what was changed.` |

**Why the bad version is hard:** `best practices` is a community-relative norm
that the model may interpret differently across runs. The good version is
target-grounded: it asks for one observable property (presence of a *why*
sentence) that the model can locate or not locate in the supplied commit text.

---

## 4. Relationship to existing docs

**`docs/example-rules.md`** shows filesystem checks and GitHub PR checks
compiled by `gate-keeper`. It does not contain semantic rules. For concrete
semantic-rule specimens, see `tests/fixtures/semantic/entries/*.json` (the
benchmark fixture set) and `tests/fixtures/semantic/README.md` (fixture schema
and vocabulary). The patterns in this guide govern how to write semantic rules
well.

**`docs/llm-rubric.md`** documents the `llm-rubric` backend: advisory status,
provider configuration, the rubric input/output shape, failure modes, and the
project completion trunk. Read it before authoring semantic rules — it defines
exactly what context (`rule_text`, `rule_kind`, `target`) the model receives,
which constrains what a rule can safely claim.

**Backend routing (#95 / #66-B):** the question of *which rules should route to
`llm-rubric`* versus `external+textlint` versus a deterministic backend is out
of scope here. That routing logic and the mechanical anti-patterns best caught
by textlint are tracked in issue #95 (the `Backend.EXTERNAL` adapter work,
umbrella #80). The `intended_backend` field in the semantic fixture schema (see
`tests/fixtures/semantic/README.md`) is the per-entry record of the correct
routing decision once that work lands.

**Fixture vocabulary:** the five rubric categories — `clarity`, `completeness`,
`justification`, `naming`, `consistency` — are defined operationally by the
benchmark fixtures in `tests/fixtures/semantic/entries/`. The worked examples in
§3 above use exactly those categories so that rule authors can cross-reference
the fixture entries as concrete ground-truth specimens of each category.
