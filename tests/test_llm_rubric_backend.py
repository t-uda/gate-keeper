"""Tests for the LLM-rubric backend stub.

Acceptance criterion: "Running LLM-backed validation without configuration
returns a non-passing diagnostic."
We verify that compute_exit_code treats these diagnostics as non-zero.
"""
from __future__ import annotations

from gate_keeper.backends import llm_rubric as llm_backend
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


def _semantic_rule(kind: RuleKind = RuleKind.SEMANTIC_RUBRIC) -> Rule:
    return Rule(
        id="stub-llm-rule",
        title="Stub semantic rule",
        source=SourceLocation(path="rules.md", line=5),
        text="The documentation should be clear and comprehensive",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params={},
    )


class TestLlmRubricBackendStub:
    def test_check_returns_unavailable(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.status is Status.UNAVAILABLE

    def test_check_backend_is_llm_rubric(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.backend is Backend.LLM_RUBRIC

    def test_check_preserves_rule_id(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.rule_id == rule.id

    def test_check_preserves_severity(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.severity is rule.severity

    def test_check_has_evidence(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert len(diag.evidence) >= 1

    def test_evidence_kind_is_provider_unconfigured(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.evidence[0].kind == "provider_unconfigured"

    def test_evidence_includes_rule_text(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.evidence[0].data["rule_text"] == rule.text

    def test_evidence_includes_rule_kind(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.evidence[0].data["rule_kind"] == rule.kind.value

    def test_evidence_includes_target(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.evidence[0].data["target"] == str(tmp_path)

    def test_check_has_remediation(self, tmp_path):
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        assert diag.remediation is not None
        assert len(diag.remediation) > 0

    def test_unavailable_exit_code_is_nonzero(self, tmp_path):
        """Acceptance criterion: unavailable → exit non-zero."""
        rule = _semantic_rule()
        diag = llm_backend.check(rule, tmp_path)
        code = compute_exit_code([diag])
        assert code == EXIT_FAIL
        assert code != EXIT_OK

    def test_check_with_pr_target_still_unavailable(self):
        """Non-filesystem targets (PR references) also return UNAVAILABLE."""
        rule = _semantic_rule()
        diag = llm_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.backend is Backend.LLM_RUBRIC

    def test_via_validator_auto_dispatch_semantic_rule_nonzero(self, tmp_path):
        """End-to-end: validator auto-dispatches semantic_rubric rule to llm-rubric."""
        from gate_keeper.models import RuleSet

        rule = _semantic_rule()
        ruleset = RuleSet(rules=[rule])
        report = validate(ruleset, tmp_path, backend="auto")
        assert len(report.diagnostics) == 1
        diag = report.diagnostics[0]
        assert diag.status is Status.UNAVAILABLE
        assert diag.backend is Backend.LLM_RUBRIC
        assert compute_exit_code(report.diagnostics) == EXIT_FAIL
