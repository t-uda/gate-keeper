"""LLM-rubric backend stub for gate-keeper (MVP placeholder).

Provider-specific implementation is out of scope for the MVP. This stub is
registered so that `--backend llm-rubric` is a valid choice. All rules return
Status.UNAVAILABLE (fail-closed) until a provider is configured.
"""
from __future__ import annotations

from pathlib import Path

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

name = "llm-rubric"


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Return UNAVAILABLE; no LLM provider is configured for the MVP."""
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message="LLM rubric backend is not configured; skipping rule.",
        evidence=[
            Evidence(
                kind="backend_stub",
                data={"backend": "llm-rubric", "rule_kind": rule.kind.value},
            )
        ],
    )
