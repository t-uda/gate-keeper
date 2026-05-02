"""Tests for the GitHub PR target resolver (gate_keeper.backends._target).

Covers:
- parse_target: valid and invalid input formats
- resolve_target: full path with monkeypatched run_gh
- Command construction: exact argv captured
- Round-trip schema compliance for all produced Diagnostic shapes
"""

from __future__ import annotations

from gate_keeper.backends._gh import GhResult
from gate_keeper.backends._target import PrTarget, parse_target, resolve_target
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
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


def _ok_gh_result(number: int = 123, owner: str = "octocat", repo: str = "hello") -> GhResult:
    return GhResult(
        ok=True,
        stdout=f'{{"number":{number},"url":"https://github.com/{owner}/{repo}/pull/{number}"}}',
        stderr="",
        returncode=0,
        cmd=("gh", "pr", "view", str(number), "-R", f"{owner}/{repo}", "--json", "number,url"),
    )


def _missing_binary_result() -> GhResult:
    return GhResult(
        ok=False,
        stdout="",
        stderr="gh binary not found",
        returncode=127,
        cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        binary_missing=True,
    )


def _auth_failure_result() -> GhResult:
    return GhResult(
        ok=False,
        stdout="",
        stderr="To get started, please run: gh auth login",
        returncode=1,
        cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
    )


def _pr_not_found_result() -> GhResult:
    return GhResult(
        ok=False,
        stdout="",
        stderr="GraphQL: Could not resolve to a Repository with the name 'octo/missing'",
        returncode=1,
        cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
    )


# ---------------------------------------------------------------------------
# parse_target — valid inputs
# ---------------------------------------------------------------------------


class TestParseTargetValid:
    def test_full_https_url(self):
        t, err = parse_target("https://github.com/octocat/hello/pull/123")
        assert err is None
        assert t is not None
        assert t.owner == "octocat"
        assert t.repo == "hello"
        assert t.number == 123
        assert t.url == "https://github.com/octocat/hello/pull/123"

    def test_url_with_trailing_slash_and_query_and_fragment(self):
        t, err = parse_target("https://github.com/octocat/hello/pull/123/?notify=true#discussion")
        assert err is None
        assert t is not None
        assert t.number == 123
        # Canonical URL has no trailing slash, query, or fragment
        assert t.url == "https://github.com/octocat/hello/pull/123"
        assert "?" not in t.url
        assert "#" not in t.url

    def test_http_url_upgraded_to_https_canonical(self):
        # http:// is accepted and silently upgraded to https in canonical URL
        t, err = parse_target("http://github.com/octocat/hello/pull/456")
        assert err is None
        assert t is not None
        assert t.number == 456
        assert t.url.startswith("https://")

    def test_url_with_www_prefix(self):
        t, err = parse_target("https://www.github.com/octocat/hello/pull/789")
        assert err is None
        assert t is not None
        assert t.number == 789

    def test_shorthand(self):
        t, err = parse_target("octocat/hello#123")
        assert err is None
        assert t is not None
        assert t.owner == "octocat"
        assert t.repo == "hello"
        assert t.number == 123
        assert t.url == "https://github.com/octocat/hello/pull/123"

    def test_shorthand_with_dots_and_dashes(self):
        t, err = parse_target("my.org/my-repo.js#42")
        assert err is None
        assert t is not None
        assert t.owner == "my.org"
        assert t.repo == "my-repo.js"
        assert t.number == 42

    def test_shorthand_large_pr_number(self):
        t, err = parse_target("octocat/hello#99999")
        assert err is None
        assert t is not None
        assert t.number == 99999

    def test_owner_and_repo_preserved_exactly_as_captured(self):
        # No case folding
        t, err = parse_target("OctoCAT/Hello-World#1")
        assert err is None
        assert t is not None
        assert t.owner == "OctoCAT"
        assert t.repo == "Hello-World"


# ---------------------------------------------------------------------------
# parse_target — invalid inputs
# ---------------------------------------------------------------------------


