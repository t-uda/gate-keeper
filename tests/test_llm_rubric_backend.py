"""Tests for the LLM-rubric backend.

Covers both the unconfigured fallback (fail-closed ``unavailable``) and the
configured-provider paths (``pass``, ``fail``, provider error, unparseable
response). Provider clients and the dotenv loader are monkeypatched so no
network call is ever made.
"""

from __future__ import annotations

from pathlib import Path

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


class TestDotenvLoader:
    def test_returns_empty_when_file_absent(self, tmp_path):
        missing = tmp_path / "no-such.env"
        assert llm_backend._load_env_file(missing) == {}

    def test_parses_existing_file(self, tmp_path):
        path = tmp_path / "gate-keeper.env"
        path.write_text(
            "GATE_KEEPER_LLM_PROVIDER=anthropic\nANTHROPIC_API_KEY=sk-ant-test\n",
            encoding="utf-8",
        )
        env = llm_backend._load_env_file(path)
        assert env["GATE_KEEPER_LLM_PROVIDER"] == "anthropic"
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_does_not_mutate_os_environ(self, tmp_path, monkeypatch):
        import os

        path = tmp_path / "gate-keeper.env"
        path.write_text("GATE_KEEPER_TEST_SENTINEL=tripped\n", encoding="utf-8")
        monkeypatch.delenv("GATE_KEEPER_TEST_SENTINEL", raising=False)
        llm_backend._load_env_file(path)
        assert "GATE_KEEPER_TEST_SENTINEL" not in os.environ


class TestIsConfigured:
    def test_false_when_provider_missing(self, monkeypatch):
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: {})
        assert llm_backend._is_configured() is False

    def test_false_when_provider_unsupported(self, monkeypatch):
        monkeypatch.setattr(
            llm_backend,
            "_load_env_file",
            lambda *a, **k: {"GATE_KEEPER_LLM_PROVIDER": "bedrock"},
        )
        assert llm_backend._is_configured() is False

    def test_false_when_anthropic_key_missing(self, monkeypatch):
        monkeypatch.setattr(
            llm_backend,
            "_load_env_file",
            lambda *a, **k: {"GATE_KEEPER_LLM_PROVIDER": "anthropic"},
        )
        assert llm_backend._is_configured() is False

    def test_true_when_anthropic_configured(self, monkeypatch):
        monkeypatch.setattr(
            llm_backend,
            "_load_env_file",
            lambda *a, **k: {
                "GATE_KEEPER_LLM_PROVIDER": "anthropic",
                "ANTHROPIC_API_KEY": "sk-ant-test",
            },
        )
        assert llm_backend._is_configured() is True

    def test_true_when_openai_configured(self, monkeypatch):
        monkeypatch.setattr(
            llm_backend,
            "_load_env_file",
            lambda *a, **k: {
                "GATE_KEEPER_LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-openai-test",
            },
        )
        assert llm_backend._is_configured() is True


def _patch_env(monkeypatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env)


class TestProviderDispatchAnthropic:
    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }

    def test_pass_response_maps_to_pass(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda key, system, user, model: (
                '{"judgment": "pass", "explanation": "Documentation reads clearly."}'
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.PASS
        assert diag.backend is Backend.LLM_RUBRIC
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["judgment"] == "pass"
        assert diag.evidence[0].data["model"] == llm_backend.ANTHROPIC_DEFAULT_MODEL
        assert diag.evidence[0].data["explanation"] == "Documentation reads clearly."
        assert diag.remediation is None
        assert compute_exit_code([diag]) == EXIT_OK

    def test_fail_response_maps_to_fail(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda key, system, user, model: (
                '{"judgment": "fail", "explanation": "Section headings are missing."}'
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.FAIL
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["judgment"] == "fail"
        assert diag.remediation is not None
        assert compute_exit_code([diag]) == EXIT_FAIL

    def test_provider_exception_maps_to_unavailable(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)

        def _boom(*_a, **_k):
            raise RuntimeError("HTTP 503 from upstream")

        monkeypatch.setattr(llm_backend, "_call_anthropic", _boom)
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert diag.evidence[0].data["provider"] == "anthropic"
        assert diag.evidence[0].data["failure_mode"] == "RuntimeError"
        assert "HTTP 503" in diag.evidence[0].data["detail"]
        assert compute_exit_code([diag]) == EXIT_FAIL

    def test_unparseable_response_maps_to_unavailable(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: "I refuse to answer in JSON.",
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert diag.evidence[0].data["failure_mode"] == "unparseable_response"

    def test_invalid_judgment_value_maps_to_unavailable(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: '{"judgment": "maybe", "explanation": "unsure"}',
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["failure_mode"] == "unparseable_response"

    def test_does_not_call_openai_branch(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        called: dict[str, bool] = {"openai": False, "anthropic": False}

        def _anthropic(*_a, **_k):
            called["anthropic"] = True
            return '{"judgment": "pass", "explanation": "ok"}'

        def _openai(*_a, **_k):
            called["openai"] = True
            return ""

        monkeypatch.setattr(llm_backend, "_call_anthropic", _anthropic)
        monkeypatch.setattr(llm_backend, "_call_openai", _openai)
        llm_backend.check(_semantic_rule(), tmp_path)
        assert called == {"anthropic": True, "openai": False}


class TestProviderDispatchOpenAI:
    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    def test_pass_response_maps_to_pass(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: '{"judgment": "pass", "explanation": "Reads clearly."}',
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.PASS
        assert diag.evidence[0].data["model"] == llm_backend.OPENAI_DEFAULT_MODEL
        assert diag.evidence[0].data["judgment"] == "pass"

    def test_fail_response_maps_to_fail(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: '{"judgment": "fail", "explanation": "Missing sections."}',
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.FAIL
        assert diag.evidence[0].data["model"] == llm_backend.OPENAI_DEFAULT_MODEL

    def test_provider_exception_maps_to_unavailable(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)

        def _boom(*_a, **_k):
            raise TimeoutError("upstream timed out")

        monkeypatch.setattr(llm_backend, "_call_openai", _boom)
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["provider"] == "openai"
        assert diag.evidence[0].data["failure_mode"] == "TimeoutError"


class TestParseResponseDirect:
    def test_pass_with_explanation(self):
        judgment, explanation = llm_backend._parse_response('{"judgment": "pass", "explanation": "ok"}')
        assert judgment == "pass"
        assert explanation == "ok"

    def test_invalid_json_raises(self):
        import pytest as _pytest

        with _pytest.raises(ValueError):
            llm_backend._parse_response("not json at all")

    def test_non_object_raises(self):
        import pytest as _pytest

        with _pytest.raises(ValueError):
            llm_backend._parse_response('["pass"]')

    def test_empty_explanation_raises(self):
        import pytest as _pytest

        with _pytest.raises(ValueError):
            llm_backend._parse_response('{"judgment": "pass", "explanation": ""}')


class TestPathConstants:
    def test_dotenv_path_matches_spec(self):
        """Issue #51 hard-codes the host-side dotenv path; do not regress it."""
        assert llm_backend.DOTENV_PATH == Path("/home/vscode/.config/hermes-projects/gate-keeper.env")
