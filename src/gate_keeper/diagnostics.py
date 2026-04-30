"""Diagnostic renderers and exit-code policy for gate-keeper."""

from __future__ import annotations

import json
from typing import Sequence

from gate_keeper.models import Diagnostic, DiagnosticReport, Status

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_NON_PASS = frozenset({Status.FAIL, Status.UNAVAILABLE, Status.UNSUPPORTED, Status.ERROR})


def compute_exit_code(diagnostics: Sequence[Diagnostic]) -> int:
    """Return EXIT_OK if all diagnostics pass, EXIT_FAIL if any do not."""
    for d in diagnostics:
        if d.status in _NON_PASS:
            return EXIT_FAIL
    return EXIT_OK


def usage_error(message: str) -> tuple[str, int]:
    """Format a CLI usage/input error and return (text, EXIT_USAGE)."""
    return f"error: {message}", EXIT_USAGE


def _compact_evidence(diagnostic: Diagnostic) -> str:
    parts = []
    for e in diagnostic.evidence:
        if e.data:
            pairs = ", ".join(f"{k}={v}" for k, v in e.data.items())
            parts.append(f"{e.kind}({pairs})")
        else:
            parts.append(e.kind)
    return "; ".join(parts)


def render_text(diagnostics: Sequence[Diagnostic]) -> str:
    """Render diagnostics as compiler-style text lines.

    Each line: path:line: severity: [backend/status] rule_id: message [evidence]
    """
    lines = []
    for d in diagnostics:
        evidence_str = _compact_evidence(d)
        evidence_part = f" [{evidence_str}]" if evidence_str else ""
        lines.append(
            f"{d.source.path}:{d.source.line}: "
            f"{d.severity.value}: "
            f"[{d.backend.value}/{d.status.value}] "
            f"{d.rule_id}: "
            f"{d.message}"
            f"{evidence_part}"
        )
    return "\n".join(lines)


def render_json(diagnostics: Sequence[Diagnostic]) -> str:
    """Render diagnostics as deterministic JSON for CI consumers.

    The status field is preserved exactly — unavailable is distinct from fail.
    """
    report = DiagnosticReport(diagnostics=list(diagnostics))
    return json.dumps(report.to_dict(), sort_keys=True, indent=2)
