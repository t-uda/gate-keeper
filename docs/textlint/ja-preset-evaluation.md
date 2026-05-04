# textlint Japanese preset evaluation — decision record (#83)

Tracking umbrella: #80. This evaluation informs future additions to `.textlintrc`
when Japanese prose is added to the corpus. Configuration file authoring is #82;
severity policy is #85.

---

## 1. Evaluated presets

### 1.1 `textlint-rule-preset-ja-technical-writing`

**Purpose**: Enforces rules derived from the JTF (Japan Translation Federation)
style guide for Japanese technical writing. Covers sentence length, kanji/kana
ratio, prohibited expressions, punctuation normalisation, and more.

**Rule list (defaults)**:

| Rule | Default setting | Category |
|------|----------------|----------|
| `max-ten` | ≤ 3 `、` per sentence | Punctuation |
| `max-comma` | ≤ 3 `,` per sentence | Punctuation |
| `max-kanji-continuous-len` | ≤ 6 consecutive kanji | Readability |
| `arabic-kanji-numbers` | warn on kanji numbers where Arabic numerals preferred | Normalisation |
| `no-mix-dearu-desumasu` | prohibit mixed dearu/desumasu register | Register |
| `ja-no-mixed-period` | require `。` at sentence end (not `.`) | Punctuation |
| `no-double-negative-ja` | prohibit double negation in Japanese | Clarity |
| `no-dropping-the-ra` | prohibit ら-dropping (ら抜き言葉) | Grammar |
| `no-doubled-conjunction` | prohibit adjacent identical conjunctions | Clarity |
| `no-doubled-joshi` | prohibit adjacent identical particles | Grammar |
| `no-nfd` | prohibit NFD-normalised characters | Encoding |
| `no-invalid-control-character` | prohibit ASCII control characters | Encoding |
| `no-zero-width-spaces` | prohibit U+200B etc. | Encoding |
| `ja-no-weak-phrase` | warn on vague hedging phrases | Clarity |
| `ja-no-successive-word` | prohibit same word repeated immediately | Clarity |
| `ja-no-abusage` | flag common Japanese abusage | Grammar |

**False-positive tendency on English corpus**:

Most rules fire exclusively on Unicode characters in the CJK range. An
English-only document triggers zero rules from this preset. The encoding-safety
rules (`no-nfd`, `no-zero-width-spaces`, `no-invalid-control-character`) apply
universally, but identical protection is available through simpler means (editor
config, git hooks) and these rules have negligible false-positive risk on ASCII
text.

**False-positive tendency on mixed corpus (English + incidental Japanese)**:

`ja-no-mixed-period` fires when a Japanese sentence ends with `.` instead of
`。`. In documents that quote Japanese examples or names inside otherwise-English
prose, this rule produces consistent false positives because the surrounding
English context justifies `.` as the period.

`no-mix-dearu-desumasu` is highly context-sensitive. A document fragment copied
from a Japanese source may mix registers legitimately (e.g., quotations); the
rule cannot distinguish intentional mixing from authoring error.

`max-ten` and `max-comma` count occurrences in the entire paragraph regardless
of language. If a paragraph contains a Japanese list notation alongside English
prose, the count may exceed the threshold without an actual style problem.

---

### 1.2 `textlint-rule-preset-ja-spacing`

**Purpose**: Enforces spacing conventions around CJK characters — specifically
ensuring a half-width space between full-width CJK and half-width ASCII.

**Rule list (defaults)**:

| Rule | Default setting | Category |
|------|----------------|----------|
| `ja-space-between-half-and-full-width` | require space at CJK–ASCII boundary | Spacing |
| `ja-space-around-code` | require space around inline code in Japanese context | Spacing |
| `ja-space-after-exclamation` | require space after `！` | Spacing |
| `ja-space-after-question` | require space after `？` | Spacing |
| `ja-no-space-between-full-width` | prohibit space between two full-width chars | Spacing |

**False-positive tendency on English corpus**:

No false positives on ASCII-only content. Rules inspect full-width Unicode
ranges; English text passes silently.

**False-positive tendency on mixed corpus**:

`ja-space-between-half-and-full-width` is the highest-risk rule. In mixed
documents, a Japanese product name adjacent to an English parenthetical
(e.g., `ゲートキーパー(gate-keeper)`) will alert if no space precedes `(`.
This is correct per the JTF convention but often looks unnatural to English
readers. The rule requires per-occurrence suppression in mixed documents.

