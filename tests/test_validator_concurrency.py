"""Unit tests for the bounded ThreadPoolExecutor concurrency path in
:func:`gate_keeper.validator.validate` (Slice 1 of issue #249).

All backend interactions use lightweight stubs registered via the registry so
no filesystem I/O or network access is required. Stubs deliberately complete
in non-rule-order, take observable wall-clock time, or raise — the validator
must still emit diagnostics in ``ruleset.rules`` order, must respect the
``max_workers`` cap, and must short-circuit the deterministic precheck path
without scheduling any provider call.
"""

from __future__ import annotations

import threading
import time

import pytest

from gate_keeper import backends as registry
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
    TargetKind,
)
from gate_keeper.validator import validate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_rule(
    rule_id: str,
    target_kind: TargetKind = TargetKind.UNSPECIFIED,
) -> Rule:
    """Build an llm-rubric rule suitable for the concurrency path."""
    return Rule(
        id=rule_id,
        title=f"Rule {rule_id}",
        source=SourceLocation(path="test.md", line=1),
        text=f"rule text {rule_id}",
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.ERROR,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.HIGH,
        params={},
        target_kind=target_kind,
    )


def _stub_pass(rule: Rule, target: object) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.PASS,
        severity=rule.severity,
        message="stub pass",
        evidence=[],
    )


# ---------------------------------------------------------------------------
# concurrency=1 preserves exact sequential behaviour
# ---------------------------------------------------------------------------


class TestConcurrencyOneIsSequential:
    """``concurrency=1`` (the default) must exercise the original sequential
    path and produce identical output to the legacy default callers."""

    def test_default_equals_explicit_one(self, tmp_path, monkeypatch):
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _stub_pass)
        rules = [_llm_rule(f"r{i}") for i in range(4)]
        ruleset = RuleSet(rules=rules)

        report_default = validate(ruleset, "target", backend="auto")
        report_one = validate(ruleset, "target", backend="auto", concurrency=1)

        assert [d.rule_id for d in report_default.diagnostics] == [d.rule_id for d in report_one.diagnostics]
        assert report_default.diagnostics[0].to_dict() == report_one.diagnostics[0].to_dict()

    def test_concurrency_zero_rejected(self, tmp_path):
        rules = [_llm_rule("r0")]
        with pytest.raises(ValueError, match="concurrency must be >= 1"):
            validate(RuleSet(rules=rules), "target", concurrency=0)

    def test_concurrency_negative_rejected(self, tmp_path):
        rules = [_llm_rule("r0")]
        with pytest.raises(ValueError, match="concurrency must be >= 1"):
            validate(RuleSet(rules=rules), "target", concurrency=-3)


# ---------------------------------------------------------------------------
# Ordering preserved when stubs complete out of order
# ---------------------------------------------------------------------------


class TestOrderingPreservedUnderConcurrency:
    """With concurrency>1 and stubs that complete in reverse rule order,
    the report must still be in ``ruleset.rules`` order."""

    def test_reverse_completion_order_yields_rule_order_diagnostics(self, tmp_path, monkeypatch):
        n = 6
        rules = [_llm_rule(f"r{i}") for i in range(n)]

        # Each rule sleeps for (n - rule_index) * unit so later rules finish
        # first. The sleep unit is small enough that the whole test stays
        # well under a second on CI but large enough that the executor
        # cannot serialise the calls and accidentally fix the ordering.
        unit = 0.02
        delays = {f"r{i}": (n - i) * unit for i in range(n)}
        completion_order: list[str] = []
        order_lock = threading.Lock()

        def _delayed(rule: Rule, target: object) -> Diagnostic:
            time.sleep(delays[rule.id])
            with order_lock:
                completion_order.append(rule.id)
            return _stub_pass(rule, target)

        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _delayed)
        report = validate(RuleSet(rules=rules), "target", backend="auto", concurrency=n)

        # Sanity: stubs really did complete out of rule order.
        assert completion_order != [f"r{i}" for i in range(n)]
        # Contract: diagnostics are in rule order regardless of completion.
        assert [d.rule_id for d in report.diagnostics] == [f"r{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# Bounded concurrency cap is respected
# ---------------------------------------------------------------------------


class TestConcurrencyCapRespected:
    """The number of in-flight stub calls must never exceed ``concurrency``."""

    @pytest.mark.parametrize("cap", [2, 3, 4])
    def test_in_flight_calls_never_exceed_cap(self, tmp_path, monkeypatch, cap):
        n_rules = 12
        rules = [_llm_rule(f"r{i}") for i in range(n_rules)]

        in_flight = 0
        max_observed = 0
        lock = threading.Lock()

        def _bounded(rule: Rule, target: object) -> Diagnostic:
            nonlocal in_flight, max_observed
            with lock:
                in_flight += 1
                if in_flight > max_observed:
                    max_observed = in_flight
            # Hold the slot long enough that other workers can pile up if
            # the cap is broken.
            time.sleep(0.05)
            with lock:
                in_flight -= 1
            return _stub_pass(rule, target)

        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _bounded)
        report = validate(RuleSet(rules=rules), "target", backend="auto", concurrency=cap)

        assert len(report.diagnostics) == n_rules
        assert max_observed <= cap, (
            f"concurrency cap broken: observed {max_observed} concurrent calls, cap={cap}"
        )
        # And it really did reach the cap (otherwise the test is vacuous).
        assert max_observed >= 2


