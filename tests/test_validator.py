"""Unit tests for gate_keeper.validator — the validation orchestrator.

All backend interactions use lightweight stubs registered via the registry so
no filesystem I/O or network access is required.
"""

from __future__ import annotations

import pytest

from gate_keeper import backends as registry
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    DiagnosticReport,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)
from gate_keeper.validator import validate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(
    rule_id: str = "test-rule",
    kind: RuleKind = RuleKind.FILE_EXISTS,
    backend_hint: Backend = Backend.FILESYSTEM,
) -> Rule:
    return Rule(
        id=rule_id,
        title="Test rule",
        source=SourceLocation(path="test.md", line=1),
        text="test rule text",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=backend_hint,
        confidence=Confidence.HIGH,
        params={},
    )


def _make_ruleset(*rules: Rule):
    from gate_keeper.models import RuleSet

    return RuleSet(rules=list(rules))


def _stub_diag(rule: Rule, status: Status, backend: Backend) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=backend,
        status=status,
        severity=rule.severity,
        message=f"stub: {status.value}",
        evidence=[],
    )


# ---------------------------------------------------------------------------
# Auto-dispatch tests
# ---------------------------------------------------------------------------


class TestAutoDispatch:
    """In 'auto' mode each rule is routed by its backend_hint."""

    def test_filesystem_rule_dispatched_to_filesystem(self, tmp_path):
        rule = _rule(kind=RuleKind.FILE_EXISTS, backend_hint=Backend.FILESYSTEM)
        # The real filesystem backend is used; tmp_path exists → PASS.
        report = validate(_make_ruleset(rule), tmp_path, backend="auto")
        assert len(report.diagnostics) == 1
        assert report.diagnostics[0].backend is Backend.FILESYSTEM
        assert report.diagnostics[0].status is Status.PASS

    def test_github_rule_dispatched_to_github_stub(self, tmp_path):
        rule = _rule(kind=RuleKind.GITHUB_PR_OPEN, backend_hint=Backend.GITHUB)
        report = validate(_make_ruleset(rule), tmp_path, backend="auto")
        assert len(report.diagnostics) == 1
        diag = report.diagnostics[0]
        assert diag.backend is Backend.GITHUB
        assert diag.status is Status.UNAVAILABLE

    def test_llm_rule_dispatched_to_llm_stub(self, tmp_path):
        rule = _rule(kind=RuleKind.SEMANTIC_RUBRIC, backend_hint=Backend.LLM_RUBRIC)
        report = validate(_make_ruleset(rule), tmp_path, backend="auto")
        assert len(report.diagnostics) == 1
        diag = report.diagnostics[0]
        assert diag.backend is Backend.LLM_RUBRIC
        assert diag.status is Status.UNAVAILABLE

    def test_order_preserved_with_mixed_backends(self, tmp_path):
        r1 = _rule("r1", RuleKind.FILE_EXISTS, Backend.FILESYSTEM)
        r2 = _rule("r2", RuleKind.GITHUB_PR_OPEN, Backend.GITHUB)
        r3 = _rule("r3", RuleKind.SEMANTIC_RUBRIC, Backend.LLM_RUBRIC)
        report = validate(_make_ruleset(r1, r2, r3), tmp_path, backend="auto")
        ids = [d.rule_id for d in report.diagnostics]
        assert ids == ["r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# Explicit backend tests
# ---------------------------------------------------------------------------


class TestExplicitBackend:
    """Named backend sends all rules through that single backend."""

    def test_filesystem_explicit_passes_filesystem_rule(self, tmp_path):
        rule = _rule(kind=RuleKind.FILE_EXISTS, backend_hint=Backend.FILESYSTEM)
        report = validate(_make_ruleset(rule), tmp_path, backend="filesystem")
        assert report.diagnostics[0].status is Status.PASS
        assert report.diagnostics[0].backend is Backend.FILESYSTEM

    def test_filesystem_explicit_unsupported_for_github_rule(self, tmp_path):
        rule = _rule(kind=RuleKind.GITHUB_PR_OPEN, backend_hint=Backend.GITHUB)
        report = validate(_make_ruleset(rule), tmp_path, backend="filesystem")
        diag = report.diagnostics[0]
        # filesystem backend returns UNSUPPORTED for github rules
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.FILESYSTEM

    def test_github_explicit_all_rules_use_github(self, tmp_path):
        r1 = _rule("r1", RuleKind.FILE_EXISTS, Backend.FILESYSTEM)
        r2 = _rule("r2", RuleKind.GITHUB_PR_OPEN, Backend.GITHUB)
        report = validate(_make_ruleset(r1, r2), tmp_path, backend="github")
        for diag in report.diagnostics:
            assert diag.backend is Backend.GITHUB

    def test_llm_rubric_explicit_all_rules_unavailable(self, tmp_path):
        r1 = _rule("r1", RuleKind.FILE_EXISTS, Backend.FILESYSTEM)
        r2 = _rule("r2", RuleKind.SEMANTIC_RUBRIC, Backend.LLM_RUBRIC)
        report = validate(_make_ruleset(r1, r2), tmp_path, backend="llm-rubric")
        for diag in report.diagnostics:
            assert diag.status is Status.UNAVAILABLE


# ---------------------------------------------------------------------------
# Unknown backend raises ValueError
# ---------------------------------------------------------------------------


class TestUnknownBackend:
    def test_unknown_backend_raises_value_error(self, tmp_path):
        rule = _rule()
        with pytest.raises(ValueError, match="unknown backend"):
            validate(_make_ruleset(rule), tmp_path, backend="nonexistent-backend")


# ---------------------------------------------------------------------------
# Error wrapping — exceptions from backend become ERROR diagnostics
# ---------------------------------------------------------------------------


class TestErrorWrapping:
    """Unexpected exceptions from a backend call become Status.ERROR diagnostics."""

    def test_backend_exception_becomes_error_diagnostic(self, tmp_path, monkeypatch):
        rule = _rule(kind=RuleKind.FILE_EXISTS, backend_hint=Backend.FILESYSTEM)

        def _raise(r, t):
            raise RuntimeError("simulated crash")

        monkeypatch.setitem(registry._REGISTRY, "filesystem", _raise)
        report = validate(_make_ruleset(rule), tmp_path, backend="filesystem")
        diag = report.diagnostics[0]
        assert diag.status is Status.ERROR
        assert "simulated crash" in diag.message
        assert any(e.kind == "exception" for e in diag.evidence)

    def test_error_diagnostic_round_trips(self, tmp_path, monkeypatch):
        rule = _rule(kind=RuleKind.FILE_EXISTS, backend_hint=Backend.FILESYSTEM)

        def _raise(r, t):
            raise ValueError("boom")

        monkeypatch.setitem(registry._REGISTRY, "filesystem", _raise)
        report = validate(_make_ruleset(rule), tmp_path, backend="filesystem")
        diag = report.diagnostics[0]
        rebuilt = Diagnostic.from_dict(diag.to_dict())
        assert rebuilt.status is Status.ERROR


# ---------------------------------------------------------------------------
# Empty ruleset
# ---------------------------------------------------------------------------


class TestEmptyRuleset:
    def test_empty_ruleset_returns_empty_report(self, tmp_path):
        report = validate(_make_ruleset(), tmp_path, backend="auto")
        assert isinstance(report, DiagnosticReport)
        assert report.diagnostics == []


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------


class TestReturnContract:
    def test_returns_diagnostic_report(self, tmp_path):
        rule = _rule()
        report = validate(_make_ruleset(rule), tmp_path)
        assert isinstance(report, DiagnosticReport)

    def test_one_diagnostic_per_rule(self, tmp_path):
        rules = [_rule(f"r{i}") for i in range(5)]
        report = validate(_make_ruleset(*rules), tmp_path)
        assert len(report.diagnostics) == 5

    def test_diagnostics_carry_rule_ids(self, tmp_path):
        r1 = _rule("alpha")
        r2 = _rule("beta")
        report = validate(_make_ruleset(r1, r2), tmp_path)
        ids = [d.rule_id for d in report.diagnostics]
        assert ids == ["alpha", "beta"]
