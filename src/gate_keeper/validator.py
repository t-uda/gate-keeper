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
        One ``Diagnostic`` per rule, in source order.  Never raises.
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
                # #68: only the llm-rubric backend has a meaningful reproducibility
                # path; for other backends an N>1 setting is silently ignored.
                if reproducibility > 1 and resolved_name == "llm-rubric":
                    from gate_keeper.backends import llm_rubric as _llm_mod

                    diag = _llm_mod.run_n(rule, target, reproducibility)
                else:
                    diag = check_fn(rule, target)
            except Exception as exc:  # noqa: BLE001
                diag = _error_diagnostic(rule, exc, _backend_for(resolved_name))
        diagnostics.append(diag)

    return DiagnosticReport(diagnostics=diagnostics)


__all__ = ["validate"]
