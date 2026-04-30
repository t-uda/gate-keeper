"""Tests for the review-thread handler added in issue #12.

Uses a ``_patch_both``-style helper that monkeypatches:
- ``gate_keeper.backends._target.run_gh``  — for resolve_target
- ``gate_keeper.backends.github.run_gh``   — for _fetch_review_threads

For the threads handler the two gh calls happen sequentially:
  1. resolve_target → ``gh pr view`` (returns number/url)
  2. _fetch_review_threads → ``gh api graphql``

So _make_run_gh_sequence is used with a two-element queue when both
calls are exercised.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from gate_keeper.backends import _gh, _target
from gate_keeper.backends import github as gh_backend
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, compute_exit_code
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    Rule,
    RuleKind,
    RuleSet,
    Severity,
    SourceLocation,
    Status,
)
from gate_keeper.validator import validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESOLVE_OK = json.dumps({"number": 42, "url": "https://github.com/owner/repo/pull/42"})

_PR_COORDS = {
    "owner": "owner",
    "repo": "repo",
    "number": 42,
    "url": "https://github.com/owner/repo/pull/42",
}


def _ok(stdout: str, args: tuple[str, ...] = ()) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh", *args))


def _fail(stderr: str = "error", returncode: int = 1, binary_missing: bool = False) -> _gh.GhResult:
    return _gh.GhResult(
        ok=False,
        stdout="",
        stderr=stderr,
        returncode=returncode,
        cmd=("gh",),
        binary_missing=binary_missing,
    )


def _make_github_rule(kind: RuleKind = RuleKind.GITHUB_THREADS_RESOLVED) -> Rule:
    return Rule(
        id="test-threads-rule",
        title="All review threads must be resolved",
        source=SourceLocation(path="rules.md", line=1),
        text="All review threads must be resolved before merge.",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.GITHUB,
        confidence=Confidence.HIGH,
        params={},
    )


def _make_run_gh_sequence(results: list[_gh.GhResult]):
    """Return a fake run_gh that pops results from the list in order."""
    queue = list(results)

    def _fake(args, **kwargs):
        if queue:
            return queue.pop(0)
        raise AssertionError(f"run_gh called more times than expected; args={args!r}")

    return _fake


def _patch_both(
    monkeypatch,
    resolve_result: _gh.GhResult,
    graphql_result: _gh.GhResult,
):
    """Monkeypatch both target resolver and the github backend run_gh."""
    monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
    monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([graphql_result]))


def _graphql_response(
    nodes: list[dict],
    has_next_page: bool = False,
    end_cursor: str | None = "Y3Vy",
) -> str:
    """Build a well-formed GraphQL response JSON string."""
    return json.dumps({
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": nodes,
                        "pageInfo": {
                            "hasNextPage": has_next_page,
                            "endCursor": end_cursor,
                        },
                    }
                }
            }
        }
    })


def _make_thread(
    is_resolved: bool,
    path: str = "src/foo.py",
    line: int = 42,
    author: str = "alice",
    url: str = "https://github.com/owner/repo/pull/42#discussion_r1",
) -> dict:
    return {
        "id": "PRRT_kwDOA1",
        "isResolved": is_resolved,
        "path": path,
        "line": line,
        "comments": {
            "nodes": [
                {
                    "author": {"login": author},
                    "body": "Please fix this.",
                    "url": url,
                }
            ]
        },
    }


def _check(monkeypatch, nodes: list[dict], **kwargs) -> Diagnostic:
    """Run the threads check with a given set of nodes."""
    _patch_both(
        monkeypatch,
        _ok(_RESOLVE_OK),
        _ok(_graphql_response(nodes, **kwargs)),
    )
    rule = _make_github_rule()
    return gh_backend.check(rule, "owner/repo#42")


# ---------------------------------------------------------------------------
# Zero nodes → PASS
# ---------------------------------------------------------------------------

class TestZeroNodes:
    def test_empty_nodes_passes(self, monkeypatch):
        diag = _check(monkeypatch, [])
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.kind == "review_threads"
        assert ev.data["total"] == 0
        assert ev.data["unresolved_count"] == 0
        assert ev.data["unresolved"] == []

    def test_empty_nodes_evidence_coords(self, monkeypatch):
        diag = _check(monkeypatch, [])
        ev = diag.evidence[0]
        assert ev.data["owner"] == "owner"
        assert ev.data["repo"] == "repo"
        assert ev.data["number"] == 42
        assert ev.data["url"] == "https://github.com/owner/repo/pull/42"


# ---------------------------------------------------------------------------
# One resolved node only → PASS
# ---------------------------------------------------------------------------

class TestAllResolved:
    def test_single_resolved_passes(self, monkeypatch):
        diag = _check(monkeypatch, [_make_thread(is_resolved=True)])
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total"] == 1
        assert ev.data["unresolved_count"] == 0
        assert ev.data["unresolved"] == []

    def test_multiple_resolved_passes(self, monkeypatch):
        nodes = [_make_thread(is_resolved=True), _make_thread(is_resolved=True, path="other.py")]
        diag = _check(monkeypatch, nodes)
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total"] == 2
        assert ev.data["unresolved_count"] == 0


# ---------------------------------------------------------------------------
# One unresolved node → FAIL
# ---------------------------------------------------------------------------

class TestOneUnresolved:
    def test_single_unresolved_fails(self, monkeypatch):
        thread = _make_thread(is_resolved=False, path="src/bar.py", line=10)
        diag = _check(monkeypatch, [thread])
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["total"] == 1
        assert ev.data["unresolved_count"] == 1
        assert len(ev.data["unresolved"]) == 1
        entry = ev.data["unresolved"][0]
        assert entry["path"] == "src/bar.py"
        assert entry["line"] == 10
        assert entry["first_comment_author"] == "alice"
        assert "discussion" in entry["first_comment_url"]

    def test_unresolved_message_mentions_count(self, monkeypatch):
        diag = _check(monkeypatch, [_make_thread(is_resolved=False)])
        assert "1 unresolved" in diag.message


# ---------------------------------------------------------------------------
# Mix: one resolved + one unresolved → FAIL, only unresolved in list
# ---------------------------------------------------------------------------

class TestMixedThreads:
    def test_mix_one_resolved_one_unresolved_fails(self, monkeypatch):
        nodes = [
            _make_thread(is_resolved=True, path="resolved.py", line=1),
            _make_thread(is_resolved=False, path="unresolved.py", line=99),
        ]
        diag = _check(monkeypatch, nodes)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["total"] == 2
        assert ev.data["unresolved_count"] == 1
        assert len(ev.data["unresolved"]) == 1
        assert ev.data["unresolved"][0]["path"] == "unresolved.py"
        assert ev.data["unresolved"][0]["line"] == 99


# ---------------------------------------------------------------------------
# Three unresolved → FAIL, count=3
# ---------------------------------------------------------------------------

class TestMultipleUnresolved:
    def test_three_unresolved_fails_count_three(self, monkeypatch):
        nodes = [
            _make_thread(is_resolved=False, path=f"file{i}.py", line=i * 10)
            for i in range(3)
        ]
        diag = _check(monkeypatch, nodes)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["total"] == 3
        assert ev.data["unresolved_count"] == 3
        assert len(ev.data["unresolved"]) == 3


# ---------------------------------------------------------------------------
# Non-bool isResolved → treated as unresolved (conservative)
# ---------------------------------------------------------------------------

class TestNonBoolIsResolved:
    def test_string_is_resolved_treated_as_unresolved(self, monkeypatch):
        thread = {
            "id": "T1",
            "isResolved": "yes",  # not a bool
            "path": "src/x.py",
            "line": 5,
            "comments": {"nodes": []},
        }
        diag = _check(monkeypatch, [thread])
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["unresolved_count"] == 1

    def test_null_is_resolved_treated_as_unresolved(self, monkeypatch):
        thread = {
            "id": "T1",
            "isResolved": None,
            "path": "src/x.py",
            "line": 5,
            "comments": {"nodes": []},
        }
        diag = _check(monkeypatch, [thread])
        assert diag.status is Status.FAIL

    def test_missing_path_and_line_falls_back_to_null(self, monkeypatch):
        thread = {
            "id": "T1",
            "isResolved": False,
            "comments": {"nodes": []},
        }
        diag = _check(monkeypatch, [thread])
        assert diag.status is Status.FAIL
        entry = diag.evidence[0].data["unresolved"][0]
        assert entry["path"] is None
        assert entry["line"] is None


# ---------------------------------------------------------------------------
# Pagination: hasNextPage true → UNAVAILABLE
# ---------------------------------------------------------------------------

class TestPagination:
    def test_has_next_page_is_unavailable(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_graphql_response([], has_next_page=True, end_cursor="abc123")),
        )
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_pagination_unavailable"
        assert ev.data["has_next_page"] is True
        assert ev.data["end_cursor"] == "abc123"

    def test_has_next_page_does_not_pass(self, monkeypatch):
        """Verify the pagination path never returns PASS."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_graphql_response([], has_next_page=True)),
        )
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is not Status.PASS


