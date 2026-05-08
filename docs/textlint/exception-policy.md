# textlint false-positive handling and exception policy (#90)

> Status: internal process artifact — not user-facing reference.

Tracking umbrella: #80. This document defines how contributors handle textlint
false positives and propose project-level exceptions. It is policy-only;
concrete exception entries belong in their respective files
(`.textlintignore`, `prh.yml`, `.textlintrc.json`, or inline comments), not
here.

Related decisions:
- [`severity-policy.md`](severity-policy.md) — severity-level intent and the
  `error` / `warning` / `info` mapping (#85).
- [`per-doc-type-policy.md`](per-doc-type-policy.md) — global vs per-doc-type
  scope (#91).
- [`package-set.md`](package-set.md) — the rule inventory (#81).
- `prh-process.md` (planned, #84) — the project-specific terminology dictionary
  process. Until that document lands, treat references below to the future
  `prh.yml` as descriptions of the planned mechanism.

---

## 1. When false positives happen

A false positive is a textlint finding that fires on text the project
considers correct in context. They cluster into three common categories:

- **Project-specific terms not in dictionaries.** General-purpose rules
  (e.g. `textlint-rule-terminology`) ship a curated brand-term list and do not
  know about repository-internal names (for example, the `gate-keeper` CLI,
  module names, or product-specific concepts).
- **Code-string false matches in prose.** Identifiers, file paths, command
  names, and copied configuration fragments may collide with rule patterns
  meant for prose (for example, a literal `eslint` token inside a JSON snippet
  in a code fence).
- **Regional or linguistic exceptions.** Rule presets occasionally assume a
  specific register or locale (UK vs US spelling, formal Japanese vs informal
  Japanese, paper-style vs dev-doc prose). The current corpus is English-only
  developer prose, but presets evaluated under #83 may surface locale
  mismatches as Japanese content is added.

Each category has a different lowest-cost mitigation; see §3.

---

## 2. Mitigation channels

Contributors have four channels for handling a false positive. They are listed
from narrowest scope to broadest.

### 2.1 Inline suppression

Inline disable comments scope a suppression to a single line, a span, or a
single rule. Use this when the false positive is structural (a code identifier
in prose, a deliberate quoted phrase) and is unlikely to recur.

Block disable / enable around a span:

```markdown
<!-- textlint-disable -->

Paragraph that textlint would otherwise flag, kept verbatim for a reason.

<!-- textlint-enable -->
```

Per-rule disable for the same span:

```markdown
<!-- textlint-disable terminology -->

A paragraph where only the `terminology` rule should be silenced.

<!-- textlint-enable terminology -->
```

Single-line variant for a one-off match:

```markdown
<!-- textlint-disable-next-line terminology -->
The next line contains a deliberately styled term that the terminology rule misreads.
```

Prefer the per-rule form over the bare disable so unrelated rules continue to
fire.

### 2.2 File-scope ignores

`.textlintignore` uses gitignore-style patterns and excludes whole files from
linting. Use this for vendored or imported documents we cannot edit, lock
files, generated artifacts, and fixture content that must stay verbatim.

Current ignores live at the repository root (`.textlintignore`); add new
patterns alongside the existing entries. A new entry should include a brief
inline comment explaining why the file or directory is exempt.

This channel is wrong for one-off prose adjustments — those belong inline
(§2.1).

### 2.3 Dictionary additions (`prh.yml`)

`textlint-rule-prh` is driven by a project-specific dictionary that maps
incorrect or inconsistent variants to the canonical form. Use this when the
same term recurs across multiple files and the project has a single agreed
spelling, capitalisation, or hyphenation.

The dictionary file (`prh.yml`) and its review process are tracked under #84.
Until that issue lands, propose dictionary entries on the issue thread or in
the PR that needs them; once the process document exists, follow it. A
dictionary entry is the right channel when:

- The term is project-specific (not covered by `textlint-rule-terminology`).
- The same false positive (or the inverse — the same misuse the rule should
  catch) appears in two or more files.
- A single canonical form is uncontroversial.

A dictionary entry is **not** the right channel for one-off structural false
matches; use §2.1 instead.

### 2.4 Rule disabling or downgrading

`.textlintrc.json` controls which rules run and at what severity. Two
sub-channels:

- **Full disable.** Set the rule to `false` (or omit it) when the rule is
  consistently wrong for this project's corpus. This is the broadest possible
  suppression and should be a last resort.
- **Severity downgrade.** Set the severity to `"warning"` or `"info"` when
  the rule produces useful signal but a non-trivial false-positive rate
  warrants advisory-only treatment. The severity intent and its runtime
  semantics are defined in [`severity-policy.md`](severity-policy.md);
  downgrading does not silence the rule, it only changes how the gate-keeper
  adapter (#94) records the finding's severity.

Promotion in either direction (full disable to enabled, or warning back to
error) requires the same review process as any other `.textlintrc.json`
change.

---

## 3. Decision rubric

When a false positive appears, walk this decision tree from the top. The first
matching condition selects the channel; do not skip ahead.

1. **Recurring across many files AND project-specific term** → add to the
   project dictionary (§2.3, future `prh.yml`). Durable, scales, and keeps the
   canonical form discoverable.
2. **Single file, not a term, structural false match** (a code identifier in
   prose, a quoted command, a one-off styling choice) → inline
   `<!-- textlint-disable-next-line <rule> -->` (§2.1). Narrowest scope, no
   global side effects.
3. **Whole vendored or imported file the project does not own** → add a
   `.textlintignore` entry (§2.2) with an inline comment explaining the
   exemption.
4. **Rule is consistently noisy and wrong for this project's corpus** →
   downgrade or disable in `.textlintrc.json` (§2.4). Prefer downgrading to
   `"warning"` first; full disable only when the warning channel still
   produces no useful signal.
5. **Rule fires only once or twice across the corpus** → accept the warning,
   no exception needed. The cost of a project-level exception is higher than
   the cost of the noise.

Pick the narrowest channel that resolves the case. A dictionary entry that
would only ever match a single file should be an inline comment instead; a
`.textlintignore` line that would only exempt a single paragraph should be an
inline comment instead. Conversely, a string of inline comments that all
suppress the same rule for the same term is a signal to promote to a
dictionary entry.

---

## 4. PR-review process for proposing exceptions

There is no automated tooling for exception proposals (out of scope per the
issue body). Exceptions are reviewed in the same PR that introduces them.

Authoring requirements for the proposer:

- The PR commit message (or PR body) cites the false positive: file path,
  line number, and the original textlint message text.
- The exception itself carries a one-line motivation: an inline comment on
  the `<!-- textlint-disable-next-line -->` line, an entry comment on a
  `.textlintignore` rule, or a PR-body explanation for a `.textlintrc.json` /
  `prh.yml` change.
- The proposer states which channel they chose and why the lower-cost
  channels above it in §3 do not apply.

Reviewer checklist:

- Did the proposer try the lower-cost channel first? An inline comment that
  belongs in the dictionary, or a rule disable that should have been a
  warning downgrade, are both reasons to request changes.
- Is the dictionary the wrong place? Project-specific terms belong in
  `prh.yml`; structural false matches do not.
- Does the change leave a trail? Future readers should be able to identify
  why a given suppression exists from the comment alone.

---

## 5. What this policy does NOT cover

- **Concrete corpus exceptions.** Specific dictionary entries, ignore-file
  patterns, and rule-level overrides live in `prh.yml`,
  `.textlintignore`, and `.textlintrc.json` respectively, not here.
- **Automated exception-proposal tooling.** Out of scope per the #90 issue
  body.
- **Severity mapping at runtime.** Defined in
  [`severity-policy.md`](severity-policy.md).
- **Per-doc-type config decisions.** Defined in
  [`per-doc-type-policy.md`](per-doc-type-policy.md).
- **The dictionary content and authoring workflow.** Tracked under #84;
  reference `prh-process.md` once it lands.
