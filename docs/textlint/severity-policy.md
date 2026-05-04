# textlint severity policy — decision record (#85)

Tracking umbrella: #80. This document defines the mapping from textlint
severity levels to gate-keeper severity levels, and the fail-closed criteria
per document type.

Package set and rule enable/disable decisions are in
[`package-set.md`](package-set.md). Config file authoring is #82.
The textlint adapter that consumes this policy at runtime is #94.

---

## 1. Severity levels

### 1.1 textlint native levels

textlint emits three levels per rule:

| textlint level | Meaning |
|---|---|
| `error` | Rule violation; textlint exits non-zero when at least one error fires. |
| `warning` | Advisory finding; does not affect exit code by default. |
| `info` | Informational note; informational-only, no exit-code impact. |

### 1.2 gate-keeper severity model

gate-keeper's `Severity` enum (defined in `src/gate_keeper/models.py`):

| gate-keeper `Severity` | Semantics |
|---|---|
| `Severity.ERROR` | Fail-closed: the gate does not pass. Evidence is blocking. |
| `Severity.WARNING` | Advisory: reported in output, does not block the gate by default. |
| `Severity.ADVISORY` | Informational: surfaced in verbose output; not a finding in normal mode. |

---

## 2. Mapping table

The textlint adapter (#94) uses this table to convert each textlint finding
to a gate-keeper `Severity` value.

| textlint severity | gate-keeper `Severity` | Notes |
|---|---|---|
| `error` | `Severity.ERROR` | Direct mapping. Textlint `error`-level findings represent deterministic rule violations; they are fail-closed from day one. |
| `warning` | `Severity.WARNING` | Direct mapping. Textlint `warning`-level findings are advisory; they appear in the diagnostic report but do not block the gate unless the caller promotes them (see §3). |
| `info` | `Severity.ADVISORY` | Direct mapping. Informational findings are surfaced only in verbose mode. |

**Rationale for direct mapping**: textlint's three-level schema aligns exactly
with gate-keeper's three-level schema. A one-to-one mapping avoids information
loss and preserves the intent of each rule's configured severity. The adapter
does not need a translation layer beyond the enum conversion.

**Comparison to the semantic (LLM rubric) side**: on the semantic side,
`Severity` promotion depends on reproducibility data (#79). On the textlint
(deterministic) side, `Severity.ERROR` is the correct default from day one
because the rules are mechanically enforceable with no false-positive ambiguity
at correctly-configured thresholds. This asymmetry is intentional; see
`CLAUDE.md`: "prefer deterministic backends over LLM rubric whenever evidence
is available."

---

## 3. Document-type fail-closed conditions

The current corpus is homogeneous dev/process/design prose; a single global
config applies to all files (see [`per-doc-type-policy.md`](per-doc-type-policy.md)).
Fail-closed conditions are therefore uniform across document types.

| Document category | Examples | Fail-closed condition |
|---|---|---|
| Dev / process docs | `docs/*.md`, `README.md` | Any `Severity.ERROR` finding blocks the gate. |
| Design / IR docs | `docs/rule-ir.md`, `docs/llm-rubric.md` | Any `Severity.ERROR` finding blocks the gate. |
| Test fixture targets | `tests/fixtures/semantic/targets/*.md` | Any `Severity.ERROR` finding blocks the gate. |

**Advisory-only promotion path**: individual textlint rules may be configured
at `warning` (rather than `error`) when empirical data from the corpus shows
a meaningful false-positive rate. In that case the gate-keeper adapter maps
the finding to `Severity.WARNING`, which is non-blocking by default. Promotion
from `warning` to `error` at the rule level requires an explicit decision in
`.textlintrc` and is not automatic.

---

## 4. Initial severity assignments for the package-set rules

These are the severity assignments for the two rules in the initial package
set (see `package-set.md §3`). These assignments feed directly into
`.textlintrc` (authored in #82) and the adapter's rule-metadata constants (#94).

| Rule | Default textlint severity | gate-keeper `Severity` | Rationale |
|---|---|---|---|
| `textlint-rule-terminology` | `error` | `Severity.ERROR` | Brand/term capitalisation is deterministic and unambiguous. False-positive rate on this corpus is near zero (English-only, technical prose). Fail-closed from day one. |
| `textlint-rule-prh` | `error` | `Severity.ERROR` | Project-specific terminology violations are deterministic (exact dictionary lookup). The dictionary author controls the false-positive rate by keeping `prh.yml` accurate. Fail-closed from day one. |

**Deferred rules** (Japanese presets, currently excluded from `.textlintrc`):
when Japanese rules are enabled in a future revision, their initial severity
assignment will be `warning` until empirical corpus data from #83 confirms the
false-positive rate is acceptable. This is documented here to prevent silent
promotion at the time of enablement.

---

## 5. Adapter interface contract

The textlint adapter (#94) must expose a function with the following
semantic contract (implementation language is Python; signature is indicative):

```python
def textlint_severity_to_gate_keeper(textlint_severity: str) -> Severity:
    """Map a textlint severity string to a gate-keeper Severity enum value.

    textlint_severity: one of "error", "warning", "info"
    Returns: Severity.ERROR, Severity.WARNING, or Severity.ADVISORY respectively.
    Raises: ValueError on unrecognised severity string (fail-closed).
    """
    mapping = {
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.ADVISORY,
    }
    if textlint_severity not in mapping:
        raise ValueError(f"Unknown textlint severity: {textlint_severity!r}")
    return mapping[textlint_severity]
```

This contract is the runtime expression of §2's mapping table. The adapter
implementation in #94 must match this contract; any divergence from §2 requires
an update to this document first.

---

## 6. What this policy does NOT settle

- **Which rules are enabled** — `package-set.md` (#81) and `.textlintrc` (#82).
- **False-positive suppression** — inline disable comments and `.textlintignore`
  are a separate policy (#80-10).
- **CI workflow** — which files textlint runs against, in which job, is #87.
- **Adapter implementation** — the Python code for the textlint adapter is #94.
- **Per-doc-type severity overrides** — not needed for the current homogeneous
  corpus; revisit if `per-doc-type-policy.md` reopen criteria are triggered (#91).