`ja-space-around-code` fires when an inline code span `` `npm install` `` is
embedded inside a Japanese sentence without a surrounding space. In mixed
documents this is a recurring pattern and the alerts are valid but numerous.

---

### 1.3 `textlint-rule-ja-no-mixed-period`

**Purpose**: Standalone rule (also bundled inside `textlint-rule-preset-ja-technical-writing`
as the `ja-no-mixed-period` sub-rule) that enforces a single period style —
either `。` (Japanese) or `.` (Western) — within a document. Referencing it as
a standalone package allows stricter version pinning or enabling it in isolation
without activating the full preset.

**Rule list**: Single rule; configurable via `periodMark` option (`。` default).

**False-positive tendency on English corpus**: None. Fires only on CJK sentence
boundaries.

**False-positive tendency on mixed corpus**:

High false-positive risk when a document contains both Japanese prose (expecting
`。`) and English prose or code comments (expecting `.`). The rule does not
support per-language per-paragraph mode; it applies the same `periodMark` to the
entire file. In a mixed document, sentences whose language convention differs
from the configured `periodMark` will trigger alerts. English paragraphs and code
comments use `.` and do not contain CJK sentence boundaries, so the rule fires
only on Japanese sentences ending with the non-configured mark — but the inverse
is also true if `periodMark` is set to `.`. The practical result is that one
language's convention must be suppressed via inline disable comments or file
exclusion.

---

## 2. Scenario analysis

### Scenario A: English corpus with incidental Japanese text

**Definition**: The primary language is English. Japanese appears only in:
quoted names or terms, example strings, file paths, or very short annotations.
Japanese prose paragraphs are absent.

**Recommended rule set**:

- **Enable** `no-nfd`, `no-zero-width-spaces`, `no-invalid-control-character`
  (from `textlint-rule-preset-ja-technical-writing`, or individually) —
  low-noise encoding hygiene applicable to all text.
- **Disable** all other rules from the three presets above. Every other rule
  fires on Japanese sentence structure, punctuation style, or CJK spacing
  conventions; applying them to incidental terms produces only noise.
- `textlint-rule-ja-no-mixed-period` — **disable**; the document is not a
  Japanese document and the rule cannot be configured to ignore English blocks.

**Rationale**: This repo's current corpus falls into Scenario A (see
`docs/textlint/package-set.md §5`). The encoding rules are the only universally
safe additions; all stylistic rules assume a primarily-Japanese document and
should wait for Scenario B evidence.

### Scenario B: Pure Japanese documents

**Definition**: The document is authored entirely in Japanese. English appears
only in code spans, variable names, product names, and similarly non-prose
contexts.

**Recommended rule set**:

- **Enable** `textlint-rule-preset-ja-technical-writing` as a whole, with the
  following tuning applied before first use:
  - `ja-no-mixed-period`: set `periodMark: "。"` explicitly (it is the default;
    document it to prevent silent override).
  - `max-ten`: consider raising from 3 to 4 for technical content, which
    legitimately uses more enumeration than narrative prose.
  - `max-kanji-continuous-len`: consider raising from 6 to 10 for technical
    docs, where compound nouns of 7–9 kanji are common (e.g., `仮想化基盤環境構成`
    at 9 chars).
  - `no-mix-dearu-desumasu`: enable, but add a project-wide allow-list for
    quotations that explicitly switch register.
- **Enable** `textlint-rule-preset-ja-spacing` as a whole — the spacing rules
  are well-defined and low-risk for pure Japanese documents.
- **Enable** `textlint-rule-ja-no-mixed-period` with `periodMark: "。"`.

**False-positive suppression notes**:

- Inline code spans (`` ` `` … `` ` ``) are excluded by textlint's AST
  traversal by default; code content does not trigger prose rules.
- Product names in katakana (e.g., `ゲートキーパー`) are not affected by
  `no-dropping-the-ra` or `no-doubled-joshi`.
- The highest residual false-positive source in Scenario B is `max-kanji-continuous-len`
  for technical terms. For example, `仮想化基盤環境構成` is 9 consecutive kanji and
  exceeds both the default limit of 6 and a raised limit of 8. Terms of this
  length appear routinely in infrastructure documentation. Consider raising the
  limit to 10 for technical docs, or adding per-term inline disable comments for
  compound nouns that cannot be split without losing technical precision.

---

## 3. Rule-by-rule summary