class TestParseTargetInvalid:
    def test_empty_string(self):
        t, err = parse_target("")
        assert t is None
        assert err is not None

    def test_whitespace_only(self):
        t, err = parse_target("   ")
        assert t is None
        assert err is not None

    def test_shorthand_missing_hash(self):
        t, err = parse_target("octocat/hello/123")
        assert t is None
        assert err is not None

    def test_shorthand_non_numeric_number(self):
        t, err = parse_target("octocat/hello#abc")
        assert t is None
        assert err is not None

    def test_shorthand_zero_pr_number(self):
        t, err = parse_target("octocat/hello#0")
        assert t is None
        assert err is not None
        assert "positive" in err.lower() or "0" in err

    def test_shorthand_owner_with_slash(self):
        # "org/sub/repo#1" has two slashes — does not match OWNER/REPO#N
        t, err = parse_target("org/sub/repo#1")
        assert t is None
        assert err is not None

    def test_url_pointing_at_issues_path(self):
        t, err = parse_target("https://github.com/octocat/hello/issues/123")
        assert t is None
        assert err is not None

    def test_url_on_non_github_host(self):
        t, err = parse_target("https://gitlab.com/octocat/hello/pull/123")
        assert t is None
        assert err is not None

    def test_bare_number(self):
        t, err = parse_target("123")
        assert t is None
        assert err is not None

    def test_plain_repo_slug(self):
        t, err = parse_target("octocat/hello")
        assert t is None
        assert err is not None

    def test_error_message_mentions_target(self):
        bad = "not-a-target"
        t, err = parse_target(bad)
        assert t is None
        assert err is not None
        assert bad in err


# ---------------------------------------------------------------------------
# resolve_target — success path
# ---------------------------------------------------------------------------


class TestResolveTargetSuccess:
    def test_success_returns_pr_target_no_diagnostic(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _ok_gh_result(),
        )
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is None
        assert pr is not None
        assert pr.owner == "octocat"
        assert pr.repo == "hello"
        assert pr.number == 123
        assert pr.url == "https://github.com/octocat/hello/pull/123"

    def test_success_uses_gh_canonical_url(self, monkeypatch):
        # gh returns a slightly different canonical URL (e.g. without fragment)
        canonical = "https://github.com/octocat/hello/pull/123"
        gh_result = GhResult(
            ok=True,
            stdout=f'{{"number":123,"url":"{canonical}"}}',
            stderr="",
            returncode=0,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: gh_result)
        # Supply a URL target with fragment — the returned PrTarget.url should
        # be gh's canonical, not the input.
        pr, diag = resolve_target(_rule(), "https://github.com/octocat/hello/pull/123?tab=files")
        assert diag is None
        assert pr is not None
        assert pr.url == canonical


# ---------------------------------------------------------------------------
# resolve_target — failure paths
# ---------------------------------------------------------------------------


