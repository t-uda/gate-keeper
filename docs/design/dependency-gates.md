# Design: Declarative Artifact-Dependency Gates

> Status: internal process artifact — not user-facing reference.

Tracking umbrella: #159. Coordinates children #156 (code↔doc / doc↔doc dependency gating) and #158 (paired-artifact existence/sync gates). This note is the joint contract that both children consume so that two divergent manifest designs do not have to be reconciled later.

> **Implementation status (first slice).** Issue #161 implements the first slice. Shipped:
>
> - This design note — load-bearing for #156 and #158.
> - `.gate-keeper/dependency-manifest.yml` — single edge from `src/gate_keeper/cli.py` to `docs/cli-reference.md`.
> - Loader at `scripts/dependency_gates/manifest.py` (graph schema, `pairs:` sugar, `mode: stamped` parsing).
> - Validator at `scripts/dependency_gates/check_cli_reference.py` wired through the existing `external_check` + `command` adapter (#149). No new `RuleKind`.
> - IR rule file `docs/dependency-gate-rules.json` exercisable via `gate-keeper validate --rules-format ir ... --allow-command-adapter`.
> - Loader and validator tests, including a `@pytest.mark.integration` end-to-end round-trip.
>
> **Issue #181 (Mode B slice).** Adds target-stamped freshness checking:
>
> - Stamp parser at `scripts/dependency_gates/_stamp.py` (frontmatter + canonical syntax, see §3.3).
> - Mode B branch in the validator emits one of `dependent_artifact_unaffected`, `dependent_artifact_stale_by_hash`, `target_stamp_missing`, `target_stamp_malformed`, `dependent_artifact_source_missing`, or `dependent_artifact_target_missing`.
> - Whole-file SHA-256 source hashing (no anchors); single source per target; target frontmatter required (no inline lines).
>
> **Out of slice (deferred):** PR-trailer ack transport, MCP validator transport, cycle-detection rule, in-repo #158 reference validator, automatic dependency discovery, any new `RuleKind`, opaque source anchors, multi-source-per-target stamp representation, automatic stamp rewriting, semantic equivalence between source and target, cross-repository source hashing. See §7 for the full out-of-scope list.

---

## 1. Problem Statement

Documents and paired artifacts drift because dependencies are implicit. Two parallel exploration issues identified the same gap from different angles:

- **#156** — code↔doc and doc↔doc dependencies (e.g. CLI implementation versus CLI reference). PR-driven affected-set computation; "dependent doc was reviewed but does not need an edit" evidence shape.
- **#158** — paired artifacts that should remain complete and synchronized: ja↔en docs, paper theorem ↔ Lean formalization, Lean theorem ↔ appendix explanation, source ↔ translation. Existence checks, sync metadata, and (separately) advisory semantic correspondence.

Both proposals converged on the same shape: a declarative manifest that expresses dependency edges, a deterministic gate enforced by `gate-keeper`, and a first slice routed through the trusted `command` external adapter (#149). The question is whether they share one manifest contract or fragment into two.

This note answers: **one manifest contract, two staleness modes, no new core rule kinds in slice 1.**

---

## 2. Settled Rules

The seven rules below are load-bearing. The slice-1 implementation honours them. Future slices may extend but must not contradict them.

### 2.1 Manifest schema is graph-shaped

Top-level `nodes` (`id`, `path`, `anchor`?, `kind`?) and `edges` (`from`-id, `to`-id, `relation`, `mode`?, `pair_id`?). A `pairs:` sugar form is accepted as input but the loader desugars it to nodes+edges before any consumer sees it.

A pair is just two nodes connected by an edge. A graph is the strict superset of a list of pairs. Choosing the superset gives one loader, one schema, one validation pass.

### 2.2 Anchor is opaque to the loader

A node's `anchor` is a string. The loader stores it verbatim and does not interpret it. Validators that consume an edge interpret the anchor in whatever vocabulary fits — Markdown heading, LaTeX label, Lean declaration, code symbol ID, regular-expression selector.

Anchor *resolution* is a per-validator concern. Pinning an anchor grammar in the manifest schema would conflate the manifest's contract (what edges exist) with each validator's contract (how it locates content within a file).

### 2.3 Edges are directed

Bidirectional relations (translation pairs, mutual references) lower to two directed edges sharing a `pair_id`. There is no `direction: bidirectional` field on an edge.

#156's primary use case (PR-affected-set traversal) is a reverse traversal of a directed graph. Mixing directed and bidirectional primitives in the same loader complicates traversal without expressive gain. Two directed edges with a shared `pair_id` recovers the bidirectional semantics where needed.

### 2.4 Two staleness modes coexist

Per-edge selection via `edge.mode`:

- **`affected_set`** (default, Mode A) — the validator consumes a changed-file set and emits diagnostics for dependents whose source changed. No target-side stamp required. Matches #156's primary use case.
- **`stamped`** (Mode B) — the target frontmatter records `tracks: <source-id>@<sha256>`; the validator compares against the current source. Matches #158's primary use case.

Both modes use the same manifest. The mode determines which check the validator runs on a given edge.

### 2.5 Ack transport is a file

Reviewer evidence that a source change was reviewed without requiring a target edit lives in `.gate-keeper/acks/<edge-id>.yml` with shape:

```yaml
edge_id: <edge id from manifest>
source_sha: <sha256 of source file content at ack time>
ack_by: <reviewer identifier>
ack_at: <ISO-8601 timestamp>
reason: <free-form rationale>
```

The validator accepts the ack only when `source_sha` matches the source file's current content sha. Stale acks (source has changed again since the ack) do not satisfy the gate.

PR-trailer transport is deferred. A reviewer-supplied file is durably committable, machine-readable, and reviewable in the diff. PR trailers add a transport without expressive gain in the first slice.

### 2.6 First slice adds NO new `RuleKind`

The slice uses `external_check` + `params.tool == "command"` (#149). A validator script reads the manifest and the changed-file set (or target frontmatter), and emits one `Diagnostic` per affected edge. Promotion of any check to a core rule kind is a slice-2 decision.

The existing dispatch primitive is sufficient. Adding `manifest_valid`, `dependent_artifacts_acknowledged`, or similar to `RuleKind` before any reference validator has been exercised would be premature.

### 2.7 Failure subtypes are evidence-record `kind` strings

Slice-1 vocabulary:

- `manifest_invalid` — schema/parse failure on the manifest itself.
- `manifest_target_missing` — declared `path` does not exist on disk.
- `dependent_artifact_unaffected` — source not in the changed set; nothing to do.
- `dependent_artifact_co_changed` — source changed and target also changed in the same set.
- `dependent_artifact_acked` — source changed, target unchanged, valid ack file present.
- `dependent_artifact_changed_without_target_update` — source changed, target unchanged, no valid ack.
- `dependent_artifact_stale_by_hash` — Mode B: target's `tracks:` digest does not match the source's current SHA-256. Emitted by the issue #181 Mode B implementation.
- `target_stamp_missing` — Mode B: target has no `tracks:` field in YAML frontmatter (issue #181).
- `target_stamp_malformed` — Mode B: frontmatter unparseable, `tracks:` not in canonical `<source-id>@sha256:<digest>` form, or stamp source-ID does not match the manifest source ID (issue #181).
- `dependent_artifact_source_missing` — Mode B: manifest source path absent on disk; emitted as `unavailable` (issue #181).
- `dependent_artifact_target_missing` — Mode B: manifest target path absent on disk; emitted as `unavailable` (issue #181).
- `ack_invalid` — ack file exists but cannot be parsed as a YAML mapping; emitted as FAIL so the author is not left wondering why a file they committed was silently ignored.
- `edge_not_applicable` — the validator was invoked on a target that does not appear in any edge; emitted as PASS so the rule is safe to enable on multiple targets.
- `changed_file_source_unresolved` — Mode A: Git unavailable or base ref unresolvable; emitted as `unavailable`.
- `stamped_mode_not_implemented` — historical from the slice-1-only era. Mode B is implemented as of issue #181; this evidence kind is retained for any future unrecognised mode value (defensive guard against direct construction outside the manifest loader's enum).

New subtypes are added as needed. `Status` enum values are unchanged; only the evidence vocabulary grows.

---

## 3. Manifest Schema

### 3.1 YAML shape

```yaml
nodes:
  - id: cli-implementation
    path: src/gate_keeper/cli.py
    kind: code             # optional, free-form
  - id: cli-reference
    path: docs/cli-reference.md
    kind: reference        # optional
    anchor: "## validate"  # optional, opaque

edges:
  - from: cli-implementation
    to: cli-reference
    relation: documents    # free-form
    # mode: affected_set   # default; explicit `stamped` opts into Mode B

pairs:                     # optional sugar; desugared on load
  - id: docs-foo-ja-en
    relation: translation
    source: ja-foo
    target: en-foo
```

A `pairs[].source` and `pairs[].target` reference node IDs. Each `pairs` entry produces two directed edges with `pair_id` set to `pairs[].id`.

### 3.2 JSON-Schema fragment

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "nodes": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "path"],
        "properties": {
          "id":     { "type": "string", "minLength": 1 },
          "path":   { "type": "string", "minLength": 1 },
          "anchor": { "type": "string" },
          "kind":   { "type": "string" }
        }
      }
    },
    "edges": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["from", "to", "relation"],
        "properties": {
          "from":     { "type": "string", "minLength": 1 },
          "to":       { "type": "string", "minLength": 1 },
          "relation": { "type": "string", "minLength": 1 },
          "mode":     { "enum": ["affected_set", "stamped"] },
          "pair_id":  { "type": "string" }
        }
      }
    },
    "pairs": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "source", "target", "relation"],
        "properties": {
          "id":       { "type": "string", "minLength": 1 },
          "relation": { "type": "string", "minLength": 1 },
          "source":   { "type": "string", "minLength": 1 },
          "target":   { "type": "string", "minLength": 1 },
          "mode":     { "enum": ["affected_set", "stamped"] }
        }
      }
    }
  }
}
```

The loader additionally enforces:

- node IDs are unique;
- every `edges[].from` and `edges[].to` references a known node ID;
- every `pairs[].source` and `pairs[].target` references a known node ID;
- `pair_id` set on a desugared edge equals the originating `pairs[].id`.

### 3.3 Canonical stamp syntax (Mode B)

A target whose incoming edge declares `mode: stamped` records the source revision it tracks via a YAML frontmatter `tracks:` field:

```markdown
---
tracks: <source-node-id>@sha256:<64-hex-digit-digest>
---
# CLI reference