# ---------------------------------------------------------------------------
# GraphQL errors in response → UNAVAILABLE / gh_graphql_error
# ---------------------------------------------------------------------------

class TestGraphqlErrors:
    def _graphql_error_response(self, errors: list[dict]) -> str:
        return json.dumps({"errors": errors})

    def test_graphql_errors_unavailable(self, monkeypatch):
        error_response = self._graphql_error_response([
            {"message": "Could not resolve to a Repository"},
        ])
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(error_response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_graphql_error"
        assert ev.data["op"] == "graphql"
        assert len(ev.data["errors"]) == 1
        assert "Repository" in ev.data["errors"][0]

    def test_graphql_errors_truncated_to_200_chars(self, monkeypatch):
        long_msg = "x" * 300
        error_response = self._graphql_error_response([{"message": long_msg}])
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(error_response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        ev = diag.evidence[0]
        # Message should be truncated (200 chars + ellipsis)
        assert len(ev.data["errors"][0]) <= 201 + 1  # 200 chars + ellipsis char
        assert ev.data["errors"][0].endswith("…")

    def test_graphql_errors_include_coords(self, monkeypatch):
        error_response = self._graphql_error_response([{"message": "some error"}])
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(error_response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        ev = diag.evidence[0]
        assert ev.data["owner"] == "owner"
        assert ev.data["repo"] == "repo"
        assert ev.data["number"] == 42


# ---------------------------------------------------------------------------
# Missing data key → UNAVAILABLE / gh_missing_field
# ---------------------------------------------------------------------------

class TestMissingFields:
    def test_missing_data_key_unavailable(self, monkeypatch):
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps({"other": "stuff"})))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert "data" in ev.data["field"]

    def test_missing_repository_key_unavailable(self, monkeypatch):
        response = json.dumps({"data": {"other": "stuff"}})
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert "repository" in ev.data["field"]

    def test_missing_pull_request_key_unavailable(self, monkeypatch):
        response = json.dumps({"data": {"repository": {"other": "stuff"}}})
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert "pullRequest" in ev.data["field"]

    def test_missing_review_threads_key_unavailable(self, monkeypatch):
        response = json.dumps({
            "data": {"repository": {"pullRequest": {"other": "stuff"}}}
        })
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert "reviewThreads" in ev.data["field"]


# ---------------------------------------------------------------------------
# gh non-zero exit → UNAVAILABLE / gh_failure
# ---------------------------------------------------------------------------

class TestGhFailures:
    def test_gh_nonzero_exit_unavailable(self, monkeypatch):
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _fail(stderr="rate limit exceeded", returncode=1))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_failure"

    def test_gh_missing_binary_unavailable(self, monkeypatch):
        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([
            _fail(stderr="gh binary not found", returncode=127, binary_missing=True)
        ]))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        # binary missing is detected at resolve_target stage
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing"

    def test_gh_json_parse_error_unavailable(self, monkeypatch):
        bad_json = _gh.GhResult(
            ok=True, stdout="not valid json {{{", stderr="", returncode=0, cmd=("gh",)
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), bad_json)
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_json_error"


