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
from gate_keeper.targets import ScopeResolution, TargetSpec, resolve_rule_scope


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


def _accepts_keyword(check_fn: Callable, name: str) -> bool:
    """Return ``True`` if *check_fn* declares a keyword parameter named *name*
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
        if param.name == name and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def _accepts_artifact_kind(check_fn: Callable) -> bool:
    """Return ``True`` if *check_fn* declares an ``artifact_kind`` parameter."""
    return _accepts_keyword(check_fn, "artifact_kind")


def _accepts_deterministic(check_fn: Callable) -> bool:
    """Return ``True`` if *check_fn* declares a ``deterministic`` parameter."""
    return _accepts_keyword(check_fn, "deterministic")


def _invoke_check(
    check_fn: Callable,
    rule: Rule,
    target: str | Path | TargetSpec,
    artifact_kind: TargetKind | None,
    *,
    deterministic: bool = False,
) -> Diagnostic:
    """Invoke *check_fn* with optional ``artifact_kind`` and ``deterministic`` keywords.

    The registry's published callable signature is ``(rule, target) ->
    Diagnostic``; the real ``llm_rubric.check`` adds optional keyword
    arguments. Test doubles register simple two-argument callables that do
    not declare those keywords. To keep both shapes callable through the
    same code path we introspect *check_fn*'s signature once and decide
    whether to forward each keyword:

    - When *artifact_kind* is ``None``: omit the keyword — every registered
      check accepts ``(rule, target)``.
    - When *artifact_kind* is supplied and *check_fn* declares the parameter
      (or accepts ``**kwargs``): forward it.
    - *deterministic* is forwarded when ``True`` and *check_fn* declares it
      (or accepts ``**kwargs``); omitted for stubs that don't need it.

    Using signature introspection — rather than catching ``TypeError``
    from the call — keeps real runtime ``TypeError``s raised inside
    backend bodies (e.g. ``llm_rubric.check``) from being silently
    swallowed and retried without the keyword (codex P1 review on
    #193). Such errors now propagate to the caller, which converts them
    to ``Status.ERROR`` diagnostics in the normal exception path.
    """
    kwargs: dict[str, object] = {}
    if artifact_kind is not None and _accepts_artifact_kind(check_fn):
        kwargs["artifact_kind"] = artifact_kind
    if deterministic and _accepts_deterministic(check_fn):
        kwargs["deterministic"] = deterministic
    if kwargs:
        return check_fn(rule, target, **kwargs)
    return check_fn(rule, target)


def _run_n(
    check_fn: Callable,
    rule: Rule,
    target: str | Path | TargetSpec,
    n: int,
    artifact_kind: TargetKind | None = None,
    *,
    deterministic: bool = False,
) -> Diagnostic:
    """Run *check_fn* ``n`` times and aggregate via majority vote.

    Mirrors ``llm_rubric.run_n`` semantics but calls *check_fn* (the registry
    entry) instead of the backend module directly so stubs work correctly in
    tests and future backend overrides.

    Ties break toward fail (fail-closed). Returns early on the first
    non-pass/fail diagnostic (UNAVAILABLE / ERROR) without synthesising a
    reproducibility score (fail-closed for unconfigured states).

    *artifact_kind* (#191) and *deterministic* (#69) are forwarded as keyword
    arguments to *check_fn* when supported.  Test stubs that only accept
    ``(rule, target)`` continue to work because the keywords are omitted when
    the stub doesn't declare them.
    """
    diags: list[Diagnostic] = []
    for _ in range(n):
        d = _invoke_check(check_fn, rule, target, artifact_kind, deterministic=deterministic)
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


def target_kind_mismatch_diagnostic(
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


def _run_rule_check(
    check_fn: Callable,
    rule: Rule,
    target: str | Path | TargetSpec,
    resolved_name: str,
    reproducibility: int,
    rule_artifact_kind: TargetKind | None,
    *,
    deterministic: bool = False,
) -> Diagnostic:
    """Invoke *check_fn* for *rule* with the existing reproducibility / artifact-kind logic.

    Extracted so the same code path is used by both the sequential and the
    bounded-concurrent dispatch branches in :func:`validate`. Exceptions
    propagate to the caller, which converts them to ``Status.ERROR`` via
    :func:`_error_diagnostic`. Non-LLM backends ignore ``reproducibility``,
    ``rule_artifact_kind``, and ``deterministic`` (the caller nulls them out
    or passes ``False`` for non-LLM routes).
    """
    if reproducibility > 1 and resolved_name == "llm-rubric":
        return _run_n(
            check_fn, rule, target, reproducibility, rule_artifact_kind, deterministic=deterministic
        )
    return _invoke_check(check_fn, rule, target, rule_artifact_kind, deterministic=deterministic)


# ---------------------------------------------------------------------------
# Per-rule target scope (issue #279 — S3, docs/design/multi-target.md §9)
# ---------------------------------------------------------------------------


def _derive_candidate_paths(target: str | Path | TargetSpec) -> list[Path] | None:
    """Derive the run-level candidate file pool from *target* (§9.3).

    Returns the candidate paths every scoped rule intersects its scope with, or
    ``None`` when *target* is not a filesystem pool (e.g. a GitHub PR reference)
    — in which case scoped rules cannot compute an effective set and are
    dispatched verbatim (legacy behaviour, never a silent pass).

    - :class:`TargetSpec` → its resolved ``paths``.
    - A ``str`` / :class:`Path` pointing at an existing regular file → that one
      file (a single-target run scoped rules can still narrow against).
    - Anything else (PR reference, non-existent path, directory-as-string) →
      ``None``.
    """
    if isinstance(target, TargetSpec):
        return list(target.paths)
    candidate = target if isinstance(target, Path) else Path(target)
    try:
        if candidate.is_file():
            return [candidate]
    except OSError:
        return None
    return None


class _ScopeContext:
    """Per-run lazily-resolved context for per-rule scope computation.

    The repository root and candidate pool are resolved on first use — only when
    a rule actually declares ``params.target_scope`` — so an unscoped run pays no
    extra syscalls and stays byte-identical to today (§9.9). The expansion memo
    is shared across every rule in the run (§9.6).
    """

    def __init__(self, target: str | Path | TargetSpec, repo_root: Path | None) -> None:
        self._target = target
        self._repo_root = repo_root
        self._candidate_paths: list[Path] | None = None
        self._candidate_resolved = False
        self.memo: dict[tuple[str, str], frozenset[str]] = {}

    def repo_root(self) -> Path:
        if self._repo_root is None:
            # Lazy import keeps ``changed``'s git machinery out of the hot path
            # for unscoped runs. When cwd is not inside a git repo we fall back
            # to cwd as the scope root: scopes are repo-relative globs, so cwd
            # is the sensible base when there is no ``.git`` to anchor on.
            from gate_keeper.changed import ChangedFilesError, find_repo_root

            try:
                self._repo_root = find_repo_root(Path.cwd())
            except ChangedFilesError:
                self._repo_root = Path.cwd()
        return self._repo_root

    def candidate_paths(self) -> list[Path] | None:
        if not self._candidate_resolved:
            self._candidate_paths = _derive_candidate_paths(self._target)
            self._candidate_resolved = True
        return self._candidate_paths


def _scope_short_circuit_diagnostic(rule: Rule, resolution: ScopeResolution) -> Diagnostic:
    """Build the synthetic diagnostic for an empty / invalid / over-cap scope (§9.4/§9.5/§9.8).

    Attributed to the rule's ``backend_hint`` even though no backend was called;
    the evidence ``kind`` distinguishes the three causes and carries the resolved
    scope / candidate / effective sizes so a reader can audit exactly why the rule
    was not evaluated against files this run.
    """
    scope_raw = rule.params.get("target_scope")
    base_data = {
        "target_scope": scope_raw,
        "scope_size": resolution.scope_size,
        "candidate_size": resolution.candidate_size,
        "effective_size": resolution.effective_size,
    }
    if resolution.status == "empty":
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=rule.backend_hint,
            status=Status.PASS,
            severity=rule.severity,
            message=(
                "no files in scope for this run: "
                f"target_scope expands to {resolution.scope_size} file(s) but none "
                "intersect the run's candidate set; nothing to check"
            ),
            evidence=[Evidence(kind="scope_empty", data=dict(base_data))],
        )
    if resolution.status == "file_limit_exceeded":
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=rule.backend_hint,
            status=Status.UNAVAILABLE,
            severity=rule.severity,
            message=(
                f"per-rule effective target set of {resolution.effective_size} file(s) "
                f"exceeds the limit of {resolution.file_limit}; narrow the rule's "
                "target_scope"
            ),
            evidence=[
                Evidence(
                    kind="scope_file_limit_exceeded",
                    data={**base_data, "file_limit": resolution.file_limit},
                )
            ],
            remediation=(
                "The rule's target_scope, intersected with this run's candidate "
                f"set, resolves to {resolution.effective_size} files, over the "
                f"{resolution.file_limit}-file per-rule cap. Narrow target_scope so "
                "the effective set stays within the cap."
            ),
        )
    # "invalid"
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=rule.backend_hint,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message=(f"invalid target_scope: {resolution.detail}"),
        evidence=[
            Evidence(
                kind="scope_invalid",
                data={"target_scope": scope_raw, "detail": resolution.detail},
            )
        ],
        remediation=(
            "Set params.target_scope to a non-empty list of repo-relative glob "
            "strings that matches at least one text file in the repository."
        ),
    )


def _scope_effective_evidence(resolution: ScopeResolution) -> Evidence:
    """Auditable ``scope_effective_set`` evidence for a dispatched scoped rule (§9.8).

    Rides on the rule's normal verdict so every scoped verdict records which
    files it actually ran against.
    """
    return Evidence(
        kind="scope_effective_set",
        data={
            "scope_size": resolution.scope_size,
            "candidate_size": resolution.candidate_size,
            "effective_size": resolution.effective_size,
            "effective_paths": list(resolution.effective_relpaths),
        },
    )


@dataclasses.dataclass(frozen=True)
class _RuleDispatch:
    """Per-rule dispatch plan produced by :func:`_plan_rule_dispatch`.

    Exactly one of ``short_circuit`` (skip the backend, use this diagnostic) or a
    live dispatch (run the backend against ``target``, then append
    ``scope_evidence`` if present) applies.
    """

    target: str | Path | TargetSpec
    short_circuit: Diagnostic | None
    scope_evidence: Evidence | None


def _plan_rule_dispatch(
    rule: Rule,
    run_target: str | Path | TargetSpec,
    scope_ctx: _ScopeContext,
) -> _RuleDispatch:
    """Decide how *rule* dispatches under per-rule target scope (§9.2).

    A rule with no ``params.target_scope`` is dispatched against *run_target*
    byte-for-byte as today. A scoped rule's effective set (scope ∩ candidate) is
    computed; a non-empty set dispatches a per-rule :class:`TargetSpec` and
    records ``scope_effective_set`` evidence, while empty / invalid / over-cap
    outcomes short-circuit to a synthetic diagnostic without a backend call.
    """
    scope_raw = rule.params.get("target_scope")
    if scope_raw is None:
        return _RuleDispatch(target=run_target, short_circuit=None, scope_evidence=None)

    candidate = scope_ctx.candidate_paths()
    if candidate is None:
        # Run-level target is not a filesystem pool (e.g. a PR reference); a
        # per-rule scope cannot be intersected. Dispatch verbatim — the rule is
        # still evaluated against the actual target, never silently passed.
        return _RuleDispatch(target=run_target, short_circuit=None, scope_evidence=None)

    resolution = resolve_rule_scope(
        scope_raw,
        candidate,
        scope_ctx.repo_root(),
        memo=scope_ctx.memo,
    )
    if resolution.status == "dispatch" and resolution.spec is not None:
        return _RuleDispatch(
            target=resolution.spec,
            short_circuit=None,
            scope_evidence=_scope_effective_evidence(resolution),
        )
    return _RuleDispatch(
        target=run_target,
        short_circuit=_scope_short_circuit_diagnostic(rule, resolution),
        scope_evidence=None,
    )


def _append_scope_evidence(diag: Diagnostic, scope_evidence: Evidence | None) -> Diagnostic:
    """Append ``scope_effective_set`` evidence to *diag* when the rule was scoped."""
    if scope_evidence is None:
        return diag
    return dataclasses.replace(diag, evidence=[*diag.evidence, scope_evidence])


def validate(
    ruleset: RuleSet,
    target: str | Path | TargetSpec,
    backend: str = "auto",
    reproducibility: int = 1,
    artifact_kind: TargetKind | None = None,
    *,
    concurrency: int = 1,
    deterministic: bool = False,
    eval_cache: bool = False,
    repo_root: Path | None = None,
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
    concurrency:
        Maximum number of rule checks executed concurrently (#249, Slice 1).
        The default ``1`` preserves the strict sequential path bit-for-bit.
        When ``> 1``, rule checks that survive the deterministic prechecks
        (target-kind mismatch, registry miss) are dispatched to a bounded
        :class:`concurrent.futures.ThreadPoolExecutor` with
        ``max_workers=concurrency``. Deterministic precheck outcomes
        (UNSUPPORTED target-kind mismatch, UNAVAILABLE registry miss) are
        produced inline without involving the executor, so no provider call
        is scheduled for those rules.

        Diagnostic ordering matches ``ruleset.rules`` regardless of
        concurrency: futures are submitted in rule order and their results
        are collected in submission order, so out-of-order completion
        cannot perturb the report. Backend exceptions still become
        :data:`Status.ERROR` diagnostics at the original rule position.

        Must be ``>= 1``. This flag does not change provider cost per
        request and is not a Batch-API substitute; it only reduces
        wall-clock by overlapping otherwise-independent calls.
    deterministic:
        When ``True``, forward ``deterministic=True`` to LLM-rubric checks
        (#69, Slice A).  The backend injects provider-specific sampling
        parameters (e.g. ``temperature=0.0``) where the capability table
        says they are supported; unsupported combinations (reasoning-class
        OpenAI models) receive no extra params but still record the flag in
        evidence.  Non-LLM backends ignore this parameter.
    eval_cache:
        When ``True``, enable the content-addressed local evaluation cache
        (#69, Slice B).  On a cache hit, the stored ``Diagnostic`` is
        returned with zero provider calls and a ``cache_hit`` evidence record
        appended; on a miss the full evaluation runs and the result is stored
        (PASS/FAIL only — UNAVAILABLE/ERROR are never cached). Default
        ``False`` preserves prior behaviour byte-for-byte. The cache is
        stored under ``.gate-keeper/cache/eval/`` relative to the working
        directory. Can also be enabled project-wide via
        ``GATE_KEEPER_EVAL_CACHE=1`` in the project dotenv (dotenv value
        applies before this parameter in the CLI layer; programmatic callers
        must set this flag explicitly).  Non-LLM backends are unaffected.
    repo_root:
        Repository root that per-rule ``params.target_scope`` globs expand
        against (#279, ``docs/design/multi-target.md`` §9). Only consulted for
        rules that declare a scope; when ``None`` it is resolved lazily from the
        process cwd (git root, or cwd itself when not in a repo). Rules without a
        scope are unaffected and dispatch byte-for-byte as before.

    Returns
    -------
    DiagnosticReport
        One ``Diagnostic`` per rule, in source order.

    Raises
    ------
    ValueError
        If *backend* is not ``"auto"`` and is not a registered backend name,
        or if *reproducibility* is less than 1, or if *concurrency* is less
        than 1. All other exceptions from backend calls are caught and
        converted to ``Status.ERROR`` diagnostics so the pipeline always
        produces a report.
    """
    if backend != "auto" and not _registry.is_registered(backend):
        raise ValueError(f"unknown backend {backend!r}; registered names: {_registry.BACKEND_NAMES}")
    if reproducibility < 1:
        raise ValueError(f"reproducibility must be >= 1, got {reproducibility}")
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    scope_ctx = _ScopeContext(target, repo_root)

    if concurrency == 1:
        return _validate_sequential(
            ruleset,
            target,
            backend,
            reproducibility,
            artifact_kind,
            scope_ctx,
            deterministic=deterministic,
            eval_cache=eval_cache,
        )
    return _validate_concurrent(
        ruleset,
        target,
        backend,
        reproducibility,
        artifact_kind,
        concurrency,
        scope_ctx,
        deterministic=deterministic,
        eval_cache=eval_cache,
    )


def _validate_sequential(
    ruleset: RuleSet,
    target: str | Path | TargetSpec,
    backend: str,
    reproducibility: int,
    artifact_kind: TargetKind | None,
    scope_ctx: _ScopeContext,
    *,
    deterministic: bool = False,
    eval_cache: bool = False,
) -> DiagnosticReport:
    """Sequential dispatch — the historical default path.

    Kept as its own function so ``concurrency == 1`` callers exercise
    exactly the prior code shape, with no executor in the call stack.
    """
    diagnostics: list[Diagnostic] = []
    for rule in ruleset.rules:
        resolved_name = _resolve_backend_name(rule, backend)

        # #178 — deterministic target-kind-mismatch precheck. Applied only
        # to rules routed to the llm-rubric backend that carry an explicit
        # ``target_kind`` annotation (UNSPECIFIED rules preserve the
        # prompt-level fallback). The check is performed here, before
        # dispatch, so no provider call is made on mismatch — the test
        # suite asserts this by counting calls into the stubbed provider.
        # (#279 §9.7: this precheck stays run-level and untouched; per-rule
        # scope resolution below layers on top of the rules it lets through.)
        if (
            artifact_kind is not None
            and resolved_name == "llm-rubric"
            and rule.target_kind is not TargetKind.UNSPECIFIED
            and rule.target_kind is not artifact_kind
        ):
            diagnostics.append(target_kind_mismatch_diagnostic(rule, artifact_kind))
            continue

        # #279 — per-rule target scope. Unscoped rules dispatch against the
        # run-level target verbatim; scoped rules dispatch against their own
        # effective set or short-circuit to scope_empty / scope_invalid /
        # scope_file_limit_exceeded without a backend call.
        plan = _plan_rule_dispatch(rule, target, scope_ctx)
        if plan.short_circuit is not None:
            diagnostics.append(plan.short_circuit)
            continue

        check_fn = _registry.get(resolved_name)
        if check_fn is None:
            diag = _registry_miss_diagnostic(rule, resolved_name)
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
                #
                # #69: forward ``deterministic`` for the llm-rubric route.
                rule_artifact_kind = artifact_kind if resolved_name == "llm-rubric" else None
                rule_deterministic = deterministic if resolved_name == "llm-rubric" else False

                # #69 Slice B — eval-cache intercept. Check before calling
                # _run_rule_check (which would issue provider calls). On a
                # hit, return the cached diagnostic immediately without
                # entering _run_n or the strategy dispatch. Non-LLM backends
                # are unaffected (eval_cache is only consulted for llm-rubric).
                if eval_cache and resolved_name == "llm-rubric":
                    from gate_keeper.backends.eval_cache import (  # noqa: PLC0415
                        try_lookup,
                        try_store,
                    )

                    # Keyed on the dispatched target (plan.target — the per-rule
                    # effective set for a scoped rule); scope evidence is
                    # preserved on the hit (#279).
                    cached = try_lookup(
                        rule,
                        plan.target,
                        rule_artifact_kind,
                        rule_deterministic,
                        reproducibility,
                    )
                    if cached is not None:
                        diagnostics.append(_append_scope_evidence(cached, plan.scope_evidence))
                        continue

                diag = _run_rule_check(
                    check_fn,
                    rule,
                    plan.target,
                    resolved_name,
                    reproducibility,
                    rule_artifact_kind,
                    deterministic=rule_deterministic,
                )

                # Store the result on a miss (PASS/FAIL only; UNAVAILABLE/ERROR
                # are silently skipped inside try_store per §2.4). Keyed on the
                # dispatched target for scope correctness.
                if eval_cache and resolved_name == "llm-rubric":
                    try_store(
                        rule,
                        plan.target,
                        rule_artifact_kind,
                        rule_deterministic,
                        reproducibility,
                        diag,
                    )
            except Exception as exc:  # noqa: BLE001
                diag = _error_diagnostic(rule, exc, _backend_for(resolved_name))
        diagnostics.append(_append_scope_evidence(diag, plan.scope_evidence))

    return DiagnosticReport(diagnostics=diagnostics)


def _validate_concurrent(
    ruleset: RuleSet,
    target: str | Path | TargetSpec,
    backend: str,
    reproducibility: int,
    artifact_kind: TargetKind | None,
    concurrency: int,
    scope_ctx: _ScopeContext,
    *,
    deterministic: bool = False,
    eval_cache: bool = False,
) -> DiagnosticReport:
    """Bounded-parallel dispatch via :class:`ThreadPoolExecutor` (#249, Slice 1).

    Deterministic prechecks (target-kind mismatch, registry miss) and per-rule
    scope resolution (#279) run synchronously in the main loop and their
    diagnostics are recorded directly. Surviving rules are submitted to the
    executor in rule order. Results are collected in submission order so report
    ordering matches ``ruleset.rules`` regardless of completion order. Backend
    exceptions raised inside a future are re-raised by ``future.result()`` and
    caught here, mirroring the sequential ``except Exception`` arm. A scoped
    rule's ``scope_effective_set`` evidence is appended to its resolved
    diagnostic during collection (including the error and cache-hit paths).

    Eval-cache (#69 Slice B) lookups happen before scheduling each future: a
    cache hit short-circuits to a finalised ``Diagnostic`` in the slot without
    involving the executor. Cache stores happen in the post-executor pass after
    resolving each future's result. Both cache operations key on the rule's
    dispatched target (``plan.target`` — the per-rule effective set for a scoped
    rule), not the run-level target.
    """
    # Late import keeps the sequential path's import footprint unchanged.
    from concurrent.futures import Future, ThreadPoolExecutor

    if eval_cache:
        from gate_keeper.backends.eval_cache import try_lookup, try_store  # noqa: PLC0415
    else:
        try_lookup = try_store = None  # type: ignore[assignment]

    # Mixed list of finalised diagnostics (deterministic prechecks / scope
    # short-circuits / cache hits) and in-flight futures, in rule order. The
    # post-executor pass replaces each future with its resolved diagnostic at
    # the same index. Each future slot carries the rule and resolved_name (for
    # error reporting), rule_artifact_kind / rule_deterministic / dispatch_target
    # (for cache stores), and scope_evidence (appended to the resolved verdict).
    slots: list[
        Diagnostic
        | tuple[
            Future[Diagnostic],
            Rule,
            str,
            TargetKind | None,
            bool,
            str | Path | TargetSpec,
            Evidence | None,
        ]
    ] = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for rule in ruleset.rules:
            resolved_name = _resolve_backend_name(rule, backend)

            # #279 §9.7: target-kind precheck stays run-level and untouched.
            if (
                artifact_kind is not None
                and resolved_name == "llm-rubric"
                and rule.target_kind is not TargetKind.UNSPECIFIED
                and rule.target_kind is not artifact_kind
            ):
                slots.append(target_kind_mismatch_diagnostic(rule, artifact_kind))
                continue

            # #279 — per-rule target scope, resolved inline (cheap set ops over a
            # memoized expansion) so short-circuits schedule no provider call.
            plan = _plan_rule_dispatch(rule, target, scope_ctx)
            if plan.short_circuit is not None:
                slots.append(plan.short_circuit)
                continue

            check_fn = _registry.get(resolved_name)
            if check_fn is None:
                slots.append(
                    _append_scope_evidence(
                        _registry_miss_diagnostic(rule, resolved_name), plan.scope_evidence
                    )
                )
                continue

            rule_artifact_kind = artifact_kind if resolved_name == "llm-rubric" else None
            rule_deterministic = deterministic if resolved_name == "llm-rubric" else False

            # #69 Slice B — cache lookup before scheduling.  A hit avoids
            # submitting a future entirely; the cached diagnostic is stored
            # directly in the slot as a finalised Diagnostic. Keyed on the
            # dispatched target (plan.target — the per-rule effective set for a
            # scoped rule); scope evidence is preserved on the hit (#279).
            if eval_cache and resolved_name == "llm-rubric" and try_lookup is not None:
                cached = try_lookup(
                    rule,
                    plan.target,
                    rule_artifact_kind,
                    rule_deterministic,
                    reproducibility,
                )
                if cached is not None:
                    slots.append(_append_scope_evidence(cached, plan.scope_evidence))
                    continue

            future = executor.submit(
                _run_rule_check,
                check_fn,
                rule,
                plan.target,
                resolved_name,
                reproducibility,
                rule_artifact_kind,
                deterministic=rule_deterministic,
            )
            slots.append(
                (
                    future,
                    rule,
                    resolved_name,
                    rule_artifact_kind,
                    rule_deterministic,
                    plan.target,
                    plan.scope_evidence,
                )
            )

    diagnostics: list[Diagnostic] = []
    for slot in slots:
        if isinstance(slot, Diagnostic):
            diagnostics.append(slot)
            continue
        (
            future,
            rule,
            resolved_name,
            rule_artifact_kind,
            rule_deterministic,
            dispatch_target,
            scope_evidence,
        ) = slot
        try:
            diag = future.result()
        except Exception as exc:  # noqa: BLE001
            diag = _error_diagnostic(rule, exc, _backend_for(resolved_name))

        # #69 Slice B — cache store after resolving the future. Keyed on the
        # dispatched target so a scoped rule caches against its effective set.
        if eval_cache and resolved_name == "llm-rubric" and try_store is not None:
            try_store(
                rule,
                dispatch_target,
                rule_artifact_kind,
                rule_deterministic,
                reproducibility,
                diag,
            )

        # Append scope evidence even on the error path so a scoped rule's
        # effective set stays auditable, matching the sequential branch (codex
        # P2 review on #289).
        diagnostics.append(_append_scope_evidence(diag, scope_evidence))

    return DiagnosticReport(diagnostics=diagnostics)


def _registry_miss_diagnostic(rule: Rule, resolved_name: str) -> Diagnostic:
    """Build an ``UNAVAILABLE`` diagnostic for an unregistered backend name.

    Defensive path: name resolved from ``backend_hint`` is not registered.
    Attribute the diagnostic to the IR ``Backend`` that *should* have
    handled it when the name maps to one; otherwise fall back to
    ``FILESYSTEM`` (the local-only backend) so output stays renderable.
    """
    try:
        attributed = _backend_for(resolved_name)
    except ValueError:
        attributed = Backend.FILESYSTEM
    return Diagnostic(
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


__all__ = ["validate"]