...
```

Slice-1 (issue #181) constraints:

- **Frontmatter required.** The stamp must live in a YAML frontmatter block opened with `---` on the very first line and closed with `---` on its own line. Inline tracking lines outside frontmatter are deliberately ignored in this slice.
- **Whole-file source hashing.** The digest is `sha256(source_file_bytes)` exactly — opaque source anchors are deferred (see §7).
- **Algorithm prefix is mandatory.** Only `sha256:` is accepted; the prefix is part of the canonical syntax so future algorithms can coexist without changing the schema.
- **Lowercase 64-hex-digit digest.** Uppercase digests and short digests are rejected as `target_stamp_malformed`.
- **Single source per target.** A target with multiple incoming stamped edges must currently satisfy each independently, with no multi-source representation. Multi-source stamp aggregation is deferred (see §7).
- **`<source-node-id>` references the manifest `from` node ID.** A mismatch surfaces as `target_stamp_malformed` so the author is not left wondering why a stamp is silently rejected.

The validator emits one of the following Mode B evidence kinds (extends §2.7):

- `dependent_artifact_unaffected` (PASS, `mode: stamped`) — stamp's digest equals current source SHA-256.
- `dependent_artifact_stale_by_hash` (FAIL) — stamp present but digest does not match current source.
- `target_stamp_missing` (FAIL) — target has no `tracks:` field in frontmatter.
- `target_stamp_malformed` (FAIL) — frontmatter unparseable, `tracks:` value not in canonical syntax, or stamp source-ID does not match the manifest source ID.
- `dependent_artifact_source_missing` (UNAVAILABLE) — manifest source path does not exist on disk.
- `dependent_artifact_target_missing` (UNAVAILABLE) — manifest target path does not exist on disk.

The validator never auto-edits a stamp; on a stale or malformed stamp the diagnostic's `remediation` field carries the canonical replacement string.

---

## 4. Resolved Open Items

### 4.1 Manifest path

`.gate-keeper/dependency-manifest.yml`. Sits alongside `.gate-keeper/acks/`. The `external_check` rule passes `params.manifest` to the validator; absent, the validator falls back to this default.

### 4.2 Validator script location

`scripts/dependency_gates/`. One script per "view" (e.g. `check_cli_reference.py`) sharing a common loader (`manifest.py`) and helper (`_changed_files.py`). New views in future slices are new sibling scripts; the loader stays single-sourced.

### 4.3 Changed-file source: Mode A vs CLI target selection

There are now two distinct ownership models for the changed-file diff. Both use the same underlying `gate_keeper.changed.compute_changed_files` helper (promoted to core in #278).

**Mode A — validator-owned diff source (unchanged):** the dependency-gates command adapter scripts (`check_repo_refs.py`, `check_cli_reference.py`) compute `git diff --name-only ${GATE_KEEPER_BASE_REF:-origin/main}...HEAD` internally. The validator owns its diff source; no CLI flag is involved. This is the behaviour described in slice 1 and is unchanged by #278.

If Git is unavailable or the base ref does not resolve, the validator emits `Status.UNAVAILABLE` with evidence `kind: changed_file_source_unresolved`. Fail-closed; no "all files changed" fallback.

**CLI target selection (`--target-changed`) — caller-owned diff source (#278):** `validate --target-changed [--base-ref REF]` passes the changed set through the existing `resolve_targets` text-readable and 200-file-cap filter machinery as the initial candidate pool. The caller (the `validate` command) owns the diff source, not the individual rule backend. Combined with an explicit `--target`, the result is the intersection of the two sets. An empty filtered set is explicit evidence (`kind: changed_set_empty`), never a silent success.

`GATE_KEEPER_BASE_REF` may be set in CI or by the developer; default `origin/main` works for both PR-context CI and `git rebase`-style local workflows.

---

## 5. Failure-Mode Taxonomy

All evidence-record `kind` strings share one flat vocabulary namespace across all dependency-gate validators. No fork, no per-validator sub-namespace. §2.7 defines the base vocabulary; this section extends it with the kinds introduced by later slices.

Every validator always exits 0 per the `command` adapter contract (`docs/backend-external.md` § "command"). Pass/fail is carried by the `Diagnostic.status` field, not by the process exit code.

### 5.1 Base vocabulary (§2.7, all validators)

`manifest_invalid`, `manifest_target_missing`, `dependent_artifact_unaffected`, `dependent_artifact_co_changed`, `dependent_artifact_acked`, `dependent_artifact_changed_without_target_update`, `dependent_artifact_stale_by_hash`, `target_stamp_missing`, `target_stamp_malformed`, `dependent_artifact_source_missing`, `dependent_artifact_target_missing`, `ack_invalid`, `edge_not_applicable`, `changed_file_source_unresolved`, `stamped_mode_not_implemented`.

### 5.2 Manifest-coverage vocabulary (issue #280, `check_manifest_coverage.py`)

Emitted by the manifest-coverage validator. These kinds live in the same namespace as §5.1 — not a fork.

- `uncovered_file` — the target is in the changed-file set but has neither a manifest edge (as `from` or `to` node) nor an exemption entry in `.gate-keeper/ref-exemptions.yml`. Status: `fail`. `exemption_required` (issue #269 Layer 1 naming) is an equivalent label for the same state; this validator uses `uncovered_file` as the canonical kind.
- `covering_edge` — the target participates in at least one manifest edge. Status: `pass`. Data carries an `edges` array summarising each covering edge.
- `exemption_applied` — the target has an entry in the exemption file. Status: `pass`. Data carries the exemption `category` and `reason`.
- `params_error` — `target` payload is empty. Status: `unavailable`.

The `dependent_artifact_unaffected`, `manifest_invalid`, and `changed_file_source_unresolved` kinds from §5.1 are reused by this validator without redefinition.

---

## 6. Slice-2 Core Promotion Criterion

**Recommendation.** Promote `manifest_valid` and one affected-set rule kind to core IFF (a) at least one additional non-cli reference validator lands AND (b) Mode B (target-stamped) is exercised in CI.

A single additional validator surfaces the shared logic that a core rule kind would absorb. Mode B exercise in CI demonstrates the manifest carries its weight beyond #156's PR-affected-set use case. Without both signals, the `command` adapter remains the right level.

---

## 6.1. Repo-local reference scanner (issue #269, slice 1)

A second `command`-adapter validator lives alongside `check_cli_reference.py`:
`scripts/dependency_gates/check_repo_refs.py`. It is registered as the
`repo-local-reference-scanner` rule in `docs/dependency-gate-rules.json`.

The scanner walks one text-bearing target file (readme / docs / config /
rule file), extracts repo-local references via
`scripts/dependency_gates/_repo_ref_scan.py`, and classifies each one. It is
deterministic-only: no LLM, no semantic inference, no manifest-coverage
enforcement (that is a later slice — see #269 Layer 1).

Recognised reference shapes (extractor):

- Markdown inline link target `[text](path)` → `md_link`.
- Markdown inline image target `![alt](path)` → `md_image`.
- Markdown reference-style definition `[id]: path` → `md_ref_def`.
- Single-backtick inline code span whose body matches a tracked path shape
  (contains `/` AND either starts with a known top-level directory or
  ends with a known file extension) → `inline_code`.
- Bare path-like token prefixed with a known top-level directory
  (`src/`, `docs/`, `scripts/`, `tests/`, `.github/`, `.gate-keeper/`) →
  `bare_path`.

URL-shaped targets (scheme prefix, `//host`, `mailto:`), pure in-page
anchors (`#frag`), and fenced code blocks are intentionally ignored.

