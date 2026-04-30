"""GitHub backend stub for gate-keeper (MVP placeholder).

The full implementation comes in a later issue. This stub is registered so
that `--backend github` is a valid choice rather than a usage error. All rules
return Status.UNAVAILABLE until the real implementation lands.
"""
from __future__ import annotations

from pathlib import Path

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

name = "github"


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Return UNAVAILABLE; the GitHub backend is not yet implemented."""
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.GITHUB,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message="GitHub backend is not yet implemented; skipping rule.",
        evidence=[
            Evidence(
                kind="backend_stub",
                data={"backend": "github", "rule_kind": rule.kind.value},
            )
        ],
    )
