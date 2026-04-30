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
The validator never raises.  Unexpected exceptions from a backend call are
caught here and converted into ``Status.ERROR`` diagnostics so the pipeline
always produces a report.

Rule ordering
-------------
Diagnostics are emitted in the same order as ``ruleset.rules``.
"""
from __future__ import annotations

from pathlib import Path

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


def _error_diagnostic(rule: Rule, exc: Exception) -> Diagnostic:
    """Wrap an unexpected backend exception in an ERROR diagnostic."""
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.FILESYSTEM,  # best-effort; real backend unknown at this point
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


def _resolve_check_fn(rule: Rule, backend_name: str):
    """Return the check callable for *rule* given the chosen *backend_name*.

    When ``backend_name`` is ``"auto"`` we use the rule's ``backend_hint``.
    Returns ``None`` when the name is not registered (should not happen after
    CLI validation, but handled defensively).
    """
    if backend_name == "auto":
        name = rule.backend_hint.value  # e.g. "filesystem", "github", "llm-rubric"
    else:
        name = backend_name
    return _registry.get(name)


def validate(
    ruleset: RuleSet,
    target: str | Path,
    backend: str = "auto",
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

    Returns
    -------
    DiagnosticReport
        One ``Diagnostic`` per rule, in source order.  Never raises.
    """
    if backend != "auto" and not _registry.is_registered(backend):
        raise ValueError(
            f"unknown backend {backend!r}; registered names: {_registry.BACKEND_NAMES}"
        )

    diagnostics: list[Diagnostic] = []
    for rule in ruleset.rules:
        check_fn = _resolve_check_fn(rule, backend)
        if check_fn is None:
            # Defensive: backend_hint points to an unregistered name.
            diag = Diagnostic(
                rule_id=rule.id,
                source=rule.source,
                backend=Backend.FILESYSTEM,
                status=Status.UNAVAILABLE,
                severity=rule.severity,
                message=(
                    f"no backend registered for {rule.backend_hint.value!r}; "
                    "cannot validate rule"
                ),
                evidence=[
                    Evidence(
                        kind="registry_miss",
                        data={"backend_hint": rule.backend_hint.value},
                    )
                ],
            )
        else:
            try:
                diag = check_fn(rule, target)
            except Exception as exc:  # noqa: BLE001
                diag = _error_diagnostic(rule, exc)
        diagnostics.append(diag)

    return DiagnosticReport(diagnostics=diagnostics)


__all__ = ["validate"]
