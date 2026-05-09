"""Tests for ``CommandAdapter`` (#149).

The command adapter executes project-local commands, so the trust model
matters. These tests pin:

- disabled-by-default unavailability (no subprocess spawned);
- enabled-then-pass with stdin JSON contract;
- enabled-then-fail (non-zero exit is fail-closed);
- enabled-then-fail (zero exit + Status.FAIL JSON);
- invalid params (argv missing / wrong shape / non-string element /
  shell-string rejection / timeout out of range);
- command-not-found (binary missing);
- malformed JSON output;
- timeout fail-closed;
- adapter overwrites rule_id / source / severity / backend on output;
- CLI ``--allow-command-adapter`` flag toggling.

Real subprocess fixtures live under ``tests/fixtures/command/``; unit
tests stub ``run_cli`` so no subprocess is spawned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from gate_keeper.adapters.command import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    CommandAdapter,
    is_enabled,
    set_enabled,
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
    base = {"tool": "command", "argv": ["echo", "hello"]}
    base.update(params)
    return Rule(
        id="test-rule",
        title="command adapter test rule",
        source=SourceLocation(path="rules.md", line=4),
        text="rule text",
        kind=RuleKind.EXTERNAL_CHECK,
        severity=severity,
        backend_hint=Backend.EXTERNAL,
        confidence=Confidence.HIGH,
        params=base,
    )


def _ok_result(stdout: str, stderr: str = "") -> CliResult:
    return CliResult(
        ok=True,
        stdout=stdout,
        stderr=stderr,
        returncode=0,
        cmd=("echo", "hello"),
    )


def _nonzero_result(stdout: str, stderr: str, returncode: int = 2) -> CliResult:
    return CliResult(
        ok=False,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        cmd=("echo", "hello"),
    )


def _binary_missing_result() -> CliResult:
    return CliResult(
        ok=False,
        stdout="",
        stderr="'echo' binary not found",
        returncode=127,
        cmd=("echo", "hello"),
        binary_missing=True,
    )


def _timeout_result() -> CliResult:
    return CliResult(
        ok=False,
        stdout="",
        stderr="timed out after 30s",
        returncode=-2,
        cmd=("echo", "hello"),
        timed_out=True,
    )


@pytest.fixture
def patch_run_cli(monkeypatch):
    """Patch ``run_cli`` in the command adapter module."""

    def _patch(stub):
        from gate_keeper.adapters import command as adapter_module

        monkeypatch.setattr(adapter_module, "run_cli", stub)

    return _patch


@pytest.fixture(autouse=True)
def _reset_enable_flag():
    """Always start tests with the adapter disabled; restore prior state."""
    prior = is_enabled()
    set_enabled(False)
    try:
        yield
    finally:
        set_enabled(prior)


class TestProtocolConformance:
    def test_adapter_satisfies_external_adapter_protocol(self):
        assert isinstance(CommandAdapter(), ExternalAdapter)

    def test_adapter_name_matches_expected_id(self):
        assert CommandAdapter().name == "command"


class TestDisabledByDefault:
    def test_returns_unavailable_when_disabled(self):
        # Default state — no flag, no subprocess.
        diag = CommandAdapter().check(_rule(), "target")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "command_adapter_disabled"
        assert diag.evidence[0].data["flag"] == "--allow-command-adapter"
        assert diag.remediation is not None
        assert "trust" in diag.remediation.lower()

    def test_disabled_does_not_invoke_run_cli(self, patch_run_cli):
        called = {"count": 0}

        def stub(*a, **kw):
            called["count"] += 1
            return _ok_result("{}")

        patch_run_cli(stub)
        CommandAdapter().check(_rule(), "target")
        # Disabled path must short-circuit before any subprocess attempt.
        assert called["count"] == 0


class TestPassPath:
    def test_enabled_pass_diagnostic_parsed_from_stdout(self, patch_run_cli):
        set_enabled(True)
        captured: dict[str, object] = {}

        def stub(executable, args, *, input=None, timeout=None, **kw):
            captured["executable"] = executable
            captured["args"] = list(args)
            captured["timeout"] = timeout
            captured["input"] = input
            return _ok_result(
                json.dumps(
                    {
                        "status": "pass",
                        "message": "all good",
                        "evidence": [{"kind": "ok", "data": {"x": 1}}],
                    }
                )
            )

        patch_run_cli(stub)
        rule = _rule(argv=["python", "tools/check.py", "--strict"])
        diag = CommandAdapter().check(rule, "some/target")
        assert diag.status is Status.PASS
        assert diag.message == "all good"
        assert diag.backend is Backend.EXTERNAL
        assert diag.evidence[0].kind == "ok"
        assert diag.evidence[0].data == {"x": 1}
        # stdin payload contract
        assert isinstance(captured["input"], str)
        stdin_obj = json.loads(captured["input"])  # type: ignore[arg-type]
        assert stdin_obj == {
            "rule": {"id": "test-rule", "text": "rule text", "params": rule.params},
            "target": "some/target",
        }
        # argv goes through verbatim, executable comes from argv[0]
        assert captured["executable"] == "python"
        assert captured["args"] == ["tools/check.py", "--strict"]
        # default timeout
        assert captured["timeout"] == float(DEFAULT_TIMEOUT_SECONDS)

    def test_diagnostic_report_with_one_diagnostic_is_accepted(self, patch_run_cli):
        set_enabled(True)
        patch_run_cli(
            lambda *a, **kw: _ok_result(
                json.dumps(
                    {
                        "diagnostics": [
                            {
                                "status": "pass",
                                "message": "ok",
                                "evidence": [],
                            }
                        ]
                    }
                )
            )
        )
        diag = CommandAdapter().check(_rule(), "x")
        assert diag.status is Status.PASS
        assert diag.message == "ok"

    def test_adapter_overrides_rule_id_source_severity_backend(self, patch_run_cli):
        # A misbehaving command tries to spoof another rule_id; the adapter
        # is the source of truth for these fields.
        set_enabled(True)
        patch_run_cli(
            lambda *a, **kw: _ok_result(
                json.dumps(
                    {
                        "rule_id": "OTHER-RULE",
                        "source": {"path": "x", "line": 99},
                        "backend": "filesystem",
                        "severity": "advisory",
                        "status": "pass",
                        "message": "spoofed",
                        "evidence": [],
                    }
                )
            )
        )
        rule = _rule(severity=Severity.WARNING)
        diag = CommandAdapter().check(rule, "t")
        assert diag.rule_id == rule.id
        assert diag.source == rule.source
        assert diag.backend is Backend.EXTERNAL
        assert diag.severity is Severity.WARNING

    def test_remediation_string_passthrough(self, patch_run_cli):
        set_enabled(True)
        patch_run_cli(
            lambda *a, **kw: _ok_result(
                json.dumps(
                    {
                        "status": "fail",
                        "message": "violation",
                        "evidence": [],
                        "remediation": "do the thing",
                    }
                )
            )
        )
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.FAIL
        assert diag.remediation == "do the thing"


class TestFailPaths:
    def test_nonzero_exit_yields_unavailable_even_if_stdout_parses(self, patch_run_cli):
        # Strict contract: non-zero exit means the command itself faulted, so
        # parseable stdout is ignored. Status collapses to UNAVAILABLE.
        set_enabled(True)
        patch_run_cli(
            lambda *a, **kw: _nonzero_result(
                stdout=json.dumps(
                    {
                        "status": "fail",
                        "message": "would have been fail",
                        "evidence": [],
                    }
                ),
                stderr="boom",
            )
        )
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "command_failure"
        assert diag.evidence[0].data["returncode"] == 2
        assert "boom" in diag.evidence[0].data["stderr_excerpt"]

    def test_command_not_found_is_cli_missing(self, patch_run_cli):
        set_enabled(True)
        patch_run_cli(lambda *a, **kw: _binary_missing_result())
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "cli_missing"

    def test_timeout_is_error(self, patch_run_cli):
        set_enabled(True)
        patch_run_cli(lambda *a, **kw: _timeout_result())
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.ERROR
        assert diag.evidence[0].kind == "cli_timeout"


class TestParamValidation:
    def setup_method(self):
        set_enabled(True)

    def teardown_method(self):
        set_enabled(False)

    def test_argv_missing_is_unavailable(self):
        rule = Rule(
            id="r",
            title="t",
            source=SourceLocation(path="p", line=1),
            text="x",
            kind=RuleKind.EXTERNAL_CHECK,
            severity=Severity.ERROR,
            backend_hint=Backend.EXTERNAL,
            confidence=Confidence.HIGH,
            params={"tool": "command"},
        )
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data["missing"] == "argv"

    def test_argv_shell_string_rejected(self):
        rule = _rule(argv="python tools/check.py")  # type: ignore[arg-type]
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data == {"field": "argv", "type": "str"}
        assert diag.remediation is not None
        assert "list" in diag.remediation

    def test_argv_empty_list_rejected(self):
        rule = _rule(argv=[])
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data == {"field": "argv", "reason": "empty"}

    def test_argv_non_string_element_rejected(self):
        rule = _rule(argv=["python", 42])  # type: ignore[list-item]
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data["field"] == "argv"
        assert diag.evidence[0].data["index"] == 1

    def test_timeout_not_a_number_rejected(self):
        rule = _rule(timeout_seconds="forever")
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["field"] == "timeout_seconds"

    def test_timeout_zero_rejected(self):
        rule = _rule(timeout_seconds=0)
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["field"] == "timeout_seconds"

    def test_timeout_above_max_rejected(self):
        rule = _rule(timeout_seconds=MAX_TIMEOUT_SECONDS + 1)
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["field"] == "timeout_seconds"
        assert diag.evidence[0].data["max"] == MAX_TIMEOUT_SECONDS

    def test_timeout_within_range_passed_through(self, monkeypatch):
        captured: dict[str, object] = {}

        def stub(executable, args, *, input=None, timeout=None, **kw):
            captured["timeout"] = timeout
            return _ok_result(json.dumps({"status": "pass", "message": "ok", "evidence": []}))

        from gate_keeper.adapters import command as adapter_module

        monkeypatch.setattr(adapter_module, "run_cli", stub)
        CommandAdapter().check(_rule(timeout_seconds=10), "t")
        assert captured["timeout"] == 10.0


class TestOutputParsing:
    def setup_method(self):
        set_enabled(True)

    def teardown_method(self):
        set_enabled(False)

    def _stub(self, monkeypatch, stdout: str, stderr: str = ""):
        def stub(*a, **kw):
            return _ok_result(stdout, stderr)

        from gate_keeper.adapters import command as adapter_module

        monkeypatch.setattr(adapter_module, "run_cli", stub)

    def test_empty_stdout_yields_parse_error(self, monkeypatch):
        self._stub(monkeypatch, "")
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "parse_error"
        assert diag.evidence[0].data["reason"] == "empty_stdout"

    def test_invalid_json_yields_parse_error(self, monkeypatch):
        self._stub(monkeypatch, "not json")
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "parse_error"
        assert diag.evidence[0].data["reason"] == "json_decode_error"
        assert "not json" in diag.evidence[0].data["stdout_excerpt"]

    def test_top_level_array_rejected(self, monkeypatch):
        self._stub(monkeypatch, json.dumps([1, 2, 3]))
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "parse_error"

    def test_status_outside_enum_rejected(self, monkeypatch):
        self._stub(monkeypatch, json.dumps({"status": "skipped", "message": "x", "evidence": []}))
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["reason"] == "invalid_status"

    def test_missing_message_rejected(self, monkeypatch):
        self._stub(monkeypatch, json.dumps({"status": "pass", "evidence": []}))
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["reason"] == "invalid_message"

    def test_evidence_must_be_list(self, monkeypatch):
        self._stub(
            monkeypatch,
            json.dumps({"status": "pass", "message": "ok", "evidence": "nope"}),
        )
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["reason"] == "invalid_evidence_list"

    def test_evidence_item_must_have_kind_and_data(self, monkeypatch):
        self._stub(
            monkeypatch,
            json.dumps(
                {
                    "status": "pass",
                    "message": "ok",
                    "evidence": [{"data": {}}],
                }
            ),
        )
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["reason"] == "invalid_evidence_kind"

    def test_remediation_must_be_string_or_null(self, monkeypatch):
        self._stub(
            monkeypatch,
            json.dumps(
                {
                    "status": "pass",
                    "message": "ok",
                    "evidence": [],
                    "remediation": 123,
                }
            ),
        )
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["reason"] == "invalid_remediation"

    def test_diagnostics_array_must_have_exactly_one(self, monkeypatch):
        self._stub(monkeypatch, json.dumps({"diagnostics": []}))
        diag = CommandAdapter().check(_rule(), "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["reason"] == "diagnostic_count"

    def test_stderr_truncated_in_evidence(self, monkeypatch):
        # Long stderr through the parse_error empty-stdout path.
        long_stderr = "x" * 5000
        self._stub(monkeypatch, "", long_stderr)
        diag = CommandAdapter().check(_rule(), "t")
        excerpt = diag.evidence[0].data["stderr_excerpt"]
        assert len(excerpt) <= 1000 + 1  # include ellipsis char


class TestRealSubprocess:
    """End-to-end runs against the fixture commands using ``sys.executable``.

    These exercise the actual subprocess plumbing (stdin JSON, argv list, no
    shell) so the adapter is verified against real stdin/stdout I/O — not
    just stubs.
    """

    fixtures = Path(__file__).parent / "fixtures" / "command"

    def setup_method(self):
        set_enabled(True)

    def teardown_method(self):
        set_enabled(False)

    def test_pass_fixture_runs_and_parses(self):
        rule = _rule(argv=[sys.executable, str(self.fixtures / "pass_command.py")])
        diag = CommandAdapter().check(rule, "some-target")
        assert diag.status is Status.PASS
        assert "some-target" in diag.message
        assert diag.evidence[0].kind == "fixture_pass"
        assert diag.evidence[0].data == {"echo_target": "some-target"}

    def test_fail_status_zero_exit_is_fail(self):
        rule = _rule(argv=[sys.executable, str(self.fixtures / "fail_status_command.py")])
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.FAIL
        assert diag.remediation == "edit the artifact to satisfy the rule"

    def test_nonzero_exit_fixture_yields_unavailable(self):
        rule = _rule(argv=[sys.executable, str(self.fixtures / "fail_command.py")])
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "command_failure"
        assert diag.evidence[0].data["returncode"] != 0

    def test_command_not_found_real(self):
        rule = _rule(argv=["/nonexistent/gate-keeper-test-binary"])
        diag = CommandAdapter().check(rule, "t")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "cli_missing"


class TestCliFlagIntegration:
    """The CLI ``--allow-command-adapter`` flag toggles the adapter for the run."""

    def test_cli_validate_disabled_by_default(self, tmp_path, capsys, monkeypatch):
        # Build a tiny rule document that the parser/classifier will turn
        # into a rule routed somewhere; the actual classifier route does not
        # matter — we exercise the flag plumbing only.
        rules = tmp_path / "rules.md"
        rules.write_text("- The README must exist in the repository root.\n")

        from gate_keeper.adapters import command as command_adapter
        from gate_keeper.cli import main

        # Sanity: enable flag is not on after a default run.
        rc = main(["validate", str(rules), "--target", str(tmp_path)])
        assert rc in (0, 1)
        assert command_adapter.is_enabled() is False
        capsys.readouterr()

    def test_cli_validate_with_flag_enables_for_run_only(self, tmp_path, capsys):
        rules = tmp_path / "rules.md"
        rules.write_text("- The README must exist in the repository root.\n")

        from gate_keeper.adapters import command as command_adapter
        from gate_keeper.cli import main

        rc = main(["validate", str(rules), "--target", str(tmp_path), "--allow-command-adapter"])
        assert rc in (0, 1)
        # Flag is reset after the call returns.
        assert command_adapter.is_enabled() is False
        capsys.readouterr()
