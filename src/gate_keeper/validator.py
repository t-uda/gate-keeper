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
import inspect
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
    TargetKind,
)
from gate_keeper.targets import TargetSpec


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


def _accepts_artifact_kind(check_fn: Callable) -> bool:
    """Return ``True`` if *check_fn* declares an ``artifact_kind`` parameter
    (or accepts arbitrary ``**kwargs``).

    Determined by signature introspection rather than a try/except on
    ``TypeError`` so that genuine ``TypeError``s raised inside the backend
    body (e.g. by ``llm_rubric.check``) are not misinterpreted as a
    signature mismatch and silently retried with the keyword dropped
    (codex P1 review on #193). Falls back to ``False`` for builtins and
    other callables whose signature cannot be inspected — those are
    invoked through the legacy two-argument form, which is the safe
    default for test stubs.
    """
    try:
        sig = inspect.signature(check_fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "artifact_kind" and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def _invoke_check(
    check_fn: Callable,
    rule: Rule,
    target: str | Path | TargetSpec,
    artifact_kind: TargetKind | None,
) -> Diagnostic:
    """Invoke *check_fn* with optional ``artifact_kind`` keyword (#191).

    The registry's published callable signature is ``(rule, target) ->
    Diagnostic``; the real ``llm_rubric.check`` adds an optional
    ``artifact_kind=`` keyword. Test doubles register simple two-argument
    callables that do not declare the keyword. To keep both shapes
    callable through the same code path we introspect *check_fn*'s
    signature once and decide whether to forward the keyword:

    - When *artifact_kind* is ``None``: call ``check_fn(rule, target)``
      verbatim — every registered check accepts this form.
    - When *artifact_kind* is supplied and *check_fn* declares an
      ``artifact_kind`` parameter (or accepts ``**kwargs``): forward
      the keyword.
    - Otherwise: call the legacy two-argument form. Tests that exercise
      the deterministic precheck only need the call count, so omitting
      the keyword for stubs is harmless. Tests that want to assert on
      the keyword register a stub that declares ``artifact_kind`` (or
      ``**kwargs``).

    Using signature introspection — rather than catching ``TypeError``
    from the call — keeps real runtime ``TypeError``s raised inside
    backend bodies (e.g. ``llm_rubric.check``) from being silently
    swallowed and retried without the keyword (codex P1 review on
    #193). Such errors now propagate to the caller, which converts them
    to ``Status.ERROR`` diagnostics in the normal exception path.
    """
    if artifact_kind is None or not _accepts_artifact_kind(check_fn):
        return check_fn(rule, target)
    return check_fn(rule, target, artifact_kind=artifact_kind)


def _run_n(
    check_fn: Callable,
    rule: Rule,
    target: str | Path | TargetSpec,
    n: int,
    artifact_kind: TargetKind | None = None,
) -> Diagnostic:
    """Run *check_fn* ``n`` times and aggregate via majority vote.

    Mirrors ``llm_rubric.run_n`` semantics but calls *check_fn* (the registry
    entry) instead of the backend module directly so stubs work correctly in
    tests and future backend overrides.

    Ties break toward fail (fail-closed). Returns early on the first
    non-pass/fail diagnostic (UNAVAILABLE / ERROR) without synthesising a
    reproducibility score (fail-closed for unconfigured states).

    *artifact_kind* (#191) is forwarded as a keyword argument to *check_fn*
    when non-``None``. Test stubs that only accept ``(rule, target)``
    continue to work because the keyword is omitted in that case; the
    real ``llm_rubric.check`` accepts ``artifact_kind=`` and uses it to
    decide whether to substitute file content into the prompt.
    """
    diags: list[Diagnostic] = []
    for _ in range(n):
        d = _invoke_check(check_fn, rule, target, artifact_kind)
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


def _target_kind_mismatch_diagnostic(
    rule: Rule,
    artifact_kind: TargetKind,
) -> Diagnostic:
    """Synthesise an ``Status.UNSUPPORTED`` diagnostic for the deterministic
    target-kind-mismatch precheck (#178).

    The evidence carries ``dispatch=deterministic_precheck`` and
    ``llm_called=false`` so callers can distinguish the deterministic
    short-circuit from a model-returned ``unsupported`` (which surfaces as
    ``target_kind_mismatch`` evidence with provider telemetry).
    """
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.UNSUPPORTED,
        severity=rule.severity,
        message=(
            f"Rule annotated `target_kind: {rule.target_kind.value}` does not "
            f"apply to artifact of kind `{artifact_kind.value}`; skipping "
            "without invoking the LLM."
        ),
        evidence=[
            Evidence(
                kind="target_kind_mismatch",
                data={
                    "rule_target_kind": rule.target_kind.value,
                    "artifact_kind": artifact_kind.value,
                    "dispatch": "deterministic_precheck",
                    "llm_called": False,
                },
            )
        ],
        remediation=(
            "The rule's premise does not apply to this artifact kind. "
            "Either evaluate the rule against an artifact whose kind "
            f"matches its `target_kind` ({rule.target_kind.value}), or "
            "remove / change the `target_kind` annotation on the rule."
        ),
    )