# ---------------------------------------------------------------------------
# Backend exceptions still become ERROR diagnostics at the right position
# ---------------------------------------------------------------------------


class TestExceptionHandlingUnderConcurrency:
    """An exception raised by a backend in the concurrent path must surface
    as a :data:`Status.ERROR` diagnostic in the failing rule's slot —
    matching the sequential contract — without disturbing the other rules."""

    def test_single_failing_rule_becomes_error_at_correct_index(self, tmp_path, monkeypatch):
        rules = [_llm_rule(f"r{i}") for i in range(4)]
        failing_id = "r2"

        def _maybe_raise(rule: Rule, target: object) -> Diagnostic:
            if rule.id == failing_id:
                raise RuntimeError("simulated crash")
            return _stub_pass(rule, target)

        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _maybe_raise)
        report = validate(RuleSet(rules=rules), "target", backend="auto", concurrency=4)

        assert [d.rule_id for d in report.diagnostics] == [f"r{i}" for i in range(4)]
        statuses = {d.rule_id: d.status for d in report.diagnostics}
        assert statuses == {
            "r0": Status.PASS,
            "r1": Status.PASS,
            "r2": Status.ERROR,
            "r3": Status.PASS,
        }
        failing = next(d for d in report.diagnostics if d.rule_id == failing_id)
        assert "simulated crash" in failing.message
        assert any(e.kind == "exception" for e in failing.evidence)

    def test_all_failing_rules_become_errors(self, tmp_path, monkeypatch):
        rules = [_llm_rule(f"r{i}") for i in range(3)]

        def _always_raise(rule: Rule, target: object) -> Diagnostic:
            raise ValueError(f"boom-{rule.id}")

        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _always_raise)
        report = validate(RuleSet(rules=rules), "target", backend="auto", concurrency=3)

        assert [d.rule_id for d in report.diagnostics] == ["r0", "r1", "r2"]
        for diag in report.diagnostics:
            assert diag.status is Status.ERROR
            assert f"boom-{diag.rule_id}" in diag.message


# ---------------------------------------------------------------------------
# Deterministic prechecks are not scheduled
# ---------------------------------------------------------------------------


class _CountingStub:
    """Records every call so the test can assert the executor was never
    handed a precheck-rejected rule."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, rule: Rule, target: object) -> Diagnostic:
        with self.lock:
            self.calls.append(rule.id)
        return _stub_pass(rule, target)


class TestPrecheckShortCircuitUnderConcurrency:
    """Deterministic prechecks (#178 target-kind mismatch, registry miss)
    must run inline in the main loop and never reach the executor."""

    def test_target_kind_mismatch_not_submitted(self, tmp_path, monkeypatch):
        stub = _CountingStub()
        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", stub)

        # r0: matches artifact_kind → goes to executor.
        # r1: rule.target_kind=pr_description ≠ artifact_kind=commit_message
        #     → short-circuits to UNSUPPORTED before scheduling.
        # r2: matches artifact_kind → goes to executor.
        r0 = _llm_rule("r0", target_kind=TargetKind.COMMIT_MESSAGE)
        r1 = _llm_rule("r1", target_kind=TargetKind.PR_DESCRIPTION)
        r2 = _llm_rule("r2", target_kind=TargetKind.COMMIT_MESSAGE)

        report = validate(
            RuleSet(rules=[r0, r1, r2]),
            "any commit message",
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
            concurrency=4,
        )

        # The provider was called for r0 and r2 only.
        assert sorted(stub.calls) == ["r0", "r2"]

        # Diagnostic order still matches rule order; r1 is the deterministic
        # UNSUPPORTED short-circuit.
        assert [d.rule_id for d in report.diagnostics] == ["r0", "r1", "r2"]
        r1_diag = report.diagnostics[1]
        assert r1_diag.status is Status.UNSUPPORTED
        assert r1_diag.evidence[0].kind == "target_kind_mismatch"
        assert r1_diag.evidence[0].data["llm_called"] is False
        # And the executor-handled rules passed.
        assert report.diagnostics[0].status is Status.PASS
        assert report.diagnostics[2].status is Status.PASS

    def test_precheck_provider_raises_never_invoked(self, tmp_path, monkeypatch):
        """If the registered backend raises on any call, the test still
        succeeds for the short-circuited rule — proving no provider call was
        scheduled for it even under concurrency."""

        def _explode(rule: Rule, target: object) -> Diagnostic:
            raise AssertionError(
                f"Provider must NOT be invoked on deterministic mismatch; called for {rule.id}"
            )

        monkeypatch.setitem(registry._REGISTRY, "llm-rubric", _explode)
        rule = _llm_rule("only", target_kind=TargetKind.PR_DESCRIPTION)
        report = validate(
            RuleSet(rules=[rule]),
            "commit message body",
            backend="auto",
            artifact_kind=TargetKind.COMMIT_MESSAGE,
            concurrency=4,
        )
        diag = report.diagnostics[0]
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].data["llm_called"] is False