# ---------------------------------------------------------------------------
# Command construction verification
# ---------------------------------------------------------------------------

class TestCommandConstruction:
    def test_graphql_command_contains_expected_args(self, monkeypatch):
        """Capture the argv passed to run_gh and assert the shape."""
        captured_args: list = []
        resolve_queue = [_ok(_RESOLVE_OK)]

        def fake_target_run_gh(args, **kwargs):
            return resolve_queue.pop(0)

        def fake_graphql_run_gh(args, **kwargs):
            captured_args.extend(args)
            return _ok(_graphql_response([]))

        monkeypatch.setattr(_target, "run_gh", fake_target_run_gh)
        monkeypatch.setattr(gh_backend, "run_gh", fake_graphql_run_gh)

        rule = _make_github_rule()
        gh_backend.check(rule, "owner/repo#42")

        argv_str = " ".join(captured_args)
        assert "api" in captured_args
        assert "graphql" in captured_args
        # Verify query content mentions reviewThreads with first: 100
        query_arg = next((a for a in captured_args if "reviewThreads" in a), None)
        assert query_arg is not None, f"No query arg found; argv: {captured_args!r}"
        assert "reviewThreads(first: 100)" in query_arg
        # Verify owner/repo/number args
        assert any("owner=owner" in a for a in captured_args)
        assert any("repo=repo" in a for a in captured_args)
        assert "-F" in captured_args
        assert "number=42" in captured_args


