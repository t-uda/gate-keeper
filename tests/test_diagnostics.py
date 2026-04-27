from __future__ import annotations

from gate_keeper.diagnostics import exit_code_for_report, render_diagnostic_json, render_diagnostic_text
from gate_keeper.models import Backend, Diagnostic, DiagnosticReport, Evidence, Severity, SourceLocation, Status


def _report(status: Status = Status.PASS) -> DiagnosticReport:
    return DiagnosticReport(
        diagnostics=[
            Diagnostic(
                rule_id="r1",
                source=SourceLocation(path="doc.md", line=12, heading="Rules"),
                backend=Backend.FILESYSTEM,
                status=status,
                severity=Severity.ERROR,
                message="missing evidence",
                evidence=[Evidence(kind="text_match", data={"match_count": 0})],
            )
        ]
    )


def test_text_renderer_includes_required_fields():
    text = render_diagnostic_text(_report(Status.FAIL))
    assert "doc.md:12" in text
    assert "r1" in text
    assert "filesystem" in text
    assert "fail/error" in text
    assert "evidence[text_match]" in text


def test_json_renderer_round_trips():
    report = _report(Status.UNAVAILABLE)
    json_text = render_diagnostic_json(report)
    assert '"status": "unavailable"' in json_text
    assert '"rule_id": "r1"' in json_text


def test_exit_code_policy():
    assert exit_code_for_report(_report(Status.PASS)) == 0
    assert exit_code_for_report(_report(Status.FAIL)) == 1
    assert exit_code_for_report(_report(Status.UNAVAILABLE)) == 1
    assert exit_code_for_report(_report(Status.UNSUPPORTED)) == 1
