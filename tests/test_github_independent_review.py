"""Tests for _check_non_author_approval (issue #13).

Each test monkeypatches:
- ``gate_keeper.backends._target.run_gh`` for resolve_target (returns PR number/url)
- ``gate_keeper.backends.github.run_gh``  for _fetch_pr_view (returns latestReviews/author)

Helper ``_pr_view_json`` accepts keyword fields including ``reviews`` and ``author``
so callers can construct arbitrary payloads without raw JSON strings.
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
# Helpers (mirrors patterns from test_github_pr_state.py)
# ---------------------------------------------------------------------------

_RESOLVE_OK = json.dumps({"number": 42, "url": "https://github.com/owner/repo/pull/42"})


def _ok(stdout: str, args: tuple[str, ...] = ()) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh", *args))


def _make_non_author_approval_rule() -> Rule:
    return Rule(
        id="test-approval",
        title="Non-author approval required",
        source=SourceLocation(path="rules.md", line=1),
        text="At least one non-author non-bot APPROVED review is required.",
        kind=RuleKind.GITHUB_NON_AUTHOR_APPROVAL,
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


def _patch_both(monkeypatch, resolve_result: _gh.GhResult, fetch_result: _gh.GhResult):
    """Monkeypatch both target resolver and github backend run_gh."""
    monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
    monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence([fetch_result]))


def _pr_view_json(**fields: Any) -> str:
    """Build a JSON string for a gh pr view response.

    Caller passes keyword arguments; all are included verbatim in the JSON.
    ``reviews`` and ``author`` are passed as Python objects and serialized.
    """
    return json.dumps(fields)


def _review(login: str, state: str, *, is_bot: bool = False, submitted_at: str | None = None) -> dict:
    """Build a single review dict matching the gh pr view --json reviews shape."""
    entry: dict = {
        "id": f"PRR_{login}_{state}",
        "author": {"login": login, "is_bot": is_bot},
        "state": state,
    }
    if submitted_at is not None:
        entry["submittedAt"] = submitted_at
    return entry


def _author(login: str, *, is_bot: bool = False) -> dict:
    return {"login": login, "is_bot": is_bot}


def _base_payload(**extra: Any) -> dict:
    """Return a minimal pr view payload with the fields the approval rule needs.

    The github backend now requests only the fields its handler consumes (the
    approval rule asks gh for ``latestReviews,author``), so we mirror that
    narrow shape here. ``reviews`` is accepted as an alias and copied into
    ``latestReviews`` so legacy tests authored against the wider payload still
    work without rewriting every case.
    """
    payload: dict = {
        "latestReviews": [],
        "author": _author("octocat"),
    }
    payload.update(extra)
    if "reviews" in payload and "latestReviews" not in extra:
        payload["latestReviews"] = payload.pop("reviews")
    elif "reviews" in payload:
        payload.pop("reviews")
    return payload


# ---------------------------------------------------------------------------
# Missing field cases
# ---------------------------------------------------------------------------


class TestMissingFields:
    def test_missing_author_field_unavailable(self, monkeypatch):
        payload = _base_payload()
        del payload["author"]
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert "author" in diag.evidence[0].data["field"]

    def test_author_not_a_dict_unavailable(self, monkeypatch):
        payload = _base_payload(author="octocat")  # string, not dict
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"

    def test_author_login_missing_unavailable(self, monkeypatch):
        payload = _base_payload(author={"is_bot": False})  # no login key
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert "author.login" in diag.evidence[0].data["field"]

    def test_missing_reviews_field_unavailable(self, monkeypatch):
        payload = _base_payload()
        del payload["latestReviews"]
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"
        assert diag.evidence[0].data["field"] == "latestReviews"

    def test_reviews_not_a_list_unavailable(self, monkeypatch):
        payload = _base_payload(reviews="not-a-list")
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "gh_missing_field"


# ---------------------------------------------------------------------------
# Core algorithm cases
# ---------------------------------------------------------------------------


class TestNonAuthorApproval:
    def test_empty_reviews_fails(self, monkeypatch):
        """No reviews at all → no approver → FAIL."""
        payload = _base_payload(reviews=[], author=_author("octocat"))
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.kind == "pr_independent_review"
        assert ev.data["approved_by"] == []

    def test_one_non_author_approved_passes(self, monkeypatch):
        """Single non-author non-bot APPROVED review → PASS."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "APPROVED", submitted_at="2026-04-29T10:00:00Z")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.data["approved_by"] == ["alice"]
        assert ev.data["author"] == "octocat"
        assert ev.data["ignored_self_reviews"] == 0

    def test_self_review_approved_fails(self, monkeypatch):
        """Author self-review APPROVED → FAIL; self-reviews counted but excluded."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("octocat", "APPROVED")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["approved_by"] == []
        assert ev.data["ignored_self_reviews"] >= 1

    def test_bot_approved_with_is_bot_true_fails(self, monkeypatch):
        """Bot reviewer (is_bot=True) APPROVED → FAIL; login in ignored_bot_reviewers."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("github-actions[bot]", "APPROVED", is_bot=True)],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["approved_by"] == []
        assert "github-actions[bot]" in ev.data["ignored_bot_reviewers"]

    def test_bot_approved_via_login_suffix_no_is_bot_field(self, monkeypatch):
        """Bot detected by [bot] login suffix even when is_bot is absent."""
        review_entry = {
            "id": "PRR_x",
            "author": {"login": "dependabot[bot]"},  # no is_bot key
            "state": "APPROVED",
            "submittedAt": "2026-04-29T10:00:00Z",
        }
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[review_entry],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert "dependabot[bot]" in ev.data["ignored_bot_reviewers"]

    def test_latest_per_reviewer_approved_passes(self, monkeypatch):
        """gh ``latestReviews`` returns the latest state per reviewer.

        For alice the latest is APPROVED, so the rule passes. The earlier
        COMMENTED state is collapsed away by gh; we don't see it.
        """
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "APPROVED", submitted_at="2026-04-29T10:00:00Z")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert "alice" in ev.data["approved_by"]

    def test_latest_per_reviewer_dismissed_fails(self, monkeypatch):
        """For alice the latest state is DISMISSED → FAIL; an earlier APPROVAL
        on the same PR has been superseded and is not reported by gh's
        ``latestReviews``.
        """
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "DISMISSED", submitted_at="2026-04-29T10:00:00Z")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["approved_by"] == []
        assert {"login": "alice", "state": "DISMISSED"} in ev.data["non_approved_reviewers"]

    def test_one_approved_one_changes_requested_passes(self, monkeypatch):
        """One reviewer APPROVED, another CHANGES_REQUESTED → PASS (one is enough)."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[
                _review("alice", "APPROVED", submitted_at="2026-04-29T10:00:00Z"),
                _review("bob", "CHANGES_REQUESTED", submitted_at="2026-04-29T10:00:00Z"),
            ],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert "alice" in ev.data["approved_by"]
        assert any(r["login"] == "bob" for r in ev.data["non_approved_reviewers"])

    def test_all_commented_or_changes_requested_fails(self, monkeypatch):
        """All non-bot non-author reviews are COMMENTED or CHANGES_REQUESTED → FAIL."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[
                _review("alice", "COMMENTED"),
                _review("bob", "CHANGES_REQUESTED"),
            ],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL

    def test_dismissed_only_fails(self, monkeypatch):
        """DISMISSED review does not satisfy the rule."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "DISMISSED")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL

    def test_pending_only_fails(self, monkeypatch):
        """PENDING review does not satisfy the rule."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "PENDING")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL

    def test_mixed_bot_self_comment_no_qualifying_approved_fails(self, monkeypatch):
        """Bot APPROVED + author APPROVED + non-author COMMENTED → FAIL."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[
                _review("copilot-pull-request-reviewer[bot]", "APPROVED", is_bot=True),
                _review("octocat", "APPROVED"),  # self-review
                _review("alice", "COMMENTED"),
            ],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        assert ev.data["approved_by"] == []
        assert ev.data["ignored_self_reviews"] >= 1

    def test_evidence_contains_pr_coordinates(self, monkeypatch):
        """Evidence includes owner, repo, number, url."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "APPROVED")],
        )
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        ev = diag.evidence[0]
        assert ev.data["owner"] == "owner"
        assert ev.data["repo"] == "repo"
        assert ev.data["number"] == 42
        assert ev.data["url"] == "https://github.com/owner/repo/pull/42"

    def test_duplicate_login_in_latest_reviews_keeps_first(self, monkeypatch):
        """Defensive: gh's ``latestReviews`` should collapse to one entry per
        reviewer, but if a payload ever contains duplicates the handler keeps
        the first occurrence and treats subsequent entries from the same login
        as no-ops. This locks in the de-dup behaviour without depending on a
        particular ordering rule.
        """
        reviews = [
            {"id": "PRR_1", "author": {"login": "alice", "is_bot": False}, "state": "APPROVED"},
            {"id": "PRR_2", "author": {"login": "alice", "is_bot": False}, "state": "DISMISSED"},
        ]
        payload = _base_payload(author=_author("octocat"), reviews=reviews)
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.PASS
        assert diag.evidence[0].data["approved_by"] == ["alice"]

    def test_skips_non_dict_review_entries(self, monkeypatch):
        """Non-dict entries in the reviews list are silently ignored."""
        reviews: list = [
            "not-a-dict",
            None,
            42,
            _review("alice", "APPROVED"),
        ]
        payload = _base_payload(author=_author("octocat"), reviews=reviews)
        _patch_both(monkeypatch, _ok(_RESOLVE_OK), _ok(json.dumps(payload)))
        diag = gh_backend.check(_make_non_author_approval_rule(), "owner/repo#42")
        assert diag.status is Status.PASS
        assert "alice" in diag.evidence[0].data["approved_by"]


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------