Evidence vocabulary (validator), one entry per category that fires:

- `broken_reference` — referenced repo-local path does not exist on disk.
  Status: `fail`. Carries a `references` array of `{path, line, column, shape}`.
- `uncovered_reference` — a repo-local reference appeared in the target,
  the target itself is in the changed-file set, the reference path is not
  co-changed alongside the target, and the reference is not already
  recorded as a manifest edge endpoint. The data carries
  `subkind: newly_introduced`. Status: `pass` (informational, slice 1).
- `suspicious_broad` — reference shape is intentionally broad: glob
  characters (`*` / `?`), trailing slash, or bare top-level directory
  (`docs`, `src`, …). Status: `pass` (informational, slice 1).
- `repo_refs_clean` — emitted when no finding category fires. Status: `pass`.
- `scanner_not_applicable` — emitted when the target is not a text-bearing
  file. Status: `pass`.

Fail-closed (always `Status.UNAVAILABLE`):

- `params_error` — empty `target` payload.
- `target_unreadable` — target file missing or undecodable.
- `manifest_invalid` — manifest load error (only when manifest is present).
- `changed_file_source_unresolved` — `git diff` cannot resolve the base
  ref. The scanner still emits any broken/broad findings; only the
  newly-introduced category is skipped, and an explicit evidence entry
  with this kind is appended so the consumer can distinguish "no new
  refs" from "did not check".

The validator runs at `severity: warning` in slice 1 (per the
`docs/dogfooding.md` § "Promotion criteria" path). The broken-reference
category produces `Status.FAIL` regardless; the other two deterministic
categories stay informational until a future slice promotes them.

---

## 6.2. Exemption file schema (issue #280)

The manifest-coverage validator (`check_manifest_coverage.py`) reads an
optional exemption file at `.gate-keeper/ref-exemptions.yml`. Absent ⇒ no
exemptions, not an error. Present but unreadable or malformed ⇒
`manifest_invalid`, fail-closed.

### YAML shape

```yaml
# .gate-keeper/ref-exemptions.yml
exemptions:
  - path: scripts/some_helper.py    # repo-relative POSIX path; required
    category: manual                 # required; see category values below
    reason: "bootstrap — tooling"   # optional free-form rationale
```

`path` must be a non-empty string matching the repo-relative POSIX path
exactly as it would appear in the `git diff --name-only` output.

### Category values

| Category | Meaning |
|---|---|
| `manual` | Owner or agent has explicitly acknowledged this path; no further check required. |
| `llm_required` | Reserved for Layer 2 (issue #281). Deterministic checks found an ambiguous relationship; LLM narrowing is deferred. Not exercised in this slice. |

### Fail-closed semantics

- File absent → zero exemptions, no error.
- File present but unreadable (OS error, encoding error) → `manifest_invalid`, `unavailable`.
- File present but not valid YAML → `manifest_invalid`, `unavailable`.
- Top-level not a mapping, or `exemptions` not a list → `manifest_invalid`, `unavailable`.
- Any entry missing `path`, `path` is empty, or `category` is unknown → `manifest_invalid`, `unavailable`.

### Bootstrap entries

The repository ships bootstrap exemptions in `.gate-keeper/ref-exemptions.yml`
covering validator scripts, test files, and configuration artifacts that are
intentionally not manifest nodes. Bootstrap entries use `category: manual` and
are counted in the `exemption_applied` evidence when the validator runs on them.

---

## 7. Out of Scope

This note settles slice 1. The following remain deferred:

- Automatic discovery of dependencies (no parser walks code or docs to infer edges).
- PR-trailer transport for acks.
- MCP transport for validators.
- Cycle-detection rule (`documentation_dependency_graph_acyclic` candidate from #156 stays deferred).
- Cross-repository graphs.
- An in-repo #158-family reference validator. The repository has no `*.ja.md` / `*.en.md` pair, no Lean files, and no in-repo formalization↔explanation pair. The #158 manifest shape is exercised at the loader-fixture level (bidirectional `pairs:` desugaring; `mode: stamped` parsing) until a real pair exists.
- Promoting any failure subtype to a `RuleKind`.
- Semantic equivalence checks (translation faithfulness, formalization correctness). These remain advisory and route to `semantic_rubric` if and when wired; this note does not couple to that path.

---

## 8. References

- Umbrella: #159
- Children: #156, #158
- Adapter substrate: #149 (`command` adapter); #92–#94 (`Backend.EXTERNAL` pattern under #80).
- Existing example: `docs/example-rules.md` §8 (project-local validator script).
- Adapter contract: `docs/backend-external.md` § "command".
