"""Tests for the LLM-rubric backend.

Covers both the unconfigured fallback (fail-closed ``unavailable``) and the
configured-provider paths (``pass``, ``fail``, provider error, unparseable
response). Provider clients and the dotenv loader are monkeypatched so no
network call is ever made.

Updated in #67 to assert on the new structured ``LlmJudgment`` evidence shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper.backends import llm_rubric as llm_backend
from gate_keeper.backends.llm_rubric import LlmJudgment, LlmJudgmentParseError
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PASS_JSON = json.dumps(
    {
        "judgment": "pass",
        "primary_reason": "Documentation reads clearly.",
        "supporting_evidence_quotes": [],
        "suggested_action": None,
    }
)

_VALID_FAIL_JSON = json.dumps(
    {
        "judgment": "fail",
        "primary_reason": "Section headings are missing.",
        "supporting_evidence_quotes": ["README.md has no '## Usage' heading"],
        "suggested_action": "Add a '## Usage' section with a code example.",
    }
)


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


# ---------------------------------------------------------------------------
# Unconfigured / stub path
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# dotenv loader
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _is_configured
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_env(monkeypatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env)


# ---------------------------------------------------------------------------
# Provider dispatch — Anthropic (#67: updated to structured schema)
# ---------------------------------------------------------------------------

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
            lambda key, system, user, model: _VALID_PASS_JSON,
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.PASS
        assert diag.backend is Backend.LLM_RUBRIC
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["judgment"] == "pass"
        assert diag.evidence[0].data["model"] == llm_backend.ANTHROPIC_DEFAULT_MODEL
        assert diag.evidence[0].data["prompt_version"] == llm_backend.PROMPT_VERSION
        assert diag.evidence[0].data["primary_reason"] == "Documentation reads clearly."
        assert isinstance(diag.evidence[0].data["supporting_evidence_quotes"], list)
        assert diag.evidence[0].data["suggested_action"] is None
        assert diag.remediation is None
        assert compute_exit_code([diag]) == EXIT_OK

    def test_fail_response_maps_to_fail(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda key, system, user, model: _VALID_FAIL_JSON,
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.FAIL
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["judgment"] == "fail"
        assert diag.evidence[0].data["primary_reason"] == "Section headings are missing."
        assert len(diag.evidence[0].data["supporting_evidence_quotes"]) >= 1
        assert diag.evidence[0].data["suggested_action"] == "Add a '## Usage' section with a code example."
        # Diagnostic.remediation should be suggested_action
        assert diag.remediation == "Add a '## Usage' section with a code example."
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
            lambda *_a, **_k: json.dumps(
                {
                    "judgment": "maybe",
                    "primary_reason": "unsure",
                    "supporting_evidence_quotes": [],
                    "suggested_action": None,
                }
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["failure_mode"] == "unparseable_response"

    def test_does_not_call_openai_branch(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        called: dict[str, bool] = {"openai": False, "anthropic": False}

        def _anthropic(*_a, **_k):
            called["anthropic"] = True
            return _VALID_PASS_JSON

        def _openai(*_a, **_k):
            called["openai"] = True
            return ""

        monkeypatch.setattr(llm_backend, "_call_anthropic", _anthropic)
        monkeypatch.setattr(llm_backend, "_call_openai", _openai)
        llm_backend.check(_semantic_rule(), tmp_path)
        assert called == {"anthropic": True, "openai": False}


# ---------------------------------------------------------------------------
# Provider dispatch — OpenAI (#67: updated to structured schema)
# ---------------------------------------------------------------------------

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
            lambda *_a, **_k: _VALID_PASS_JSON,
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.PASS
        assert diag.evidence[0].data["model"] == llm_backend.OPENAI_DEFAULT_MODEL
        assert diag.evidence[0].data["judgment"] == "pass"
        assert diag.evidence[0].data["prompt_version"] == llm_backend.PROMPT_VERSION

    def test_fail_response_maps_to_fail(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _VALID_FAIL_JSON,
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.FAIL
        assert diag.evidence[0].data["model"] == llm_backend.OPENAI_DEFAULT_MODEL
        assert diag.evidence[0].data["judgment"] == "fail"

    def test_provider_exception_maps_to_unavailable(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)

        def _boom(*_a, **_k):
            raise TimeoutError("upstream timed out")

        monkeypatch.setattr(llm_backend, "_call_openai", _boom)
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].data["provider"] == "openai"
        assert diag.evidence[0].data["failure_mode"] == "TimeoutError"


# ---------------------------------------------------------------------------
# _parse_llm_judgment — unit tests for new structured parser (#67)
# ---------------------------------------------------------------------------

class TestParseLlmJudgment:
    """New tests covering all acceptance-criteria cases from #67."""

    def test_valid_pass(self):
        result = llm_backend._parse_llm_judgment(_VALID_PASS_JSON)
        assert isinstance(result, LlmJudgment)
        assert result.judgment == "pass"
        assert result.primary_reason == "Documentation reads clearly."
        assert result.supporting_evidence_quotes == []
        assert result.suggested_action is None

    def test_valid_fail_with_quotes_and_action(self):
        result = llm_backend._parse_llm_judgment(_VALID_FAIL_JSON)
        assert isinstance(result, LlmJudgment)
        assert result.judgment == "fail"
        assert result.primary_reason == "Section headings are missing."
        assert len(result.supporting_evidence_quotes) >= 1
        assert result.suggested_action == "Add a '## Usage' section with a code example."

    def test_missing_required_field_judgment(self):
        payload = json.dumps(
            {
                "primary_reason": "ok",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"
        assert "judgment" in result.detail

    def test_missing_required_field_primary_reason(self):
        payload = json.dumps(
            {
                "judgment": "pass",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"

    def test_missing_required_field_supporting_evidence_quotes(self):
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"

    def test_extra_field_is_ignored(self):
        """Extra JSON fields must be silently ignored (graceful forward compat)."""
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Looks good.",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
                "unknown_future_field": "should be ignored",
                "another_extra": 42,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgment)
        assert result.judgment == "pass"

    def test_invalid_judgment_enum_value(self):
        payload = json.dumps(
            {
                "judgment": "maybe",
                "primary_reason": "unsure",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "invalid_judgment_value"

    def test_malformed_json(self):
        result = llm_backend._parse_llm_judgment("not json at all {{")
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "invalid_json"

    def test_empty_string(self):
        result = llm_backend._parse_llm_judgment("")
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "empty_response"

    def test_raw_response_excerpt_populated(self):
        """Parse errors must carry first ~200 chars of the raw response."""
        long_garbage = "x" * 300
        result = llm_backend._parse_llm_judgment(long_garbage)
        assert isinstance(result, LlmJudgmentParseError)
        assert len(result.raw_response_excerpt) <= 200

    def test_fail_without_quotes_returns_error(self):
        """fail judgment with empty quotes list is a validation error."""
        payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "Missing sections.",
                "supporting_evidence_quotes": [],
                "suggested_action": "Add them.",
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"

    def test_fail_without_suggested_action_returns_error(self):
        payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "Missing sections.",
                "supporting_evidence_quotes": ["some quote"],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"

    def test_pass_suggested_action_forced_to_none(self):
        """Even if model returns suggested_action on pass, it must be coerced to None."""
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Looks good.",
                "supporting_evidence_quotes": [],
                "suggested_action": "Some spurious action from model",
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgment)
        assert result.suggested_action is None

    def test_non_object_json_returns_error(self):
        result = llm_backend._parse_llm_judgment('["pass"]')
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "invalid_json"


# ---------------------------------------------------------------------------
# _parse_response backward-compat shim (legacy tests, kept for regression)
# ---------------------------------------------------------------------------

class TestParseResponseDirect:
    def test_pass_with_primary_reason(self):
        judgment, reason = llm_backend._parse_response(_VALID_PASS_JSON)
        assert judgment == "pass"
        assert reason == "Documentation reads clearly."

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            llm_backend._parse_response("not json at all")

    def test_non_object_raises(self):
        with pytest.raises(ValueError):
            llm_backend._parse_response('["pass"]')

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            llm_backend._parse_response("")


# ---------------------------------------------------------------------------
# PROMPT_VERSION constant
# ---------------------------------------------------------------------------

class TestPromptVersion:
    def test_prompt_version_constant_exists(self):
        assert hasattr(llm_backend, "PROMPT_VERSION")
        assert llm_backend.PROMPT_VERSION == "v1"

    def test_evidence_includes_prompt_version(self, monkeypatch, tmp_path):
        _patch_env(
            monkeypatch,
            {"GATE_KEEPER_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"},
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _VALID_PASS_JSON,
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.evidence[0].data["prompt_version"] == "v1"


# ---------------------------------------------------------------------------
# Path constants (#51 regression guard)
# ---------------------------------------------------------------------------

class TestPathConstants:
    def test_dotenv_path_matches_spec(self):
        """Issue #51 hard-codes the host-side dotenv path; do not regress it."""
        assert llm_backend.DOTENV_PATH == Path("/home/vscode/.config/hermes-projects/gate-keeper.env")