class TestDiagnosticRoundTrip:
    """Verify that pr_independent_review diagnostics survive Diagnostic.from_dict(d.to_dict())."""

    def _make_diag(self, status: Status, approved_by: list[str], non_approved: list[dict]) -> Diagnostic:
        from gate_keeper.models import Evidence, SourceLocation
        return Diagnostic(
            rule_id="test-approval",
            source=SourceLocation(path="rules.md", line=1),
            backend=Backend.GITHUB,
            status=status,
            severity=Severity.ERROR,
            message="test message",
            evidence=[
                Evidence(
                    kind="pr_independent_review",
                    data={
                        "author": "octocat",
                        "approved_by": approved_by,
                        "non_approved_reviewers": non_approved,
                        "ignored_bot_reviewers": [],
                        "ignored_self_reviews": 0,
                        "owner": "owner",
                        "repo": "repo",
                        "number": 42,
                        "url": "https://github.com/owner/repo/pull/42",
                    },
                )
            ],
        )

    def test_pass_diag_round_trips(self):
        d = self._make_diag(Status.PASS, ["alice"], [])
        d2 = Diagnostic.from_dict(d.to_dict())
        assert d2.status is Status.PASS
        assert d2.evidence[0].data["approved_by"] == ["alice"]

    def test_fail_diag_round_trips(self):
        d = self._make_diag(Status.FAIL, [], [{"login": "bob", "state": "COMMENTED"}])
        d2 = Diagnostic.from_dict(d.to_dict())
        assert d2.status is Status.FAIL
        assert d2.evidence[0].data["non_approved_reviewers"][0]["login"] == "bob"

    def test_unavailable_diag_round_trips(self):
        from gate_keeper.models import Evidence, SourceLocation
        d = Diagnostic(
            rule_id="test-approval",
            source=SourceLocation(path="rules.md", line=1),
            backend=Backend.GITHUB,
            status=Status.UNAVAILABLE,
            severity=Severity.ERROR,
            message="gh 'pr-view' response is missing required field 'author'.",
            evidence=[
                Evidence(
                    kind="gh_missing_field",
                    data={"op": "pr-view", "field": "author"},
                )
            ],
        )
        d2 = Diagnostic.from_dict(d.to_dict())
        assert d2.status is Status.UNAVAILABLE
        assert d2.evidence[0].kind == "gh_missing_field"


# ---------------------------------------------------------------------------
# End-to-end via validate()
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_one_approved_reviewer_exits_ok(self, monkeypatch):
        """Full validate() with one qualifying APPROVED review → exit 0."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[_review("alice", "APPROVED", submitted_at="2026-04-29T10:00:00Z")],
        )
        monkeypatch.setattr(
            _target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)])
        )
        monkeypatch.setattr(
            gh_backend, "run_gh", _make_run_gh_sequence([_ok(json.dumps(payload))])
        )

        rule = _make_non_author_approval_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")

        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.PASS
        assert compute_exit_code(report.diagnostics) == EXIT_OK

    def test_no_qualifying_reviewer_exits_fail(self, monkeypatch):
        """Full validate() with no qualifying APPROVED review → exit 1."""
        payload = _base_payload(
            author=_author("octocat"),
            reviews=[],
        )
        monkeypatch.setattr(
            _target, "run_gh", _make_run_gh_sequence([_ok(_RESOLVE_OK)])
        )
        monkeypatch.setattr(
            gh_backend, "run_gh", _make_run_gh_sequence([_ok(json.dumps(payload))])
        )

        rule = _make_non_author_approval_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, "owner/repo#42", backend="auto")

        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].status is Status.FAIL
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL
