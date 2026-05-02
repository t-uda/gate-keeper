"""Tests for the status check rollup handler (issue #11).

Uses the same _patch_both / _ok / _RESOLVE_OK helpers as
tests/test_github_pr_state.py.  A local _pr_view_checks_json helper
builds a gh pr view payload that includes statusCheckRollup.
"""

from __future__ import annotations

import json
from typing import Any

from gate_keeper.backends import _gh, _target
from gate_keeper.backends import github as gh_backend
from gate_keeper.backends.github import _classify_check_entry
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
# Shared helpers (mirrors test_github_pr_state.py)
# ---------------------------------------------------------------------------

_RESOLVE_OK = json.dumps({"number": 42, "url": "https://github.com/owner/repo/pull/42"})


def _ok(stdout: str, args: tuple[str, ...] = ()) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh", *args))


def _make_github_rule(kind: RuleKind, params: dict | None = None) -> Rule:
    return Rule(
        id="test-rule",
        title="Test GitHub rule",
        source=SourceLocation(path="rules.md", line=1),
        text="Test rule text",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.GITHUB,
        confidence=Confidence.HIGH,
        params=params or {},
    )


def _make_run_gh_sequence(results: list[_gh.GhResult]):
    queue = list(results)

    def _fake(args, **kwargs):
        if queue:
            return queue.pop(0)
        raise AssertionError(f"run_gh called more times than expected; args={args!r}")

    return _fake


def _patch_both(monkeypatch, resolve_result: _gh.GhResult, fetch_result: _gh.GhResult):
    monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
    monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([fetch_result]))


def _pr_view_checks_json(rollup: Any = None, *, include_rollup: bool = True) -> str:
    """Build a gh pr view JSON string.

    Pass ``include_rollup=False`` to omit the field entirely (simulates missing
    field).  Otherwise ``rollup`` is the value for ``statusCheckRollup``.
    """
    payload: dict[str, Any] = {
        "state": "OPEN",
        "isDraft": False,
        "labels": [],
        "body": "",
    }
    if include_rollup:
        payload["statusCheckRollup"] = rollup
    return json.dumps(payload)


def _check_rule(monkeypatch, rollup: Any = None, *, include_rollup: bool = True) -> Diagnostic:
    _patch_both(
        monkeypatch,
        _ok(_RESOLVE_OK),
        _ok(_pr_view_checks_json(rollup, include_rollup=include_rollup)),
    )
    rule = _make_github_rule(RuleKind.GITHUB_CHECKS_SUCCESS)
    return gh_backend.check(rule, "owner/repo#42")


# ---------------------------------------------------------------------------
# Unit: _classify_check_entry
# ---------------------------------------------------------------------------


