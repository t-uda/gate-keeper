"""Sanity-check: the github backend stub produces UNAVAILABLE diagnostics.

Acceptance criterion 2: "No unavailable evidence path exits 0."
We verify that compute_exit_code treats these diagnostics as non-zero.
"""

from __future__ import annotations

import pytest

from gate_keeper.backends import github as gh_backend
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, compute_exit_code
from gate_keeper.models import (
    Backend,
    Confidence,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)
from gate_keeper.validator import validate


def _github_rule(kind: RuleKind = RuleKind.GITHUB_PR_OPEN) -> Rule:
    return Rule(
        id="stub-rule",
        title="Stub GitHub rule",
        source=SourceLocation(path="rules.md", line=1),
        text="PR must be open",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.GITHUB,
        confidence=Confidence.HIGH,
        params={},
    )


class TestGithubBackendStub:
    def test_check_returns_unavailable(self, tmp_path):
        rule = _github_rule()
        diag = gh_backend.check(rule, tmp_path)
        assert diag.status is Status.UNAVAILABLE

    def test_check_backend_is_github(self, tmp_path):
        rule = _github_rule()
        diag = gh_backend.check(rule, tmp_path)
        assert diag.backend is Backend.GITHUB

    def test_check_message_names_rule_kind_when_target_resolves(self, monkeypatch):
        """When a truly unknown kind resolves, the defensive fall-through names the kind.

        We simulate this by calling check() with a fabricated rule kind value.
        Since GITHUB_NON_AUTHOR_APPROVAL is now implemented, we use a kind that
        is NOT in the dispatch tables (SEMANTIC_RUBRIC routes to the github backend
        only if forced; here we test the defensive path directly on the handlers).
        """
        import json

        from gate_keeper.backends import _gh, _target

        resolve_payload = json.dumps({"number": 42, "url": "https://github.com/owner/repo/pull/42"})
        pr_view_payload = json.dumps(
            {
                "state": "OPEN",
                "isDraft": False,
                "labels": [],
                "body": "",
                "statusCheckRollup": [],
                "reviews": [],
                "author": {"login": "octocat"},
            }
        )

        call_count = [0]

        def _fake_run_gh(args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _gh.GhResult(
                    ok=True,
                    stdout=resolve_payload,
                    stderr="",
                    returncode=0,
                    cmd=("gh", *args),
                )
            return _gh.GhResult(
                ok=True,
                stdout=pr_view_payload,
                stderr="",
                returncode=0,
                cmd=("gh", *args),
            )

        monkeypatch.setattr(_target, "run_gh", _fake_run_gh)
        monkeypatch.setattr(gh_backend, "run_gh", _fake_run_gh)

        # Force a rule kind that is not handled by the github backend
        # by patching the handler table directly.
        from gate_keeper.backends import github as _ghmod
        from gate_keeper.models import RuleKind

        original = dict(_ghmod._PR_VIEW_HANDLERS)
        try:
            # Remove NON_AUTHOR_APPROVAL to simulate an unimplemented kind
            del _ghmod._PR_VIEW_HANDLERS[RuleKind.GITHUB_NON_AUTHOR_APPROVAL]
            rule = _github_rule(RuleKind.GITHUB_NON_AUTHOR_APPROVAL)
            diag = gh_backend.check(rule, "owner/repo#42")
            assert diag.status is Status.UNAVAILABLE
            assert "github_non_author_approval" in diag.message
        finally:
            _ghmod._PR_VIEW_HANDLERS.update(original)

    def test_check_invalid_target_surfaces_target_parse_error(self, tmp_path):
        """A non-PR target now surfaces a parse-error diagnostic via the resolver."""
        rule = _github_rule(RuleKind.GITHUB_NOT_DRAFT)
        diag = gh_backend.check(rule, tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "target_parse_error"

    @pytest.mark.parametrize(
        "kind",
        [
            RuleKind.GITHUB_PR_OPEN,
            RuleKind.GITHUB_NOT_DRAFT,
            RuleKind.GITHUB_LABELS_ABSENT,
            RuleKind.GITHUB_TASKS_COMPLETE,
            RuleKind.GITHUB_CHECKS_SUCCESS,
            RuleKind.GITHUB_THREADS_RESOLVED,
            RuleKind.GITHUB_NON_AUTHOR_APPROVAL,
        ],
    )
    def test_all_github_kinds_return_unavailable(self, tmp_path, kind):
        # tmp_path is not a valid PR target; the resolver fails closed and
        # the diagnostic is UNAVAILABLE/github (the parse-error path).
        rule = _github_rule(kind)
        diag = gh_backend.check(rule, tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.backend is Backend.GITHUB

    def test_unavailable_exit_code_is_nonzero(self, tmp_path):
        """Acceptance criterion 2: unavailable → exit non-zero."""
        rule = _github_rule()
        diag = gh_backend.check(rule, tmp_path)
        code = compute_exit_code([diag])
        assert code == EXIT_FAIL
        assert code != EXIT_OK

    def test_via_validator_auto_dispatch_github_rule_nonzero(self, tmp_path):
        """End-to-end: validator with a GitHub rule produces non-zero exit code."""
        from gate_keeper.models import RuleSet

        rule = _github_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, tmp_path, backend="auto")
        assert len(report.diagnostics) == 1
        diag = report.diagnostics[0]
        assert diag.status is Status.UNAVAILABLE
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL
