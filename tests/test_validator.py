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


# ---------------------------------------------------------------------------
# Multi-target target argument (issue #146)
# ---------------------------------------------------------------------------


class TestTargetSpecArgument:
    """``validate`` accepts a ``TargetSpec`` and forwards it to the backend."""

    def test_targetspec_forwarded_to_filesystem_backend(self, tmp_path):
        from gate_keeper.targets import TargetSpec

        a = tmp_path / "a.txt"
        a.write_text("x\n")
        b = tmp_path / "b.txt"
        b.write_text("x\n")
        spec = TargetSpec(
            paths=sorted([a, b], key=str),
            raw_targets=[str(a), str(b)],
            is_multi=True,
        )
        rule = _rule(kind=RuleKind.FILE_EXISTS, backend_hint=Backend.FILESYSTEM)
        report = validate(_make_ruleset(rule), spec, backend="filesystem")
        diag = report.diagnostics[0]
        assert diag.status is Status.PASS
        assert any(e.kind == "multi_target_summary" for e in diag.evidence)

    def test_targetspec_forwarded_to_github_backend_unsupported(self, tmp_path):
        from gate_keeper.targets import TargetSpec

        spec = TargetSpec(
            paths=[tmp_path / "a.txt"],
            raw_targets=[str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
            is_multi=True,
        )
        rule = _rule(kind=RuleKind.GITHUB_PR_OPEN, backend_hint=Backend.GITHUB)
        report = validate(_make_ruleset(rule), spec, backend="github")
        diag = report.diagnostics[0]
        assert diag.status is Status.UNSUPPORTED
        assert any(e.kind == "multi_target_unsupported" for e in diag.evidence)

    def test_targetspec_llm_rubric_params_targets_still_unsupported(self, tmp_path):
        """#281: the literal params.targets mechanism keeps the multi_target_unsupported
        decline even after S5 dynamic assembly lands — the two axes never mix."""
        from gate_keeper.models import Confidence, Rule, SourceLocation
        from gate_keeper.targets import TargetSpec

        spec = TargetSpec(
            paths=[tmp_path / "a.txt"],
            raw_targets=[str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
            is_multi=True,
        )
        rule = Rule(
            id="r-params-targets",
            title="Test rule",
            source=SourceLocation(path="test.md", line=1),
            text="test rule text",
            kind=RuleKind.SEMANTIC_RUBRIC,
            severity=Severity.ERROR,
            backend_hint=Backend.LLM_RUBRIC,
            confidence=Confidence.HIGH,
            params={"targets": [{"id": "t1", "kind": "code_change", "path": "a.py"}]},
        )
        report = validate(_make_ruleset(rule), spec, backend="llm-rubric")
        diag = report.diagnostics[0]
        assert diag.status is Status.UNSUPPORTED
        assert any(e.kind == "multi_target_unsupported" for e in diag.evidence)

    def test_targetspec_llm_rubric_no_params_targets_assembles(self, tmp_path):
        """#281: a multi-file TargetSpec with no params.targets is no longer
        multi_target_unsupported — it enters the dynamic affected-context path."""
        from gate_keeper.targets import TargetSpec

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("alpha body\n", encoding="utf-8")
        b.write_text("beta body\n", encoding="utf-8")
        spec = TargetSpec(
            paths=sorted([a, b], key=str),
            raw_targets=[str(a), str(b)],
            is_multi=True,
        )
        rule = _rule(kind=RuleKind.SEMANTIC_RUBRIC, backend_hint=Backend.LLM_RUBRIC)
        report = validate(_make_ruleset(rule), spec, backend="llm-rubric")
        diag = report.diagnostics[0]
        # No provider configured under the hermetic conftest → UNAVAILABLE, but
        # crucially NOT the multi_target_unsupported decline, and the assembled
        # set is recorded for auditability.
        assert not any(e.kind == "multi_target_unsupported" for e in diag.evidence)
        assert any(e.kind == "affected_context" for e in diag.evidence)


# ---------------------------------------------------------------------------
# Deterministic target_kind mismatch precheck (#178)
# ---------------------------------------------------------------------------


def _semantic_rule(
    target_kind=None,
) -> Rule:
    """Build a SEMANTIC_RUBRIC rule with an optional ``target_kind``."""
    from gate_keeper.models import TargetKind

    if target_kind is None:
        target_kind = TargetKind.UNSPECIFIED
    return Rule(
        id="semantic-rule",
        title="Semantic rule",
        source=SourceLocation(path="rules.md", line=1),
        text="rule text",
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.ERROR,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.HIGH,
        params={},
        target_kind=target_kind,
    )


class _CallCountingCheck:
    """Test double for the registered ``llm-rubric`` check.

    Counts invocations and returns a configurable diagnostic. The deterministic
    precheck path must short-circuit *before* this stub is called, so an
    invocation count of ``0`` is the assertion that the LLM was never invoked.
    """

    def __init__(self, status: Status = Status.PASS) -> None:
        self.calls: list[tuple[Rule, object]] = []
        self.status = status

    def __call__(self, rule, target):
        self.calls.append((rule, target))
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=self.status,
            severity=rule.severity,
            message="stub: provider response",
            evidence=[],
        )


class TestArtifactKindPrecheck:
    """#178 — deterministic short-circuit before invoking llm-rubric.

    These tests prove the dispatch is provider-independent by stubbing the
    registry entry with a call counter and asserting the LLM is not invoked
    on mismatch.
    """

    def test_pr_description_rule_with_commit_message_artifact_short_circuits(self, tmp_path, monkeypatch):
        """rule.target_kind=pr_description + artifact_kind=commit_message →
        UNSUPPORTED with target_kind_mismatch and no LLM call."""
        from gate_keeper.models import TargetKind

        stub = _CallCountingCheck()
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", stub)
        rule = _semantic_rule(TargetKind.PR_DESCRIPTION)
        report = validate(
            _make_ruleset(rule),
            "any commit message text",
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        diag = report.diagnostics[0]
        assert stub.calls == []  # Provider was NOT invoked.
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.LLM_RUBRIC
        assert len(diag.evidence) == 1
        ev = diag.evidence[0]
        assert ev.kind == "target_kind_mismatch"
        assert ev.data["rule_target_kind"] == "pr_description"
        assert ev.data["artifact_kind"] == "commit_message"
        assert ev.data["dispatch"] == "deterministic_precheck"
        assert ev.data["llm_called"] is False

    def test_matching_kinds_proceed_to_llm(self, tmp_path, monkeypatch):
        """rule.target_kind=commit_message + artifact_kind=commit_message →
        backend is invoked normally."""
        from gate_keeper.models import TargetKind

        stub = _CallCountingCheck(status=Status.PASS)
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", stub)
        rule = _semantic_rule(TargetKind.COMMIT_MESSAGE)
        report = validate(
            _make_ruleset(rule),
            "subject\n\nbody",
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        diag = report.diagnostics[0]
        assert len(stub.calls) == 1  # Provider WAS invoked.
        assert diag.status is Status.PASS

    def test_unspecified_rule_ignores_artifact_kind(self, tmp_path, monkeypatch):
        """rule.target_kind=unspecified → artifact_kind has no effect."""
        from gate_keeper.models import TargetKind

        stub = _CallCountingCheck(status=Status.FAIL)
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", stub)
        rule = _semantic_rule(TargetKind.UNSPECIFIED)
        report = validate(
            _make_ruleset(rule),
            "any artifact",
            backend="auto",
            artifact_kind=TargetKind.PR_DESCRIPTION,
        )
        # Even though artifact_kind != rule.target_kind formally, an
        # UNSPECIFIED rule must preserve the prompt-level fallback path —
        # the backend is invoked.
        assert len(stub.calls) == 1
        assert report.diagnostics[0].status is Status.FAIL

    def test_omitted_artifact_kind_preserves_compat(self, tmp_path, monkeypatch):
        """artifact_kind=None → no precheck, backend is invoked normally."""
        from gate_keeper.models import TargetKind

        stub = _CallCountingCheck(status=Status.PASS)
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", stub)
        rule = _semantic_rule(TargetKind.PR_DESCRIPTION)
        # Note: a *mismatch* would only be visible to a provider; with
        # artifact_kind=None the validator forwards the rule unchanged so
        # the prompt-level v4 fallback is responsible for declining.
        report = validate(
            _make_ruleset(rule),
            "commit message text",
            backend="auto",
            artifact_kind=None,
        )
        assert len(stub.calls) == 1
        assert report.diagnostics[0].status is Status.PASS

    def test_precheck_provider_independent(self, tmp_path, monkeypatch):
        """Mismatch path produces the same diagnostic regardless of which
        provider would have been called — proven by registering a stub that
        raises on invocation."""
        from gate_keeper.models import TargetKind

        def _raises(rule, target):
            raise AssertionError("Provider must NOT be invoked on deterministic mismatch")

        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _raises)
        rule = _semantic_rule(TargetKind.PR_DESCRIPTION)
        report = validate(
            _make_ruleset(rule),
            "any commit message",
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        diag = report.diagnostics[0]
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "target_kind_mismatch"
        assert diag.evidence[0].data["llm_called"] is False

    def test_precheck_skipped_for_non_llm_rules(self, tmp_path, monkeypatch):
        """Non-LLM rules (filesystem, github, external) are unaffected by
        artifact_kind — they receive the target as before."""
        from gate_keeper.models import TargetKind

        # Fixture file so the filesystem backend has something to evaluate.
        fixture = tmp_path / "README.md"
        fixture.write_text("# README\n")
        rule = Rule(
            id="fs-rule",
            title="fs",
            source=SourceLocation(path="rules.md", line=1),
            text="README must exist",
            kind=RuleKind.FILE_EXISTS,
            severity=Severity.ERROR,
            backend_hint=Backend.FILESYSTEM,
            confidence=Confidence.HIGH,
            params={"path": "README.md"},
            # Annotations on a filesystem rule are non-binding for routing.
            target_kind=TargetKind.PR_DESCRIPTION,
        )
        report = validate(
            _make_ruleset(rule),
            tmp_path,
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        # Filesystem rule is unaffected: it routed to filesystem and PASSed.
        diag = report.diagnostics[0]
        assert diag.backend is Backend.FILESYSTEM
        assert diag.status is Status.PASS

    def test_precheck_diagnostic_round_trips(self, tmp_path, monkeypatch):
        """Synthesised diagnostic round-trips through Diagnostic.to_dict /
        from_dict so JSON renderers stay byte-faithful."""
        from gate_keeper.models import TargetKind

        stub = _CallCountingCheck()
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", stub)
        rule = _semantic_rule(TargetKind.PR_DESCRIPTION)
        report = validate(
            _make_ruleset(rule),
            "anything",
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        diag = report.diagnostics[0]
        rebuilt = Diagnostic.from_dict(diag.to_dict())
        assert rebuilt.status is Status.UNSUPPORTED
        assert rebuilt.evidence[0].data["dispatch"] == "deterministic_precheck"
        assert rebuilt.evidence[0].data["llm_called"] is False
