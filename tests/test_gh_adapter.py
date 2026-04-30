"""Tests for the internal gh CLI adapter (gate_keeper.backends._gh).

Covers:
- run_gh: FileNotFoundError, non-zero exit, token redaction
- parse_json: malformed input, truncation
- classify_gh_failure: missing binary, auth markers, generic failure
- Diagnostic builders: correct backend, status, evidence shape
- Round-trip via Diagnostic.from_dict / to_dict
- Pagination and missing-field evidence
- Token redaction in stderr_excerpt inside evidence
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from gate_keeper.backends._gh import (
    GhResult,
    classify_gh_failure,
    failure_diag,
    gh_auth_diag,
    gh_failed_diag,
    gh_json_diag,
    gh_missing_diag,
    gh_missing_field_diag,
    gh_pagination_diag,
    parse_json,
    run_gh,
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
        title="Test GitHub rule",
        source=SourceLocation(path="rules.md", line=1),
        text="some rule text",
        kind=RuleKind.GITHUB_PR_OPEN,
        severity=Severity.ERROR,
        backend_hint=Backend.GITHUB,
        confidence=Confidence.HIGH,
        params={},
    )


def _make_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# run_gh — binary missing
# ---------------------------------------------------------------------------


class TestRunGhMissing:
    def test_file_not_found_returns_failed_result(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("not found")))
        result = run_gh(["pr", "view"])
        assert result.ok is False
        assert result.binary_missing is True
        # 127 is the POSIX convention for "command not found".
        assert result.returncode == 127
        assert "gh" in result.cmd

    def test_missing_result_classify_as_missing(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
        result = run_gh(["pr", "view"])
        assert classify_gh_failure(result) == "missing"

    def test_negative_returncode_is_not_missing(self):
        # SIGHUP terminates with returncode -1; this must NOT be misclassified
        # as "binary missing" — only the binary_missing flag carries that meaning.
        result = GhResult(
            ok=False, stdout="", stderr="", returncode=-1, cmd=("gh", "pr", "view")
        )
        assert classify_gh_failure(result) == "failure"


# ---------------------------------------------------------------------------
# run_gh — non-zero exit
# ---------------------------------------------------------------------------


class TestRunGhNonZeroExit:
    def test_nonzero_exit_ok_false(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _make_completed(stderr="something went wrong", returncode=1))
        result = run_gh(["pr", "view", "42"])
        assert result.ok is False
        assert result.returncode == 1
        assert result.stderr == "something went wrong"

    def test_zero_exit_ok_true(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _make_completed(stdout='{"number":1}', returncode=0))
        result = run_gh(["pr", "view"])
        assert result.ok is True
        assert result.returncode == 0

    def test_cmd_starts_with_gh(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _make_completed(returncode=1))
        result = run_gh(["api", "repos"])
        assert result.cmd[0] == "gh"
        assert "api" in result.cmd


# ---------------------------------------------------------------------------
# run_gh — token redaction in stderr
# ---------------------------------------------------------------------------


class TestRunGhTokenRedaction:
    def test_ghp_token_redacted_from_stderr(self, monkeypatch):
        fake_token = "ghp_" + "A" * 40
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr=f"error: token {fake_token} is invalid", returncode=1),
        )
        result = run_gh(["pr", "view"])
        assert fake_token not in result.stderr
        assert "[redacted-token]" in result.stderr

    def test_gho_token_redacted(self, monkeypatch):
        fake_token = "gho_" + "B" * 30
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr=fake_token, returncode=1),
        )
        result = run_gh(["pr", "view"])
        assert fake_token not in result.stderr

    def test_ghs_token_redacted(self, monkeypatch):
        fake_token = "ghs_" + "C" * 20
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr=f"credentials: {fake_token}", returncode=1),
        )
        result = run_gh(["pr", "view"])
        assert fake_token not in result.stderr

    def test_gh_bare_token_redacted(self, monkeypatch):
        fake_token = "gh_" + "X" * 20
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr=fake_token, returncode=1),
        )
        result = run_gh(["pr", "view"])
        assert fake_token not in result.stderr

    def test_authorization_header_line_redacted(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(
                stderr="Authorization: Bearer mysecrettoken\nsome other line",
                returncode=1,
            ),
        )
        result = run_gh(["pr", "view"])
        assert "mysecrettoken" not in result.stderr
        assert "[redacted-auth-header]" in result.stderr

    def test_token_eq_line_redacted(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr="token=mysecretvalue", returncode=1),
        )
        result = run_gh(["pr", "view"])
        assert "mysecretvalue" not in result.stderr

    def test_github_pat_token_redacted(self, monkeypatch):
        # Fine-grained personal access tokens use the github_pat_ prefix.
        fake_token = "github_pat_" + "Z" * 60
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr=f"error: bad PAT {fake_token}", returncode=1),
        )
        result = run_gh(["pr", "view"])
        assert fake_token not in result.stderr
        assert "[redacted-token]" in result.stderr


# ---------------------------------------------------------------------------
# run_gh — timeout and OSError
# ---------------------------------------------------------------------------


class TestRunGhExceptions:
    def test_timeout_returns_failed_result(self, monkeypatch):
        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["gh", "pr", "view"], timeout=5.0)

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_gh(["pr", "view"], timeout=5.0)
        assert result.ok is False
        assert result.returncode == -2

    def test_os_error_returns_failed_result(self, monkeypatch):
        def _raise(*a, **kw):
            raise OSError("permission denied")

        monkeypatch.setattr(subprocess, "run", _raise)
        result = run_gh(["pr", "view"])
        assert result.ok is False
        assert result.returncode == -3


# ---------------------------------------------------------------------------
# parse_json
# ---------------------------------------------------------------------------


class TestParseJson:
    def test_valid_json_returns_value_and_none(self):
        value, err = parse_json('{"key": "val"}')
        assert value == {"key": "val"}
        assert err is None

    def test_malformed_json_returns_none_and_short_message(self):
        value, err = parse_json("{not valid json}")
        assert value is None
        assert err is not None
        assert isinstance(err, str)
        # error message must be a single line
        assert "\n" not in err

    def test_malformed_json_error_does_not_include_full_stdout(self):
        big_input = "x" * 5000 + " invalid"
        value, err = parse_json(big_input)
        assert value is None
        assert err is not None
        # The error should be short: well under the 5000 chars of input
        assert len(err) < 600

    def test_truncation_applied(self):
        big_invalid = "{" + "a" * 5000
        value, err = parse_json(big_invalid)
        assert value is None
        assert err is not None
        # The full input should not appear in the message
        assert "a" * 5000 not in err

    def test_empty_string_returns_error(self):
        value, err = parse_json("")
        assert value is None
        assert err is not None

    def test_valid_list(self):
        value, err = parse_json("[1, 2, 3]")
        assert value == [1, 2, 3]
        assert err is None


# ---------------------------------------------------------------------------
# classify_gh_failure
# ---------------------------------------------------------------------------


class TestClassifyGhFailure:
    def _result(self, returncode: int, stderr: str = "") -> GhResult:
        return GhResult(ok=False, stdout="", stderr=stderr, returncode=returncode, cmd=("gh", "pr", "view"))

    def test_binary_missing_flag_is_missing(self):
        result = GhResult(
            ok=False,
            stdout="",
            stderr="gh binary not found",
            returncode=127,
            cmd=("gh", "pr", "view"),
            binary_missing=True,
        )
        assert classify_gh_failure(result) == "missing"

    def test_auth_login_in_stderr_is_auth(self):
        assert classify_gh_failure(self._result(1, "To get started, please run: gh auth login")) == "auth"

    def test_not_logged_in_is_auth(self):
        assert classify_gh_failure(self._result(1, "error: not logged in")) == "auth"

    def test_bad_credentials_is_auth(self):
        assert classify_gh_failure(self._result(1, "Bad credentials")) == "auth"

    def test_http_401_is_auth(self):
        assert classify_gh_failure(self._result(1, "HTTP 401 Unauthorized")) == "auth"

    def test_unauthorized_is_auth(self):
        assert classify_gh_failure(self._result(1, "unauthorized request")) == "auth"

    def test_arbitrary_nonzero_is_failure(self):
        assert classify_gh_failure(self._result(1, "some unrelated error")) == "failure"

    def test_returncode_minus2_is_failure(self):
        # timeout (-2) is not auth, not missing
        assert classify_gh_failure(self._result(-2, "timed out")) == "failure"

    def test_negative_returncode_without_binary_missing_is_failure(self):
        # SIGHUP-style termination (-1) with binary_missing=False must NOT be "missing".
        result = GhResult(
            ok=False,
            stdout="",
            stderr="",
            returncode=-1,
            cmd=("gh", "pr", "view"),
            binary_missing=False,
        )
        assert classify_gh_failure(result) == "failure"


# ---------------------------------------------------------------------------
# Diagnostic builders — backend and status
# ---------------------------------------------------------------------------


class TestDiagBuilders:
    def test_gh_missing_diag_backend_and_status(self):
        rule = _rule()
        diag = gh_missing_diag(rule)
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE
        assert len(diag.evidence) == 1
        assert diag.evidence[0].kind == "gh_missing"
        assert diag.evidence[0].data["path"] == "gh"

    def test_gh_failed_diag_normal_exit_is_unavailable(self):
        rule = _rule()
        result = GhResult(ok=False, stdout="", stderr="rate limited", returncode=1, cmd=("gh", "pr", "view"))
        diag = gh_failed_diag(rule, "pr-view", result)
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_failure"
        assert diag.evidence[0].data["op"] == "pr-view"
        assert diag.evidence[0].data["returncode"] == 1

    def test_gh_failed_diag_negative_returncode_is_error(self):
        rule = _rule()
        result = GhResult(ok=False, stdout="", stderr="OSError", returncode=-3, cmd=("gh", "pr", "view"))
        diag = gh_failed_diag(rule, "graphql", result)
        assert diag.status is Status.ERROR

    def test_gh_auth_diag_status_and_kind(self):
        rule = _rule()
        result = GhResult(ok=False, stdout="", stderr="not logged in", returncode=1, cmd=("gh", "pr", "view"))
        diag = gh_auth_diag(rule, "pr-view", result)
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_auth_failure"

    def test_gh_json_diag_status_and_kind(self):
        rule = _rule()
        diag = gh_json_diag(rule, "pr-view", "JSON parse error at pos 0: 'Expecting value'")
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_json_error"
        assert diag.evidence[0].data["op"] == "pr-view"
        assert "JSON parse error" in diag.evidence[0].data["error"]

    def test_gh_pagination_diag_evidence(self):
        rule = _rule()
        diag = gh_pagination_diag(rule, "graphql", end_cursor="cursor123")
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_pagination_unavailable"
        assert ev.data["has_next_page"] is True
        assert ev.data["end_cursor"] == "cursor123"
        assert ev.data["op"] == "graphql"

    def test_gh_pagination_diag_none_cursor(self):
        rule = _rule()
        diag = gh_pagination_diag(rule, "graphql", end_cursor=None)
        assert diag.evidence[0].data["end_cursor"] is None

    def test_gh_missing_field_diag(self):
        rule = _rule()
        diag = gh_missing_field_diag(rule, "pr-view", "headRefName")
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert ev.data["field"] == "headRefName"
        assert ev.data["op"] == "pr-view"


# ---------------------------------------------------------------------------
# failure_diag dispatch
# ---------------------------------------------------------------------------


class TestFailureDiag:
    def _result(self, returncode: int, stderr: str = "") -> GhResult:
        return GhResult(ok=False, stdout="", stderr=stderr, returncode=returncode, cmd=("gh", "pr", "view"))

    def test_missing_binary_routes_to_gh_missing(self):
        rule = _rule()
        result = GhResult(
            ok=False,
            stdout="",
            stderr="gh binary not found",
            returncode=127,
            cmd=("gh", "pr", "view"),
            binary_missing=True,
        )
        diag = failure_diag(rule, "pr-view", result)
        assert diag.evidence[0].kind == "gh_missing"

    def test_auth_failure_routes_to_gh_auth(self):
        rule = _rule()
        result = self._result(1, "gh auth login required")
        diag = failure_diag(rule, "pr-view", result)
        assert diag.evidence[0].kind == "gh_auth_failure"

    def test_generic_failure_routes_to_gh_failure(self):
        rule = _rule()
        result = self._result(1, "some unexpected error")
        diag = failure_diag(rule, "pr-view", result)
        assert diag.evidence[0].kind == "gh_failure"


# ---------------------------------------------------------------------------
# Round-trip schema compliance
# ---------------------------------------------------------------------------


class TestDiagRoundTrip:
    def _round_trip(self, diag: Diagnostic) -> Diagnostic:
        return Diagnostic.from_dict(diag.to_dict())

    def test_gh_missing_diag_round_trips(self):
        diag = gh_missing_diag(_rule())
        rebuilt = self._round_trip(diag)
        assert rebuilt.status is Status.UNAVAILABLE
        assert rebuilt.backend is Backend.GITHUB
        assert rebuilt.evidence[0].kind == "gh_missing"

    def test_gh_failed_diag_round_trips(self):
        result = GhResult(ok=False, stdout="", stderr="err", returncode=1, cmd=("gh", "pr", "view"))
        diag = gh_failed_diag(_rule(), "pr-view", result)
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].data["op"] == "pr-view"

    def test_gh_auth_diag_round_trips(self):
        result = GhResult(ok=False, stdout="", stderr="not logged in", returncode=1, cmd=("gh", "pr", "view"))
        diag = gh_auth_diag(_rule(), "pr-view", result)
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_auth_failure"

    def test_gh_json_diag_round_trips(self):
        diag = gh_json_diag(_rule(), "pr-view", "bad json")
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_json_error"

    def test_gh_pagination_diag_round_trips(self):
        diag = gh_pagination_diag(_rule(), "graphql", end_cursor="abc123")
        rebuilt = self._round_trip(diag)
        ev = rebuilt.evidence[0]
        assert ev.kind == "gh_pagination_unavailable"
        assert ev.data["has_next_page"] is True
        assert ev.data["end_cursor"] == "abc123"

    def test_gh_missing_field_diag_round_trips(self):
        diag = gh_missing_field_diag(_rule(), "pr-view", "title")
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].data["field"] == "title"


# ---------------------------------------------------------------------------
# Token redaction in evidence stderr_excerpt (regression)
# ---------------------------------------------------------------------------


class TestTokenRedactionInEvidence:
    def test_token_not_in_failed_diag_stderr_excerpt(self):
        fake_token = "ghp_" + "F" * 40
        # The token arrives in stderr; _redact is called by run_gh, and
        # gh_failed_diag uses result.stderr which is already redacted.
        result = GhResult(
            ok=False,
            stdout="",
            stderr=f"error: invalid token {fake_token}",  # NOT pre-redacted (simulating raw)
            returncode=1,
            cmd=("gh", "pr", "view"),
        )
        # Manually redact as run_gh would have done
        from gate_keeper.backends._gh import _redact
        redacted_stderr = _redact(result.stderr)
        result_redacted = GhResult(
            ok=False,
            stdout="",
            stderr=redacted_stderr,
            returncode=1,
            cmd=("gh", "pr", "view"),
        )
        diag = gh_failed_diag(_rule(), "pr-view", result_redacted)
        excerpt = diag.evidence[0].data["stderr_excerpt"]
        assert fake_token not in excerpt
        assert "[redacted-token]" in excerpt

    def test_run_gh_redacts_before_storing(self, monkeypatch):
        """Integration: run_gh stores redacted stderr; diag inherits redaction."""
        fake_token = "ghu_" + "G" * 40
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _make_completed(stderr=f"bad token: {fake_token}", returncode=1),
        )
        result = run_gh(["pr", "view"])
        # result.stderr is already redacted
        assert fake_token not in result.stderr
        # If we build a failed diag from this result, excerpt is also clean
        diag = gh_failed_diag(_rule(), "pr-view", result)
        assert fake_token not in diag.evidence[0].data["stderr_excerpt"]

    def test_token_in_argv_redacted_from_evidence_cmd(self):
        """A token passed as a gh argv element must not surface in evidence['cmd']."""
        fake_token = "ghp_" + "H" * 40
        result = GhResult(
            ok=False,
            stdout="",
            stderr="",
            returncode=1,
            cmd=("gh", "api", "-H", f"Authorization: token {fake_token}", "user"),
        )
        for builder in (
            lambda: gh_failed_diag(_rule(), "api", result),
            lambda: gh_auth_diag(_rule(), "api", result),
        ):
            diag = builder()
            cmd_value = diag.evidence[0].data["cmd"]
            assert fake_token not in cmd_value
            # Per-arg redaction may produce either the token sentinel or the
            # auth-header sentinel; both are acceptable as long as the secret
            # is gone.
            assert (
                "[redacted-token]" in cmd_value
                or "[redacted-auth-header]" in cmd_value
            )

    def test_github_pat_in_argv_redacted_from_evidence_cmd(self):
        fake_token = "github_pat_" + "K" * 60
        result = GhResult(
            ok=False,
            stdout="",
            stderr="",
            returncode=1,
            cmd=("gh", "api", "-f", f"token={fake_token}", "user"),
        )
        diag = gh_failed_diag(_rule(), "api", result)
        cmd_value = diag.evidence[0].data["cmd"]
        assert fake_token not in cmd_value
