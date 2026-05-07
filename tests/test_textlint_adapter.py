"""Tests for ``TextlintAdapter`` (#94).

Unit tests stub ``run_cli`` so no real textlint subprocess is required.
The integration test class runs against a real ``npx textlint`` install but
is guarded by ``shutil.which("npx")`` so CI without Node stays green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper.adapters.textlint import (
    TextlintAdapter,
    textlint_severity_to_gate_keeper,
)
from gate_keeper.backends._cli import CliResult
from gate_keeper.backends.external import ExternalAdapter
from gate_keeper.models import (
    Backend,
    Confidence,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)


def _rule(severity: Severity = Severity.ERROR, **params: object) -> Rule:
    return Rule(
        id="test-rule",
        title="textlint test rule",
        source=SourceLocation(path="test.md", line=1),
        text="textlint must pass",
        kind=RuleKind.EXTERNAL_CHECK,
        severity=severity,
        backend_hint=Backend.EXTERNAL,
        confidence=Confidence.HIGH,
        params={"tool": "textlint", **params},
    )


def _ok_result(stdout: str) -> CliResult:
    return CliResult(
        ok=True,
        stdout=stdout,
        stderr="",
        returncode=0,
        cmd=("npx", "textlint", "--format", "json", "target.md"),
    )


def _violation_result(stdout: str) -> CliResult:
    """textlint exits non-zero when violations are present but emits valid JSON."""
    return CliResult(
        ok=False,
        stdout=stdout,
        stderr="",
        returncode=1,
        cmd=("npx", "textlint", "--format", "json", "target.md"),
    )


def _binary_missing_result() -> CliResult:
    return CliResult(
        ok=False,
        stdout="",
        stderr="'npx' binary not found",
        returncode=127,
        cmd=("npx", "textlint", "--format", "json", "target.md"),
        binary_missing=True,
    )


def _failure_result(stderr: str) -> CliResult:
    """textlint failed to run (e.g. invalid config) — non-zero exit, empty stdout."""
    return CliResult(
        ok=False,
        stdout="",
        stderr=stderr,
        returncode=1,
        cmd=("npx", "textlint", "--format", "json", "target.md"),
    )


@pytest.fixture
def patch_run_cli(monkeypatch):
    """Patch the ``run_cli`` symbol used inside the adapter module."""

    def _patch(stub):
        from gate_keeper.adapters import textlint as adapter_module

        monkeypatch.setattr(adapter_module, "run_cli", stub)

    return _patch


class TestProtocolConformance:
    def test_adapter_satisfies_external_adapter_protocol(self):
        assert isinstance(TextlintAdapter(), ExternalAdapter)

    def test_adapter_name_matches_expected_id(self):
        assert TextlintAdapter().name == "textlint"


class TestPassPath:
    def test_clean_target_returns_pass_no_evidence(self, patch_run_cli):
        patch_run_cli(lambda *a, **kw: _ok_result("[]"))
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.PASS
        assert diag.backend is Backend.EXTERNAL
        assert diag.severity is Severity.ERROR
        assert diag.evidence == []
        assert diag.remediation is None

    def test_clean_target_with_per_file_empty_messages(self, patch_run_cli):
        payload = json.dumps(
            [
                {"filePath": "/abs/foo.md", "messages": []},
                {"filePath": "/abs/bar.md", "messages": []},
            ]
        )
        patch_run_cli(lambda *a, **kw: _ok_result(payload))
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.PASS
        assert diag.evidence == []


class TestFailPath:
    def test_two_files_each_with_one_violation_yields_two_evidence(self, patch_run_cli):
        payload = json.dumps(
            [
                {
                    "filePath": "/abs/foo.md",
                    "messages": [
                        {
                            "ruleId": "terminology",
                            "severity": 2,
                            "message": "Use 'JavaScript'",
                            "line": 3,
                            "column": 1,
                            "fix": None,
                        }
                    ],
                },
                {
                    "filePath": "/abs/bar.md",
                    "messages": [
                        {
                            "ruleId": "prh",
                            "severity": 2,
                            "message": "Use 'GitHub'",
                            "line": 5,
                            "column": 8,
                            "fix": {"range": [10, 20], "text": "GitHub"},
                        }
                    ],
                },
            ]
        )
        patch_run_cli(lambda *a, **kw: _violation_result(payload))
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.FAIL
        assert diag.severity is Severity.ERROR
        assert [e.kind for e in diag.evidence] == ["textlint_finding", "textlint_finding"]
        assert diag.evidence[0].data["file"] == "/abs/foo.md"
        assert diag.evidence[0].data["line"] == 3
        assert diag.evidence[0].data["rule_id"] == "terminology"
        assert diag.evidence[0].data["severity_int"] == 2
        assert diag.evidence[0].data["fixable"] is False
        assert diag.evidence[1].data["fixable"] is True
        assert diag.remediation is not None

    def test_severity_int_recorded_for_warning_and_error_findings(self, patch_run_cli):
        payload = json.dumps(
            [
                {
                    "filePath": "/abs/x.md",
                    "messages": [
                        {
                            "ruleId": "r1",
                            "severity": 1,
                            "message": "warn",
                            "line": 1,
                            "column": 1,
                        },
                        {
                            "ruleId": "r2",
                            "severity": 2,
                            "message": "err",
                            "line": 2,
                            "column": 1,
                        },
                    ],
                },
            ]
        )
        patch_run_cli(lambda *a, **kw: _violation_result(payload))
        diag = TextlintAdapter().check(_rule(severity=Severity.WARNING), "target.md")
        # §7 resolution: any finding fails, regardless of message-level severity integer.
        assert diag.status is Status.FAIL
        # Diagnostic.severity echoes rule.severity — not derived from the message ints.
        assert diag.severity is Severity.WARNING
        assert [e.data["severity_int"] for e in diag.evidence] == [1, 2]


class TestFailureModes:
    def test_binary_missing_yields_unavailable_with_cli_missing_evidence(self, patch_run_cli):
        patch_run_cli(lambda *a, **kw: _binary_missing_result())
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "cli_missing"
        assert diag.evidence[0].data["executable"] == "npx"

    def test_failure_no_parseable_stdout_yields_unavailable_with_cli_failure_evidence(self, patch_run_cli):
        patch_run_cli(lambda *a, **kw: _failure_result("textlint: invalid config"))
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "cli_failure"
        assert diag.evidence[0].data["returncode"] == 1
        assert "invalid config" in diag.evidence[0].data["stderr_excerpt"]

    def test_unparseable_stdout_with_zero_exit_yields_parse_error(self, patch_run_cli):
        patch_run_cli(lambda *a, **kw: _ok_result("not json"))
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "parse_error"
        assert "not json" in diag.evidence[0].data["stdout_excerpt"]


class TestTruncation:
    def test_truncates_at_limit_and_appends_truncated_evidence(self, patch_run_cli):
        msgs = [
            {
                "ruleId": f"r{i}",
                "severity": 2,
                "message": f"m{i}",
                "line": i + 1,
                "column": 1,
            }
            for i in range(60)
        ]
        payload = json.dumps([{"filePath": "/abs/big.md", "messages": msgs}])
        patch_run_cli(lambda *a, **kw: _violation_result(payload))
        diag = TextlintAdapter().check(_rule(), "target.md")
        assert diag.status is Status.FAIL
        # 50 textlint_finding + 1 textlint_truncated
        assert len(diag.evidence) == 51
        assert diag.evidence[-1].kind == "textlint_truncated"
        assert diag.evidence[-1].data == {"omitted": 10}
        assert "60 finding(s)" in diag.message
        assert "+10 truncated" in diag.message


class TestParamsForwarding:
    def test_config_param_appended_as_cli_flag(self, monkeypatch):
        captured: dict[str, object] = {}

        def stub(executable, args, *, timeout=None, **kw):
            captured["executable"] = executable
            captured["args"] = list(args)
            captured["timeout"] = timeout
            return _ok_result("[]")

        from gate_keeper.adapters import textlint as adapter_module

        monkeypatch.setattr(adapter_module, "run_cli", stub)
        TextlintAdapter().check(_rule(config="custom.textlintrc"), "target.md")
        args = captured["args"]
        assert isinstance(args, list)
        assert "--config" in args
        assert args[args.index("--config") + 1] == "custom.textlintrc"
        assert captured["executable"] == "npx"

    def test_timeout_param_passed_through(self, monkeypatch):
        captured: dict[str, object] = {}

        def stub(executable, args, *, timeout=None, **kw):
            captured["timeout"] = timeout
            return _ok_result("[]")

        from gate_keeper.adapters import textlint as adapter_module

        monkeypatch.setattr(adapter_module, "run_cli", stub)
        TextlintAdapter().check(_rule(timeout=30), "target.md")
        assert captured["timeout"] == 30.0


class TestSeverityHelper:
    def test_known_strings_map_directly(self):
        assert textlint_severity_to_gate_keeper("error") is Severity.ERROR
        assert textlint_severity_to_gate_keeper("warning") is Severity.WARNING
        assert textlint_severity_to_gate_keeper("info") is Severity.ADVISORY

    def test_unknown_falls_back_to_warning(self):
        assert textlint_severity_to_gate_keeper("notice") is Severity.WARNING
        assert textlint_severity_to_gate_keeper("") is Severity.WARNING


@pytest.mark.integration
class TestIntegration:
    """Real ``npx textlint`` subprocess; guarded by ``shutil.which``."""

    def setup_method(self) -> None:
        import shutil

        if shutil.which("npx") is None:
            pytest.skip("npx not on PATH; integration skipped")

    def test_clean_fixture_passes_or_unavailable(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "textlint" / "clean.md"
        diag = TextlintAdapter().check(_rule(), str(fixture))
        # textlint may not be installed even when npx is present; allow UNAVAILABLE.
        assert diag.status in {Status.PASS, Status.UNAVAILABLE}
        if diag.status is Status.PASS:
            assert diag.evidence == []

    def test_dirty_fixture_fails_or_unavailable(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "textlint" / "dirty.md"
        diag = TextlintAdapter().check(_rule(), str(fixture))
        assert diag.status in {Status.FAIL, Status.UNAVAILABLE}
        if diag.status is Status.FAIL:
            assert any(e.kind == "textlint_finding" for e in diag.evidence)
