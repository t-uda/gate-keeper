# Design: Hybrid Rule Kind — Deterministic Precheck + Semantic Judgment

Tracking: #73 (design phase). Implementation tracked under umbrella #63.

---

## 1. Problem Statement

Every `Rule` in the current IR carries a single `backend_hint` and routes to exactly
one backend. A rule like "PR description is free of textlint-prohibited terminology
AND explains the why" cannot be expressed today without splitting it into two
independent rules, which loses crucial coupling semantics:

- Two rules with the same topic produce two separate `Diagnostic` entries. There is
  no first-class relationship between them; reporters, dashboards, and humans must
  manually infer that they concern the same artifact.
- Severity and short-circuit are undefined: should the LLM judgment run (and consume
  tokens) when the deterministic precheck already failed? Two separate rules provide
  no mechanism to express that dependency.
- Evidence is siloed per-diagnostic. A reviewer reading output cannot tell whether the
  semantic judgment was informed by the same textlint violation that caused the
  deterministic fail.

### Why two-rule composition is insufficient

Composing two rules at the _policy_ level (e.g., "rule A must pass AND rule B must
pass") is isomorphic to a flat AND of two independent checks. It solves the severity
issue only superficially and does not address: evidence aggregation into a single
diagnostic, short-circuit with token-spend control, or the semantics that the LLM
judgment context should include knowledge of what the deterministic precheck found.
The hybrid kind encodes the relationship at the IR level, where the orchestrator can
reason about it deterministically.

---

## 2. Canonical Motivating Example

> PR description: free of textlint-prohibited terminology AND explains the why

The textlint adapter (landed in #80 via `Backend.EXTERNAL` + adapter pattern) checks
terminology deterministically. The "explains the why" predicate has no deterministic
expression — it requires semantic judgment. These two predicates belong to one rule
because together they define the bar for an acceptable PR description; neither alone
is sufficient.

A textlint FAIL means the PR description contains prohibited terminology. Spending
LLM tokens to evaluate whether the description "explains the why" before the author
has fixed the terminology is wasteful and misleading. The hybrid kind enables
short-circuiting: run the deterministic precheck first; proceed to the semantic
judgment only if the precheck passes (default behaviour; overridable).

---

## 3. Design Questions and Recommendations

### 3.1 Two-rule composition vs. new IR kind?

**Recommendation: new IR kind (`hybrid_check`).**

Two-rule composition at the policy layer would require the orchestrator to understand
a cross-rule dependency graph, maintain inter-rule state, and decide short-circuit
policy outside the IR. That complexity belongs in the rule definition, not in the
orchestrator's ad-hoc graph resolution.

A dedicated `RuleKind.HYBRID_CHECK` keeps the unit of work — one rule, one `Diagnostic`
— intact. The IR remains a flat list; the orchestrator needs no graph traversal. The
hybrid rule's `params` carries the full sub-rule specifications for both stages, and
the orchestrator handles sequencing internally.

Trade-off: adding a new `RuleKind` value is a minor IR version bump. However, the
existing `from_dict` strict-rejection policy already mandates that consumers handle
unknown enum values as errors, so a bump is preferable to encoding a graph in params.

### 3.2 Evidence aggregation

**Recommendation: concatenate evidence lists, label by stage.**

Each sub-backend contributes its `Evidence` items, prefixed with a `stage` key added
by the hybrid dispatcher (not by the adapter, which remains unaware of the hybrid
context):

```
evidence = [
  {"kind": "hybrid_precheck", "data": {"stage": "deterministic", "tool": "textlint",
                                        "status": "pass", ...adapter evidence...}},
  {"kind": "hybrid_semantic",  "data": {"stage": "semantic",       "model": "...",
                                        "judgment": "pass", "explanation": "..."}}
]
```

Ordering: deterministic evidence always precedes semantic evidence, regardless of
execution order. This is stable for consumers and mirrors the logical dependency
(deterministic is the precondition).

When short-circuit fires, the semantic evidence entry is absent (not null-filled).
The top-level `Diagnostic.message` should note that semantic judgment was skipped.

### 3.3 Severity arithmetic — all four quadrants

The hybrid rule carries a single `severity` (echoed from the rule definition). Status
is determined by combining sub-results:

| Deterministic | Semantic | Hybrid Status | Notes |
|-------------- |--------- |-------------- |------ |
| PASS          | PASS     | PASS          | Both conditions satisfied. |
| PASS          | FAIL     | FAIL          | Terminology clean but intent absent. |
| FAIL          | PASS     | FAIL          | Prohibited terminology used. Semantic judgment is moot. |
| FAIL          | FAIL     | FAIL          | Both conditions violated. |

A FAIL from either stage is sufficient to FAIL the hybrid rule. The `Diagnostic.status`
carries the combined verdict; individual stage statuses are readable from `evidence`.

Special cases:

- Deterministic `UNAVAILABLE` or `ERROR`: propagate as `UNAVAILABLE` on the hybrid
  diagnostic; do not proceed to semantic. Rationale: fail-closed.
- Semantic `UNAVAILABLE` when deterministic PASSED: propagate `UNAVAILABLE` on the
  hybrid diagnostic. Rationale: fail-closed; the rule cannot be fully evaluated.
- Deterministic `PASS` + semantic `UNAVAILABLE` with `severity = advisory`: propagate
  `UNAVAILABLE`. Advisory severity is never a reason to suppress unavailability.

### 3.4 Short-circuit policy

**Recommendation: short-circuit on deterministic FAIL by default; allow opt-out per rule.**

Default (`params.short_circuit = true`): if the deterministic precheck returns any
non-PASS status, skip the semantic judgment. The combined `Diagnostic` reflects the
deterministic status and notes that semantic evaluation was skipped to avoid token
spend.

Override (`params.short_circuit = false`): both backends run unconditionally. Useful
when audit logs require full evidence from both stages regardless of precheck outcome,
or when the semantic judgment is cheap (cached, local model).

The override must be explicit in the rule's `params`; there is no global default
override mechanism. This keeps behaviour visible in the compiled IR rather than in
runtime configuration.

### 3.5 Source phrasing in Markdown

**Recommendation: inline YAML front-matter block within the rule list item.**

Rules are currently authored as bullet-list items in Markdown. A hybrid rule uses an
extended syntax with a fenced annotation block immediately following the bullet, parsed
by the classifier as a sub-rule specification:

```markdown
## PR Description Checks

- PR description must be free of prohibited terminology and explain the rationale
  behind the change.

  ```gate-keeper
  kind: hybrid_check
  severity: error
  short_circuit: true
  precheck:
    backend: external
    tool: textlint
    config: .textlintrc
  semantic:
    backend: llm-rubric
    rubric: "The PR description explains *why* the change was made, not just what."
  ```
```

The fenced block uses the `gate-keeper` language tag so Markdown renderers treat it
as a code block (no special rendering needed) while the compiler detects and parses it.
Without the fenced block the rule falls through to normal single-backend classification.

This syntax is additive: existing single-backend rules require no annotation block.

---

## 4. IR Sketch

The following illustrates the proposed `Rule` extension. **Do not apply this to
`src/gate_keeper/models.py`** — it is illustrative only. See `docs/rule-ir.md` for
the current schema.

```python
# Proposed addition to RuleKind (models.py — illustrative only)
class RuleKind(str, enum.Enum):
    # ... existing values ...
    HYBRID_CHECK = "hybrid_check"   # new

# Proposed sub-rule specification (new dataclass — illustrative only)
@dataclass(frozen=True)
class HybridStage:
    """One stage within a hybrid rule."""
    backend: Backend       # which backend handles this stage
    params: dict[str, Any] # forwarded verbatim to the backend (tool, rubric, etc.)

# Rule.params layout when kind == hybrid_check (illustrative only)
# {
#   "short_circuit": bool,          # default True
#   "precheck": {                   # deterministic stage
#     "backend": "external",
#     "tool": "textlint",
#     "config": ".textlintrc",      # adapter-specific key
#   },
#   "semantic": {                   # semantic stage
#     "backend": "llm-rubric",
#     "rubric": "The PR description explains *why*...",
#   }
# }
#
# Rule.backend_hint = Backend.LLM_RUBRIC (highest-cost backend; used for routing
# decisions that need a single hint, e.g. --skip-llm flags).
```

The `backend_hint` on a hybrid rule is set to the highest-cost backend present
(`LLM_RUBRIC` takes precedence over `EXTERNAL`, which takes precedence over `FILESYSTEM`
or `GITHUB`). This lets existing `--backend` flag semantics skip the whole hybrid rule
when the flag would skip the most expensive stage.

---

## 5. Backend Dispatch Sketch

```python
# Pseudocode — illustrative only; no production code changed

def dispatch_hybrid(rule: Rule, target: str | Path) -> Diagnostic:
    """Orchestrate a hybrid_check rule against target."""
    assert rule.kind is RuleKind.HYBRID_CHECK

    short_circuit: bool = rule.params.get("short_circuit", True)
    precheck_params: dict = rule.params["precheck"]
    semantic_params: dict = rule.params["semantic"]

    # Build synthetic single-stage rules from the hybrid rule's sub-specs.
    precheck_rule = _make_sub_rule(rule, precheck_params)
    semantic_rule = _make_sub_rule(rule, semantic_params)

    # --- Stage 1: deterministic precheck ---
    pre_diag = backends.get(precheck_params["backend"])(precheck_rule, target)
    pre_evidence = _label_evidence(pre_diag.evidence, stage="deterministic")

    if short_circuit and pre_diag.status is not Status.PASS:
        # Deterministic stage non-PASS: return without spending tokens.
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,   # canonical backend for hybrid
            status=pre_diag.status,
            severity=rule.severity,
            message=(
                f"[precheck {pre_diag.status.value}] {pre_diag.message} "
                "(semantic judgment skipped)"
            ),
            evidence=pre_evidence,
            remediation=pre_diag.remediation,
        )

    # --- Stage 2: semantic judgment ---
    sem_diag = backends.get(semantic_params["backend"])(semantic_rule, target)
    sem_evidence = _label_evidence(sem_diag.evidence, stage="semantic")

    # Combine: either FAIL dominates.
    combined_status = _combine_status(pre_diag.status, sem_diag.status)
    combined_message = _combine_message(pre_diag, sem_diag, combined_status)

    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=combined_status,
        severity=rule.severity,
        message=combined_message,
        evidence=pre_evidence + sem_evidence,
        remediation=_pick_remediation(pre_diag, sem_diag, combined_status),
    )


def _combine_status(pre: Status, sem: Status) -> Status:
    """FAIL from either stage dominates. UNAVAILABLE propagates over PASS."""
    if Status.FAIL in (pre, sem):
        return Status.FAIL
    if Status.UNAVAILABLE in (pre, sem) or Status.ERROR in (pre, sem):
        return Status.UNAVAILABLE
    return Status.PASS


def _label_evidence(
    items: list[Evidence], stage: str
) -> list[Evidence]:
    """Wrap each Evidence item under a stage-labelled hybrid_* kind."""
    wrapper_kind = f"hybrid_{stage}"
    return [
        Evidence(kind=wrapper_kind, data={"stage": stage, **item.data, "_kind": item.kind})
        for item in items
    ]
```

The `_make_sub_rule` helper constructs a `Rule` whose `kind`, `backend_hint`, and
`params` mirror the sub-spec, inheriting `id`, `source`, `severity`, `title`, and
`text` from the parent hybrid rule. It is not persisted; it is ephemeral scaffolding
used only during dispatch.

---

## 6. Open Questions and Non-Goals

### Deferred by this design

- **Ordering beyond two stages.** The design handles exactly two stages (one
  deterministic, one semantic). Chains of three or more stages, or parallel execution
  with a merge function, are deferred.
- **Cross-document rule composition (#57).** That issue concerns composing rules across
  separate rule documents (e.g., a base policy plus a project overlay). Hybrid rules
  operate within a single rule and are orthogonal: a hybrid rule may itself be part of
  a composed document, but the hybrid machinery does not depend on cross-document
  resolution, and #57 does not depend on hybrid rules.
- **Classifier auto-detection.** The Markdown classifier does not yet emit
  `hybrid_check` rules; the syntax in §3.5 requires parser support that is deferred
  to sub-issue 73b.
- **Partial re-evaluation.** If the deterministic precheck result is cached and
  unchanged, can we re-run only the semantic stage? Cache and incremental evaluation
  are deferred.
- **`--backend` flag semantics for hybrid rules.** When the user passes
  `--backend filesystem`, should a hybrid rule that includes an `external` precheck
  run the precheck and skip only the semantic stage? The recommendation in §4
  (`backend_hint = highest-cost`) is a partial answer; the full flag-interaction
  contract is deferred to sub-issue 73b.
- **Semantic-only hybrid (no deterministic precheck).** A degenerate hybrid with
  `precheck = null` would be equivalent to a plain `semantic_rubric` rule.
  This edge case is deferred; the MVP requires both stages to be present.

### Explicitly out of scope

- Changes to `src/gate_keeper/models.py`, `src/gate_keeper/backends/`, or any test
  files. This is a design-phase document only.
- New `Backend` enum values. The hybrid dispatcher is an orchestration layer on top
  of existing backends; it does not require a new enum value.

---

## 7. Implementation Roadmap

The following three sub-issues are proposed for owner approval. **They are not created
here** — they are proposed as follow-ups once this design is ratified.

### 73a — IR extension: `hybrid_check` kind and `HybridStage` params

Scope:
- Add `RuleKind.HYBRID_CHECK` to `models.py`.
- Define and validate the `params` layout described in §4 (required keys `precheck`,
  `semantic`; optional `short_circuit`).
- Add fixture JSON files for a hybrid rule and a hybrid diagnostic to
  `tests/fixtures/ir/`.
- Update `docs/rule-ir.md` with the new kind and its params contract.

Acceptance: `from_dict` round-trips cleanly; strict validation rejects malformed
hybrid params; no backend code touched.

### 73b — Hybrid classifier + routing in `validate`

Scope:
- Add the fenced `gate-keeper` annotation block parser to the Markdown compiler (§3.5
  syntax).
- Implement `dispatch_hybrid` in a new `gate_keeper.backends.hybrid` module (§5
  pseudocode → production code).
- Wire `hybrid_check` into the backend dispatch table.
- Handle `--backend` flag interaction: skip entire hybrid rule if the flag excludes
  the highest-cost backend.
- Unit tests for all four quadrants (§3.3), short-circuit on/off, and
  `UNAVAILABLE` propagation.

Acceptance: `gate-keeper validate` routes `hybrid_check` rules correctly; all quadrant
tests pass; `uvx ruff check . && uvx pyright` clean.

### 73c — textlint + LLM hybrid integration test

Scope:
- End-to-end test: compile a rule document containing the canonical example (§2),
  validate against a fixture PR description (both a clean and a prohibited-term
  variant), assert evidence shape and combined status.
- Requires 73a and 73b to be merged.
- LLM call should be stubbed at the provider level (no live API key required in CI).

Acceptance: CI green on the integration test; evidence list contains `hybrid_deterministic`
and `hybrid_semantic` items with correct stage labels; short-circuit skips semantic
stub call when textlint fails.
