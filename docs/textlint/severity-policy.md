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
| `warning` | Advisory finding; does not affect textlint's exit code by default. |
| `info` | Informational note; informational-only, no textlint exit-code impact. |

### 1.2 gate-keeper severity model

gate-keeper's `Severity` enum (defined in `src/gate_keeper/models.py`):

| gate-keeper `Severity` | Role in the gate-keeper data model |
|---|---|
| `Severity.ERROR` | Indicates the rule is configured for fail-closed enforcement. A `Diagnostic` carrying `Severity.ERROR` is expected to have `Status.FAIL` (or `Status.ERROR`) when a violation is found, which causes `compute_exit_code()` to return non-zero. |
| `Severity.WARNING` | Indicates the rule is configured as advisory. The `Severity` field is metadata about the rule's intent; whether the diagnostic actually blocks the gate depends on its `Status`, not on `Severity` alone. |
| `Severity.ADVISORY` | Informational intent. Rendered in output for visibility; blocking depends on `Status`. |

**Important**: gate-keeper's exit code is driven by `Diagnostic.status` (via
`compute_exit_code()` in `src/gate_keeper/diagnostics.py`), not by
`Diagnostic.severity`. `Severity` is rule-level metadata that records the
author's intent. The adapter (#94) sets `Diagnostic.severity` from
`rule.severity` and sets `Diagnostic.status` based on the textlint output
(e.g. `Status.FAIL` for a textlint `error`, `Status.PASS` for a clean check).
A rule configured with `Severity.WARNING` that fires will still have
`Status.FAIL`, which blocks the gate — unless the adapter explicitly uses a
non-blocking status for warning-severity rules. This is a future adapter policy
decision (#94); the present policy defines the severity intent only.

---

## 2. Mapping table

The textlint adapter (#94) uses this table to assign `Severity` to rules based
on their configured textlint severity level. This is a rule-metadata mapping,
not a per-finding mapping (the adapter inherits `rule.severity` per the
`external.py` `_diag()` helper pattern).

| textlint severity | gate-keeper `Severity` | Notes |
|---|---|---|
| `error` | `Severity.ERROR` | Direct mapping. Textlint `error`-level rules are fail-closed; the adapter produces `Status.FAIL` diagnostics for violations. |
| `warning` | `Severity.WARNING` | Direct mapping. Textlint `warning`-level rules are advisory in intent; the adapter may produce `Status.FAIL` or a lighter status for violations — see §3. |
| `info` | `Severity.ADVISORY` | Direct mapping. Informational rules; adapter behavior TBD in #94. |

**Rationale for direct mapping**: textlint's three-level schema aligns with
gate-keeper's three-level schema. A one-to-one mapping avoids information loss
and preserves the intent of each rule's configured severity.

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

The table below states the intended blocking behavior. Actual gate-blocking is
determined by `Diagnostic.status` at runtime; the adapter (#94) must produce
`Status.FAIL` for violations of rules whose intent is fail-closed.

| Document category | Examples | Fail-closed intent |
|---|---|---|
| Dev / process docs | `docs/*.md`, `README.md` | Rules configured at `error` severity (`Severity.ERROR`) are fail-closed: violations produce `Status.FAIL` and block the gate. |
| Design / IR docs | `docs/rule-ir.md`, `docs/llm-rubric.md` | Same as above. |
| Test fixture targets | `tests/fixtures/semantic/targets/*.md` | Same as above. |

**Advisory-only promotion path**: individual textlint rules may be configured
at `warning` (rather than `error`) in `.textlintrc` when empirical data from the
corpus shows a meaningful false-positive rate. The adapter (#94) then assigns
`Severity.WARNING` to that rule's diagnostics. Whether `warning`-severity
violations produce `Status.FAIL` or a lighter `Status.UNAVAILABLE`/custom status
is an adapter implementation decision (#94); this policy records only the
severity-level intent. Promotion from `warning` to `error` at the rule level
requires an explicit change in `.textlintrc`.

---

## 4. Initial severity assignments for the package-set rules

These are the severity assignments for the two rules in the initial package
set (see `package-set.md §3`). These assignments feed directly into
`.textlintrc` (authored in #82) and the adapter's rule-metadata constants (#94).

| Rule | textlint severity in `.textlintrc` | gate-keeper `Severity` | Rationale |
|---|---|---|---|
| `textlint-rule-terminology` | `error` | `Severity.ERROR` | Brand/term capitalisation is deterministic and unambiguous. False-positive rate on this corpus is near zero (English-only, technical prose). Fail-closed from day one. |
| `textlint-rule-prh` | `error` | `Severity.ERROR` | Project-specific terminology violations are deterministic (exact dictionary lookup). The dictionary author controls the false-positive rate by keeping `prh.yml` accurate. Fail-closed from day one. |

**Deferred rules** (Japanese presets, currently excluded from `.textlintrc`):
when Japanese rules are enabled in a future revision, their initial textlint
severity assignment will be `warning` (mapping to `Severity.WARNING`) until
empirical corpus data from #83 confirms the false-positive rate is acceptable.
This is documented here to prevent silent promotion at the time of enablement.

---

## 5. Adapter interface contract

The textlint adapter (#94) must use the following severity mapping when
constructing `Diagnostic` objects. The mapping is applied to the textlint
severity configured for each rule (rule-level, not per-finding):

```python
def textlint_severity_to_gate_keeper(textlint_severity: str) -> Severity:
    """Map a textlint rule severity string to a gate-keeper Severity enum value.

    textlint_severity: one of "error", "warning", "info"
    Returns: Severity.ERROR, Severity.WARNING, or Severity.ADVISORY respectively.

    Per the ExternalAdapter protocol (src/gate_keeper/backends/external.py),
    adapters must never raise. On unrecognised input, fall back to Severity.WARNING
    (conservative: records the anomaly without defaulting to fail-closed).
    The adapter should also produce an adapter_error Evidence item in that case.
    """
    mapping = {
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.ADVISORY,
    }
    return mapping.get(textlint_severity, Severity.WARNING)
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
  In particular: whether `warning`-severity rule violations produce `Status.FAIL`
  or a lighter status, and how the adapter translates textlint's exit code into
  individual `Diagnostic` statuses.
- **Per-doc-type severity overrides** — not needed for the current homogeneous
  corpus; revisit if `per-doc-type-policy.md` reopen criteria are triggered (#91).
