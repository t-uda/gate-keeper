from __future__ import annotations

import json
from dataclasses import asdict

from gate_keeper.models import Diagnostic, DiagnosticReport, Status

_FAILURE_STATUSES = {
    Status.FAIL,
    Status.UNAVAILABLE,
    Status.UNSUPPORTED,
    Status.ERROR,
}


def render_diagnostic_text(report: DiagnosticReport) -> str:
    lines: list[str] = []
    for diagnostic in report.diagnostics:
        source = diagnostic.source
        header = (
            f"{source.path}:{source.line}: "
            f"[{diagnostic.backend.value}] {diagnostic.rule_id} "
            f"{diagnostic.status.value}/{diagnostic.severity.value}: {diagnostic.message}"
        )
        if source.heading:
            header += f" ({source.heading})"
        lines.append(header)
        for evidence in diagnostic.evidence:
            payload = json.dumps(evidence.data, sort_keys=True)
            lines.append(f"  evidence[{evidence.kind}]: {payload}")
        if diagnostic.remediation:
            lines.append(f"  remediation: {diagnostic.remediation}")
    return "\n".join(lines)


def render_diagnostic_json(report: DiagnosticReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def exit_code_for_report(report: DiagnosticReport) -> int:
    if any(diagnostic.status in _FAILURE_STATUSES for diagnostic in report.diagnostics):
        return 1
    return 0


def diagnostic_from_error(rule_id: str, message: str, *, severity: str = "error") -> Diagnostic:
    from gate_keeper.models import Backend, Evidence, Severity, SourceLocation, Status

    return Diagnostic(
        rule_id=rule_id,
        source=SourceLocation(path="<cli>", line=1),
        backend=Backend.FILESYSTEM,
        status=Status.ERROR,
        severity=Severity(severity),
        message=message,
        evidence=[Evidence(kind="cli_error", data={"message": message})],
    )


__all__ = ["render_diagnostic_text", "render_diagnostic_json", "exit_code_for_report"]