class TestClassifyCheckEntry:
    def test_status_context_success(self):
        entry = {"__typename": "StatusContext", "context": "ci/build", "state": "SUCCESS"}
        name, label, ok = _classify_check_entry(entry)
        assert name == "ci/build"
        assert label == "SUCCESS"
        assert ok is True

    def test_status_context_failure(self):
        entry = {"__typename": "StatusContext", "context": "ci/build", "state": "FAILURE"}
        name, label, ok = _classify_check_entry(entry)
        assert name == "ci/build"
        assert label == "FAILURE"
        assert ok is False

    def test_status_context_error(self):
        entry = {"__typename": "StatusContext", "context": "ci/lint", "state": "ERROR"}
        name, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "ERROR"

    def test_status_context_pending(self):
        entry = {"__typename": "StatusContext", "context": "ci/test", "state": "PENDING"}
        _, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "PENDING"

    def test_status_context_missing_state(self):
        entry = {"__typename": "StatusContext", "context": "ci/build"}
        name, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "UNKNOWN"

    def test_checkrun_completed_success(self):
        entry = {
            "__typename": "CheckRun",
            "name": "lint",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        name, label, ok = _classify_check_entry(entry)
        assert name == "lint"
        assert label == "SUCCESS"
        assert ok is True

    def test_checkrun_completed_neutral(self):
        entry = {
            "__typename": "CheckRun",
            "name": "lint",
            "status": "COMPLETED",
            "conclusion": "NEUTRAL",
        }
        name, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "NEUTRAL"

    def test_checkrun_completed_cancelled(self):
        entry = {
            "__typename": "CheckRun",
            "name": "build",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
        }
        _, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "CANCELLED"

    def test_checkrun_queued(self):
        entry = {"__typename": "CheckRun", "name": "build", "status": "QUEUED"}
        name, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "QUEUED"

    def test_checkrun_in_progress(self):
        entry = {
            "__typename": "CheckRun",
            "name": "tests",
            "status": "IN_PROGRESS",
        }
        _, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "IN_PROGRESS"

    def test_checkrun_completed_missing_conclusion(self):
        entry = {"__typename": "CheckRun", "name": "build", "status": "COMPLETED"}
        _, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "MISSING_CONCLUSION"

    def test_unknown_typename(self):
        entry = {"__typename": "SomeFutureType", "name": "x"}
        name, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "UNKNOWN"

    def test_no_typename(self):
        entry = {"name": "mystery"}
        name, label, ok = _classify_check_entry(entry)
        assert ok is False
        assert label == "UNKNOWN"

    def test_no_name_or_context_falls_back_to_unnamed(self):
        entry = {"__typename": "CheckRun", "status": "QUEUED"}
        name, label, ok = _classify_check_entry(entry)
        assert name == "<unnamed>"
        assert ok is False

    def test_uses_name_over_context(self):
        entry = {
            "__typename": "CheckRun",
            "name": "explicit-name",
            "context": "ignored-context",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        }
        name, _, _ = _classify_check_entry(entry)
        assert name == "explicit-name"

    def test_non_dict_entry(self):
        name, label, ok = _classify_check_entry("not-a-dict")  # type: ignore[arg-type]
        assert name == "<unnamed>"
        assert label == "UNKNOWN"
        assert ok is False


# ---------------------------------------------------------------------------
# Handler: missing / malformed rollup field
# ---------------------------------------------------------------------------


class TestMissingRollup:
    def test_missing_statuscheckrollup_field_unavailable(self, monkeypatch):
        diag = _check_rule(monkeypatch, include_rollup=False)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "statusCheckRollup"

    def test_non_list_rollup_unavailable(self, monkeypatch):
        """A non-list, non-recognized-dict value for statusCheckRollup fails closed."""
        diag = _check_rule(monkeypatch, rollup="not-a-list")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"

    def test_unrecognized_dict_rollup_unavailable(self, monkeypatch):
        """A dict without contexts.nodes or nodes also fails closed."""
        diag = _check_rule(monkeypatch, rollup={"unexpected": "shape"})
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"


class TestRollupShapeFlexibility:
    """gh's --json statusCheckRollup currently flattens to a list, but be
    tolerant of the GraphQL StatusCheckRollup object shape so a future gh
    upgrade or alternate access path doesn't silently break the rule."""

    _ENTRY = {
        "__typename": "CheckRun",
        "name": "lint",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
    }

    def test_dict_rollup_with_contexts_nodes_is_evaluated(self, monkeypatch):
        diag = _check_rule(
            monkeypatch,
            rollup={"contexts": {"nodes": [self._ENTRY]}},
        )
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total"] == 1
        assert ev.data["successful"] == ["lint"]

    def test_dict_rollup_with_top_level_nodes_is_evaluated(self, monkeypatch):
        diag = _check_rule(
            monkeypatch,
            rollup={"nodes": [self._ENTRY]},
        )
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total"] == 1
        assert ev.data["successful"] == ["lint"]


# ---------------------------------------------------------------------------
# Handler: empty rollup → vacuous PASS
# ---------------------------------------------------------------------------


class TestEmptyRollup:
    def test_empty_rollup_passes(self, monkeypatch):
        diag = _check_rule(monkeypatch, rollup=[])
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.kind == "checks_rollup"
        assert ev.data["total"] == 0
        assert ev.data["successful"] == []
        assert ev.data["non_successful"] == []
        assert "0 checks defined" in diag.message

    def test_empty_rollup_evidence_coords(self, monkeypatch):
        diag = _check_rule(monkeypatch, rollup=[])
        d = diag.evidence[0].data
        assert d["owner"] == "owner"
        assert d["repo"] == "repo"
        assert d["number"] == 42
        assert d["url"] == "https://github.com/owner/repo/pull/42"


# ---------------------------------------------------------------------------
# Handler: single SUCCESS entries
# ---------------------------------------------------------------------------


class TestSingleSuccessEntries:
    def test_single_checkrun_success_passes(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total"] == 1
        assert ev.data["successful"] == ["lint"]
        assert ev.data["non_successful"] == []

    def test_single_statuscontext_success_passes(self, monkeypatch):
        """StatusContext uses 'context' as name."""
        rollup = [
            {
                "__typename": "StatusContext",
                "context": "ci/build",
                "state": "SUCCESS",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["successful"] == ["ci/build"]

    def test_mix_checkrun_and_statuscontext_success(self, monkeypatch):
        """Both kinds of entries can coexist."""
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "StatusContext",
                "context": "ci/build",
                "state": "SUCCESS",
            },
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["total"] == 2
        assert set(ev.data["successful"]) == {"lint", "ci/build"}
        assert ev.data["non_successful"] == []


# ---------------------------------------------------------------------------
# Handler: failures / non-successful checks
# ---------------------------------------------------------------------------


class TestNonSuccessfulChecks:
    def test_one_failure_fails(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["non_successful"] == [{"name": "tests", "state": "FAILURE"}]
        assert ev.data["successful"] == []

    def test_pending_fails(self, monkeypatch):
        """StatusContext with state PENDING fails."""
        rollup = [{"__typename": "StatusContext", "context": "ci/test", "state": "PENDING"}]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["non_successful"][0]["name"] == "ci/test"
        assert ev.data["non_successful"][0]["state"] == "PENDING"

    def test_in_progress_fails(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "build",
                "status": "IN_PROGRESS",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL

    def test_checkrun_neutral_conclusion_fails(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "style",
                "status": "COMPLETED",
                "conclusion": "NEUTRAL",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["non_successful"][0]["state"] == "NEUTRAL"

    def test_checkrun_cancelled_conclusion_fails(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "deploy",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL

    def test_checkrun_queued_fails(self, monkeypatch):
        rollup = [{"__typename": "CheckRun", "name": "waiting", "status": "QUEUED"}]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["non_successful"][0]["state"] == "QUEUED"

    def test_statuscontext_error_fails(self, monkeypatch):
        rollup = [{"__typename": "StatusContext", "context": "ci/deploy", "state": "ERROR"}]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL

    def test_unknown_typename_fails(self, monkeypatch):
        rollup = [{"__typename": "FutureType", "name": "mystery"}]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["non_successful"][0]["state"] == "UNKNOWN"

    def test_missing_fields_unknown_and_unnamed(self, monkeypatch):
        """Entries missing name/context and type fields → '<unnamed>' + UNKNOWN."""
        rollup = [{}]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["non_successful"] == [{"name": "<unnamed>", "state": "UNKNOWN"}]

    def test_multi_fail_one_success_and_one_failure(self, monkeypatch):
        """Mixed rollup: successful and non-successful are categorised correctly."""
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "StatusContext",
                "context": "ci/integration",
                "state": "FAILURE",
            },
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["successful"] == ["lint"]
        assert ev.data["non_successful"] == [{"name": "ci/integration", "state": "FAILURE"}]
        assert ev.data["total"] == 2
        assert ev.data["summary"] == "1/2 checks passed"


# ---------------------------------------------------------------------------
# Evidence: round-trip through to_dict / from_dict
# ---------------------------------------------------------------------------


class TestDiagRoundTrip:
    def _round_trip(self, diag: Diagnostic) -> Diagnostic:
        return Diagnostic.from_dict(diag.to_dict())

    def test_pass_round_trips(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        rt = self._round_trip(diag)
        assert rt.status is Status.PASS
        assert rt.evidence[0].kind == "checks_rollup"

    def test_fail_round_trips(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            }
        ]
        diag = _check_rule(monkeypatch, rollup=rollup)
        rt = self._round_trip(diag)
        assert rt.status is Status.FAIL

    def test_unavailable_round_trips(self, monkeypatch):
        diag = _check_rule(monkeypatch, include_rollup=False)
        rt = self._round_trip(diag)
        assert rt.status is Status.UNAVAILABLE

    def test_empty_rollup_round_trips(self, monkeypatch):
        diag = _check_rule(monkeypatch, rollup=[])
        rt = self._round_trip(diag)
        assert rt.status is Status.PASS
        assert rt.evidence[0].data["total"] == 0


# ---------------------------------------------------------------------------
# End-to-end via validate()
# ---------------------------------------------------------------------------


class TestEndToEndValidator:
    def test_all_success_passes_exit_0(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "lint",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "StatusContext",
                "context": "ci/build",
                "state": "SUCCESS",
            },
        ]
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_checks_json(rollup)),
        )
        rule = _make_github_rule(RuleKind.GITHUB_CHECKS_SUCCESS)
        report = validate(RuleSet(rules=[rule]), "owner/repo#42", backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.PASS
        assert compute_exit_code(report.diagnostics) == EXIT_OK

    def test_one_failure_exit_1(self, monkeypatch):
        rollup = [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            }
        ]
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_checks_json(rollup)),
        )
        rule = _make_github_rule(RuleKind.GITHUB_CHECKS_SUCCESS)
        report = validate(RuleSet(rules=[rule]), "owner/repo#42", backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.FAIL
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL
