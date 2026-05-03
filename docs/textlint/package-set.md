# textlint initial package set — decision record (#81)

Tracking umbrella: #80. Configuration, severity policy, prh dictionary,
and CI workflow are deferred to #82, #85, #84, and #87 respectively.

---

## 1. Selection criteria

The governing principle from `CLAUDE.md`: **prefer deterministic backends over
LLM rubric whenever evidence is available.** For textlint this means:

**Include** a package when:

- Its rules are mechanically enforceable against text without human judgment.
- Default settings produce a low rate of false positives on a
  heterogeneous English-prose + Markdown corpus without project-specific tuning.
- The package is actively maintained and has a stable rule surface.

**Exclude or defer** a package when:

- Its rules are Japanese-specific and the corpus contains no Japanese text
  (see §5 below).
- It introduces style-guide choices that require significant per-project option
  tuning to avoid noisy alerts.
- It bundles other packages whose individual cost/benefit is unclear.
- It is domain-specific (e.g. engineering papers, SI units) and the corpus is
  document-type-heterogeneous; such rules belong in a per-doc-type config
  once #91 is settled.

Conservative selection is the correct default: it is easier to add a package
later (with data from #83) than to remove one after rules have been suppressed
throughout the corpus.

---

## 2. Decision matrix

| Package | Type | Status | Rationale | Risk of false positives |
|---------|------|--------|-----------|------------------------|
| `textlint-rule-prh` | Dictionary / terminology | **in** | Pure dictionary lookup; rules fire only on exact matches in `prh.yml`. Deterministic, zero false positives when the dictionary is correctly authored. Central to project-specific terminology control. | Very low — user-controlled dictionary. |
| `textlint-rule-terminology` | English brand/term spelling | **in** | Enforces consistent capitalisation of English technical terms (GitHub, JavaScript, npm, …). Deterministic, rule list is well-curated, minimal tuning needed on English prose. | Low — well-tested rule list; occasional conflicts with deliberate lower-case use (mitigated by `allowTerms` option). |
| `textlint-rule-preset-ja-spacing` | Japanese spacing conventions | **defer** | Corpus is English-only (see §5). All rules in this preset fire on Japanese-adjacent characters that do not appear in this repo. Zero value; all alerts would be false positives. Reopen: if Japanese prose is added to the corpus. | N/A — inapplicable corpus. |
| `textlint-rule-preset-japanese` | General Japanese writing | **defer** | Same rationale as ja-spacing. Corpus contains no Japanese text. Reopen: if Japanese prose is added to the corpus. | N/A — inapplicable corpus. |
| `textlint-rule-preset-ja-technical-writing` | Japanese tech writing | **defer** | Japanese-specific preset; intentionally ships rules that require per-project exception lists. Even if the corpus had Japanese text, this would require tuning before first use. Reopen: after Japanese corpus exists and #83 eval data is available. | High without tuning — acknowledged in #80 body. |
| `textlint-rule-preset-jtf-style` | JTF Japanese style guide | **defer** | Japanese-specific. #80 body notes "some style-guide items are difficult or impossible to enforce mechanically." Reopen: after Japanese corpus exists and after empirical evaluation under #83. | High — mechanical enforcement of style-guide items known to misfire. |
| `textlint-rule-preset-ja-engineering-paper` | Engineering paper (JA) | **out** | Japanese-specific and domain-specific (engineering papers). This repo is general-purpose developer documentation. Cross-reference #91: if per-doc-type configs are introduced and any documents are Japanese engineering papers, reconsider there. Reopen criterion: #91 introduces an engineering-paper document type with Japanese prose. | High — both Japanese-specific and paper-specific rules applied globally. |
| `textlint-rule-no-synonyms` | Japanese synonym drift | **out** | Depends on Sudachi Japanese synonym data; fires on Japanese terms only. No Japanese text in this corpus. No reopen path without Japanese prose. Reopen criterion: Japanese prose is added and #91 establishes a Japanese document type. | N/A — inapplicable corpus. |
| `textlint-rule-use-si-units` | SI unit formatting | **defer** | Potentially useful for technical docs, but the corpus contains no mathematical or physical-unit content today. Cross-reference #91: appropriate in a per-doc-type config for spec or engineering documents rather than globally. Reopen criterion: #91 introduces a document type where SI unit consistency is a stated project requirement. | Medium — SI rules applied globally to developer docs and changelogs will alert on legitimate informal usage. |

---

## 3. Recommended initial set

Two packages for the first config:

- **`textlint-rule-prh`** — project-specific terminology consistency via a
  `prh.yml` dictionary (content defined in #84). Deterministic, zero
  false positives under correct dictionary authorship.
- **`textlint-rule-terminology`** — English technical brand-term spelling.
  Immediately useful on an English-only corpus; no per-project tuning required
  for the default rule list.

This set is deliberately minimal. It delivers real, verifiable value on the
current corpus without requiring tuning, exception lists, or per-doc-type
configuration. Additional packages are candidates for a second wave after
empirical evaluation (#83) and per-doc-type config design (#91).

---

## 4. Rejected / deferred packages

| Package | Reopen criterion |
|---------|-----------------|
| `textlint-rule-preset-ja-spacing` | Japanese prose is added to the corpus. |
| `textlint-rule-preset-japanese` | Japanese prose is added to the corpus. |
| `textlint-rule-preset-ja-technical-writing` | Japanese corpus exists and #83 provides false-positive data on at least 50 documents. |
| `textlint-rule-preset-jtf-style` | Japanese corpus exists and #83 provides empirical mechanical-enforcement failure rate. |
| `textlint-rule-preset-ja-engineering-paper` | #91 introduces a Japanese engineering-paper document type. |
| `textlint-rule-no-synonyms` | Japanese prose is added and #91 defines a Japanese document type. |
| `textlint-rule-use-si-units` | #91 introduces a document type (e.g. spec, engineering note) where SI unit consistency is a stated requirement; apply in that type's config, not globally. |

---

## 5. Repo corpus consideration

**Language**: every authored Markdown file in this repository is English. The
full sample — `docs/*.md` (9 files), `README.md`, and `tests/fixtures/**/*.md`
(~20 files) — contains no Japanese characters. This is the primary driver for
deferring all Japanese-language packages.

**Document types present**:

- Developer documentation (`docs/backend-external.md`, `docs/rule-ir.md`,
  `docs/llm-rubric.md`, etc.) — prose + code blocks + tables.
- Spec / planning documents (`docs/mvp-spec.md`, `docs/issue-plan.md`) — prose
  + task lists.
- Example rule documents (`docs/example-rules.md`) — normative bullet lists.
- Test fixtures (`tests/fixtures/**/*.md`) — minimal synthetic documents for
  unit tests; not canonical prose.

**Implications**:

- No engineering papers, no scientific notation, no Japanese technical writing —
  the specialist presets add no value today.
- `textlint-rule-terminology` fires on English brand-term capitalisation, which
  is directly relevant to developer docs.
- `textlint-rule-prh` covers project-specific terms (`gate-keeper`, `Backend`,
  `LLM rubric`, etc.) that no off-the-shelf English rule will catch.
- Document-type heterogeneity (dev docs vs. specs vs. fixtures) supports a
  conservative global-config stance; domain-specific rules belong under #91.

---

## 6. What this decision does NOT settle

- **Actual config file** (`.textlintrc`, rule options, ignore paths) — #82.
- **Severity policy** (which rules are `error` vs `warning` vs `info`) — #85.
- **`prh.yml` content** (preferred terms, prohibited patterns, allowlists) — #84.
- **CI workflow** (GitHub Actions, changed-file checks, annotation formatter) — #87.
- **Per-doc-type config** (whether engineering-paper or SI-unit rules apply to
  any document type in this repo) — #91.
- **False-positive policy** (inline suppression, ignore-file conventions) — #90.
- **Empirical preset evaluation** on the existing corpus (data that will inform
  any future addition of deferred packages) — #83.