| Rule / Preset | English corpus | Mixed corpus | Pure JA corpus | Recommended action |
|---|---|---|---|---|
| `preset-ja-technical-writing` (whole) | No hits | Noisy | Valid | Scenario B only; tune before enabling. |
| `preset-ja-spacing` (whole) | No hits | High FP risk | Valid | Scenario B only. |
| `ja-no-mixed-period` | No hits | High FP risk | Valid | Scenario B only; set `periodMark: "。"`. |
| `no-nfd` | No hits | No hits | No hits | Safe globally; add to any config. |
| `no-zero-width-spaces` | No hits | No hits | No hits | Safe globally; add to any config. |
| `no-invalid-control-character` | No hits | No hits | No hits | Safe globally; add to any config. |
| `max-ten` | No hits | Occasional over-count | Valid | Scenario B; consider raising to 4 for tech. |
| `max-kanji-continuous-len` | No hits | No hits | Valid | Scenario B; consider raising to 10 for tech. |
| `no-mix-dearu-desumasu` | No hits | Context-dependent | Valid | Scenario B with allow-list. |
| `ja-space-between-half-and-full-width` | No hits | High FP at boundaries | Valid | Scenario B only. |

---

## 4. Documents that would trigger the most noise

If the three presets were applied to this repo's current corpus without
suppression:

| Document | Noise source | Volume estimate |
|---|---|---|
| All current `docs/*.md` files (excl. textlint eval) | No Japanese chars → zero hits | 0 |
| `README.md` | No Japanese chars → zero hits | 0 |
| `tests/fixtures/semantic/targets/*.md` | No Japanese chars → zero hits | 0 |
| `docs/textlint/package-set.md` | Preset names in English prose → zero hits | 0 |
| `docs/textlint/ja-preset-evaluation.md` (this file) | Contains Japanese examples and terms | >0 |

**Conclusion**: the existing corpus (excluding this evaluation document) produces
zero true hits and zero false positives from all three presets. This confirms
`package-set.md §5`: the presets deliver no value for the current English-only
corpus.

This evaluation document itself now contains Japanese text (rule names, example
strings, katakana product names) and would generate alerts if the presets were
applied to it. It should be excluded from the textlint Japanese-preset pass or
treated as a Scenario A document — see §2 (Scenario A) and the forthcoming
`.textlintignore` configuration in #82.

The noise forecast is for **future** Japanese-language documents. If Japanese
prose is added to `docs/`, every file containing full-width CJK text will begin
generating hits, with the highest noise expected from `ja-no-mixed-period` (in
mixed documents) and `ja-space-between-half-and-full-width` (at CJK–ASCII
boundaries).

---

## 5. Recommended overrides (for future adoption)

When Japanese prose is added and the presets are enabled for the first time, the
following overrides should be applied in `.textlintrc`.

The overrides below show rule-level tuning; file-path scoping is handled
separately. textlint's flat `rules` object does not natively scope by file glob
— file targeting is achieved through `.textlintignore` (to exclude files from
the whole run) or separate `textlint` invocations with `--rulesdir` or `--rule`
flags (to run only the Japanese rules against a filtered file set). The config
authoring in #82 will wire up the scoping mechanism; this snippet records only
the tuned rule options.

```jsonc
// Rule-level overrides for Scenario B (pure Japanese documents).
// This block applies globally if placed in the root .textlintrc; file-glob
// scoping via .textlintignore or separate invocations is required to restrict
// these rules to Japanese-language documents — see #82.
{
  "rules": {
    "preset-ja-technical-writing": {
      "max-ten": { "max": 4 },
      "max-kanji-continuous-len": { "max": 10 },
      "no-mix-dearu-desumasu": {
        "preferInHeader": "",
        "preferInBody": "である",
        "preferInList": "である",
        "strict": false
      }
    },
    "preset-ja-spacing": true,
    "ja-no-mixed-period": { "periodMark": "。" }
  }
}
```

File-scope disabling (via `.textlintignore` or inline disable comments) will be
needed for mixed English/Japanese documents. This is the responsibility of the
false-positive suppression policy (#80-10) rather than this evaluation.

---

## 6. What this evaluation does NOT settle

- **Actual `.textlintrc` changes** — #82. This evaluation documents evidence;
  config authoring is a separate deliverable.
- **Severity policy** — #85. Whether a Japanese-preset alert is `error` or
  `warning` depends on the severity framework, not on which rules are enabled.
- **prh.yml Japanese entries** — #84. Project-specific Japanese terminology
  (e.g. preferred katakana forms) is a dictionary concern.
- **CI workflow** — #87. Which textlint invocation covers which file set.
- **False-positive suppression** — #80-10. Per-file or per-occurrence inline
  disabling for legitimate exceptions.
