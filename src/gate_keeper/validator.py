"""Validation orchestrator for gate-keeper.

``validate`` is the single runtime entry point that connects a compiled
``RuleSet`` to the registered backends and produces a ``DiagnosticReport``.

Dispatch policy
---------------
- ``backend == "auto"``: each rule is dispatched to the backend named by its
  ``backend_hint`` field (the IR ``Backend`` enum value, e.g. ``"filesystem"``).
- Any other registered name: every rule is sent to that backend; the backend is
  expected to return ``UNSUPPORTED`` diagnostics for rule kinds it cannot handle.
- Unknown name: raises ``ValueError`` (callers should validate before calling).

Error policy
------------
``validate`` raises ``ValueError`` for invalid arguments (unknown backend name,
``reproducibility < 1``).  Unexpected exceptions from backend calls are caught
and converted into ``Status.ERROR`` diagnostics so the pipeline always produces
a report for valid inputs.

Rule ordering
-------------
Diagnostics are emitted in the same order as ``ruleset.rules``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Callable

from gate_keeper import backends as _registry
from gate_keeper.models import (
    Backend,
    Diagnostic,
    DiagnosticReport,
    Evidence,
    Rule,
    RuleSet,
    Status,
)


def _backend_for(name: str) -> Backend:
    """Map a registered backend name to its IR ``Backend`` enum.

    Registered names mirror enum values exactly; this raises ``ValueError`` only
    if a caller registers a name outside the enum, which is a programmer error.
    """
    return Backend(name)


def _error_diagnostic(rule: Rule, exc: Exception, backend: Backend) -> Diagnostic:
    """Wrap an unexpected backend exception in an ERROR diagnostic.

    The ``backend`` argument is the implementation that actually raised, so the
    diagnostic attributes the failure correctly even when several backends are
    registered.
    """
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=backend,
        status=Status.ERROR,
        severity=rule.severity,
        message=f"internal error during validation: {exc}",
        evidence=[
            Evidence(
                kind="exception",
                data={"type": type(exc).__name__, "message": str(exc)},
            )
        ],
    )


def _resolve_backend_name(rule: Rule, backend_name: str) -> str:
    """Return the registered backend name to use for *rule*.

    When ``backend_name`` is ``"auto"`` we use the rule's ``backend_hint``.
    """
    if backend_name == "auto":
        return rule.backend_hint.value
    return backend_name


def _run_n(
    check_fn: Callable,
    rule: Rule,
    target: str | Path,
    n: int,
) -> Diagnostic:
    """Run *check_fn* ``n`` times and aggregate via majority vote.

    Mirrors ``llm_rubric.run_n`` semantics but calls *check_fn* (the registry
    entry) instead of the backend module directly so stubs work correctly in
    tests and future backend overrides.

    Ties break toward fail (fail-closed). Returns early on the first
    non-pass/fail diagnostic (UNAVAILABLE / ERROR) without synthesising a
    reproducibility score (fail-closed for unconfigured states).
    """
    diags: list[Diagnostic] = []
    for _ in range(n):
        d = check_fn(rule, target)
        if d.status not in (Status.PASS, Status.FAIL):
            return d
        diags.append(d)

    pass_count = sum(1 for d in diags if d.status is Status.PASS)
    fail_count = n - pass_count
    majority_is_pass = pass_count > fail_count
    majority_judgment = "pass" if majority_is_pass else "fail"
    majority_count = pass_count if majority_is_pass else fail_count
    score = majority_count / n

    target_status = Status.PASS if majority_is_pass else Status.FAIL
    representative = next(d for d in diags if d.status is target_status)

    repro_evidence = Evidence(
        kind="reproducibility_score",
        data={
            "score": score,
            "n": n,
            "pass_count": pass_count,
            "majority_judgment": majority_judgment,
        },
    )
    return dataclasses.replace(
        representative,
        evidence=[*representative.evidence, repro_evidence],
    )


def validate(
    ruleset: RuleSet,
    target: str | Path,
    backend: str = "auto",
    reproducibility: int = 1,
) -> DiagnosticReport:
    """Validate *ruleset* against *target* using *backend*.

    Parameters
    ----------
    ruleset:
        Compiled ``RuleSet`` (output of parse + classify).
    target:
        Local path or GitHub PR reference passed through to the backend.
    backend:
        ``"auto"`` dispatches each rule by its ``backend_hint``; any other
        registered name sends all rules to that single backend.
    reproducibility:
        Number of times to evaluate each LLM-rubric rule (#68). The default ``1``
        preserves the original behaviour. Values ``> 1`` apply only to rules
        dispatched to the ``llm-rubric`` backend; non-LLM backends ignore this
        parameter. Must be ``>= 1``.

    Returns
    -------
    DiagnosticReport
        One ``Diagnostic`` per rule, in source order.

    Raises
    ------
    ValueError
        If *backend* is not ``"auto"`` and is not a registered backend name,
        or if *reproducibility* is less than 1.  All other exceptions from
        backend calls are caught and converted to ``Status.ERROR`` diagnostics
        so the pipeline always produces a report.
    """
    if backend != "auto" and not _registry.is_registered(backend):
        raise ValueError(f"unknown backend {backend!r}; registered names: {_registry.BACKEND_NAMES}")
    if reproducibility < 1:
        raise ValueError(f"reproducibility must be >= 1, got {reproducibility}")

    diagnostics: list[Diagnostic] = []
    for rule in ruleset.rules:
        resolved_name = _resolve_backend_name(rule, backend)
        check_fn = _registry.get(resolved_name)
        if check_fn is None:
            # Defensive: name resolved from backend_hint is not registered.
            # Attribute the diagnostic to the IR Backend that *should* have
            # handled it when the name maps to one; otherwise fall back to
            # filesystem (the local-only backend) so output stays renderable.
            try:
                attributed = _backend_for(resolved_name)
            except ValueError:
                attributed = Backend.FILESYSTEM
            diag = Diagnostic(
                rule_id=rule.id,
                source=rule.source,
                backend=attributed,
                status=Status.UNAVAILABLE,
                severity=rule.severity,
                message=(f"no backend registered for {resolved_name!r}; cannot validate rule"),
                evidence=[
                    Evidence(
                        kind="registry_miss",
                        data={"backend_hint": resolved_name},
                    )
                ],
            )
        else:
            try:
                # #68: apply multi-run reproducibility for llm-rubric rules via
                # the registry check_fn so stubs/overrides work in tests.
                # Non-LLM backends silently ignore N>1.
                if reproducibility > 1 and resolved_name == "llm-rubric":
                    diag = _run_n(check_fn, rule, target, reproducibility)
                else:
                    diag = check_fn(rule, target)
            except Exception as exc:  # noqa: BLE001
                diag = _error_diagnostic(rule, exc, _backend_for(resolved_name))
        diagnostics.append(diag)

    return DiagnosticReport(diagnostics=diagnostics)


__all__ = ["validate"]
