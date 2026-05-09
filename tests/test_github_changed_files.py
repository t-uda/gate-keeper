"""Tests for the ``github_changed_files_absent`` rule kind (issue #147).

Mirrors the structure of ``test_github_threads.py``: each test patches
``gate_keeper.backends._target.run_gh`` (the resolver) and
``gate_keeper.backends.github.run_gh`` (the changed-file fetcher) using a
sequenced fake so the two ``gh`` calls receive distinct responses.

Coverage:
- pass (no changed files match)
- fail (one or more changed files match)
- params_error (missing/invalid ``patterns``, invalid ``case_sensitive``)
- GitHub unavailable (``gh`` failure, GraphQL errors, JSON parse error)
- pagination across multiple pages, including a hard truncation case
- glob semantics (``**``, single ``*``, ``?``, case folding)
"""

from __future__ import annotations

import json

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


def _ok(stdout: str) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh",))


def _fail(stderr: str = "error", returncode: int = 1, binary_missing: bool = False) -> _gh.GhResult:
    return _gh.GhResult(
        ok=False,
        stdout="",
        stderr=stderr,
        returncode=returncode,
        cmd=("gh",),
        binary_missing=binary_missing,
    )


def _make_rule(
    patterns: object = ("outputs/**", "**/*.xlsx"),
    case_sensitive: object | None = None,
    *,
    omit_patterns: bool = False,
) -> Rule:
    params: dict[str, object] = {}
    if not omit_patterns:
        params["patterns"] = list(patterns) if isinstance(patterns, (list, tuple)) else patterns
    if case_sensitive is not None:
        params["case_sensitive"] = case_sensitive
    return Rule(
        id="changed-files-rule",
        title="PRs must not change generated workbook outputs",
        source=SourceLocation(path="rules.md", line=1),
        text="PRs must not change generated workbook outputs.",
        kind=RuleKind.GITHUB_CHANGED_FILES_ABSENT,
        severity=Severity.ERROR,
        backend_hint=Backend.GITHUB,
        confidence=Confidence.HIGH,
        params=params,
    )


def _make_run_gh_sequence(results: list[_gh.GhResult]):
    queue = list(results)

    def _fake(args, **kwargs):
        if queue:
            return queue.pop(0)
        raise AssertionError(f"run_gh called more times than expected; args={args!r}")

    return _fake


def _patch_with_pages(monkeypatch, resolve_result: _gh.GhResult, pages: list[_gh.GhResult]):
    monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
    monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence(pages))