def validate(
    ruleset: RuleSet,
    target: str | Path | TargetSpec,
    backend: str = "auto",
    reproducibility: int = 1,
    artifact_kind: TargetKind | None = None,
) -> DiagnosticReport:
    """Validate *ruleset* against *target* using *backend*.

    Parameters
    ----------
    ruleset:
        Compiled ``RuleSet`` (output of parse + classify).
    target:
        One of:

        - a local filesystem path (``str`` or ``Path``);
        - a GitHub PR reference (``str``);
        - a :class:`gate_keeper.targets.TargetSpec` describing a multi-target
          filesystem evaluation produced by ``--target`` repetition,
          directory, or glob expansion (issue #146).

        Single-target callers continue to pass a path or reference directly;
        the value is forwarded verbatim to the backend.  Multi-target callers
        pass a ``TargetSpec``; backends that cannot honour it must produce an
        ``UNSUPPORTED`` / ``UNAVAILABLE`` diagnostic rather than silently
        evaluating only one of the resolved paths.
    backend:
        ``"auto"`` dispatches each rule by its ``backend_hint``; any other
        registered name sends all rules to that single backend.
    reproducibility:
        Number of times to evaluate each LLM-rubric rule (#68). The default ``1``
        preserves the original behaviour. Values ``> 1`` apply only to rules
        dispatched to the ``llm-rubric`` backend; non-LLM backends ignore this
        parameter. Must be ``>= 1``.
    artifact_kind:
        Optional caller-declared :class:`TargetKind` for *target* (#178).
        When supplied, every rule routed to the ``llm-rubric`` backend with
        an explicit ``rule.target_kind`` annotation is checked
        deterministically: if the rule's ``target_kind`` is set
        (i.e. not :data:`TargetKind.UNSPECIFIED`) and differs from
        *artifact_kind*, the rule is short-circuited to
        :data:`Status.UNSUPPORTED` with ``evidence.kind=target_kind_mismatch``
        (carrying ``dispatch=deterministic_precheck`` and
        ``llm_called=false``) **without invoking the provider**. Rules whose
        ``target_kind`` is ``unspecified`` are unaffected. Rules dispatched
        to other backends (filesystem, github, external) ignore this
        parameter. Default ``None`` preserves prior behaviour, including
        the prompt-level fallback inside the llm-rubric backend.

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

        # #178 — deterministic target-kind-mismatch precheck. Applied only
        # to rules routed to the llm-rubric backend that carry an explicit
        # ``target_kind`` annotation (UNSPECIFIED rules preserve the
        # prompt-level fallback). The check is performed here, before
        # dispatch, so no provider call is made on mismatch — the test
        # suite asserts this by counting calls into the stubbed provider.
        if (
            artifact_kind is not None
            and resolved_name == "llm-rubric"
            and rule.target_kind is not TargetKind.UNSPECIFIED
            and rule.target_kind is not artifact_kind
        ):
            diagnostics.append(_target_kind_mismatch_diagnostic(rule, artifact_kind))
            continue

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
                #
                # #191: forward ``artifact_kind`` via ``_invoke_check`` only
                # for the llm-rubric route — non-LLM backends (filesystem,
                # github, external) take ``(rule, target)`` and would not
                # benefit from the keyword.
                rule_artifact_kind = artifact_kind if resolved_name == "llm-rubric" else None
                if reproducibility > 1 and resolved_name == "llm-rubric":
                    diag = _run_n(check_fn, rule, target, reproducibility, rule_artifact_kind)
                else:
                    diag = _invoke_check(check_fn, rule, target, rule_artifact_kind)
            except Exception as exc:  # noqa: BLE001
                diag = _error_diagnostic(rule, exc, _backend_for(resolved_name))
        diagnostics.append(diag)

    return DiagnosticReport(diagnostics=diagnostics)


__all__ = ["validate"]
