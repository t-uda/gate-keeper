"""Sanity-check: the github backend stub produces UNAVAILABLE diagnostics.

Acceptance criterion 2: "No unavailable evidence path exits 0."
We verify that compute_exit_code treats these diagnostics as non-zero.
"""
from __future__ import annotations

from pathlib import Path

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
        """When the target resolves, a stub kind still names the rule kind in its message.

        GITHUB_THREADS_RESOLVED is not yet implemented (#12), so it returns
        UNAVAILABLE with the rule kind in the message even after resolution.
        """
        from gate_keeper.backends import _gh, _target

        def _fake_run_gh(args, **kwargs):
            return _gh.GhResult(
                ok=True,
                stdout='{"number": 42, "url": "https://github.com/owner/repo/pull/42"}',
                stderr="",
                returncode=0,
                cmd=("gh", *args),
            )

        monkeypatch.setattr(_target, "run_gh", _fake_run_gh)
        rule = _github_rule(RuleKind.GITHUB_THREADS_RESOLVED)
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert "github_threads_resolved" in diag.message

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
