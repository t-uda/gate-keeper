"""Foundation tests for ``Backend.EXTERNAL`` (issue #92).

These tests pin the dispatcher contract documented in
``docs/backend-external.md``: how the backend reacts when ``params.tool`` is
missing, points at an unknown adapter, points at a registered adapter, or when
the adapter raises. They do not exercise any concrete adapter — that lives in
follow-ups under umbrella #80.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gate_keeper import backends as _registry
from gate_keeper.backends import external
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    Evidence,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot and restore the adapter registry around every test."""
    snapshot = external.snapshot_adapters()
    external.clear_adapters()
    try:
        yield
    finally:
        external.restore_adapters(snapshot)


def _rule(kind: RuleKind = RuleKind.EXTERNAL_CHECK, **params) -> Rule:
    return Rule(
        id="test-rule",
        title="Test rule",
        source=SourceLocation(path="test.md", line=1),
        text="test rule text",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.EXTERNAL,
        confidence=Confidence.HIGH,
        params=dict(params),
    )


class _PassingAdapter:
    """Minimal adapter that returns a PASS diagnostic with adapter-named evidence."""

    name = "fake-tool"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.EXTERNAL,
            status=Status.PASS,
            severity=rule.severity,
            message=f"{self.name} ok",
            evidence=[Evidence(kind="fake_tool_run", data={"target": str(target)})],
        )


class _FailingAdapter:
    name = "fail-tool"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.EXTERNAL,
            status=Status.FAIL,
            severity=rule.severity,
            message=f"{self.name} reported a violation",
            evidence=[Evidence(kind="fake_tool_run", data={"target": str(target)})],
            remediation="fix the violation",
        )


class _RaisingAdapter:
    name = "boom-tool"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        raise RuntimeError("adapter exploded")


# ---------------------------------------------------------------------------
# Registry behaviour
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_registry_starts_empty(self):
        assert external.adapter_names() == []

    def test_register_adds_adapter(self):
        external.register(_PassingAdapter())
        assert external.adapter_names() == ["fake-tool"]

    def test_register_rejects_duplicate_name(self):
        external.register(_PassingAdapter())
        with pytest.raises(ValueError, match="already registered"):
            external.register(_PassingAdapter())

    def test_register_rejects_empty_name(self):
        class _Bad:
            name = ""

            def check(self, rule, target):
                raise AssertionError("should not be called")

        with pytest.raises(ValueError, match="non-empty string"):
            external.register(_Bad())  # type: ignore[arg-type]

    def test_unregister_is_idempotent(self):
        external.register(_PassingAdapter())
        external.unregister("fake-tool")
        external.unregister("fake-tool")  # second call is a no-op
        assert external.adapter_names() == []

    def test_clear_adapters_drops_everything(self):
        external.register(_PassingAdapter())
        external.register(_FailingAdapter())
        external.clear_adapters()
        assert external.adapter_names() == []


# ---------------------------------------------------------------------------
# Dispatcher behaviour (fail-closed contract)
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_unsupported_kind_is_unsupported(self):
        rule = _rule(kind=RuleKind.FILE_EXISTS, tool="fake-tool")
        diag = external.check(rule, "irrelevant")
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.EXTERNAL
        assert diag.evidence[0].kind == "backend_capability"
        assert diag.evidence[0].data == {"backend": "external", "kind": "file_exists"}

    def test_missing_tool_is_unavailable(self):
        diag = external.check(_rule(), "irrelevant")
        assert diag.status is Status.UNAVAILABLE
        assert diag.backend is Backend.EXTERNAL
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data == {"missing": "tool"}
        assert diag.remediation is not None  # contract: explain how to fix

    @pytest.mark.parametrize("bad", [None, "", 0, [], {}, 42])
    def test_non_string_or_empty_tool_is_unavailable(self, bad):
        rule = _rule(tool=bad)
        diag = external.check(rule, "irrelevant")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"

    def test_unknown_tool_is_unsupported_and_lists_registered(self):
        external.register(_PassingAdapter())
        rule = _rule(tool="not-registered")
        diag = external.check(rule, "irrelevant")
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.EXTERNAL
        assert diag.evidence[0].kind == "adapter_unknown"
        assert diag.evidence[0].data["tool"] == "not-registered"
        assert diag.evidence[0].data["registered"] == ["fake-tool"]

    def test_dispatch_to_registered_adapter_returns_adapter_diagnostic(self):
        external.register(_PassingAdapter())
        rule = _rule(tool="fake-tool")
        diag = external.check(rule, "some/target")
        assert diag.status is Status.PASS
        assert diag.backend is Backend.EXTERNAL
        assert diag.rule_id == rule.id
        assert diag.source == rule.source
        assert diag.severity is Severity.ERROR
        assert diag.evidence[0].kind == "fake_tool_run"
        assert diag.evidence[0].data == {"target": "some/target"}

    def test_failing_adapter_passthrough(self):
        external.register(_FailingAdapter())
        rule = _rule(tool="fail-tool")
        diag = external.check(rule, "some/target")
        assert diag.status is Status.FAIL
        assert diag.remediation == "fix the violation"

    def test_raising_adapter_is_unavailable_not_pass(self):
        external.register(_RaisingAdapter())
        rule = _rule(tool="boom-tool")
        diag = external.check(rule, "some/target")
        assert diag.status is Status.UNAVAILABLE
        assert diag.backend is Backend.EXTERNAL
        assert diag.evidence[0].kind == "adapter_error"
        assert diag.evidence[0].data["tool"] == "boom-tool"
        assert diag.evidence[0].data["type"] == "RuntimeError"
        assert "exploded" in diag.evidence[0].data["message"]

    def test_dispatcher_does_not_validate_adapter_specific_params(self):
        """Per-tool params are forwarded; the dispatcher only owns ``tool``."""
        captured: dict[str, object] = {}

        class _CapturingAdapter:
            name = "capture"

            def check(self, rule: Rule, target: str | Path) -> Diagnostic:
                captured["params"] = dict(rule.params)
                return Diagnostic(
                    rule_id=rule.id,
                    source=rule.source,
                    backend=Backend.EXTERNAL,
                    status=Status.PASS,
                    severity=rule.severity,
                    message="ok",
                    evidence=[Evidence(kind="capture", data={})],
                )

        external.register(_CapturingAdapter())
        rule = _rule(tool="capture", config_path="cfg.json", strict=True)
        external.check(rule, "x")
        assert captured["params"] == {"tool": "capture", "config_path": "cfg.json", "strict": True}


# ---------------------------------------------------------------------------
# Backend registry integration
# ---------------------------------------------------------------------------


class TestBackendRegistryIntegration:
    def test_external_is_registered_under_enum_value(self):
        assert _registry.is_registered(Backend.EXTERNAL.value)

    def test_registry_get_returns_dispatcher(self):
        fn = _registry.get(Backend.EXTERNAL.value)
        assert fn is external.check

    def test_backend_names_include_external(self):
        assert "external" in _registry.BACKEND_NAMES