def _files_response(
    paths: list[str],
    *,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> str:
    """Build a single-page GraphQL response for the files connection."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "nodes": [{"path": p} for p in paths],
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                        }
                    }
                }
            }
        }
    )


def _check(monkeypatch, paths: list[str], rule: Rule | None = None) -> Diagnostic:
    """Run a single-page check against *paths* with default patterns."""
    rule = rule or _make_rule()
    _patch_with_pages(
        monkeypatch,
        _ok(_RESOLVE_OK),
        [_ok(_files_response(paths, has_next_page=False))],
    )
    return gh_backend.check(rule, "owner/repo#42")


# ---------------------------------------------------------------------------
# PASS — no offending file
# ---------------------------------------------------------------------------


class TestPass:
    def test_no_offending_files_passes(self, monkeypatch):
        diag = _check(monkeypatch, ["src/foo.py", "README.md"])
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.kind == "pr_changed_files"
        assert ev.data["total_changed_files"] == 2
        assert ev.data["offending"] == []
        assert ev.data["pagination_complete"] is True
        assert ev.data["page_count"] == 1
        assert ev.data["forbidden_patterns"] == ["outputs/**", "**/*.xlsx"]

    def test_zero_changed_files_passes(self, monkeypatch):
        diag = _check(monkeypatch, [])
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total_changed_files"] == 0
        assert ev.data["offending"] == []

    def test_pass_includes_pr_coords(self, monkeypatch):
        diag = _check(monkeypatch, ["src/foo.py"])
        ev = diag.evidence[0]
        assert ev.data["owner"] == "owner"
        assert ev.data["repo"] == "repo"
        assert ev.data["number"] == 42
        assert ev.data["url"] == "https://github.com/owner/repo/pull/42"


# ---------------------------------------------------------------------------
# FAIL — offending paths matched
# ---------------------------------------------------------------------------


class TestFail:
    def test_single_offending_file_fails(self, monkeypatch):
        diag = _check(monkeypatch, ["outputs/results.xlsx"])
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["total_changed_files"] == 1
        assert len(ev.data["offending"]) == 1
        offending = ev.data["offending"][0]
        assert offending["path"] == "outputs/results.xlsx"
        # Either pattern is acceptable since the matcher records the first hit.
        assert offending["pattern"] in {"outputs/**", "**/*.xlsx"}

    def test_multiple_offending_files_fails(self, monkeypatch):
        diag = _check(
            monkeypatch,
            ["src/foo.py", "outputs/a.csv", "data.xlsx", "README.md"],
        )
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["total_changed_files"] == 4
        assert len(ev.data["offending"]) == 2
        paths = {entry["path"] for entry in ev.data["offending"]}
        assert paths == {"outputs/a.csv", "data.xlsx"}

    def test_fail_message_lists_offending_paths(self, monkeypatch):
        diag = _check(monkeypatch, ["outputs/x"])
        assert "outputs/x" in diag.message
        assert "1 of 1" in diag.message

    def test_matched_patterns_recorded(self, monkeypatch):
        diag = _check(monkeypatch, ["outputs/x", "y.xlsx"])
        ev = diag.evidence[0]
        assert set(ev.data["matched_patterns"]) == {"outputs/**", "**/*.xlsx"}


# ---------------------------------------------------------------------------
# Params error — missing or invalid `patterns` / `case_sensitive`
# ---------------------------------------------------------------------------


class TestParamsError:
    def test_missing_patterns_unavailable(self, monkeypatch):
        rule = _make_rule(omit_patterns=True)
        # Patches not strictly needed since params are validated before any
        # gh call; provide them anyway so an unexpected gh call would fail.
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "params_error"
        assert ev.data["missing"] == "patterns"

    def test_empty_patterns_list_unavailable(self, monkeypatch):
        rule = _make_rule(patterns=[])
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "params_error"
        assert ev.data["field"] == "patterns"

    def test_patterns_not_a_list_unavailable(self, monkeypatch):
        rule = _make_rule(patterns="outputs/**")  # bare string, not a list
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "params_error"
        assert ev.data["field"] == "patterns"

    def test_patterns_with_non_string_unavailable(self, monkeypatch):
        rule = _make_rule(patterns=["outputs/**", 7])
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "params_error"
        assert ev.data["field"] == "patterns[1]"

    def test_patterns_with_empty_string_unavailable(self, monkeypatch):
        rule = _make_rule(patterns=["outputs/**", ""])
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "params_error"
        assert ev.data["field"] == "patterns[1]"

    def test_invalid_case_sensitive_unavailable(self, monkeypatch):
        rule = _make_rule(case_sensitive="yes")
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "params_error"
        assert ev.data["field"] == "case_sensitive"


# ---------------------------------------------------------------------------
# GitHub unavailable — gh failures / GraphQL errors / JSON errors
# ---------------------------------------------------------------------------


class TestGithubUnavailable:
    def test_gh_nonzero_exit_unavailable(self, monkeypatch):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_fail(stderr="rate limit exceeded", returncode=1)],
        )
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_failure"

    def test_gh_binary_missing_unavailable(self, monkeypatch):
        # The resolver detects binary-missing first.
        monkeypatch.setattr(
            _target,
            "run_gh",
            _make_run_gh_sequence([_fail(stderr="gh binary not found", returncode=127, binary_missing=True)]),
        )
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing"

    def test_graphql_errors_unavailable(self, monkeypatch):
        body = json.dumps({"errors": [{"message": "Could not resolve to a Repository"}]})
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [_ok(body)])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_graphql_error"
        assert ev.data["op"] == "graphql"
        assert "Repository" in ev.data["errors"][0]

    def test_malformed_json_unavailable(self, monkeypatch):
        bad_json = _gh.GhResult(ok=True, stdout="not valid json {{{", stderr="", returncode=0, cmd=("gh",))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [bad_json])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_json_error"

    def test_missing_files_field_unavailable(self, monkeypatch):
        body = json.dumps({"data": {"repository": {"pullRequest": {"other": "stuff"}}}})
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [_ok(body)])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert "files" in ev.data["field"]

    def test_node_missing_path_unavailable(self, monkeypatch):
        body = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "files": {
                                "nodes": [{"path": "ok.py"}, {"other": "thing"}],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        )
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [_ok(body)])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "gh_missing_field"
        assert "nodes[].path" in ev.data["field"]


# ---------------------------------------------------------------------------
# Pagination — two pages succeed, partial pagination is UNAVAILABLE
# ---------------------------------------------------------------------------


class TestPagination:
    def test_two_pages_succeed(self, monkeypatch):
        page1 = _ok(
            _files_response(
                ["src/a.py", "src/b.py"],
                has_next_page=True,
                end_cursor="cursor-1",
            )
        )
        page2 = _ok(_files_response(["outputs/c.csv"], has_next_page=False, end_cursor=None))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [page1, page2])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["total_changed_files"] == 3
        assert ev.data["page_count"] == 2
        assert ev.data["pagination_complete"] is True
        offending_paths = {entry["path"] for entry in ev.data["offending"]}
        assert offending_paths == {"outputs/c.csv"}

    def test_three_pages_with_no_offending_passes(self, monkeypatch):
        pages = [
            _ok(_files_response(["a.py", "b.py"], has_next_page=True, end_cursor="c1")),
            _ok(_files_response(["c.py", "d.py"], has_next_page=True, end_cursor="c2")),
            _ok(_files_response(["e.py"], has_next_page=False, end_cursor=None)),
        ]
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), pages)
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total_changed_files"] == 5
        assert ev.data["page_count"] == 3
        assert ev.data["pagination_complete"] is True

    def test_pagination_continues_with_endcursor_from_previous_page(self, monkeypatch):
        captured: list[list[str]] = []

        def fake_fetcher(args, **kwargs):
            captured.append(list(args))
            # First call → has next page, cursor "c1"; second call → end.
            if len(captured) == 1:
                return _ok(_files_response(["a.py"], has_next_page=True, end_cursor="cursor-A"))
            return _ok(_files_response(["b.py"], has_next_page=False, end_cursor=None))

        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)]))
        monkeypatch.setattr(gh_backend, "run_gh", fake_fetcher)

        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        # First request: no after; second request: after=cursor-A.
        assert not any(arg.startswith("after=") for arg in captured[0])
        assert any(arg == "after=cursor-A" for arg in captured[1])

    def test_has_next_true_with_missing_cursor_unavailable(self, monkeypatch):
        body = _files_response(["src/a.py"], has_next_page=True, end_cursor=None)
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [_ok(body)])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_pagination_unavailable"

    def test_has_next_non_bool_unavailable(self, monkeypatch):
        body = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "files": {
                                "nodes": [{"path": "a.py"}],
                                "pageInfo": {"hasNextPage": "yes", "endCursor": "c"},
                            }
                        }
                    }
                }
            }
        )
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [_ok(body)])
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_pagination_unavailable"

    def test_pagination_runaway_capped_at_max_pages(self, monkeypatch):
        # Every page reports has_next_page=True with a fresh cursor; the
        # backend must stop after _CHANGED_FILES_MAX_PAGES and return
        # UNAVAILABLE rather than loop forever.
        def fake_fetcher(args, **kwargs):
            return _ok(_files_response(["a.py"], has_next_page=True, end_cursor="c"))

        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)]))
        monkeypatch.setattr(gh_backend, "run_gh", fake_fetcher)
        rule = _make_rule()
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_pagination_unavailable"


# ---------------------------------------------------------------------------
# Glob semantics
# ---------------------------------------------------------------------------


class TestGlobSemantics:
    def test_globstar_matches_nested_paths(self, monkeypatch):
        rule = _make_rule(patterns=["context/researcher/raw/**"])
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [
                _ok(
                    _files_response(
                        [
                            "context/researcher/raw/a.txt",
                            "context/researcher/raw/sub/b.txt",
                            "src/foo.py",
                        ]
                    )
                )
            ],
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        offending = {e["path"] for e in diag.evidence[0].data["offending"]}
        assert offending == {
            "context/researcher/raw/a.txt",
            "context/researcher/raw/sub/b.txt",
        }

    def test_double_star_matches_any_xlsx(self, monkeypatch):
        rule = _make_rule(patterns=["**/*.xlsx"])
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["report.xlsx", "deep/nested/data.xlsx", "src/x.py"]))],
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        offending = {e["path"] for e in diag.evidence[0].data["offending"]}
        assert offending == {"report.xlsx", "deep/nested/data.xlsx"}

    def test_single_star_does_not_cross_segments(self, monkeypatch):
        # Single ``*`` should not match across path separators, so
        # ``deep/nested/x.py`` must NOT match ``src/*.py`` or ``*.py``
        # if those would require crossing slashes.
        rule = _make_rule(patterns=["src/*.py"])
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["src/a.py", "src/sub/b.py"]))],
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        offending = {e["path"] for e in diag.evidence[0].data["offending"]}
        assert offending == {"src/a.py"}

    def test_question_mark_matches_single_char(self, monkeypatch):
        rule = _make_rule(patterns=["log?.txt"])
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["log1.txt", "log10.txt"]))],
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        offending = {e["path"] for e in diag.evidence[0].data["offending"]}
        assert offending == {"log1.txt"}

    def test_case_sensitive_default(self, monkeypatch):
        rule = _make_rule(patterns=["**/*.XLSX"])
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["data.xlsx", "DATA.XLSX"]))],
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        offending = {e["path"] for e in diag.evidence[0].data["offending"]}
        assert offending == {"DATA.XLSX"}

    def test_case_insensitive_matches_either(self, monkeypatch):
        rule = _make_rule(patterns=["**/*.XLSX"], case_sensitive=False)
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["data.xlsx", "DATA.XLSX"]))],
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        offending = {e["path"] for e in diag.evidence[0].data["offending"]}
        assert offending == {"data.xlsx", "DATA.XLSX"}


# ---------------------------------------------------------------------------
# Diagnostic round-trip and end-to-end integration
# ---------------------------------------------------------------------------


class TestRoundTripAndIntegration:
    def test_pass_round_trips(self, monkeypatch):
        diag = _check(monkeypatch, ["src/foo.py"])
        rt = Diagnostic.from_dict(diag.to_dict())
        assert rt.status is Status.PASS
        assert rt.evidence[0].kind == "pr_changed_files"

    def test_fail_round_trips(self, monkeypatch):
        diag = _check(monkeypatch, ["outputs/x"])
        rt = Diagnostic.from_dict(diag.to_dict())
        assert rt.status is Status.FAIL
        assert rt.evidence[0].data["offending"][0]["path"] == "outputs/x"

    def test_validate_pass_returns_exit_ok(self, monkeypatch):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["src/foo.py"]))],
        )
        rule = _make_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.PASS
        assert compute_exit_code(report.diagnostics) == EXIT_OK

    def test_validate_fail_returns_exit_fail(self, monkeypatch):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["outputs/forbidden.csv"]))],
        )
        rule = _make_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.FAIL
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL


# ---------------------------------------------------------------------------
# Command construction — argv shape for the GraphQL call
# ---------------------------------------------------------------------------


class TestCommandConstruction:
    def test_first_page_argv_shape(self, monkeypatch):
        captured: list[str] = []

        def fake_fetcher(args, **kwargs):
            captured.extend(args)
            return _ok(_files_response([]))

        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)]))
        monkeypatch.setattr(gh_backend, "run_gh", fake_fetcher)

        rule = _make_rule()
        gh_backend.check(rule, "owner/repo#42")

        assert "api" in captured
        assert "graphql" in captured
        # The query arg must reference the files connection, not threads.
        query_arg = next((a for a in captured if "files(first: $first" in a), None)
        assert query_arg is not None
        assert "reviewThreads" not in query_arg
        # owner/repo/number/first variables must appear.
        assert any(a == "owner=owner" for a in captured)
        assert any(a == "repo=repo" for a in captured)
        assert "-F" in captured
        assert any(a == "number=42" for a in captured)
        assert any(a == "first=100" for a in captured)
        # First request must NOT include an ``after`` cursor.
        assert not any(a.startswith("after=") for a in captured)
