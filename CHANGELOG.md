# Changelog

All notable changes to `gate-keeper` are documented here. The project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Per
[SemVer §4](https://semver.org/#spec-item-4), the pre-1.0 (`0.y.z`)
phase is initial development and backward compatibility is not yet
guaranteed; releases during this phase increment the patch version
regardless of whether they ship features, fixes, or tooling. Once the
public API stabilises and 1.0 ships, normal SemVer rules apply.

## [0.1.1] — 2026-05-12

This release consolidates the semantic rubric backend's quality work
(umbrella #63), the declarative artifact-dependency-gates expansion
(umbrella #159 / #163), and dogfooding-comment hygiene. No breaking
changes vs 0.1.0.

### Added

- LLM rubric: model-class-aware `reasoning_effort` defaults for gpt-5*
  and o-series models (#233 / PR #234).
- LLM rubric: multi-target context assembly slice 2 with prompt
  bumped v5→v6 (#225 / PR #228).
- LLM rubric: adaptive single→consensus escalation strategy (#186 / PR #219).
- LLM rubric: review strategy with two-pass primary+reviewer (#185 / PR #217).
- LLM rubric: consensus strategy with majority-vote aggregation (#184 / PR #213).
- LLM rubric: span-based evidence offsets for supporting quotes (#179 / PR #192).
- LLM rubric: deterministic `target_kind` mismatch precheck (#178 / PR #190).
- GitHub backend: `changed_file_policy` rule kind for real-data intake
  repos (#230 / PR #232).
- Classifier: non-binding textlint suggestion for textlint-suitable
  rules (#215 / PR #227).
- Bench: `--model-matrix` CLI flag (#205 / PR #216).
- Bench: cross-model llm-rubric matrix script (#177 / PR #195).
- Dependency gates: Mode B target-stamped freshness checks (#181 / PR #196).
- Dependency gates: five additional code→reference edges (#163 / PR #238).
- Scripts: one-off classifier-feedback sampler for gpt-5 routing audit
  (#235 / PR #236).

### Changed

- Dogfood advisory comment filters structurally-skipped UNSUPPORTED
  diagnostics (`target_kind_mismatch`, `llm_quote_fabrication`) into a
  collapsed `<details>` summary instead of the main table (#240 / PR #241).
- Dogfood advisory comment is now upserted on a sticky marker and
  suppressed on fallback output to avoid PR-comment spam (PR #239).
- Dogfood validate step passes the actual PR body as `--target` and
  `--artifact-kind pr_description` (#189 / PR #220, #198 / PR #201).
- LLM rubric: strip filename from prompt when `--artifact-kind` is
  declared (#191 / PR #193).
- LLM rubric: parser fallback strips markdown code fences (#194 / PR #203).
- LLM rubric: `LlmJudgment` migrated from dataclass to Pydantic model
  (#187 / PR #207).

### Testing

- LLM-rubric test suite is now hermetic by default; live-provider tests
  are gated behind the `live_llm` pytest marker (#180 / PR #229).
- Bench fixture eval threads `artifact_kind` through dispatch (#204 / PR #211).
- Dogfood advisory-comment formatter has unit tests for the
  structural-skip filter (#240 / PR #241).

### Documentation

- Adapter authoring guide for Vale/ESLint-style external tools (#214 / PR #218).
- Semantic-rules backend-choice decision tree appendix (#208 / PR #210).
- Textlint info-severity mapping cross-link (#209 / PR #212).
- Retrospective on v3→v4 LLM-rubric prompt regression (#199 / PR #202).
- Local-LLM strategy profile contract (#188 / PR #200).
- Dogfooding doc cross-references the structural-skip advisory filter
  (#240 / PR #241).

### Known issues (not blocking 0.1.1)

- #242 — Dogfood rule L25 (`commit_message`) is structurally UNSUPPORTED
  on every PR run because the workflow validates `pr_description` only.
  The advisory-comment noise is suppressed; the rule-pool design
  follow-up is tracked separately.
- #243 — Dogfood rule L24 (testing-method-stated) frequently surfaces
  `llm_quote_fabrication` on gpt-4o-mini; the closed escalation work
  (#186) does not rescue fabrication by design. Measurement + remediation
  tracked separately.

## [0.1.0]

Initial scaffold of the gate-keeper compiler pipeline (not tagged).
Establishes the rule extractor, IR, backend router, and the
`filesystem`, `github`, `llm-rubric`, and `external` backends. The
self-gating dogfood loop runs against the project's own PRs in advisory
mode; promotion criteria live in `docs/dogfooding.md`.