# ---------------------------------------------------------------------------
# Diagnostic round-trip: to_dict → from_dict
# ---------------------------------------------------------------------------

class TestDiagRoundTrip:
    def _rt(self, diag: Diagnostic) -> Diagnostic:
        return Diagnostic.from_dict(diag.to_dict())

    def test_pass_round_trips(self, monkeypatch):
        diag = _check(monkeypatch, [])
        rt = self._rt(diag)
        assert rt.status is Status.PASS
        assert rt.evidence[0].kind == "review_threads"
        assert rt.evidence[0].data["total"] == 0

    def test_fail_round_trips(self, monkeypatch):
        diag = _check(monkeypatch, [_make_thread(is_resolved=False)])
        rt = self._rt(diag)
        assert rt.status is Status.FAIL
        assert rt.evidence[0].data["unresolved_count"] == 1

    def test_unavailable_pagination_round_trips(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_graphql_response([], has_next_page=True, end_cursor="cur123")),
        )
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        rt = self._rt(diag)
        assert rt.status is Status.UNAVAILABLE
        assert rt.evidence[0].kind == "gh_pagination_unavailable"
        assert rt.evidence[0].data["end_cursor"] == "cur123"

    def test_unavailable_graphql_error_round_trips(self, monkeypatch):
        error_response = json.dumps({"errors": [{"message": "some graphql error"}]})
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(error_response))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        rt = self._rt(diag)
        assert rt.status is Status.UNAVAILABLE
        assert rt.evidence[0].kind == "gh_graphql_error"

    def test_unavailable_missing_field_round_trips(self, monkeypatch):
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps({"no_data": True})))
        rule = _make_github_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        rt = self._rt(diag)
        assert rt.status is Status.UNAVAILABLE
        assert rt.evidence[0].kind == "gh_missing_field"


# ---------------------------------------------------------------------------
# End-to-end via validate()
# ---------------------------------------------------------------------------

class TestEndToEndValidator:
    def test_zero_unresolved_threads_exit_0(self, monkeypatch):
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(_graphql_response([])))
        rule = _make_github_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.PASS
        assert compute_exit_code(report.diagnostics) == EXIT_OK

    def test_one_unresolved_thread_exit_1(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_graphql_response([_make_thread(is_resolved=False)])),
        )
        rule = _make_github_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.FAIL
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL
