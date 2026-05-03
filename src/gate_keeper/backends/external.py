"""Backend.EXTERNAL — adapter-pattern contract for third-party tools.

Third-party validation tools (textlint, vale, eslint, …) integrate as adapters
behind this single backend rather than minting one ``Backend`` enum value per
tool. The umbrella tracking issue is #80; this module is the foundation only —
no adapter implementations live here.

Routing
-------
``validate`` reaches this module via the ``backends`` registry under the name
``external``. The dispatcher routes a rule to the adapter named by
``rule.params["tool"]``.

Params contract
---------------
Rules with ``backend_hint == external`` and ``kind == external_check`` MUST set
``params["tool"]`` to a registered adapter id (string). All other ``params``
keys are forwarded verbatim to the adapter, which owns its own per-tool keys.

Fail-closed behaviour
---------------------
The dispatcher never raises:

- missing or non-string ``params.tool`` → ``UNAVAILABLE`` /
  ``params_error`` evidence;
- unknown ``params.tool`` (no adapter registered) → ``UNSUPPORTED`` /
  ``adapter_unknown`` evidence (lists currently registered adapters);
- adapter raises → ``UNAVAILABLE`` / ``adapter_error`` evidence (never
  surfaces ``PASS`` or crashes the orchestrator);
- a rule whose ``kind`` is not ``external_check`` → ``UNSUPPORTED`` /
  ``backend_capability`` evidence, mirroring the filesystem backend.

The registry is process-local and intentionally empty by default. Tests must
restore the registry they touch (use ``snapshot_adapters`` /
``restore_adapters`` or ``clear_adapters``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from gate_keeper.models import (
    Backend,
    Diagnostic,
    Evidence,
    Rule,
    RuleKind,
    Status,
)

name = "external"


@runtime_checkable
class ExternalAdapter(Protocol):
    """Protocol every external-tool adapter must satisfy.

    Implementations must never raise: any unexpected error should be returned
    as an ``UNAVAILABLE`` ``Diagnostic`` with ``adapter_error`` evidence so the
    overall report stays well-formed. The dispatcher will additionally trap
    raised exceptions as a defence-in-depth measure, but adapters should still
    handle their own errors.
    """

    name: str

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        """Evaluate *rule* against *target* and return a Diagnostic."""
        ...


_ADAPTERS: dict[str, ExternalAdapter] = {}


def register(adapter: ExternalAdapter) -> None:
    """Register *adapter* under ``adapter.name``.

    Raises ``ValueError`` if the name is empty or already registered. This is a
    programmer-error path that surfaces during application setup, not during
    rule evaluation, so raising here is safe.
    """
    if not isinstance(adapter.name, str) or not adapter.name:
        raise ValueError("ExternalAdapter.name must be a non-empty string")
    if adapter.name in _ADAPTERS:
        raise ValueError(f"external adapter {adapter.name!r} is already registered")
    _ADAPTERS[adapter.name] = adapter


def unregister(adapter_name: str) -> None:
    """Remove the adapter registered under *adapter_name* (no-op if absent)."""
    _ADAPTERS.pop(adapter_name, None)


def clear_adapters() -> None:
    """Drop every registered adapter. Intended for test isolation."""
    _ADAPTERS.clear()


def adapter_names() -> list[str]:
    """Return a sorted snapshot of currently registered adapter ids."""
    return sorted(_ADAPTERS)


def snapshot_adapters() -> dict[str, ExternalAdapter]:
    """Return a shallow copy of the current registry (for test save/restore)."""
    return dict(_ADAPTERS)


def restore_adapters(snapshot: dict[str, ExternalAdapter]) -> None:
    """Replace the registry with *snapshot* (intended for test teardown)."""
    _ADAPTERS.clear()
    _ADAPTERS.update(snapshot)


def _diag(
    rule: Rule,
    status: Status,
    message: str,
    evidence: list[Evidence],
    remediation: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.EXTERNAL,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
        remediation=remediation,
    )


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Dispatch *rule* to the adapter named by ``rule.params['tool']``.

    See module docstring for the full fail-closed contract.
    """
    if rule.kind is not RuleKind.EXTERNAL_CHECK:
        return _diag(
            rule,
            Status.UNSUPPORTED,
            f"rule kind {rule.kind.value!r} is not supported by the external backend",
            [Evidence(kind="backend_capability", data={"backend": name, "kind": rule.kind.value})],
        )

    tool = rule.params.get("tool")
    if not isinstance(tool, str) or not tool:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.tool is required for external_check but was not provided",
            [Evidence(kind="params_error", data={"missing": "tool"})],
            remediation=(
                "Set params.tool to a registered external adapter id. See "
                "docs/backend-external.md for the contract."
            ),
        )

    adapter = _ADAPTERS.get(tool)
    if adapter is None:
        return _diag(
            rule,
            Status.UNSUPPORTED,
            f"no external adapter registered for tool {tool!r}",
            [
                Evidence(
                    kind="adapter_unknown",
                    data={"tool": tool, "registered": adapter_names()},
                )
            ],
            remediation=(
                "Register an adapter for this tool before validating, or remove the rule. "
                "See docs/backend-external.md."
            ),
        )

    try:
        return adapter.check(rule, target)
    except Exception as exc:  # noqa: BLE001 — fail-closed: never let adapter errors crash validation
        return _diag(
            rule,
            Status.UNAVAILABLE,
            f"external adapter {tool!r} raised; treating as unavailable",
            [
                Evidence(
                    kind="adapter_error",
                    data={
                        "tool": tool,
                        "type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                )
            ],
            remediation=(
                "Investigate the adapter exception (see evidence) and rerun once the adapter is healthy."
            ),
        )


__all__ = [
    "ExternalAdapter",
    "adapter_names",
    "check",
    "clear_adapters",
    "name",
    "register",
    "restore_adapters",
    "snapshot_adapters",
    "unregister",
]
