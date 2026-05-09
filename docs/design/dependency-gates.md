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
> **Out of slice (deferred):** PR-trailer ack transport, MCP validator transport, cycle-detection rule, in-repo #158 reference validator, automatic dependency discovery, any new `RuleKind`. See §7 for the full out-of-scope list.

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

Top-level `nodes` (id, path, anchor?, kind?) and `edges` (from-id, to-id, relation, mode?, pair_id?). A `pairs:` sugar form is accepted as input but the loader desugars it to nodes+edges before any consumer sees it.

A pair is just two nodes connected by an edge. A graph is the strict superset of a list of pairs. Choosing the superset gives one loader, one schema, one validation pass.

### 2.2 Anchor is opaque to the loader

A node's `anchor` is a string. The loader stores it verbatim and does not interpret it. Validators that consume an edge interpret the anchor in whatever vocabulary fits — Markdown heading, LaTeX label, Lean declaration, code symbol id, regex selector.

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
- `dependent_artifact_stale_by_hash` — Mode B: target's `tracks:` sha does not match the source's current sha.
- `edge_not_applicable` — the validator was invoked on a target that does not appear in any edge; emitted as PASS so the rule is safe to enable on multiple targets.
- `changed_file_source_unresolved` — Mode A: git unavailable or base ref unresolvable; emitted as `unavailable`.

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

A `pairs[].source` and `pairs[].target` reference node ids. Each `pairs` entry produces two directed edges with `pair_id` set to `pairs[].id`.

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

- node ids are unique;
- every `edges[].from` and `edges[].to` references a known node id;
- every `pairs[].source` and `pairs[].target` references a known node id;
- `pair_id` set on a desugared edge equals the originating `pairs[].id`.

---

## 4. Resolved Open Items

### 4.1 Manifest path

`.gate-keeper/dependency-manifest.yml`. Co-located with `.gate-keeper/acks/`. The `external_check` rule passes `params.manifest` to the validator; absent, the validator falls back to this default.

### 4.2 Validator script location

`scripts/dependency_gates/`. One script per "view" (e.g. `check_cli_reference.py`) sharing a common loader (`manifest.py`) and helper (`_changed_files.py`). New views in future slices are new sibling scripts; the loader stays single-sourced.

### 4.3 Changed-file source for Mode A

The validator computes `git diff --name-only ${GATE_KEEPER_BASE_REF:-origin/main}...HEAD` internally via `subprocess.run(..., shell=False)`. No new CLI flag in slice 1.

If git is unavailable or the base ref does not resolve, the validator emits `Status.UNAVAILABLE` with evidence `kind: changed_file_source_unresolved` rather than treating "all files changed" as a fallback. Fail-closed.

`GATE_KEEPER_BASE_REF` may be set in CI or by the developer; default `origin/main` works for both PR-context CI and `git rebase`-style local workflows.

---

## 5. Failure-Mode Taxonomy

See §2.7 for the evidence-record `kind` vocabulary. Every diagnostic the validator emits carries exactly one evidence record from that vocabulary. The validator never emits multiple evidence records per diagnostic in slice 1 — one edge, one verdict, one evidence kind.

The validator always exits 0 per the `command` adapter contract (`docs/backend-external.md` § "command"). Pass/fail is carried by the `Diagnostic.status` field, not by the process exit code.

---

## 6. Slice-2 Core Promotion Criterion

**Recommendation.** Promote `manifest_valid` and one affected-set rule kind to core IFF (a) at least one additional non-cli reference validator lands AND (b) Mode B (target-stamped) is exercised in CI.

A single additional validator surfaces the shared logic that a core rule kind would absorb. Mode B exercise in CI demonstrates the manifest carries its weight beyond #156's PR-affected-set use case. Without both signals, the `command` adapter remains the right level.

---

## 7. Out of Scope

This note settles slice 1. The following remain deferred:

- Automatic discovery of dependencies (no parser walks code or docs to infer edges).
- PR-trailer transport for acks.
- MCP transport for validators.
- Cycle-detection rule (`documentation_dependency_graph_acyclic` candidate from #156 stays deferred).
- Cross-repository graphs.
- An in-repo #158-family reference validator. The repo has no `*.ja.md` / `*.en.md` pair, no Lean files, and no in-repo formalization↔explanation pair. The #158 manifest shape is exercised at the loader-fixture level (bidirectional `pairs:` desugaring; `mode: stamped` parsing) until a real pair exists.
- Promoting any failure subtype to a `RuleKind`.
- A new CLI flag for the changed-file set; the validator owns its diff source.
- Semantic equivalence checks (translation faithfulness, formalization correctness). These remain advisory and route to `semantic_rubric` if and when wired; this note does not couple to that path.

---

## 8. References

- Umbrella: #159
- Children: #156, #158
- Adapter substrate: #149 (`command` adapter); #92–#94 (`Backend.EXTERNAL` pattern under #80).
- Existing example: `docs/example-rules.md` §8 (project-local validator script).
- Adapter contract: `docs/backend-external.md` § "command".
