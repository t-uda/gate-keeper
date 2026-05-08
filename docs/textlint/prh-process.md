# `prh.yml` contribution process — issue #84

> Status: internal process artifact — not user-facing reference.

Tracking umbrella: #80. The dictionary itself lives at `prh.yml` in the
repository root and is wired through `.textlintrc.json` via
`textlint-rule-prh`. Rule severity, package selection, and the broader
textlint policy are owned by the documents listed under "Related decisions"
below.

---

## 1. Purpose and scope

`prh.yml` enforces project-specific terminology that no off-the-shelf
English style rule covers. Examples: the canonical spelling of the project
identifier `gate-keeper`, and the canonical lowercase form of upstream tool
names that are otherwise prone to title-casing (`textlint`).

`prh.yml` is **not** the right home for:

- General developer vocabulary (`GitHub`, `JSON`, `YAML`, `Readme`,
  `Git`, `ESLint`). These are owned by `textlint-rule-terminology` and must
  not be duplicated here. Adding the same term in both rules produces
  duplicate diagnostics and a confusing fix workflow.
- Ad-hoc one-off prose preferences. A rule that fires once across the
  corpus does not justify a permanent dictionary entry — fix the prose in
  place instead.
- Broad linguistic preferences without consensus. House-style choices
  (active vs. passive voice, sentence length, comma placement) are not
  appropriate for a deterministic dictionary; deferred to `terminology` or
  to future house-style guidance.

---

## 2. When to add an entry

Add an entry to `prh.yml` when **all** of the following hold:

1. The term is a project-specific identifier or piece of vocabulary whose
   canonical form is fixed by repository convention (e.g. CLI name, backend
   identifier, schema kind).
2. There is observed or anticipated recurrence — the same miscapitalisation
   has appeared more than once, or the term is high-traffic enough that
   future drift is likely.
3. The canonical form is unambiguous in every context the rule can fire.
   If the same surface form is correct in some contexts and wrong in
   others, the rule will produce false positives — defer or scope it via
   a narrower `pattern` instead.
4. `textlint-rule-terminology` does not already cover it. Run
   `npm run textlint` first; if `terminology` already flags the wrong form,
   no new entry is needed.

When **not** to add:

- One-off typos. Fix in place; do not codify.
- Terms whose canonical form depends on context (heading vs. inline,
  sentence-initial vs. mid-sentence, identifier vs. plain noun). For
  example, the bare English word "gatekeeper" as a category noun is
  acceptable; only the project-identifier variants `Gate-Keeper` and
  `GateKeeper` are codified.
- Terms used inside fenced code blocks or shell command examples — prh
  does not check fenced code blocks by default, so a dictionary entry will
  not help there.

---

## 3. PR template snippet

Each new or modified entry in `prh.yml` should be motivated in the pull
request body. A minimum acceptable description is one to two lines per
entry covering:

- **Term** — the `expected` value being added.
- **Patterns** — the surface forms the entry catches.
- **Motivation** — what observed drift or risk justifies codifying it.
- **Context constraint** (if any) — why the chosen `pattern` is narrow
  enough to avoid over-matching (e.g. exact identifier form, no word
  boundary on a substring that occurs in unrelated terms).

Example PR body fragment:

```markdown
### prh.yml additions

- `gate-keeper` (patterns: `/Gate-Keeper/`, `/GateKeeper/`) — project
  identifier. Title-case and PascalCase variants observed in document
  headings; the canonical form is the lowercase hyphenated identifier.
  The bare English noun "gatekeeper" is intentionally not flagged.
- `textlint` (patterns: `TextLint`, `Textlint`) — upstream tool name is
  always lowercase. Title-case appears at sentence start in tables and
  prose; this entry forces the canonical lowercase form.
```

A PR that adds entries without this rationale block should be sent back
for revision.

---

## 4. Coordination with `terminology` and other rules

The repository runs two rules together: `textlint-rule-terminology` and
`textlint-rule-prh`. Each owns a distinct slice of the vocabulary:

| Rule | Owns |
|---|---|
| `textlint-rule-terminology` | General developer brand and tool spelling — `GitHub`, `JavaScript`, `Readme`, `Git`, `ESLint`, `JSON`, `YAML`, etc. |
| `textlint-rule-prh` (`prh.yml`) | Project-specific terms only — the gate-keeper CLI name, backend identifiers when not already covered by `terminology`, and any project-internal vocabulary the package set agrees to enforce. |

If a term you want to add already fires under `terminology`, do not add it
to `prh.yml`. If `terminology` covers a term but with a wording you
disagree with, raise that as a separate decision under the package-set
rationale (see [`package-set.md`](package-set.md)) rather than overriding
it locally — duplicate dictionary entries make diagnostics harder to read.

---

## 5. Validation before merging

Before merging a `prh.yml` change:

1. Run `npm run textlint` from the repository root.
2. Confirm the new rule fires on the intended patterns (add a sample
   sentence in the PR description if the rule is non-obvious, or include a
   `specs:` block in the entry — prh refuses to load when `specs` fail).
3. Confirm the new rule does **not** introduce additional findings beyond
   the pre-existing baseline. Either adjust the rule's pattern or update
   the offending prose in the same PR.
4. If a finding is judged a true false positive that cannot be narrowed
   via `pattern`, document the trade-off in the PR description and either
   omit the entry or scope it via `.textlintignore`. Do not silence prh
   globally.

---

## 6. Related decisions

- [`package-set.md`](package-set.md) — why `textlint-rule-prh` is in the
  initial package set (#81).
- [`severity-policy.md`](severity-policy.md) — `prh` rule severity is
  `error` from day one (#85).
- [`per-doc-type-policy.md`](per-doc-type-policy.md) — single global
  config; `prh.yml` applies uniformly to all Markdown (#91).
- [`ja-preset-evaluation.md`](ja-preset-evaluation.md) — Japanese-language
  presets are deferred; this process applies to the English corpus only.

False-positive handling in general (inline disable comments, file-scope
ignores, accept-the-warning trade-offs) is owned separately by the
exception-policy decision tracked under #90.