class TestResolveTargetFailures:
    def test_missing_binary_returns_gh_missing_diag(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _missing_binary_result(),
        )
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "gh_missing"

    def test_auth_failure_returns_gh_auth_diag(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _auth_failure_result(),
        )
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "gh_auth_failure"

    def test_pr_not_found_returns_gh_pr_not_found_diag(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _pr_not_found_result(),
        )
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "gh_pr_not_found"
        ev_data = diag.evidence[0].data
        assert ev_data["op"] == "pr-view"
        assert ev_data["owner"] == "octocat"
        assert ev_data["repo"] == "hello"
        assert ev_data["number"] == 123
        assert (
            "Could not resolve" in ev_data["stderr_excerpt"]
            or "not found" in ev_data["stderr_excerpt"].lower()
        )

    def test_pr_not_found_stderr_excerpt_included(self, monkeypatch):
        stderr_msg = "GraphQL: Could not resolve to a Repository with the name 'octo/missing'"
        result = GhResult(
            ok=False,
            stdout="",
            stderr=stderr_msg,
            returncode=1,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].data["stderr_excerpt"] == stderr_msg

    def test_malformed_json_returns_gh_json_error_diag(self, monkeypatch):
        result = GhResult(
            ok=True,
            stdout="{not json}",
            stderr="",
            returncode=0,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "gh_json_error"

    def test_missing_url_field_returns_gh_missing_field_diag(self, monkeypatch):
        # url absent, number present
        result = GhResult(
            ok=True,
            stdout='{"number":123}',
            stderr="",
            returncode=0,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "url"

    def test_invalid_target_string_returns_target_parse_error_diag(self, monkeypatch):
        # run_gh should never be called for an invalid target
        called = []
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: called.append(args) or _ok_gh_result(),
        )
        pr, diag = resolve_target(_rule(), "not-a-valid-target")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "target_parse_error"
        assert diag.evidence[0].data["target"] == "not-a-valid-target"
        # run_gh must not have been called
        assert called == []

    def test_generic_gh_failure_returns_gh_failure_diag(self, monkeypatch):
        result = GhResult(
            ok=False,
            stdout="",
            stderr="some unexpected server error",
            returncode=1,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        pr, diag = resolve_target(_rule(), "octocat/hello#123")
        assert pr is None
        assert diag is not None
        assert diag.evidence[0].kind == "gh_failure"


# ---------------------------------------------------------------------------
# Command construction — assert exact argv
# ---------------------------------------------------------------------------


class TestCommandConstruction:
    def test_exact_argv_for_shorthand_target(self, monkeypatch):
        captured_args = []

        def fake_run_gh(args):
            captured_args.append(list(args))
            return _ok_gh_result()

        monkeypatch.setattr("gate_keeper.backends._target.run_gh", fake_run_gh)
        resolve_target(_rule(), "octocat/hello#123")

        assert len(captured_args) == 1
        assert captured_args[0] == ["pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"]

    def test_exact_argv_for_url_target(self, monkeypatch):
        captured_args = []

        def fake_run_gh(args):
            captured_args.append(list(args))
            return _ok_gh_result(number=456, owner="org", repo="project")

        monkeypatch.setattr("gate_keeper.backends._target.run_gh", fake_run_gh)
        resolve_target(_rule(), "https://github.com/org/project/pull/456")

        assert len(captured_args) == 1
        assert captured_args[0] == ["pr", "view", "456", "-R", "org/project", "--json", "number,url"]

    def test_run_gh_not_called_for_invalid_target(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: called.append(args) or _ok_gh_result(),
        )
        resolve_target(_rule(), "invalid-target-no-slash-hash")
        assert called == []


# ---------------------------------------------------------------------------
# Round-trip schema compliance
# ---------------------------------------------------------------------------


class TestDiagRoundTrip:
    def _round_trip(self, diag: Diagnostic) -> Diagnostic:
        return Diagnostic.from_dict(diag.to_dict())

    def test_target_parse_error_round_trips(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _ok_gh_result(),
        )
        _, diag = resolve_target(_rule(), "not-valid")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "target_parse_error"

    def test_gh_missing_round_trips(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _missing_binary_result(),
        )
        _, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_missing"

    def test_gh_auth_failure_round_trips(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _auth_failure_result(),
        )
        _, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_auth_failure"

    def test_gh_pr_not_found_round_trips(self, monkeypatch):
        monkeypatch.setattr(
            "gate_keeper.backends._target.run_gh",
            lambda args: _pr_not_found_result(),
        )
        _, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_pr_not_found"

    def test_gh_json_error_round_trips(self, monkeypatch):
        result = GhResult(
            ok=True,
            stdout="{not json}",
            stderr="",
            returncode=0,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        _, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_json_error"

    def test_gh_missing_field_round_trips(self, monkeypatch):
        result = GhResult(
            ok=True,
            stdout='{"number":123}',
            stderr="",
            returncode=0,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        _, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_missing_field"

    def test_gh_failure_round_trips(self, monkeypatch):
        result = GhResult(
            ok=False,
            stdout="",
            stderr="some unexpected error",
            returncode=1,
            cmd=("gh", "pr", "view", "123", "-R", "octocat/hello", "--json", "number,url"),
        )
        monkeypatch.setattr("gate_keeper.backends._target.run_gh", lambda args: result)
        _, diag = resolve_target(_rule(), "octocat/hello#123")
        assert diag is not None
        rebuilt = self._round_trip(diag)
        assert rebuilt.evidence[0].kind == "gh_failure"


# ---------------------------------------------------------------------------
# Re-export from gate_keeper.backends.github
# ---------------------------------------------------------------------------


class TestReExports:
    def test_resolve_target_re_exported(self):
        from gate_keeper.backends.github import resolve_target as rt

        assert rt is resolve_target

    def test_pr_target_re_exported(self):
        from gate_keeper.backends.github import PrTarget as PT

        assert PT is PrTarget
