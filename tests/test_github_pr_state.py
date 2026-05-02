"""Tests for the four PR-state handlers added in issue #10.

Each test monkeypatches both:
- ``gate_keeper.backends._target.run_gh`` — for resolve_target (returns PR number/url)
- ``gate_keeper.backends.github.run_gh``  — for _fetch_pr_view (returns state/isDraft/labels/body)

A helper ``_make_run_gh`` produces a fake ``run_gh`` that cycles through a list
of pre-configured ``GhResult`` objects in order, so the two calls (resolve +
fetch) can return different payloads.
"""

from __future__ import annotations

import json

from gate_keeper.backends import _gh, _target
from gate_keeper.backends import github as gh_backend
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, compute_exit_code
from gate_keeper.models import (
    Backend,
    Confidence,
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


def _ok(stdout: str, args: tuple[str, ...] = ()) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh", *args))


def _fail_missing() -> _gh.GhResult:
    return _gh.GhResult(
        ok=False,
        stdout="",
        stderr="gh binary not found",
        returncode=127,
        cmd=("gh",),
        binary_missing=True,
    )


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
    """Return a fake run_gh that pops results from the list in order."""
    queue = list(results)

    def _fake(args, **kwargs):
        if queue:
            return queue.pop(0)
        raise AssertionError(f"run_gh called more times than expected; args={args!r}")

    return _fake


def _patch_both(monkeypatch, resolve_result: _gh.GhResult, fetch_result: _gh.GhResult):
    """Monkeypatch both target resolver and github backend run_gh."""
    monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
    monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([fetch_result]))


def _pr_view_json(**fields) -> str:
    """Build a JSON string for a gh pr view response."""
    return json.dumps(fields)


# ---------------------------------------------------------------------------
# PR Open
# ---------------------------------------------------------------------------


class TestCheckPrOpen:
    def test_open_state_passes(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "pr_state"
        assert diag.evidence[0].data["state"] == "OPEN"

    def test_closed_state_fails(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="CLOSED", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert diag.evidence[0].data["state"] == "CLOSED"

    def test_merged_state_fails(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="MERGED", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert diag.evidence[0].data["state"] == "MERGED"

    def test_missing_state_field_unavailable(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "state"


# ---------------------------------------------------------------------------
# Not Draft
# ---------------------------------------------------------------------------


class TestCheckNotDraft:
    def test_not_draft_passes(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_NOT_DRAFT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "pr_draft"
        assert diag.evidence[0].data["is_draft"] is False

    def test_is_draft_fails(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=True, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_NOT_DRAFT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert diag.evidence[0].data["is_draft"] is True

    def test_missing_is_draft_field_unavailable(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_NOT_DRAFT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "isDraft"

    def test_non_bool_is_draft_unavailable(self, monkeypatch):
        """isDraft must be a bool; a string value fails closed."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft="yes", labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_NOT_DRAFT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"


# ---------------------------------------------------------------------------
# Labels Absent
# ---------------------------------------------------------------------------


class TestCheckLabelsAbsent:
    def test_missing_labels_field_unavailable(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_LABELS_ABSENT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "labels"

    def test_empty_labels_passes_with_defaults(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_LABELS_ABSENT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "pr_labels"
        assert diag.evidence[0].data["matched"] == []

    def test_blocking_label_do_not_merge_fails(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(
                _pr_view_json(
                    state="OPEN",
                    isDraft=False,
                    labels=[{"name": "do-not-merge"}],
                    body="",
                )
            ),
        )
        rule = _make_github_rule(RuleKind.GITHUB_LABELS_ABSENT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert "do-not-merge" in diag.evidence[0].data["matched"]

    def test_blocking_label_case_insensitive(self, monkeypatch):
        """Label matching is case-insensitive."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(
                _pr_view_json(
                    state="OPEN",
                    isDraft=False,
                    labels=[{"name": "DO-NOT-MERGE"}],
                    body="",
                )
            ),
        )
        rule = _make_github_rule(RuleKind.GITHUB_LABELS_ABSENT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert "DO-NOT-MERGE" in diag.evidence[0].data["matched"]

    def test_mixed_labels_one_blocking(self, monkeypatch):
        """Only the blocking label appears in matched; feature label is ignored."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(
                _pr_view_json(
                    state="OPEN",
                    isDraft=False,
                    labels=[{"name": "blocked"}, {"name": "feature"}],
                    body="",
                )
            ),
        )
        rule = _make_github_rule(RuleKind.GITHUB_LABELS_ABSENT)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert diag.evidence[0].data["matched"] == ["blocked"]

    def test_custom_blocking_label_fails(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(
                _pr_view_json(
                    state="OPEN",
                    isDraft=False,
                    labels=[{"name": "custom-block"}],
                    body="",
                )
            ),
        )
        rule = _make_github_rule(
            RuleKind.GITHUB_LABELS_ABSENT,
            params={"labels": ["custom-block"]},
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert "custom-block" in diag.evidence[0].data["matched"]

    def test_explicit_empty_params_labels_means_no_blocking_pass(self, monkeypatch):
        """An explicit empty list in params.labels means no blocking labels → always PASS."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(
                _pr_view_json(
                    state="OPEN",
                    isDraft=False,
                    labels=[{"name": "do-not-merge"}],
                    body="",
                )
            ),
        )
        rule = _make_github_rule(
            RuleKind.GITHUB_LABELS_ABSENT,
            params={"labels": []},
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        assert diag.evidence[0].data["matched"] == []

    def test_invalid_params_labels_type_unavailable(self, monkeypatch):
        """params.labels with a non-list value fails closed (UNAVAILABLE)."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(
            RuleKind.GITHUB_LABELS_ABSENT,
            params={"labels": "do-not-merge"},  # bare string is wrong shape
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "rule_params_invalid"
        assert diag.evidence[0].data["param"] == "labels"

    def test_invalid_params_labels_non_string_items_unavailable(self, monkeypatch):
        """params.labels with non-string items fails closed."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(
            RuleKind.GITHUB_LABELS_ABSENT,
            params={"labels": ["ok", 42]},
        )
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "rule_params_invalid"

    def test_missing_params_labels_key_uses_defaults(self, monkeypatch):
        """When params has no 'labels' key at all, the default blocking list is used."""
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(
                _pr_view_json(
                    state="OPEN",
                    isDraft=False,
                    labels=[{"name": "needs-decision"}],
                    body="",
                )
            ),
        )
        rule = _make_github_rule(RuleKind.GITHUB_LABELS_ABSENT)  # no params
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        assert "needs-decision" in diag.evidence[0].data["matched"]
        assert "needs-decision" in diag.evidence[0].data["blocking"]


# ---------------------------------------------------------------------------
# Tasks Complete
# ---------------------------------------------------------------------------


class TestCheckTasksComplete:
    def test_null_body_unavailable(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body=None)),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "body"

    def test_missing_body_field_unavailable(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[])),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"

    def test_non_string_body_fails_closed(self, monkeypatch):
        """A non-string body (e.g. malformed gh response) must NOT crash; UNAVAILABLE."""
        # Build the JSON payload by hand because _pr_view_json filters None.
        import json

        bad_payload = json.dumps(
            {
                "state": "OPEN",
                "isDraft": False,
                "labels": [],
                "body": 42,  # numeric, not a string
            }
        )
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(bad_payload),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "body"

    def test_empty_body_passes(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["unchecked"] == 0
        assert ev.data["total"] == 0

    def test_one_unchecked_item_fails(self, monkeypatch):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body="- [ ] item")),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["unchecked"] == 1
        assert ev.data["checked"] == 0

    def test_mixed_tasks_fail_when_unchecked(self, monkeypatch):
        body = "- [x] done\n- [ ] todo\n"
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body=body)),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["unchecked"] == 1
        assert ev.data["checked"] == 1
        assert ev.data["total"] == 2

    def test_all_checked_passes(self, monkeypatch):
        body = "- [x] done\n- [X] also done\n"
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body=body)),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["unchecked"] == 0
        assert ev.data["checked"] == 2

    def test_fenced_code_task_ignored(self, monkeypatch):
        """Task boxes inside fenced code blocks are ignored."""
        body = "Real content\n```\n- [ ] inside fence\n```\n"
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body=body)),
        )
        rule = _make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["unchecked"] == 0


# ---------------------------------------------------------------------------
# Evidence structure
# ---------------------------------------------------------------------------


class TestEvidenceStructure:
    """Verify that evidence payloads contain the expected coordinate fields."""

    def _resolve_and_fetch(self, monkeypatch, pr_view_payload: dict):
        _patch_both(
            monkeypatch,
            _ok(_RESOLVE_OK),
            _ok(json.dumps(pr_view_payload)),
        )

    def _coords_present(self, data: dict):
        assert data["owner"] == "owner"
        assert data["repo"] == "repo"
        assert data["number"] == 42
        assert data["url"] == "https://github.com/owner/repo/pull/42"

    def test_pr_open_evidence_coords(self, monkeypatch):
        self._resolve_and_fetch(monkeypatch, {"state": "OPEN", "isDraft": False, "labels": [], "body": ""})
        diag = gh_backend.check(_make_github_rule(RuleKind.GITHUB_PR_OPEN), "owner/repo#42")
        self._coords_present(diag.evidence[0].data)

    def test_not_draft_evidence_coords(self, monkeypatch):
        self._resolve_and_fetch(monkeypatch, {"state": "OPEN", "isDraft": False, "labels": [], "body": ""})
        diag = gh_backend.check(_make_github_rule(RuleKind.GITHUB_NOT_DRAFT), "owner/repo#42")
        self._coords_present(diag.evidence[0].data)

    def test_labels_absent_evidence_coords(self, monkeypatch):
        self._resolve_and_fetch(monkeypatch, {"state": "OPEN", "isDraft": False, "labels": [], "body": ""})
        diag = gh_backend.check(_make_github_rule(RuleKind.GITHUB_LABELS_ABSENT), "owner/repo#42")
        self._coords_present(diag.evidence[0].data)

    def test_tasks_complete_evidence_coords(self, monkeypatch):
        self._resolve_and_fetch(monkeypatch, {"state": "OPEN", "isDraft": False, "labels": [], "body": ""})
        diag = gh_backend.check(_make_github_rule(RuleKind.GITHUB_TASKS_COMPLETE), "owner/repo#42")
        self._coords_present(diag.evidence[0].data)


# ---------------------------------------------------------------------------
# Fetch failures (gh error, auth, JSON) propagate before dispatch
# ---------------------------------------------------------------------------


class TestFetchFailures:
    def test_gh_missing_binary_returns_unavailable(self, monkeypatch):
        """If gh is missing at resolve time, we get UNAVAILABLE immediately."""
        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([_fail_missing()]))
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE

    def test_fetch_failure_returns_unavailable(self, monkeypatch):
        """If the pr-view fetch call fails, handler never runs."""
        fail_result = _gh.GhResult(
            ok=False,
            stdout="",
            stderr="rate limit exceeded",
            returncode=1,
            cmd=("gh",),
        )
        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)]))
        monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([fail_result]))
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE

    def test_fetch_json_error_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)]))
        bad_json = _gh.GhResult(ok=True, stdout="not-json", stderr="", returncode=0, cmd=("gh",))
        monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([bad_json]))
        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_json_error"


# ---------------------------------------------------------------------------
# End-to-end via validator
# ---------------------------------------------------------------------------


class TestEndToEndValidator:
    def test_pr_open_rule_passes_via_validator(self, monkeypatch):
        """Full validate() call with a GITHUB_PR_OPEN rule returns PASS and exit 0."""
        resolve_result = _ok(_RESOLVE_OK)
        fetch_result = _ok(_pr_view_json(state="OPEN", isDraft=False, labels=[], body=""))

        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
        monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([fetch_result]))

        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")

        assert len(report.diagnostics) == 1
        diag = report.diagnostics[0]
        assert diag.status is Status.PASS
        assert compute_exit_code(report.diagnostics) == EXIT_OK

    def test_closed_pr_fails_via_validator(self, monkeypatch):
        resolve_result = _ok(_RESOLVE_OK)
        fetch_result = _ok(_pr_view_json(state="CLOSED", isDraft=False, labels=[], body=""))

        monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
        monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([fetch_result]))

        rule = _make_github_rule(RuleKind.GITHUB_PR_OPEN)
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")

        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.FAIL
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL
