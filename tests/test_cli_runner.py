"""Tests for the tool-agnostic CLI runner (gate_keeper.backends._cli).

Covers:
- run_cli: missing binary, non-zero exit, timeout, OS error, optional redactor
- classify_cli_failure: each CliFault category, signal-terminated runs
- Diagnostic builders: backend, status, evidence shape, fail-closed status mapping
- failure_diag dispatch
- Round-trip via Diagnostic.from_dict / to_dict
- Redactor applied to stderr and to evidence['cmd']
"""

from __future__ import annotations

import subprocess

from gate_keeper.backends._cli import (
    CliFault,
    CliResult,
    classify_cli_failure,
    cli_failed_diag,
    cli_missing_diag,
    cli_os_error_diag,
    cli_timeout_diag,
    failure_diag,
    run_cli,
)
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(rule_id: str = "test-rule") -> Rule:
    return Rule(
        id=rule_id,
        title="Test CLI rule",
        source=SourceLocation(path="rules.md", line=1),
        text="some rule text",
        kind=RuleKind.TEXT_REQUIRED,
        severity=Severity.ERROR,
        backend_hint=Backend.FILESYSTEM,
        confidence=Confidence.HIGH,
        params={},
    )


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["tool"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# run_cli — binary missing
# ---------------------------------------------------------------------------


class TestRunCliMissing:
    def test_file_not_found_returns_failed_result(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("not found")),
        )
        result = run_cli("textlint", ["README.md"])
        assert result.ok is False
        assert result.binary_missing is True
        assert result.timed_out is False
        assert result.returncode == 127
        assert result.cmd == ("textlint", "README.md")
        assert "textlint" in result.stderr

    def test_missing_classifies_as_missing(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()),
        )
        result = run_cli("textlint", ["README.md"])
        assert classify_cli_failure(result) is CliFault.MISSING


# ---------------------------------------------------------------------------
# run_cli — non-zero exit
# ---------------------------------------------------------------------------


class TestRunCliNonZeroExit:
    def test_nonzero_exit_ok_false(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr="bad input", returncode=2),
        )
        result = run_cli("textlint", ["x"])
        assert result.ok is False
        assert result.returncode == 2
        assert result.stderr == "bad input"
        assert result.binary_missing is False
        assert result.timed_out is False

    def test_zero_exit_ok_true(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stdout="all good", returncode=0),
        )
        result = run_cli("textlint", [])
        assert result.ok is True
        assert result.returncode == 0
        assert result.stdout == "all good"

    def test_argv_starts_with_executable(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _make_completed(returncode=1))
        result = run_cli("eslint", ["--fix", "src/"])
        assert result.cmd == ("eslint", "--fix", "src/")


# ---------------------------------------------------------------------------
# run_cli — timeout and OSError
# ---------------------------------------------------------------------------


class TestRunCliExceptions:
    def test_timeout_returns_failed_result(self, monkeypatch):
        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["tool"], timeout=5.0)

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_cli("tool", [], timeout=5.0)
        assert result.ok is False
        assert result.returncode == -2
        assert result.timed_out is True
        assert result.binary_missing is False

    def test_timeout_classifies_as_timeout(self, monkeypatch):
        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["tool"], timeout=1.0)

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_cli("tool", [], timeout=1.0)
        assert classify_cli_failure(result) is CliFault.TIMEOUT

    def test_os_error_returns_failed_result(self, monkeypatch):
        def _raise(*a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_cli("tool", [])
        assert result.ok is False
        assert result.returncode == -3
        assert result.binary_missing is False
        assert result.timed_out is False
        assert "permission denied" in result.stderr

    def test_os_error_classifies_as_os_error(self, monkeypatch):
        def _raise(*a, **kw):
            raise OSError("nope")

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_cli("tool", [])
        assert classify_cli_failure(result) is CliFault.OS_ERROR


# ---------------------------------------------------------------------------
# run_cli — redactor
# ---------------------------------------------------------------------------


class TestRunCliRedactor:
    def test_default_no_redaction(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr="secret=abc123", returncode=1),
        )
        result = run_cli("tool", [])
        # No redactor passed → stderr unchanged.
        assert result.stderr == "secret=abc123"

    def test_custom_redactor_applied_to_stderr(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr="secret=abc123", returncode=1),
        )
        result = run_cli(
            "tool",
            [],
            redactor=lambda s: s.replace("abc123", "[redacted]"),
        )
        assert "abc123" not in result.stderr
        assert "[redacted]" in result.stderr

    def test_redactor_applied_to_partial_stderr_on_timeout(self, monkeypatch):
        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["tool"], timeout=2.0, stderr="leak=xyz")

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_cli(
            "tool",
            [],
            timeout=2.0,
            redactor=lambda s: s.replace("xyz", "[scrubbed]"),
        )
        assert "xyz" not in result.stderr
        assert "[scrubbed]" in result.stderr


# ---------------------------------------------------------------------------
# classify_cli_failure
# ---------------------------------------------------------------------------


class TestClassifyCliFailure:
    def _result(
        self,
        returncode: int,
        *,
        binary_missing: bool = False,
        timed_out: bool = False,
    ) -> CliResult:
        return CliResult(
            ok=False,
            stdout="",
            stderr="",
            returncode=returncode,
            cmd=("tool",),
            binary_missing=binary_missing,
            timed_out=timed_out,
        )

    def test_binary_missing_flag_is_missing(self):
        assert classify_cli_failure(self._result(127, binary_missing=True)) is CliFault.MISSING

    def test_timed_out_flag_is_timeout(self):
        assert classify_cli_failure(self._result(-2, timed_out=True)) is CliFault.TIMEOUT

    def test_os_error_returncode_is_os_error(self):
        # Returncode -3 without timed_out/binary_missing flags maps to OS_ERROR.
        assert classify_cli_failure(self._result(-3)) is CliFault.OS_ERROR

    def test_arbitrary_nonzero_is_nonzero_exit(self):
        assert classify_cli_failure(self._result(1)) is CliFault.NONZERO_EXIT

    def test_signal_terminated_negative_returncode_is_nonzero_exit(self):
        # SIGHUP terminates with returncode -1; flags are not set, so this
        # must NOT be classified as MISSING or TIMEOUT.
        assert classify_cli_failure(self._result(-1)) is CliFault.NONZERO_EXIT


# ---------------------------------------------------------------------------
# Diagnostic builders
# ---------------------------------------------------------------------------


class TestDiagBuilders:
    def test_cli_missing_diag(self):
        rule = _rule()
        diag = cli_missing_diag(rule, Backend.FILESYSTEM, "textlint")
        assert diag.backend is Backend.FILESYSTEM
        assert diag.status is Status.UNAVAILABLE
        assert diag.severity is rule.severity
        assert len(diag.evidence) == 1
        ev = diag.evidence[0]
        assert ev.kind == "cli_missing"
        assert ev.data["executable"] == "textlint"

    def test_cli_failed_diag_status_unavailable(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="bad input",
            returncode=2,
            cmd=("textlint", "README.md"),
        )
        diag = cli_failed_diag(rule, Backend.FILESYSTEM, "lint", result)
        # Fail-closed: a non-zero exit yields UNAVAILABLE, not FAIL.
        assert diag.status is Status.UNAVAILABLE
        assert diag.backend is Backend.FILESYSTEM
        ev = diag.evidence[0]
        assert ev.kind == "cli_failure"
        assert ev.data["op"] == "lint"
        assert ev.data["executable"] == "textlint"
        assert ev.data["returncode"] == 2
        assert "bad input" in ev.data["stderr_excerpt"]
        assert ev.data["cmd"] == "textlint README.md"

    def test_cli_failed_diag_truncates_long_stderr(self):
        rule = _rule()
        long_stderr = "x" * 5000
        result = CliResult(ok=False, stdout="", stderr=long_stderr, returncode=1, cmd=("tool",))
        diag = cli_failed_diag(rule, Backend.FILESYSTEM, "op", result)
        excerpt = diag.evidence[0].data["stderr_excerpt"]
        assert len(excerpt) < len(long_stderr)
        assert excerpt.endswith("…")

    def test_cli_timeout_diag_status_error(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="timed out after 5.0s",
            returncode=-2,
            cmd=("tool",),
            timed_out=True,
        )
        diag = cli_timeout_diag(rule, Backend.FILESYSTEM, "op", result)
        # Timeout is an environmental failure → ERROR, not UNAVAILABLE.
        assert diag.status is Status.ERROR
        assert diag.evidence[0].kind == "cli_timeout"

    def test_cli_os_error_diag_status_error(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="OSError: nope",
            returncode=-3,
            cmd=("tool",),
        )
        diag = cli_os_error_diag(rule, Backend.FILESYSTEM, "op", result)
        assert diag.status is Status.ERROR
        assert diag.evidence[0].kind == "cli_os_error"

    def test_redactor_applied_to_evidence_cmd(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="",
            returncode=1,
            cmd=("tool", "--token", "supersecret"),
        )
        diag = cli_failed_diag(
            rule,
            Backend.FILESYSTEM,
            "op",
            result,
            redactor=lambda s: s.replace("supersecret", "[redacted]"),
        )
        cmd_value = diag.evidence[0].data["cmd"]
        assert "supersecret" not in cmd_value
        assert "[redacted]" in cmd_value

    def test_diag_propagates_rule_source_and_severity(self):
        rule = Rule(
            id="r1",
            title="t",
            source=SourceLocation(path="a/b.md", line=42, heading="H"),
            text="t",
            kind=RuleKind.TEXT_REQUIRED,
            severity=Severity.WARNING,
            backend_hint=Backend.FILESYSTEM,
            confidence=Confidence.LOW,
            params={},
        )
        diag = cli_missing_diag(rule, Backend.FILESYSTEM, "tool")
        assert diag.rule_id == "r1"
        assert diag.source.path == "a/b.md"
        assert diag.source.line == 42
        assert diag.severity is Severity.WARNING


# ---------------------------------------------------------------------------
# failure_diag dispatch
# ---------------------------------------------------------------------------


class TestFailureDiagDispatch:
    def test_missing_routes_to_cli_missing(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="not found",
            returncode=127,
            cmd=("textlint",),
            binary_missing=True,
        )
        diag = failure_diag(rule, Backend.FILESYSTEM, "lint", result)
        assert diag.evidence[0].kind == "cli_missing"
        assert diag.evidence[0].data["executable"] == "textlint"
        assert diag.status is Status.UNAVAILABLE

    def test_timeout_routes_to_cli_timeout(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="timed out",
            returncode=-2,
            cmd=("tool",),
            timed_out=True,
        )
        diag = failure_diag(rule, Backend.FILESYSTEM, "lint", result)
        assert diag.evidence[0].kind == "cli_timeout"
        assert diag.status is Status.ERROR

    def test_os_error_routes_to_cli_os_error(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="OSError: x",
            returncode=-3,
            cmd=("tool",),
        )
        diag = failure_diag(rule, Backend.FILESYSTEM, "lint", result)
        assert diag.evidence[0].kind == "cli_os_error"
        assert diag.status is Status.ERROR

    def test_generic_nonzero_routes_to_cli_failure(self):
        rule = _rule()
        result = CliResult(ok=False, stdout="", stderr="boom", returncode=1, cmd=("tool",))
        diag = failure_diag(rule, Backend.FILESYSTEM, "lint", result)
        assert diag.evidence[0].kind == "cli_failure"
        assert diag.status is Status.UNAVAILABLE

    def test_dispatch_passes_redactor_through(self):
        rule = _rule()
        result = CliResult(
            ok=False,
            stdout="",
            stderr="leaked=topsecret",
            returncode=1,
            cmd=("tool", "--key", "topsecret"),
        )
        diag = failure_diag(
            rule,
            Backend.FILESYSTEM,
            "lint",
            result,
            redactor=lambda s: s.replace("topsecret", "[X]"),
        )
        assert "topsecret" not in diag.evidence[0].data["cmd"]


# ---------------------------------------------------------------------------
# Round-trip schema compliance
# ---------------------------------------------------------------------------


class TestDiagRoundTrip:
    def _round_trip(self, diag: Diagnostic) -> Diagnostic:
        return Diagnostic.from_dict(diag.to_dict())

    def test_cli_missing_diag_round_trips(self):
        diag = cli_missing_diag(_rule(), Backend.FILESYSTEM, "textlint")
        rebuilt = self._round_trip(diag)
        assert rebuilt.status is Status.UNAVAILABLE
        assert rebuilt.backend is Backend.FILESYSTEM
        assert rebuilt.evidence[0].kind == "cli_missing"
        assert rebuilt.evidence[0].data["executable"] == "textlint"

    def test_cli_failed_diag_round_trips(self):
        result = CliResult(ok=False, stdout="", stderr="err", returncode=1, cmd=("tool", "arg"))
        diag = cli_failed_diag(_rule(), Backend.FILESYSTEM, "op", result)
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].data["op"] == "op"
        assert rebuilt.evidence[0].data["returncode"] == 1

    def test_cli_timeout_diag_round_trips(self):
        result = CliResult(ok=False, stdout="", stderr="t", returncode=-2, cmd=("t",), timed_out=True)
        diag = cli_timeout_diag(_rule(), Backend.FILESYSTEM, "op", result)
        rebuilt = self._round_trip(diag)
        assert rebuilt.status is Status.ERROR
        assert rebuilt.evidence[0].kind == "cli_timeout"

    def test_cli_os_error_diag_round_trips(self):
        result = CliResult(ok=False, stdout="", stderr="o", returncode=-3, cmd=("t",))
        diag = cli_os_error_diag(_rule(), Backend.FILESYSTEM, "op", result)
        rebuilt = self._round_trip(diag)
        assert rebuilt.status is Status.ERROR
        assert rebuilt.evidence[0].kind == "cli_os_error"
