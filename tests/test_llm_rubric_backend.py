"""Tests for the LLM-rubric backend.

Covers both the unconfigured fallback (fail-closed ``unavailable``) and the
configured-provider paths (``pass``, ``fail``, provider error, unparseable
response). Provider clients and the dotenv loader are monkeypatched so no
network call is ever made.

Updated in #67 to assert on the new structured ``LlmJudgment`` evidence shape.
"""

from __future__ import annotations

import dataclasses
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

# Artifact text the canned PASS/FAIL stub responses claim to quote. Tests
# that exercise the parser-side fabrication validator (#172) must pass this
# text **inline** as the ``target`` argument so the canned quotes are real
# substrings of the rendered prompt's ``Target reference`` block. The
# substring check runs against ``str(target)`` — the same string the model
# sees — not against any file off-band; passing a tmp ``Path`` whose file
# contains the quotes would defeat the check (Codex review on PR #173).
_STUB_ARTIFACT_TEXT = (
    "The README documents both install and validate commands. "
    "README.md has no '## Usage' heading and no example."
)

_VALID_PASS_JSON = json.dumps(
    {
        "judgment": "pass",
        "primary_reason": "Documentation reads clearly.",
        "supporting_evidence_quotes": ["The README documents both install and validate commands."],
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

# Default telemetry stub for monkeypatched provider helpers (#76).
_STUB_TELEMETRY: dict[str, int] = {
    "latency_ms": 42,
    "tokens_in": 123,
    "tokens_out": 45,
}


def _stub_response(text: str, telemetry: dict[str, int] | None = None) -> tuple[str, dict[str, int]]:
    """Build a ``(text, telemetry)`` tuple matching the real helper signature."""
    return text, dict(telemetry) if telemetry is not None else dict(_STUB_TELEMETRY)


def _target_with_artifact(tmp_path, text: str = _STUB_ARTIFACT_TEXT) -> str:
    """Return *text* — an inline artifact string suitable as a ``target``.

    Kept as a helper (rather than an inline ``_STUB_ARTIFACT_TEXT`` literal
    at every call site) so that future tweaks to the canned artifact text
    only need to change the default here. ``tmp_path`` is accepted only to
    keep the existing call-site signature; it is not used (the substring
    check runs against the string itself, not against a file).
    """
    del tmp_path
    return text


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
# _resolve_model — dotenv-driven model override (#157)
# ---------------------------------------------------------------------------


class TestResolveModel:
    """Issue #157 — provider-specific model override from the dotenv snapshot."""

    def test_anthropic_override_takes_precedence(self):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GATE_KEEPER_ANTHROPIC_MODEL": "claude-opus-4-7",
        }
        assert llm_backend._resolve_model("anthropic", env) == "claude-opus-4-7"

    def test_anthropic_unset_falls_back_to_default(self):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        assert llm_backend._resolve_model("anthropic", env) == llm_backend.ANTHROPIC_DEFAULT_MODEL

    def test_anthropic_blank_override_falls_back_to_default(self):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GATE_KEEPER_ANTHROPIC_MODEL": "   ",
        }
        assert llm_backend._resolve_model("anthropic", env) == llm_backend.ANTHROPIC_DEFAULT_MODEL

    def test_openai_override_takes_precedence(self):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "GATE_KEEPER_OPENAI_MODEL": "gpt-4o",
        }
        assert llm_backend._resolve_model("openai", env) == "gpt-4o"

    def test_openai_unset_falls_back_to_default(self):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
        }
        assert llm_backend._resolve_model("openai", env) == llm_backend.OPENAI_DEFAULT_MODEL

    def test_openai_blank_override_falls_back_to_default(self):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "GATE_KEEPER_OPENAI_MODEL": "",
        }
        assert llm_backend._resolve_model("openai", env) == llm_backend.OPENAI_DEFAULT_MODEL

    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError):
            llm_backend._resolve_model("bedrock", {})

    def test_cross_provider_override_does_not_apply(self):
        """``GATE_KEEPER_OPENAI_MODEL`` must not influence the anthropic resolution."""
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GATE_KEEPER_OPENAI_MODEL": "gpt-4o",
        }
        assert llm_backend._resolve_model("anthropic", env) == llm_backend.ANTHROPIC_DEFAULT_MODEL

    def test_check_uses_anthropic_override(self, monkeypatch, tmp_path):
        """check() must pass the override model to the provider helper and record it."""
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GATE_KEEPER_ANTHROPIC_MODEL": "claude-opus-4-7",
        }
        _patch_env(monkeypatch, env)
        seen: dict[str, str] = {}

        def _capture(_key, _system, _user, model):
            seen["model"] = model
            return _stub_response(_VALID_PASS_JSON)

        monkeypatch.setattr(llm_backend, "_call_anthropic", _capture)
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert seen["model"] == "claude-opus-4-7"
        assert diag.evidence[0].data["model"] == "claude-opus-4-7"

    def test_check_uses_openai_override(self, monkeypatch, tmp_path):
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "GATE_KEEPER_OPENAI_MODEL": "gpt-4o",
        }
        _patch_env(monkeypatch, env)
        seen: dict[str, str] = {}

        def _capture(_key, _system, _user, model):
            seen["model"] = model
            return _stub_response(_VALID_PASS_JSON)

        monkeypatch.setattr(llm_backend, "_call_openai", _capture)
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert seen["model"] == "gpt-4o"
        assert diag.evidence[0].data["model"] == "gpt-4o"

    def test_check_unknown_override_model_yields_null_cost(self, monkeypatch, tmp_path):
        """An override model not in ``_MODEL_PRICING`` yields ``cost_estimate_usd: None``."""
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-openai-test",
            "GATE_KEEPER_OPENAI_MODEL": "gpt-future-unknown",
        }
        _patch_env(monkeypatch, env)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 100, "tokens_in": 1000, "tokens_out": 200},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert data["model"] == "gpt-future-unknown"
        assert "cost_estimate_usd" in data
        assert data["cost_estimate_usd"] is None


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
            lambda key, system, user, model: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
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
            lambda key, system, user, model: _stub_response(_VALID_FAIL_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
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
            lambda *_a, **_k: _stub_response("I refuse to answer in JSON."),
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
            lambda *_a, **_k: _stub_response(
                json.dumps(
                    {
                        "judgment": "maybe",
                        "primary_reason": "unsure",
                        "supporting_evidence_quotes": [],
                        "suggested_action": None,
                    }
                )
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
            return _stub_response(_VALID_PASS_JSON)

        def _openai(*_a, **_k):
            called["openai"] = True
            return _stub_response("")

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
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        assert diag.evidence[0].data["model"] == llm_backend.OPENAI_DEFAULT_MODEL
        assert diag.evidence[0].data["judgment"] == "pass"
        assert diag.evidence[0].data["prompt_version"] == llm_backend.PROMPT_VERSION

    def test_fail_response_maps_to_fail(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(_VALID_FAIL_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
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
# Per-rule observability (#76): latency_ms, tokens_in, tokens_out
# ---------------------------------------------------------------------------


class TestEvidenceObservability:
    """Issue #76 — per-rule observability fields on llm_judgment evidence.

    The structured ``llm_judgment`` evidence dict must carry ``latency_ms``,
    ``tokens_in``, and ``tokens_out`` from the underlying provider call. These
    are the data substrate for future drift detection / cost analysis.
    Provider-error and unconfigured paths intentionally do NOT synthesize
    telemetry — fields are only meaningful for successful API calls.
    """

    _ANTHROPIC_ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }
    _OPENAI_ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    def test_evidence_includes_latency_ms_anthropic(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ANTHROPIC_ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 137, "tokens_in": 250, "tokens_out": 60},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "latency_ms" in data
        assert isinstance(data["latency_ms"], int)
        assert data["latency_ms"] >= 0
        assert data["latency_ms"] == 137

    def test_evidence_includes_tokens_in_and_out_anthropic(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ANTHROPIC_ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 50, "tokens_in": 250, "tokens_out": 60},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "tokens_in" in data
        assert "tokens_out" in data
        assert isinstance(data["tokens_in"], int)
        assert isinstance(data["tokens_out"], int)
        assert data["tokens_in"] >= 0
        assert data["tokens_out"] >= 0
        assert data["tokens_in"] == 250
        assert data["tokens_out"] == 60

    def test_evidence_includes_latency_ms_openai(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._OPENAI_ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 281, "tokens_in": 410, "tokens_out": 88},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "latency_ms" in data
        assert isinstance(data["latency_ms"], int)
        assert data["latency_ms"] >= 0
        assert data["latency_ms"] == 281

    def test_evidence_includes_tokens_in_and_out_openai(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._OPENAI_ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_FAIL_JSON,
                {"latency_ms": 12, "tokens_in": 410, "tokens_out": 88},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "tokens_in" in data
        assert "tokens_out" in data
        assert isinstance(data["tokens_in"], int)
        assert isinstance(data["tokens_out"], int)
        assert data["tokens_in"] == 410
        assert data["tokens_out"] == 88

    def test_provider_error_evidence_omits_telemetry(self, monkeypatch, tmp_path):
        """Exception paths must NOT synthesize fake telemetry (#76 requirement)."""
        _patch_env(monkeypatch, self._ANTHROPIC_ENV)

        def _boom(*_a, **_k):
            raise RuntimeError("network is down")

        monkeypatch.setattr(llm_backend, "_call_anthropic", _boom)
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        data = diag.evidence[0].data
        # Exception path → provider_error evidence carries no telemetry fields.
        assert "latency_ms" not in data
        assert "tokens_in" not in data
        assert "tokens_out" not in data

    def test_unconfigured_evidence_omits_telemetry(self, tmp_path):
        """Unconfigured path also omits telemetry (no API call happened)."""
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        data = diag.evidence[0].data
        assert "latency_ms" not in data
        assert "tokens_in" not in data
        assert "tokens_out" not in data

    def test_missing_usage_anthropic_maps_to_unavailable(self, monkeypatch, tmp_path):
        """Anthropic response without usage fields must fail closed (no synthetic 0).

        Regression guard for the Copilot review on PR #119: previously the
        helper coerced missing ``usage.input_tokens`` / ``usage.output_tokens``
        to ``0``, producing synthetic telemetry. Per CLAUDE.md the project
        rule is "Treat missing evidence as fail-closed; do not paper over with
        defaults". The helper now raises and ``check()`` dispatches a
        ``provider_error`` diagnostic.
        """
        _patch_env(monkeypatch, self._ANTHROPIC_ENV)

        class _UsageMissingInput:
            output_tokens = 60
            # input_tokens deliberately absent

        class _Block:
            text = _VALID_PASS_JSON

        class _Msg:
            content = [_Block()]
            usage = _UsageMissingInput()

        class _Client:
            def __init__(self, *_a, **_k):
                pass

            class messages:  # noqa: N801 — match SDK shape
                @staticmethod
                def create(*_a, **_k):
                    return _Msg()

        monkeypatch.setattr(llm_backend, "Anthropic", _Client, raising=False)
        # Patch via the import inside _call_anthropic by monkeypatching the
        # SDK module's attribute via sys.modules.
        import sys

        fake_mod = type(sys)("anthropic")
        fake_mod.Anthropic = _Client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert diag.evidence[0].data["provider"] == "anthropic"
        # No synthetic telemetry on the provider_error evidence.
        assert "tokens_in" not in diag.evidence[0].data
        assert "tokens_out" not in diag.evidence[0].data
        # Failure mode tag identifies it as a usage/telemetry shortfall.
        detail = diag.evidence[0].data["detail"]
        assert "usage" in detail.lower() or "input_tokens" in detail.lower()

    def test_missing_usage_openai_maps_to_unavailable(self, monkeypatch, tmp_path):
        """OpenAI response without usage fields must fail closed (no synthetic 0)."""
        _patch_env(monkeypatch, self._OPENAI_ENV)

        class _UsageMissingCompletion:
            prompt_tokens = 250
            # completion_tokens deliberately absent

        class _Choice:
            class message:  # noqa: N801
                content = _VALID_PASS_JSON

        class _Resp:
            choices = [_Choice()]
            usage = _UsageMissingCompletion()

        class _Client:
            def __init__(self, *_a, **_k):
                pass

            class chat:  # noqa: N801
                class completions:  # noqa: N801
                    @staticmethod
                    def create(*_a, **_k):
                        return _Resp()

        import sys

        fake_mod = type(sys)("openai")
        fake_mod.OpenAI = _Client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "openai", fake_mod)

        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert diag.evidence[0].data["provider"] == "openai"
        assert "tokens_in" not in diag.evidence[0].data
        assert "tokens_out" not in diag.evidence[0].data
        detail = diag.evidence[0].data["detail"]
        assert "usage" in detail.lower() or "completion_tokens" in detail.lower()

    def test_missing_usage_block_anthropic_maps_to_unavailable(self, monkeypatch, tmp_path):
        """Anthropic response with no usage object at all must fail closed."""
        _patch_env(monkeypatch, self._ANTHROPIC_ENV)

        class _Block:
            text = _VALID_PASS_JSON

        class _Msg:
            content = [_Block()]
            usage = None  # entire usage block absent

        class _Client:
            def __init__(self, *_a, **_k):
                pass

            class messages:  # noqa: N801
                @staticmethod
                def create(*_a, **_k):
                    return _Msg()

        import sys

        fake_mod = type(sys)("anthropic")
        fake_mod.Anthropic = _Client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

        diag = llm_backend.check(_semantic_rule(), tmp_path)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert diag.evidence[0].data["provider"] == "anthropic"

    def test_run_n_majority_run_carries_telemetry(self, monkeypatch, tmp_path):
        """Per-run telemetry is preserved on the representative run; not aggregated."""
        _patch_env(monkeypatch, self._ANTHROPIC_ENV)
        # Each run produces distinct telemetry values; we just want to confirm
        # the chosen run's telemetry is preserved on its evidence dict.
        telemetries = iter(
            [
                {"latency_ms": 100, "tokens_in": 10, "tokens_out": 1},
                {"latency_ms": 200, "tokens_in": 20, "tokens_out": 2},
                {"latency_ms": 300, "tokens_in": 30, "tokens_out": 3},
            ]
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON, next(telemetries)),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 3)
        # llm_judgment evidence (representative run) carries telemetry.
        judgment_data = diag.evidence[0].data
        assert isinstance(judgment_data["latency_ms"], int)
        assert isinstance(judgment_data["tokens_in"], int)
        assert isinstance(judgment_data["tokens_out"], int)
        # reproducibility_score evidence (#68) is unchanged — no telemetry there.
        repro_data = diag.evidence[-1].data
        assert "latency_ms" not in repro_data
        assert "tokens_in" not in repro_data
        assert "tokens_out" not in repro_data


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
        # #168: pass verdicts now also require non-empty supporting quotes.
        assert len(result.supporting_evidence_quotes) >= 1
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
                "supporting_evidence_quotes": ["a quoted phrase from the artifact"],
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
                "supporting_evidence_quotes": ["a quoted phrase from the artifact"],
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

    # ------------------------------------------------------------------
    # Code-fence extraction fallback (#194)
    # ------------------------------------------------------------------

    def test_json_in_json_code_fence_is_parsed(self):
        """gpt-4o-mini sometimes wraps its JSON in a ```json ... ``` fence (#194).

        The parser must strip the fence and succeed rather than returning
        ``invalid_json``.  This is a recorded failure mode from the
        completeness-05-rule-doc-has-target-cue bench run.
        """
        inner = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Each bullet names its target artefact.",
                "supporting_evidence_quotes": ["The pull request title is at most 70 characters."],
                "suggested_action": None,
            }
        )
        fenced = f"```json\n{inner}\n```"
        result = llm_backend._parse_llm_judgment(fenced)
        assert isinstance(result, LlmJudgment), f"Expected LlmJudgment, got {result!r}"
        assert result.judgment == "pass"
        assert result.primary_reason == "Each bullet names its target artefact."

    def test_json_in_plain_code_fence_is_parsed(self):
        """Plain ``` ... ``` (no language tag) should also be stripped (#194)."""
        inner = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "Rule body omits target artefact names.",
                "supporting_evidence_quotes": ["The CHANGELOG file has an Unreleased section."],
                "suggested_action": "Name the artefact each rule addresses.",
            }
        )
        fenced = f"```\n{inner}\n```"
        result = llm_backend._parse_llm_judgment(fenced)
        assert isinstance(result, LlmJudgment), f"Expected LlmJudgment, got {result!r}"
        assert result.judgment == "fail"

    def test_code_fence_extraction_failure_returns_invalid_json(self):
        """When the code-fence body is itself invalid JSON, the parser returns invalid_json (#194)."""
        fenced = "```json\nnot valid json at all\n```"
        result = llm_backend._parse_llm_judgment(fenced)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "invalid_json"
        # The excerpt must reference the original response, not the extracted candidate.
        assert result.raw_response_excerpt.startswith("```json")

    def test_prose_with_no_fence_returns_invalid_json(self):
        """When no code fence is present, a prose response still returns invalid_json (#194)."""
        result = llm_backend._parse_llm_judgment(
            "The rule is satisfied because each bullet names its artefact."
        )
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "invalid_json"


# ---------------------------------------------------------------------------
# Code-fence extraction helper (#194)
# ---------------------------------------------------------------------------


class TestExtractJsonCandidate:
    """Unit tests for :func:`_extract_json_candidate`."""

    def test_json_fence_extracted(self):
        inner = '{"a": 1}'
        assert llm_backend._extract_json_candidate(f"```json\n{inner}\n```") == inner

    def test_plain_fence_extracted(self):
        inner = '{"b": 2}'
        assert llm_backend._extract_json_candidate(f"```\n{inner}\n```") == inner

    def test_no_fence_returns_none(self):
        assert llm_backend._extract_json_candidate("just some prose") is None

    def test_fence_with_surrounding_prose(self):
        """Code fence may be preceded / followed by prose (model preamble)."""
        inner = '{"c": 3}'
        text = f"Here is my answer:\n```json\n{inner}\n```\nEnd of response."
        result = llm_backend._extract_json_candidate(text)
        assert result == inner

    def test_first_fence_wins_on_multiple(self):
        """When multiple fences appear the first match is returned."""
        first = '{"first": true}'
        second = '{"second": true}'
        text = f"```json\n{first}\n```\n\nsome text\n\n```json\n{second}\n```"
        result = llm_backend._extract_json_candidate(text)
        assert result == first


# ---------------------------------------------------------------------------
# LlmJudgment Pydantic model properties (#187)
# ---------------------------------------------------------------------------


class TestLlmJudgmentPydantic:
    """Verify Pydantic-specific behaviour of LlmJudgment (#187).

    These tests cover frozen=True immutability and model_dump() shape — the
    two guarantees that required migrating from @dataclass(frozen=True) to
    BaseModel.
    """

    def _valid_pass(self) -> LlmJudgment:
        return LlmJudgment(
            judgment="pass",
            primary_reason="Clear documentation.",
            supporting_evidence_quotes=["direct quote from artifact"],
            suggested_action=None,
        )

    def _valid_fail(self) -> LlmJudgment:
        return LlmJudgment(
            judgment="fail",
            primary_reason="Section headings are missing.",
            supporting_evidence_quotes=["direct quote from artifact"],
            suggested_action="Add a ## Usage section.",
        )

    def test_frozen_prevents_attribute_assignment(self):
        """frozen=True must raise pydantic.ValidationError on field mutation."""
        import pydantic
        import pytest

        j = self._valid_pass()
        with pytest.raises(pydantic.ValidationError):
            j.judgment = "fail"  # type: ignore[misc]

    def test_model_dump_contains_all_fields(self):
        """model_dump() must return a dict with all four field keys."""
        j = self._valid_fail()
        d = j.model_dump()
        assert set(d.keys()) == {
            "judgment",
            "primary_reason",
            "supporting_evidence_quotes",
            "suggested_action",
        }
        assert d["judgment"] == "fail"
        assert d["suggested_action"] == "Add a ## Usage section."

    def test_model_dump_pass_suggested_action_none(self):
        """model_dump() on a pass verdict has suggested_action=None."""
        j = self._valid_pass()
        d = j.model_dump()
        assert d["suggested_action"] is None

    def test_field_validator_rejects_empty_primary_reason(self):
        """primary_reason must be a non-empty string."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LlmJudgment(
                judgment="pass",
                primary_reason="   ",
                supporting_evidence_quotes=["some quote"],
                suggested_action=None,
            )

    def test_cross_field_validator_rejects_pass_with_empty_quotes(self):
        """pass verdict with empty supporting_evidence_quotes is invalid."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LlmJudgment(
                judgment="pass",
                primary_reason="Looks good.",
                supporting_evidence_quotes=[],
                suggested_action=None,
            )

    def test_cross_field_validator_rejects_pass_with_suggested_action(self):
        """pass verdict with a non-None suggested_action is invalid."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LlmJudgment(
                judgment="pass",
                primary_reason="Looks good.",
                supporting_evidence_quotes=["some quote"],
                suggested_action="This should not be here.",
            )

    def test_cross_field_validator_rejects_fail_without_suggested_action(self):
        """fail verdict with suggested_action=None is invalid."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LlmJudgment(
                judgment="fail",
                primary_reason="Missing sections.",
                supporting_evidence_quotes=["some quote"],
                suggested_action=None,
            )

    def test_cross_field_validator_rejects_fail_with_empty_suggested_action(self):
        """fail verdict with a whitespace-only suggested_action is invalid."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LlmJudgment(
                judgment="fail",
                primary_reason="Missing sections.",
                supporting_evidence_quotes=["some quote"],
                suggested_action="   ",
            )


# ---------------------------------------------------------------------------
# Quote-fabrication validator (#172)
# ---------------------------------------------------------------------------


class TestQuoteFabricationDetection:
    """#172 — parser-side enforcement of substring grounding for quotes.

    The v2 prompt (#168) instructs the model that every quote in
    ``supporting_evidence_quotes`` must be drawn as a near-verbatim substring
    of the artifact text. Tick 6 of umbrella #164's dogfood loop (issue
    #172) showed that prompt-side instruction alone is insufficient: the
    model can return generic placeholder strings that have zero overlap with
    the artifact. The parser-side validator below converts such judgments to
    ``UNSUPPORTED`` with ``llm_quote_fabrication`` evidence so the verdict
    is not actioned.
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    def test_fabricated_quote_yields_unsupported(self, monkeypatch):
        """Quotes that are not substrings of the artifact reject the verdict.

        The substring check is performed against the same string the prompt
        renders (``str(target)``), since that is what the model actually
        sees. This mirrors the tick 6 dogfood failure (#172) where
        ``--target "<PR body>"`` was passed inline and the model returned
        generic placeholder strings unrelated to the body.
        """
        _patch_env(monkeypatch, self._ENV)
        target_text = "feat(llm-rubric): tighten supporting_evidence_quotes constraints\n\nCloses #168.\n"
        fabricated_payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "PR body lacks rationale.",
                "supporting_evidence_quotes": ["This PR implements several changes to improve performance."],
                "suggested_action": "Add a rationale paragraph.",
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(fabricated_payload),
        )
        diag = llm_backend.check(_semantic_rule(), target_text)
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.LLM_RUBRIC
        assert len(diag.evidence) == 1
        assert diag.evidence[0].kind == "llm_quote_fabrication"
        # The evidence preserves the model's claimed verdict and the offending
        # quotes so reviewers can see what the model returned.
        data = diag.evidence[0].data
        assert data["claimed_judgment"] == "fail"
        assert data["primary_reason"] == "PR body lacks rationale."
        assert data["fabricated_quotes"] == ["This PR implements several changes to improve performance."]
        assert data["supporting_evidence_quotes"] == data["fabricated_quotes"]
        # Telemetry / cost are still recorded (the API call did happen).
        assert data["latency_ms"] == _STUB_TELEMETRY["latency_ms"]
        assert data["tokens_in"] == _STUB_TELEMETRY["tokens_in"]
        assert data["tokens_out"] == _STUB_TELEMETRY["tokens_out"]
        assert "cost_estimate_usd" in data
        # Remediation steers the operator away from acting on the verdict.
        assert diag.remediation is not None
        assert "Do not act" in diag.remediation

    def test_partially_fabricated_quotes_yield_unsupported(self, monkeypatch):
        """Even one fabricated quote is sufficient to reject the verdict."""
        _patch_env(monkeypatch, self._ENV)
        target_text = "Real phrase that the model legitimately quoted.\n\nMore body here.\n"
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Body explains motivation.",
                "supporting_evidence_quotes": [
                    "Real phrase that the model legitimately quoted.",
                    "Fabricated extra string never present in the artifact.",
                ],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), target_text)
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "llm_quote_fabrication"
        # Only the offending quote is recorded as fabricated; the legitimate
        # quote stays in supporting_evidence_quotes.
        assert diag.evidence[0].data["fabricated_quotes"] == [
            "Fabricated extra string never present in the artifact."
        ]

    def test_well_behaved_quote_still_passes(self, monkeypatch):
        """Quotes that ARE substrings of the artifact produce normal llm_judgment."""
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), _STUB_ARTIFACT_TEXT)
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["judgment"] == "pass"

    def test_whitespace_normalisation_tolerance(self, monkeypatch):
        """Cosmetic whitespace differences must not trigger fabrication."""
        _patch_env(monkeypatch, self._ENV)
        # Artifact has line wrapping; the model emits the same content as a
        # single line. Both should normalise to the same form.
        target_text = "The PR description names\nthe user-visible change in the\nfirst sentence."
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": [
                    "The PR description names the user-visible change in the first sentence."
                ],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), target_text)
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"

    def test_smart_quote_folding_tolerance(self, monkeypatch):
        """ASCII-quote normalisation in the model's quote must still match a
        smart-quote-bearing artifact."""
        _patch_env(monkeypatch, self._ENV)
        # Artifact has a curly apostrophe; the model returns ASCII.
        target_text = "It’s the body that matters, not the subject line."
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": ["It's the body that matters, not the subject line."],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), target_text)
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"

    def test_path_target_substring_check_uses_path_string(self, monkeypatch, tmp_path):
        """For path targets the substring check runs against the path string itself.

        Rationale: the prompt renders ``str(target)`` into the ``Target
        reference`` block, and the provider helpers do not give the model a
        file-read tool. The model only ever sees the path. Reading the file
        contents off-band would diverge from what the model has access to
        and would force every legitimate verdict on a path target into a
        false fabrication rejection (Codex review on PR #173).
        """
        _patch_env(monkeypatch, self._ENV)
        artifact = tmp_path / "pr_body.md"
        artifact.write_text("Body text the model never sees.", encoding="utf-8")
        # The model returns a quote that *would* be a substring of the file
        # contents but is NOT a substring of the path string. The verdict
        # must still be rejected because the model never had access to the
        # file contents.
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": ["Body text the model never sees."],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "llm_quote_fabrication"

    def test_path_target_quote_drawn_from_path_passes(self, monkeypatch, tmp_path):
        """When the model quotes from the path string itself (the only
        artifact text it has access to), the verdict is accepted."""
        _patch_env(monkeypatch, self._ENV)
        artifact = tmp_path / "pr_body.md"
        artifact.write_text("contents irrelevant", encoding="utf-8")
        # Quote uses the file's basename which IS a substring of the path
        # string the model was shown.
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "judging from filename",
                "supporting_evidence_quotes": ["pr_body.md"],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"

    def test_inline_string_target_validates_against_string_itself(self, monkeypatch):
        """The dogfood case from #172: ``--target "<PR body>"`` passes the
        literal body inline; quotes must be substrings of it."""
        _patch_env(monkeypatch, self._ENV)
        target_string = "feat(llm-rubric): tighten supporting_evidence_quotes constraints"
        # Quote that is NOT a substring of the inline target.
        fabricated_payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "Generic.",
                "supporting_evidence_quotes": ["This PR includes some refactoring and adds new tests."],
                "suggested_action": "Be specific.",
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(fabricated_payload),
        )
        diag = llm_backend.check(_semantic_rule(), target_string)
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "llm_quote_fabrication"

    def test_empty_quote_string_treated_as_fabricated(self, monkeypatch):
        """An empty / whitespace-only quote is a degenerate zero-grounding case."""
        _patch_env(monkeypatch, self._ENV)
        payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "x",
                "supporting_evidence_quotes": ["   "],
                "suggested_action": "fix it",
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), _STUB_ARTIFACT_TEXT)
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "llm_quote_fabrication"

    def test_find_fabricated_quotes_helper(self):
        """Direct unit test for the substring-validation helper."""
        artifact = "alpha beta gamma delta epsilon"
        # All present
        assert llm_backend._find_fabricated_quotes(["alpha", "gamma"], artifact) == []
        # One missing
        assert llm_backend._find_fabricated_quotes(["alpha", "zeta"], artifact) == ["zeta"]
        # All missing
        assert llm_backend._find_fabricated_quotes(["zeta", "kappa"], artifact) == [
            "zeta",
            "kappa",
        ]
        # Whitespace tolerance — quote contains line wrapping
        assert llm_backend._find_fabricated_quotes(["alpha\nbeta"], artifact) == []

    def test_normalise_for_substring_collapses_whitespace(self):
        norm = llm_backend._normalise_for_substring
        assert norm("a   b\n\tc") == "a b c"
        assert norm("  trim me  ") == "trim me"

    def test_normalise_for_substring_folds_smart_quotes(self):
        norm = llm_backend._normalise_for_substring
        assert norm("It’s") == norm("It's")
        assert norm("“hello”") == norm('"hello"')


# ---------------------------------------------------------------------------
# Span resolution for validated quotes (#179)
# ---------------------------------------------------------------------------


class TestQuoteSpanResolution:
    """#179 — span metadata for validated supporting quotes.

    Once :func:`_find_fabricated_quotes` confirms a quote is a normalised
    substring of the artifact, :func:`_resolve_quote_span_in` locates the
    first match inside the original artifact text and returns deterministic
    span metadata: character offsets and 1-indexed line/column ranges, plus
    a ``match_normalisation`` tag identifying which tolerance was needed.

    These unit tests exercise the resolver directly so a future refactor
    that changes how spans are wired into ``check()`` can leave the span
    semantics covered without depending on the provider-dispatch fixtures.
    """

    def test_exact_match_first_line(self):
        artifact = "alpha beta gamma\ndelta epsilon"
        span = llm_backend._resolve_quote_span_in(artifact, "alpha beta")
        assert span is not None
        assert span.match_normalisation == "exact"
        assert span.artifact_index == 0
        assert span.start_offset == 0
        assert span.end_offset == 10
        assert span.start_line == 1
        assert span.start_column == 1
        assert span.end_line == 1
        # 1-indexed half-open: column one past the last matched character.
        assert span.end_column == 11
        # Slicing the original artifact by these offsets yields the quote.
        assert artifact[span.start_offset : span.end_offset] == "alpha beta"

    def test_exact_match_second_line_spanning_no_newline(self):
        artifact = "first line\nsecond line\n"
        span = llm_backend._resolve_quote_span_in(artifact, "second line")
        assert span is not None
        assert span.match_normalisation == "exact"
        assert span.start_offset == artifact.index("second line")
        assert span.start_line == 2
        assert span.start_column == 1
        assert span.end_line == 2
        assert span.end_column == 12

    def test_exact_match_spanning_newline(self):
        # A multi-line quote: end_line is one greater than start_line.
        artifact = "line one\nline two\nline three"
        quote = "one\nline two"
        span = llm_backend._resolve_quote_span_in(artifact, quote)
        assert span is not None
        assert span.match_normalisation == "exact"
        assert span.start_line == 1
        assert span.end_line == 2
        # Slicing reproduces the multi-line quote.
        assert artifact[span.start_offset : span.end_offset] == quote

    def test_whitespace_normalised_match(self):
        # Artifact line-wraps the phrase; quote arrives as one line. The
        # resolver must locate the run in the original (line-wrapped) text
        # and tag the span as whitespace-normalised.
        artifact = "The PR description names\nthe user-visible change in the\nfirst sentence."
        quote = "The PR description names the user-visible change in the first sentence."
        span = llm_backend._resolve_quote_span_in(artifact, quote)
        assert span is not None
        assert span.match_normalisation == "whitespace"
        # The span points back into the **original** artifact: slicing it
        # yields the line-wrapped phrase, not the collapsed quote.
        assert artifact[span.start_offset : span.end_offset].replace("\n", " ") == quote
        assert span.start_line == 1
        assert span.start_column == 1
        assert span.end_line == 3
        # 1-indexed half-open column at end of "first sentence."
        assert span.end_column == len("first sentence.") + 1

    def test_smart_quote_normalised_match(self):
        # Artifact has curly apostrophe; quote arrives as ASCII.
        artifact = "It’s the body that matters, not the subject line."
        quote = "It's the body that matters, not the subject line."
        span = llm_backend._resolve_quote_span_in(artifact, quote)
        assert span is not None
        assert span.match_normalisation == "smart_quotes"
        # Slicing the original yields the curly-quote form (not the ASCII
        # form the model returned). This is intentional — the span points
        # into the original artifact text.
        assert artifact[span.start_offset : span.end_offset] == artifact
        assert span.start_line == 1

    def test_duplicate_quote_first_match_wins(self):
        artifact = "echo line.\nMiddle filler.\necho line."
        span = llm_backend._resolve_quote_span_in(artifact, "echo line.")
        assert span is not None
        assert span.match_normalisation == "exact"
        # First match — line 1, not line 3.
        assert span.start_offset == 0
        assert span.start_line == 1

    def test_no_match_returns_none(self):
        artifact = "Real artifact body here."
        # Substring not present even after normalisation — model fabrication.
        span = llm_backend._resolve_quote_span_in(artifact, "completely unrelated")
        assert span is None

    def test_empty_quote_returns_none(self):
        # Defence in depth: the fabrication validator already rejects empty
        # quotes, but the resolver must also be safe to call on them.
        assert llm_backend._resolve_quote_span_in("any text", "") is None

    def test_resolve_quote_spans_skips_unresolvable_silently(self):
        # If a quote slips past the validator but cannot be located, it is
        # silently dropped from the span list — supporting_evidence_quotes
        # remains the authoritative compatibility surface.
        artifact = "alpha beta"
        spans = llm_backend._resolve_quote_spans(["alpha", "missing"], artifact)
        assert [s.quote for s in spans] == ["alpha"]

    def test_resolve_quote_spans_preserves_quote_string_verbatim(self):
        # span.quote is the model's raw quote, not the in-artifact form,
        # so a consumer can correlate spans[i] with quotes[i] across
        # smart-quote folding.
        artifact = "It’s here."
        spans = llm_backend._resolve_quote_spans(["It's here."], artifact)
        assert len(spans) == 1
        assert spans[0].quote == "It's here."
        assert spans[0].match_normalisation == "smart_quotes"

    def test_to_dict_returns_documented_keys(self):
        span = llm_backend._resolve_quote_span_in("alpha beta", "alpha")
        assert span is not None
        d = span.to_dict()
        # Documented contract: every span dict carries these nine keys
        # (and no others). Consumers may rely on this shape.
        assert set(d.keys()) == {
            "quote",
            "artifact_index",
            "start_offset",
            "end_offset",
            "start_line",
            "start_column",
            "end_line",
            "end_column",
            "match_normalisation",
        }

    def test_line_col_helper_clamps_offset(self):
        # Defence in depth: callers may pass end_offset == len(text) for a
        # trailing match; the helper must not raise.
        line, col = llm_backend._line_col_from_offset("abc", 3)
        assert line == 1
        assert col == 4
        # Negative clamps to (1, 1).
        line, col = llm_backend._line_col_from_offset("abc", -1)
        assert line == 1
        assert col == 1


class TestQuoteSpansInEvidence:
    """#179 — supporting_evidence_spans surfaces alongside quotes in evidence.

    The success path of :func:`check` must attach a ``supporting_evidence_spans``
    list to every ``llm_judgment`` evidence entry. The pre-existing
    ``supporting_evidence_quotes`` field must remain untouched (additive
    extension, not replacement). Quote fabrication still produces
    ``llm_quote_fabrication`` and does not attach spans.
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    def test_pass_evidence_carries_spans(self, monkeypatch):
        _patch_env(monkeypatch, self._ENV)
        artifact = (
            "The README documents both install and validate commands. "
            "README.md has no '## Usage' heading and no example."
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"
        data = diag.evidence[0].data
        # Compatibility: existing field is preserved verbatim.
        assert data["supporting_evidence_quotes"] == [
            "The README documents both install and validate commands."
        ]
        # New field: one span per quote, with the documented schema.
        spans = data["supporting_evidence_spans"]
        assert isinstance(spans, list)
        assert len(spans) == 1
        span = spans[0]
        assert span["quote"] == "The README documents both install and validate commands."
        assert span["artifact_index"] == 0
        assert span["start_offset"] == 0
        assert span["end_offset"] == len(span["quote"])
        assert span["match_normalisation"] == "exact"
        assert span["start_line"] == 1
        assert span["start_column"] == 1

    def test_fail_evidence_carries_spans(self, monkeypatch):
        _patch_env(monkeypatch, self._ENV)
        artifact = (
            "The README documents both install and validate commands. "
            "README.md has no '## Usage' heading and no example."
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(_VALID_FAIL_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.FAIL
        spans = diag.evidence[0].data["supporting_evidence_spans"]
        assert len(spans) == 1
        # The fail-stub's quote is "README.md has no '## Usage' heading"
        assert spans[0]["quote"] == "README.md has no '## Usage' heading"
        assert spans[0]["match_normalisation"] == "exact"
        assert artifact[spans[0]["start_offset"] : spans[0]["end_offset"]] == spans[0]["quote"]

    def test_whitespace_normalised_quote_records_normalisation(self, monkeypatch):
        _patch_env(monkeypatch, self._ENV)
        # Artifact is line-wrapped; the model emits a single-line quote.
        artifact = "The PR description names\nthe user-visible change in the\nfirst sentence."
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": [
                    "The PR description names the user-visible change in the first sentence."
                ],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.PASS
        spans = diag.evidence[0].data["supporting_evidence_spans"]
        assert len(spans) == 1
        assert spans[0]["match_normalisation"] == "whitespace"
        assert spans[0]["start_line"] == 1
        assert spans[0]["end_line"] == 3

    def test_smart_quote_quote_records_normalisation(self, monkeypatch):
        _patch_env(monkeypatch, self._ENV)
        artifact = "It’s the body that matters, not the subject line."
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": ["It's the body that matters, not the subject line."],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.PASS
        spans = diag.evidence[0].data["supporting_evidence_spans"]
        assert len(spans) == 1
        assert spans[0]["match_normalisation"] == "smart_quotes"

    def test_duplicate_quote_evidence_first_match(self, monkeypatch):
        _patch_env(monkeypatch, self._ENV)
        artifact = "echo line.\nMiddle filler.\necho line."
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": ["echo line."],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.PASS
        spans = diag.evidence[0].data["supporting_evidence_spans"]
        # First-match policy: first occurrence at offset 0, line 1.
        assert spans[0]["start_offset"] == 0
        assert spans[0]["start_line"] == 1

    def test_fabricated_quote_yields_no_spans(self, monkeypatch):
        # Fabricated quotes route through llm_quote_fabrication, not
        # llm_judgment. The fabrication evidence MUST NOT carry spans —
        # there is no in-artifact location for a string the model invented.
        _patch_env(monkeypatch, self._ENV)
        artifact = "Real artifact body."
        fabricated_payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "x",
                "supporting_evidence_quotes": ["Wholly fabricated phrase not in body."],
                "suggested_action": "fix it",
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(fabricated_payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "llm_quote_fabrication"
        # Fabrication evidence keeps the existing fields and does not
        # introduce a spans field — the quote is by definition unlocatable.
        assert "supporting_evidence_spans" not in diag.evidence[0].data

    def test_multiple_quotes_each_get_a_span(self, monkeypatch):
        _patch_env(monkeypatch, self._ENV)
        artifact = "alpha beta gamma\ndelta epsilon zeta\neta theta iota"
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "ok",
                "supporting_evidence_quotes": [
                    "alpha beta",
                    "delta epsilon",
                    "iota",
                ],
                "suggested_action": None,
            }
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(payload),
        )
        diag = llm_backend.check(_semantic_rule(), artifact)
        assert diag.status is Status.PASS
        spans = diag.evidence[0].data["supporting_evidence_spans"]
        assert [s["quote"] for s in spans] == [
            "alpha beta",
            "delta epsilon",
            "iota",
        ]
        # Each span resolves to the documented line in the artifact.
        assert [s["start_line"] for s in spans] == [1, 2, 3]


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
        # #168 bumped v1 → v2 to mark the supporting_evidence_quotes
        # constraint tightening.
        # #169 bumped v2 → v3 to add the optional artifact-kind block and
        # authorise an ``unsupported`` verdict for the target-kind-mismatch
        # case.
        # #175 bumped v3 → v4 to ground rule.target_kind more strongly in
        # the prompt (kind name echoed in the artifact-kind block, canned
        # ``unsupported`` example replaced with a kind-neutral schema
        # illustration so gpt-4o-mini stops parroting "PR descriptions"
        # regardless of the rule's actual annotation).
        assert llm_backend.PROMPT_VERSION == "v4"

    def test_evidence_includes_prompt_version(self, monkeypatch, tmp_path):
        _patch_env(
            monkeypatch,
            {"GATE_KEEPER_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"},
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
        assert diag.evidence[0].data["prompt_version"] == "v4"


# ---------------------------------------------------------------------------
# Path constants (#51 regression guard)
# ---------------------------------------------------------------------------


class TestPathConstants:
    def test_dotenv_path_matches_spec(self):
        """Issue #51 hard-codes the host-side dotenv path; do not regress it."""
        assert llm_backend.DOTENV_PATH == Path("/home/vscode/.config/hermes-projects/gate-keeper.env")


# ---------------------------------------------------------------------------
# Prompt-template constraint guard (#168)
# ---------------------------------------------------------------------------


class TestPromptTemplateEvidenceConstraints:
    """#168 — guard that the rendered prompt includes the v2 evidence-quote constraints.

    These assertions are intentionally string-presence checks, not behavioural
    tests. They protect against silent prompt drift: a future edit that
    deletes the "near-verbatim substring" or "every verdict — both pass and
    fail" language will trip these tests independently of any model-side
    regression. They sit alongside ``TestPromptVersion`` so a prompt change
    forces a deliberate update of both the version constant and these
    constraint assertions.
    """

    def _rendered(self) -> str:
        rule = _semantic_rule()
        # _build_prompt only string-formats the target reference into the
        # rendered prompt; it does not read the file. We point at an
        # existing fixture so this assertion stays grep-friendly even
        # though the prompt-text checks below are file-content-independent.
        _system, user = llm_backend._build_prompt(
            rule, "tests/fixtures/semantic/targets/changelog_no_rationale.md"
        )
        return user

    def test_prompt_requires_quotes_on_every_verdict(self):
        """The prompt must instruct that quotes are required on both pass and fail."""
        rendered = self._rendered()
        # The v3 prompt names "every pass or fail verdict" — the v2 prompt
        # said "every verdict — both pass and fail" without distinguishing
        # ``unsupported`` (where quotes may legitimately be absent).
        assert "every pass or fail verdict" in rendered
        assert '"pass"' in rendered
        assert '"fail"' in rendered

    def test_prompt_requires_substring_grounding(self):
        """The prompt must instruct that quotes be drawn from the artifact text."""
        rendered = self._rendered()
        # "near-verbatim substring" is the v2 wording that distinguishes
        # quotes-from-the-artifact from paraphrases of primary_reason.
        assert "near-verbatim substring" in rendered

    def test_prompt_forbids_paraphrasing_primary_reason(self):
        """The prompt must explicitly forbid paraphrasing primary_reason as a quote."""
        rendered = self._rendered()
        assert "paraphrase" in rendered
        assert "primary_reason" in rendered

    def test_prompt_warns_against_first_line_only(self):
        """The prompt must steer the model away from anchoring on opening lines only."""
        rendered = self._rendered()
        # Either "opening line" or "do not only cite" — both are v2 cues
        # against the L25 first-line-quote bias.
        assert "opening line" in rendered or "do not only cite" in rendered

    def test_prompt_requires_representative_evidence(self):
        """The prompt must require at least one quote that reflects the strongest evidence."""
        rendered = self._rendered()
        assert "representative" in rendered or "strongest evidence" in rendered

    def test_judgment_dataclass_docstring_documents_v2_constraints(self):
        """The LlmJudgment docstring must mention the non-empty-on-every-verdict rule.

        This catches the case where someone tightens the prompt but forgets to
        update the dataclass docstring, leaving the IR contract documentation
        out of sync with the prompt-side constraint.
        """
        doc = LlmJudgment.__doc__ or ""
        # Reference to issue #168 keeps the link from constraint to history.
        assert "#168" in doc
        # Both verdicts must be named in the docstring so a reader cannot
        # infer the v1 "may be empty on pass" rule from the docstring alone.
        assert '"pass"' in doc and '"fail"' in doc


# ---------------------------------------------------------------------------
# Target-kind annotation (#169)
# ---------------------------------------------------------------------------


def _semantic_rule_with_target_kind(target_kind):
    """Build a Rule with the given ``target_kind`` annotation."""
    return Rule(
        id="stub-llm-rule-tk",
        title="Stub semantic rule (target_kind)",
        source=SourceLocation(path="rules.md", line=5),
        text="The PR description should name the user-visible change in the first sentence.",
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.WARNING,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params={},
        target_kind=target_kind,
    )


class TestTargetKindPromptInjection:
    """#169 / #175 — the prompt template includes / omits the artifact-kind block."""

    def test_unspecified_target_kind_omits_artifact_kind_block(self):
        """A rule without target_kind must omit the artifact-kind block."""
        from gate_keeper.models import TargetKind

        rule = _semantic_rule_with_target_kind(TargetKind.UNSPECIFIED)
        _system, user = llm_backend._build_prompt(rule, "an inline target string")
        # The artifact-kind block heading appears as a level-2 Markdown
        # heading at the start of a line ("\n## Artifact kind\n"). The
        # constraints text now references the block in prose form
        # (``... when an `## Artifact kind` block is present ...``), so a
        # bare-substring check would false-positive on the constraint
        # reference. Match the heading at line start instead.
        assert "\n## Artifact kind\n" not in user
        assert "This rule is annotated `target_kind:" not in user

    def test_pr_description_target_kind_renders_artifact_kind_block(self):
        from gate_keeper.models import TargetKind

        rule = _semantic_rule_with_target_kind(TargetKind.PR_DESCRIPTION)
        _system, user = llm_backend._build_prompt(rule, "an inline target string")
        assert "## Artifact kind" in user
        assert "`pr_description`" in user
        # #175 — the block must explicitly name the rule's annotated kind
        # rather than relying on a generic "the rule's premise" phrase.
        assert "This rule is annotated `target_kind: pr_description`" in user

    def test_commit_message_target_kind_renders_artifact_kind_block(self):
        from gate_keeper.models import TargetKind

        rule = _semantic_rule_with_target_kind(TargetKind.COMMIT_MESSAGE)
        _system, user = llm_backend._build_prompt(rule, "an inline target string")
        assert "## Artifact kind" in user
        assert "`commit_message`" in user
        # #175 — the block must explicitly name the rule's annotated kind.
        assert "This rule is annotated `target_kind: commit_message`" in user

    def test_schema_block_documents_unsupported_verdict(self):
        """The response-schema block must always advertise ``unsupported`` (#169)."""
        from gate_keeper.models import TargetKind

        rule = _semantic_rule_with_target_kind(TargetKind.UNSPECIFIED)
        _system, user = llm_backend._build_prompt(rule, "an inline target string")
        assert '"unsupported"' in user


class TestTargetKindGroundingV4:
    """#175 — v4 prompt must ground the rule's ``target_kind`` value.

    These assertions guard the regression that motivated the v3 → v4 bump:
    at v3, every ``target_kind_mismatch`` verdict from gpt-4o-mini reported
    "The rule addresses PR descriptions" verbatim — even when the rule was
    annotated ``commit_message`` — because (a) the canned ``unsupported``
    example response hardcoded "PR descriptions ... commit message" as the
    primary_reason and (b) the artifact-kind block illustrated the mismatch
    case using the same hardcoded "PR description / commit message" pair.

    The v4 prompt instead names the rule's annotated kind in the
    artifact-kind block and uses kind-neutral placeholders in the example
    so the model has nothing to parrot.
    """

    def _render(self, target_kind):
        rule = _semantic_rule_with_target_kind(target_kind)
        _system, user = llm_backend._build_prompt(rule, "an inline target string")
        return user

    def test_artifact_kind_block_quotes_rules_target_kind_value(self):
        """Each known target_kind value must appear literally in its block."""
        from gate_keeper.models import TargetKind

        for tk in (
            TargetKind.PR_DESCRIPTION,
            TargetKind.COMMIT_MESSAGE,
            TargetKind.ISSUE_BODY,
            TargetKind.DOCUMENTATION,
            TargetKind.CODE_CHANGE,
        ):
            rendered = self._render(tk)
            # The rule's annotated kind must appear in the artifact-kind
            # block as a backticked literal so the model sees the exact
            # string it must echo back.
            assert f"`{tk.value}`" in rendered, f"missing backticked `{tk.value}` for {tk!r}"
            # And again as a quoted-target-kind anchor so the instruction
            # forms a closed loop ("annotated X — premise applies to X").
            assert f"target_kind: {tk.value}" in rendered, (
                f"missing 'target_kind: {tk.value}' anchor for {tk!r}"
            )

    def test_unsupported_example_uses_neutral_placeholders(self):
        """The canned ``unsupported`` example must NOT hardcode 'PR descriptions'.

        Regression guard for the v3 bug (#175). The example response in
        v3 said: ``"primary_reason": "The rule addresses PR descriptions
        but the artifact provided is a commit message."`` — gpt-4o-mini
        copied this verbatim regardless of the rule's actual target_kind.
        v4 replaces the example with kind-neutral placeholders.
        """
        from gate_keeper.models import TargetKind

        # Render every target_kind variant — none of them may contain the
        # canned v3 example string.
        for tk in (
            TargetKind.UNSPECIFIED,
            TargetKind.PR_DESCRIPTION,
            TargetKind.COMMIT_MESSAGE,
            TargetKind.ISSUE_BODY,
            TargetKind.DOCUMENTATION,
            TargetKind.CODE_CHANGE,
        ):
            rendered = self._render(tk)
            assert "The rule addresses PR descriptions" not in rendered, (
                f"v3 hardcoded example primary_reason still appears for {tk!r}; "
                "this is exactly the parroted phrase #175 reports"
            )
            assert "but the artifact provided is a commit message" not in rendered, (
                f"v3 hardcoded example continuation still appears for {tk!r}"
            )

    def test_unsupported_example_uses_kind_neutral_template_tokens(self):
        """The kind-neutral example placeholders must be present for annotated rules (#175)."""
        from gate_keeper.models import TargetKind

        # Pick any annotated target_kind — the example block is the same
        # across all of them by design (the kind-specific guidance is in
        # the artifact-kind block, not the example).
        rendered = self._render(TargetKind.PR_DESCRIPTION)
        # Both placeholders must appear so the model sees the substitution
        # contract instead of a copy-pasteable canned phrase.
        assert "<RULE_KIND>" in rendered
        assert "<ARTIFACT_KIND>" in rendered

    def test_unsupported_example_block_omitted_for_unspecified_target_kind(self):
        """Unannotated rules must NOT see the unsupported-example block (#175 follow-up).

        The Copilot review on PR #176 / a re-bench surfaced a v4 regression
        on ``completeness-05-rule-doc-has-target-cue`` (an unannotated
        rule): the model saw the canned ``unsupported`` example, picked
        up the schema option, and emitted a stray ``"unsupported"``
        verdict on a rule that carried no ``target_kind`` annotation. The
        backend then degraded that to ``provider_error /
        unsupported_without_target_kind`` (correct fail-closed behaviour
        but a wasted provider call). v4 fixes this by gating the example
        block on annotation: an unannotated rule never sees the lure.
        """
        from gate_keeper.models import TargetKind

        rendered = self._render(TargetKind.UNSPECIFIED)
        assert "<RULE_KIND>" not in rendered
        assert "<ARTIFACT_KIND>" not in rendered
        # The "unsupported" example heading must also be absent so the
        # only schema document the model sees is pass/fail.
        assert "An unsupported verdict —" not in rendered

    def test_unsupported_constraint_clarifies_when_unsupported_is_valid(self):
        """The constraints block must reserve ``unsupported`` to the artifact-kind case (#175 follow-up).

        Even with the example block gated, the schema's ``"judgment"``
        line still names ``"unsupported"`` (callers / parsers must accept
        all three values), so the constraints text must spell out that
        ``"unsupported"`` is valid only when an `## Artifact kind` block
        is rendered above. Otherwise the model on an unannotated rule
        could still invent an ``unsupported`` verdict from the schema
        alone.
        """
        from gate_keeper.models import TargetKind

        rendered = self._render(TargetKind.UNSPECIFIED)
        # The schema must still list "unsupported" — the parser accepts it.
        assert '"unsupported"' in rendered
        # But the constraint must say it is reserved for the
        # target-kind-mismatch case and gate it on the artifact-kind
        # block being rendered.
        assert "reserved for the target-kind-mismatch case" in rendered
        assert "ONLY valid when an `## Artifact kind` block is present" in rendered

    def test_instruction_step_5_names_target_kind_grounding_contract(self):
        """Instructions step 5 must require quoting the rule's target_kind verbatim (#175)."""
        from gate_keeper.models import TargetKind

        rendered = self._render(TargetKind.COMMIT_MESSAGE)
        # The step must reference the annotated target_kind concept and
        # the verbatim-quoting requirement (the two pieces gpt-4o-mini
        # was missing at v3).
        assert "annotated `target_kind`" in rendered
        assert "verbatim" in rendered

    def test_artifact_kind_block_requires_echoing_rule_kind_in_primary_reason(self):
        """The artifact-kind block must instruct the model to echo target_kind on decline (#175)."""
        from gate_keeper.models import TargetKind

        rendered = self._render(TargetKind.COMMIT_MESSAGE)
        # The instruction "MUST quote the rule's annotated kind" is the
        # core behavioural fix for the v3 regression. With the rendered
        # rule_kind_value = ``commit_message``, the example phrase must
        # quote that value verbatim, not "PR descriptions".
        assert "MUST quote the rule's annotated kind" in rendered
        assert "`commit_message`" in rendered
        # And — the smoking-gun regression check from the issue: the
        # rendered prompt for a ``commit_message`` rule must NOT say
        # "rule addresses PR descriptions" anywhere.
        assert "rule addresses PR descriptions" not in rendered


class TestTargetKindParseAcceptsUnsupported:
    """#169 — the parser accepts ``unsupported`` and waives quote-grounding."""

    def test_unsupported_with_empty_quotes_is_valid(self):
        payload = json.dumps(
            {
                "judgment": "unsupported",
                "primary_reason": "The rule addresses PR descriptions but the artifact is a commit message.",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgment)
        assert result.judgment == "unsupported"
        assert result.supporting_evidence_quotes == []
        assert result.suggested_action is None

    def test_pass_with_empty_quotes_still_rejected(self):
        """Empty quote list remains invalid for pass / fail (regression guard for #168)."""
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Looks ok.",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"

    def test_fail_with_empty_quotes_still_rejected(self):
        payload = json.dumps(
            {
                "judgment": "fail",
                "primary_reason": "Missing rationale.",
                "supporting_evidence_quotes": [],
                "suggested_action": "Add a rationale paragraph.",
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert result.failure_mode == "missing_field"

    def test_invalid_judgment_value_message_lists_unsupported(self):
        payload = json.dumps(
            {
                "judgment": "maybe",
                "primary_reason": "x",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
            }
        )
        result = llm_backend._parse_llm_judgment(payload)
        assert isinstance(result, LlmJudgmentParseError)
        assert "unsupported" in result.detail


class TestUnsupportedDispatch:
    """#169 — an ``unsupported`` verdict maps to ``Status.UNSUPPORTED``.

    The diagnostic carries ``evidence.kind=target_kind_mismatch`` with the
    rule's annotated kind. The substring-grounding check from #172 is
    bypassed because an empty quote list is legitimate for this verdict.
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }

    _UNSUPPORTED_JSON = json.dumps(
        {
            "judgment": "unsupported",
            "primary_reason": "The rule addresses PR descriptions but the artifact is a commit message.",
            "supporting_evidence_quotes": [],
            "suggested_action": None,
        }
    )

    def test_unsupported_maps_to_status_unsupported(self, monkeypatch):
        from gate_keeper.models import TargetKind

        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(self._UNSUPPORTED_JSON),
        )
        rule = _semantic_rule_with_target_kind(TargetKind.PR_DESCRIPTION)
        diag = llm_backend.check(rule, "fix(parser): handle CRLF in evidence blocks\n\nrelated to #foo")
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.LLM_RUBRIC
        assert diag.evidence[0].kind == "target_kind_mismatch"
        assert diag.evidence[0].data["judgment"] == "unsupported"
        assert diag.evidence[0].data["rule_target_kind"] == "pr_description"
        assert diag.evidence[0].data["prompt_version"] == "v4"

    def test_unsupported_remediation_explains_mismatch(self, monkeypatch):
        from gate_keeper.models import TargetKind

        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(self._UNSUPPORTED_JSON),
        )
        rule = _semantic_rule_with_target_kind(TargetKind.PR_DESCRIPTION)
        diag = llm_backend.check(rule, "a commit message")
        assert diag.remediation is not None
        assert "pr_description" in diag.remediation

    def test_unsupported_without_target_kind_degrades_to_unavailable(self, monkeypatch):
        """#169 + Codex review — a stray ``"unsupported"`` from a flaky model on a
        rule with ``target_kind=unspecified`` must NOT silently invent a
        target-kind-mismatch. The artifact-kind block was never injected
        into the prompt so the model has no basis for that verdict; the
        backend treats it as a contract violation and returns
        ``Status.UNAVAILABLE`` with ``provider_error`` /
        ``unsupported_without_target_kind``.
        """
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(self._UNSUPPORTED_JSON),
        )
        diag = llm_backend.check(_semantic_rule(), "any artifact")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert diag.evidence[0].data["failure_mode"] == "unsupported_without_target_kind"


class TestArtifactKindStripsFilenameFromPrompt:
    """#191 — when ``--artifact-kind`` is declared and ``--target`` is a real file
    path, the prompt's ``Target reference`` block must carry the file
    *content*, not the file *path*.

    Background: gpt-4o-mini at v4 was observed parroting filenames as
    artifact-kind evidence on positive controls (rule.target_kind matches
    --artifact-kind, deterministic precheck does NOT fire, the LLM is
    invoked). The model returned ``judgment=unsupported`` with
    ``primary_reason: "The rule is annotated 'commit_message' but the
    artifact provided is a target-03-commit.txt."`` — i.e. it cited the
    filename instead of honouring the declared kind.

    The fix strips filename context from the prompt when ``--artifact-kind``
    is declared by reading the file content and rendering it into the
    ``Target reference`` slot. The substring fabrication validator
    (``_resolve_artifact_text``) follows the same substitution so quote
    grounding stays consistent with what the model actually saw.
    """

    @staticmethod
    def _commit_rule():
        from gate_keeper.models import TargetKind

        return _semantic_rule_with_target_kind(TargetKind.COMMIT_MESSAGE)

    def test_path_target_with_artifact_kind_renders_file_content(self, tmp_path):
        """The prompt's ``Target reference`` slot must contain the file
        content — not the path string — when ``artifact_kind`` is
        declared and ``target`` is an existing file path."""
        from gate_keeper.models import TargetKind

        commit_path = tmp_path / "target-03-commit.txt"
        commit_body = "fix(parser): handle CRLF in evidence blocks\n\nrelated to #foo"
        commit_path.write_text(commit_body, encoding="utf-8")

        _system, user = llm_backend._build_prompt(
            self._commit_rule(),
            commit_path,
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )

        # The file content must appear inline.
        assert commit_body in user
        # The path / filename must NOT appear in the rendered prompt — the
        # whole point of #191 is to deny the model a filename to parrot.
        assert str(commit_path) not in user
        assert commit_path.name not in user

    def test_path_target_with_artifact_kind_string_path_also_strips(self, tmp_path):
        """The substitution applies to both ``Path`` and string-path
        targets — the CLI surface forwards ``--target`` as a string."""
        from gate_keeper.models import TargetKind

        commit_path = tmp_path / "target-03-commit.txt"
        commit_body = "feat(cli): emit --help on stderr\n\nbecause stdout is reserved for results"
        commit_path.write_text(commit_body, encoding="utf-8")

        _system, user = llm_backend._build_prompt(
            self._commit_rule(),
            str(commit_path),
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )

        assert commit_body in user
        assert str(commit_path) not in user
        assert commit_path.name not in user

    def test_path_target_without_artifact_kind_preserves_legacy_path_render(self, tmp_path):
        """When ``artifact_kind`` is omitted, legacy v4 behaviour is
        preserved byte-for-byte: the path string is rendered into the
        ``Target reference`` slot, not the file content. This guards
        against accidentally widening the file-content substitution to
        unannotated callers.
        """
        commit_path = tmp_path / "target-03-commit.txt"
        commit_body = "fix(parser): pre-191 default behaviour\n\nbody intentionally unique"
        commit_path.write_text(commit_body, encoding="utf-8")

        _system, user = llm_backend._build_prompt(self._commit_rule(), commit_path)

        # Legacy: the path is rendered, the body is not.
        assert str(commit_path) in user
        assert commit_body not in user

    def test_inline_target_with_artifact_kind_stays_inline(self):
        """Inline string targets (no on-disk file) are passed through
        unchanged regardless of ``artifact_kind`` — there is no path to
        strip and no file to read."""
        from gate_keeper.models import TargetKind

        inline = "fix(parser): handle CRLF in evidence blocks\n\nrelated to #foo"
        _system, user = llm_backend._build_prompt(
            self._commit_rule(),
            inline,
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        assert inline in user

    def test_nonexistent_path_with_artifact_kind_falls_back_to_string(self, tmp_path):
        """A non-existent path with ``artifact_kind`` set must not raise;
        the helper falls back to ``str(target)`` so the model still gets
        a reference (and the existing instruction "judge from the
        reference alone" applies)."""
        from gate_keeper.models import TargetKind

        missing = tmp_path / "does-not-exist.txt"
        _system, user = llm_backend._build_prompt(
            self._commit_rule(),
            missing,
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )
        assert str(missing) in user

    def test_resolve_artifact_text_mirrors_prompt_substitution(self, tmp_path):
        """The substring fabrication validator must see the same
        artifact text the prompt rendered, otherwise legitimate
        content-grounded quotes would be falsely flagged
        ``llm_quote_fabrication``.
        """
        from gate_keeper.models import TargetKind

        path = tmp_path / "target-03-commit.txt"
        body = "fix(parser): handle CRLF in evidence blocks"
        path.write_text(body, encoding="utf-8")

        # With artifact_kind declared, the validator returns file content.
        assert llm_backend._resolve_artifact_text(path, TargetKind.COMMIT_MESSAGE) == body
        # Without artifact_kind, the validator returns str(target) (legacy).
        assert llm_backend._resolve_artifact_text(path, None) == str(path)
        assert llm_backend._resolve_artifact_text(path) == str(path)

    def test_path_target_with_artifact_kind_judges_content_via_check(self, monkeypatch, tmp_path):
        """End-to-end: ``check`` with a path + ``artifact_kind`` produces
        an ``llm_judgment`` whose grounding quotes are content
        substrings (not path / filename substrings).

        The provider helper is monkeypatched so the test is deterministic
        — the substring-grounding validator (#172) is what proves the
        ``Target reference`` slot rendered file content rather than a
        path string: a quote that is a substring of the file body but
        not of the path string would otherwise be flagged
        ``llm_quote_fabrication``.
        """
        from gate_keeper.models import TargetKind

        env = {
            "GATE_KEEPER_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test",
        }
        _patch_env(monkeypatch, env)

        commit_path = tmp_path / "target-03-commit.txt"
        commit_body = (
            "fix(parser): handle CRLF in evidence blocks\n\n"
            "Without this guard, blocks copied from Windows editors silently "
            "fail substring grounding because the artifact text carries \\r\\n "
            "while the LLM emits \\n quotes."
        )
        commit_path.write_text(commit_body, encoding="utf-8")

        # The model returns a quote that is a substring of the body — but
        # NOT a substring of the path / filename. Without #191 this would
        # be flagged as fabricated; with #191 the validator sees the
        # body and the quote is grounded.
        body_quote = "blocks copied from Windows editors silently fail substring grounding"
        response = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Body explains the failure mode and the fix concretely.",
                "supporting_evidence_quotes": [body_quote],
                "suggested_action": None,
            }
        )

        captured: dict[str, object] = {}

        def _capture_call(api_key, system, user, model):
            captured["user"] = user
            return _stub_response(response)

        monkeypatch.setattr(llm_backend, "_call_openai", _capture_call)

        rule = self._commit_rule()
        diag = llm_backend.check(
            rule,
            commit_path,
            artifact_kind=TargetKind.COMMIT_MESSAGE,
        )

        # Sanity: the prompt the helper saw included the body and not the path.
        assert isinstance(captured["user"], str)
        rendered_prompt: str = captured["user"]  # type: ignore[assignment]
        assert body_quote in rendered_prompt
        assert str(commit_path) not in rendered_prompt
        assert commit_path.name not in rendered_prompt

        # End result: PASS with content-grounded evidence (no fabrication).
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["supporting_evidence_quotes"] == [body_quote]


# ---------------------------------------------------------------------------
# Reproducibility metric (#68): run_n
# ---------------------------------------------------------------------------


class TestRunN:
    """Tests for ``llm_rubric.run_n`` — reproducibility aggregation (#68)."""

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }

    def test_n_must_be_at_least_one(self, tmp_path):
        with pytest.raises(ValueError):
            llm_backend.run_n(_semantic_rule(), tmp_path, 0)

    def test_n_one_is_equivalent_to_check(self, monkeypatch, tmp_path):
        """n=1 returns the same diagnostic as ``check`` (no extra evidence)."""
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda key, system, user, model: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 1)
        # Only the llm_judgment evidence — no reproducibility_score appended.
        assert len(diag.evidence) == 1
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.status is Status.PASS

    def test_unanimous_pass_score_is_one(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 3)
        assert diag.status is Status.PASS
        # last evidence is the reproducibility entry
        repro = diag.evidence[-1]
        assert repro.kind == "reproducibility_score"
        assert repro.data["score"] == 1.0
        assert repro.data["n"] == 3
        assert repro.data["pass_count"] == 3
        assert repro.data["majority_judgment"] == "pass"

    def test_unanimous_fail_score_is_one(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(_VALID_FAIL_JSON),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 3)
        assert diag.status is Status.FAIL
        repro = diag.evidence[-1]
        assert repro.kind == "reproducibility_score"
        assert repro.data["score"] == 1.0
        assert repro.data["pass_count"] == 0
        assert repro.data["majority_judgment"] == "fail"

    def test_mixed_majority_pass(self, monkeypatch, tmp_path):
        """2 pass + 1 fail in 3 runs -> pass with score 2/3."""
        _patch_env(monkeypatch, self._ENV)
        responses = iter([_VALID_PASS_JSON, _VALID_FAIL_JSON, _VALID_PASS_JSON])
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(next(responses)),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 3)
        assert diag.status is Status.PASS
        repro = diag.evidence[-1]
        assert repro.kind == "reproducibility_score"
        assert repro.data["pass_count"] == 2
        assert abs(repro.data["score"] - 2 / 3) < 1e-9
        assert repro.data["majority_judgment"] == "pass"

    def test_mixed_majority_fail(self, monkeypatch, tmp_path):
        """1 pass + 2 fail in 3 runs -> fail with score 2/3."""
        _patch_env(monkeypatch, self._ENV)
        responses = iter([_VALID_FAIL_JSON, _VALID_PASS_JSON, _VALID_FAIL_JSON])
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(next(responses)),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 3)
        assert diag.status is Status.FAIL
        repro = diag.evidence[-1]
        assert repro.kind == "reproducibility_score"
        assert repro.data["pass_count"] == 1
        assert abs(repro.data["score"] - 2 / 3) < 1e-9
        assert repro.data["majority_judgment"] == "fail"

    def test_tie_breaks_toward_fail(self, monkeypatch, tmp_path):
        """Even N with 50/50 split -> fail-closed."""
        _patch_env(monkeypatch, self._ENV)
        responses = iter([_VALID_PASS_JSON, _VALID_FAIL_JSON])
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(next(responses)),
        )
        diag = llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 2)
        assert diag.status is Status.FAIL
        repro = diag.evidence[-1]
        assert repro.data["pass_count"] == 1
        assert repro.data["score"] == 0.5
        assert repro.data["majority_judgment"] == "fail"

    def test_unconfigured_returns_unavailable_without_aggregation(self, tmp_path):
        """When provider is unconfigured, run_n short-circuits to UNAVAILABLE."""
        diag = llm_backend.run_n(_semantic_rule(), tmp_path, 5)
        assert diag.status is Status.UNAVAILABLE
        # No reproducibility_score evidence (aggregation aborted).
        assert all(e.kind != "reproducibility_score" for e in diag.evidence)

    def test_provider_error_short_circuits(self, monkeypatch, tmp_path):
        """If any single run errors, return that diagnostic without aggregating."""
        _patch_env(monkeypatch, self._ENV)

        def _boom(*_a, **_k):
            raise RuntimeError("transient 503")

        monkeypatch.setattr(llm_backend, "_call_anthropic", _boom)
        diag = llm_backend.run_n(_semantic_rule(), tmp_path, 3)
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_error"
        assert all(e.kind != "reproducibility_score" for e in diag.evidence)

    def test_calls_provider_n_times(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, self._ENV)
        call_count = {"n": 0}

        def _counter(*_a, **_k):
            call_count["n"] += 1
            return _stub_response(_VALID_PASS_JSON)

        monkeypatch.setattr(llm_backend, "_call_anthropic", _counter)
        llm_backend.run_n(_semantic_rule(), _target_with_artifact(tmp_path), 5)
        assert call_count["n"] == 5


# ---------------------------------------------------------------------------
# _estimate_cost (#133)
# ---------------------------------------------------------------------------


class TestEstimateCost:
    """Unit tests for ``_estimate_cost`` — per-model static pricing table.

    Snapshot date: 2026-05-08.
    - gpt-4o-mini: $0.15/1M input, $0.60/1M output
    - claude-haiku-4-5: $0.80/1M input, $4.00/1M output
    """

    def test_gpt4o_mini_known_tokens(self):
        """1000 tokens_in + 200 tokens_out at gpt-4o-mini pricing.

        cost = (1000 * $0.15 + 200 * $0.60) / 1,000,000
             = (150 + 120) / 1,000,000
             = 270 / 1,000,000
             = $0.00027
        """
        expected = (1000 * 0.15 + 200 * 0.60) / 1_000_000
        result = llm_backend._estimate_cost("gpt-4o-mini", 1000, 200)
        assert result is not None
        assert abs(result - expected) < 1e-12
        assert abs(result - 0.00027) < 1e-9

    def test_claude_haiku_known_tokens(self):
        """500 tokens_in + 100 tokens_out at claude-haiku-4-5 pricing."""
        expected = (500 * 0.80 + 100 * 4.00) / 1_000_000
        result = llm_backend._estimate_cost("claude-haiku-4-5", 500, 100)
        assert result is not None
        assert abs(result - expected) < 1e-12

    def test_unknown_model_returns_none(self):
        """Unknown model must return None (fail-closed: no cost guessing)."""
        assert llm_backend._estimate_cost("gpt-5-turbo-ultra", 1000, 200) is None

    def test_unknown_model_empty_string_returns_none(self):
        assert llm_backend._estimate_cost("", 0, 0) is None

    def test_zero_tokens_returns_zero_cost(self):
        result = llm_backend._estimate_cost("gpt-4o-mini", 0, 0)
        assert result == 0.0

    def test_cost_estimate_in_evidence_openai(self, monkeypatch, tmp_path):
        """check() emits cost_estimate_usd in llm_judgment evidence (OpenAI)."""
        _patch_env(
            monkeypatch,
            {"GATE_KEEPER_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"},
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 100, "tokens_in": 1000, "tokens_out": 200},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "cost_estimate_usd" in data
        expected = (1000 * 0.15 + 200 * 0.60) / 1_000_000
        assert abs(data["cost_estimate_usd"] - expected) < 1e-12

    def test_cost_estimate_in_evidence_anthropic(self, monkeypatch, tmp_path):
        """check() emits cost_estimate_usd in llm_judgment evidence (Anthropic)."""
        _patch_env(
            monkeypatch,
            {"GATE_KEEPER_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"},
        )
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 150, "tokens_in": 500, "tokens_out": 100},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "cost_estimate_usd" in data
        expected = (500 * 0.80 + 100 * 4.00) / 1_000_000
        assert abs(data["cost_estimate_usd"] - expected) < 1e-12

    def test_cost_estimate_is_none_for_unknown_model_openai(self, monkeypatch, tmp_path):
        """check() emits cost_estimate_usd: None when the resolved model is unknown.

        Fail-closed evidence contract (#133): when the provider model is not in
        ``_MODEL_PRICING`` the field must still be present in the evidence dict
        with value ``None`` — never absent, never a guessed default.
        """
        _patch_env(
            monkeypatch,
            {"GATE_KEEPER_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"},
        )
        # Force the OpenAI dispatch branch to use a model that's not in the
        # pricing table.
        monkeypatch.setattr(llm_backend, "OPENAI_DEFAULT_MODEL", "gpt-future-unknown")
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 100, "tokens_in": 1000, "tokens_out": 200},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        # Field must be present but null.
        assert "cost_estimate_usd" in data
        assert data["cost_estimate_usd"] is None
        # Telemetry fields are still present (this is the success path).
        assert data["tokens_in"] == 1000
        assert data["tokens_out"] == 200

    def test_cost_estimate_is_none_for_unknown_model_anthropic(self, monkeypatch, tmp_path):
        """check() emits cost_estimate_usd: None for unknown Anthropic model."""
        _patch_env(
            monkeypatch,
            {"GATE_KEEPER_LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"},
        )
        monkeypatch.setattr(llm_backend, "ANTHROPIC_DEFAULT_MODEL", "claude-future-unknown")
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 150, "tokens_in": 500, "tokens_out": 100},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), tmp_path)
        data = diag.evidence[0].data
        assert "cost_estimate_usd" in data
        assert data["cost_estimate_usd"] is None


# ---------------------------------------------------------------------------
# Strategy seam (#183): default `single`, reserved-id placeholders, unknown ids
# ---------------------------------------------------------------------------


def _semantic_rule_with_strategy(strategy: object) -> Rule:
    """Return a stub semantic rule with ``params['strategy']`` set to *strategy*.

    Mirrors :func:`_semantic_rule` but plumbs the new ``rule.params['strategy']``
    surface (#183) through so the dispatcher path is exercised end-to-end.
    """
    return Rule(
        id="stub-llm-rule",
        title="Stub semantic rule",
        source=SourceLocation(path="rules.md", line=5),
        text="The documentation should be clear and comprehensive",
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.ERROR,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params={"strategy": strategy},
    )


class TestStrategySeam:
    """Issue #183 — strategy abstraction for LLM rubric judgment.

    The default strategy id is ``"single"`` and preserves pre-#183 behaviour
    byte-for-byte except for the additive metadata fields on success
    evidence. Reserved ids (``"consensus"`` / ``"review"`` / ``"adaptive"``)
    return ``UNAVAILABLE`` with ``strategy_unavailable`` evidence so callers
    discover the gap explicitly. Unknown ids likewise fail closed.
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    def test_default_strategy_is_single(self):
        """Module-level constant declares ``single`` as the default."""
        assert llm_backend.DEFAULT_STRATEGY == "single"

    def test_known_strategies_set(self):
        """All four reserved ids are declared so the IR can validate values."""
        assert llm_backend.KNOWN_STRATEGIES == frozenset({"single", "consensus", "review", "adaptive"})

    def test_resolve_strategy_id_defaults_to_single(self):
        """Unset ``rule.params['strategy']`` resolves to the default id."""
        rule = _semantic_rule()
        assert llm_backend._resolve_strategy_id(rule) == "single"

    def test_resolve_strategy_id_honours_explicit_value(self):
        rule = _semantic_rule_with_strategy("consensus")
        assert llm_backend._resolve_strategy_id(rule) == "consensus"

    def test_strategies_registry_contains_single(self):
        """``single`` is the reference implementation in the #183 slice."""
        assert "single" in llm_backend._STRATEGIES
        assert callable(llm_backend._STRATEGIES["single"])

    def test_strategies_registry_omits_reserved_ids(self):
        """All four known strategy ids now have concrete implementations.

        ``consensus`` is implemented (#184), ``review`` is implemented (#185),
        and ``adaptive`` is implemented (#186). All must appear in the registry.
        """
        # All four are now concrete.
        assert "consensus" in llm_backend._STRATEGIES
        assert "review" in llm_backend._STRATEGIES
        assert "adaptive" in llm_backend._STRATEGIES

    def test_default_pass_evidence_carries_strategy_metadata(self, monkeypatch, tmp_path):
        """Issue #183 acceptance: success evidence advertises strategy fields."""
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 137, "tokens_in": 250, "tokens_out": 60},
            ),
        )
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        # Strategy metadata is appended additively — the legacy fields are
        # untouched so existing consumers keep working.
        assert data["llm_strategy"] == "single"
        assert data["llm_call_count"] == 1
        assert data["models"] == [llm_backend.OPENAI_DEFAULT_MODEL]
        assert data["latency_ms_total"] == 137
        # cost_estimate_usd_total mirrors cost_estimate_usd for a single call;
        # both are populated for known models in _MODEL_PRICING.
        assert data["cost_estimate_usd_total"] == data["cost_estimate_usd"]
        # Legacy fields untouched.
        assert data["latency_ms"] == 137
        assert data["tokens_in"] == 250
        assert data["tokens_out"] == 60
        assert data["judgment"] == "pass"
        assert data["prompt_version"] == llm_backend.PROMPT_VERSION

    def test_explicit_single_strategy_id_honoured(self, monkeypatch, tmp_path):
        """An explicit ``params.strategy=single`` id behaves like the default."""
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(
                _VALID_PASS_JSON,
                {"latency_ms": 50, "tokens_in": 100, "tokens_out": 20},
            ),
        )
        rule = _semantic_rule_with_strategy("single")
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        assert diag.evidence[0].kind == "llm_judgment"
        assert diag.evidence[0].data["llm_strategy"] == "single"
        assert diag.evidence[0].data["llm_call_count"] == 1

    def test_all_four_known_strategies_are_concrete(self):
        """All four known strategy ids are now concrete implementations (#186).

        ``consensus`` (#184), ``review`` (#185), and ``adaptive`` (#186) are
        all fully implemented. No known strategy dispatches through
        ``_run_not_implemented_strategy`` any more.
        """
        for strategy_id in llm_backend.KNOWN_STRATEGIES:
            assert strategy_id in llm_backend._STRATEGIES, (
                f"Expected {strategy_id!r} to be a concrete strategy but it is absent "
                "from _STRATEGIES. Update this test when new reserved ids are added."
            )

    def test_unknown_strategy_returns_unavailable(self, monkeypatch, tmp_path):
        """Strings outside ``KNOWN_STRATEGIES`` fail closed with ``unknown_strategy``."""
        _patch_env(monkeypatch, self._ENV)
        monkeypatch.setattr(
            llm_backend,
            "_call_openai",
            lambda *_a, **_k: _stub_response(_VALID_PASS_JSON),
        )
        rule = _semantic_rule_with_strategy("nonsense-xyz")
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "strategy_unavailable"
        assert diag.evidence[0].data["requested_strategy"] == "nonsense-xyz"
        assert diag.evidence[0].data["failure_mode"] == "unknown_strategy"
        assert "single" in diag.evidence[0].data["available_strategies"]

    def test_strategy_seam_works_without_provider_calls(self, monkeypatch, tmp_path):
        """Acceptance: fake-provider tests can exercise the strategy seam.

        This verifies the issue #183 acceptance criterion that fake-provider
        tests can drive the strategy seam without live LLM calls. The
        provider stub is the standard ``_stub_response`` helper used
        throughout this file; no network call is involved.
        """
        _patch_env(monkeypatch, self._ENV)
        calls: list[tuple] = []

        def _spy(api_key, system, user, model):
            calls.append((api_key, model))
            return _stub_response(_VALID_PASS_JSON)

        monkeypatch.setattr(llm_backend, "_call_openai", _spy)
        diag = llm_backend.check(_semantic_rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        # Exactly one call for the single strategy.
        assert len(calls) == 1
        assert diag.evidence[0].data["llm_call_count"] == 1
        # And the model used is reflected in the strategy-aggregated list.
        assert diag.evidence[0].data["models"] == [calls[0][1]]

    def test_judgment_request_dataclass_is_frozen(self):
        """``JudgmentRequest`` is the seam contract; immutability matters."""
        rule = _semantic_rule()
        request = llm_backend.JudgmentRequest(rule=rule, target="x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            request.target = "y"  # type: ignore[misc]


class TestConsensusStrategy:
    """Issue #184 — consensus strategy with majority-vote aggregation.

    All tests use fake provider stubs; no live LLM calls are made.
    The provider stub is configured via monkeypatch on ``_call_openai``.
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    # ---- helpers ----

    def _rule(self, panel_size: int | None = None) -> "Rule":
        params: dict = {"strategy": "consensus"}
        if panel_size is not None:
            params["consensus_panel_size"] = panel_size
        return Rule(
            id="stub-consensus-rule",
            title="Stub consensus rule",
            source=SourceLocation(path="rules.md", line=1),
            text="The documentation should be clear and comprehensive",
            kind=RuleKind.SEMANTIC_RUBRIC,
            severity=Severity.ERROR,
            backend_hint=Backend.LLM_RUBRIC,
            confidence=Confidence.LOW,
            params=params,
        )

    def _make_spy(self, responses: list[str]) -> "tuple[list[tuple], object]":
        """Return (calls_log, spy_fn) where spy_fn cycles through *responses*."""
        calls: list[tuple] = []
        idx = {"i": 0}

        def _spy(api_key, system, user, model):
            calls.append((api_key, model))
            text = responses[idx["i"] % len(responses)]
            idx["i"] += 1
            return _stub_response(text, {"latency_ms": 50, "tokens_in": 100, "tokens_out": 20})

        return calls, _spy

    # ---- dispatcher routing ----

    def test_consensus_routes_to_consensus_strategy(self, monkeypatch, tmp_path):
        """``params.strategy=consensus`` dispatches to ``_run_consensus_strategy``."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        # Should have made provider calls (consensus, not unavailable).
        assert len(calls) == 3
        assert diag.evidence[0].kind == "llm_consensus"
        assert diag.evidence[0].data["llm_strategy"] == "consensus"

    # ---- unanimous verdicts ----

    def test_unanimous_pass_returns_pass(self, monkeypatch, tmp_path):
        """Three pass votes → PASS with llm_consensus evidence."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["consensus_votes"]["pass"] == 3
        assert data["consensus_votes"]["fail"] == 0
        assert data["majority_verdict"] == "pass"

    def test_unanimous_fail_returns_fail(self, monkeypatch, tmp_path):
        """Three fail votes → FAIL with llm_consensus evidence."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_spy([_VALID_FAIL_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert diag.status is Status.FAIL
        data = diag.evidence[0].data
        assert data["consensus_votes"]["fail"] == 3
        assert data["majority_verdict"] == "fail"
        assert diag.remediation is not None

    # ---- majority (2-1 splits) ----

    def test_two_pass_one_fail_returns_pass(self, monkeypatch, tmp_path):
        """2 pass + 1 fail with N=3 → PASS (majority)."""
        _patch_env(monkeypatch, self._ENV)
        # Cycle: pass, pass, fail.
        responses = [_VALID_PASS_JSON, _VALID_PASS_JSON, _VALID_FAIL_JSON]
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["consensus_votes"]["pass"] == 2
        assert data["consensus_votes"]["fail"] == 1
        assert data["majority_verdict"] == "pass"

    def test_two_fail_one_pass_returns_fail(self, monkeypatch, tmp_path):
        """2 fail + 1 pass with N=3 → FAIL (majority)."""
        _patch_env(monkeypatch, self._ENV)
        responses = [_VALID_FAIL_JSON, _VALID_FAIL_JSON, _VALID_PASS_JSON]
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert diag.status is Status.FAIL
        data = diag.evidence[0].data
        assert data["consensus_votes"]["fail"] == 2
        assert data["majority_verdict"] == "fail"

    # ---- tie (N=2, 1-1 split) ----

    def test_tie_with_n2_returns_unsupported_consensus_tie(self, monkeypatch, tmp_path):
        """N=2, 1 pass + 1 fail → UNSUPPORTED with consensus_tie evidence (fail-closed)."""
        _patch_env(monkeypatch, self._ENV)
        responses = [_VALID_PASS_JSON, _VALID_FAIL_JSON]
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=2), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "consensus_tie"
        data = diag.evidence[0].data
        assert data["consensus_votes"]["pass"] == 1
        assert data["consensus_votes"]["fail"] == 1
        assert data["majority_verdict"] == "tie"
        assert diag.remediation is not None

    # ---- telemetry ----

    def test_telemetry_call_count_equals_panel_size(self, monkeypatch, tmp_path):
        """``llm_call_count`` equals the resolved panel size."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert len(calls) == 3
        assert diag.evidence[0].data["llm_call_count"] == 3

    def test_telemetry_models_list_has_n_entries(self, monkeypatch, tmp_path):
        """``models`` list has one entry per provider call."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert len(diag.evidence[0].data["models"]) == 3

    def test_telemetry_cost_is_sum_of_judges(self, monkeypatch, tmp_path):
        """``cost_estimate_usd_total`` equals the sum of individual judge costs."""
        _patch_env(monkeypatch, self._ENV)
        stub_telem = {"latency_ms": 50, "tokens_in": 100, "tokens_out": 20}

        def _spy(*_a, **_k):
            return _stub_response(_VALID_PASS_JSON, stub_telem)

        monkeypatch.setattr(llm_backend, "_call_openai", _spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        single_cost = llm_backend._estimate_cost(
            llm_backend.OPENAI_DEFAULT_MODEL, stub_telem["tokens_in"], stub_telem["tokens_out"]
        )
        assert single_cost is not None
        expected_total = single_cost * 3
        assert abs(diag.evidence[0].data["cost_estimate_usd_total"] - expected_total) < 1e-10

    def test_telemetry_latency_is_sum_of_judges(self, monkeypatch, tmp_path):
        """``latency_ms_total`` equals the sum of individual judge latencies."""
        _patch_env(monkeypatch, self._ENV)

        def _spy(*_a, **_k):
            return _stub_response(_VALID_PASS_JSON, {"latency_ms": 100, "tokens_in": 50, "tokens_out": 10})

        monkeypatch.setattr(llm_backend, "_call_openai", _spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert diag.evidence[0].data["latency_ms_total"] == 300

    # ---- judge_results ----

    def test_judge_results_has_n_entries(self, monkeypatch, tmp_path):
        """``judge_results`` carries one entry per judge call."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        assert len(diag.evidence[0].data["judge_results"]) == 3

    # ---- parse failure is counted as unsupported ----

    def test_parse_failure_counted_as_unsupported_vote(self, monkeypatch, tmp_path):
        """A judge that returns invalid JSON contributes an unsupported vote."""
        _patch_env(monkeypatch, self._ENV)
        # Judge 0: parse error; judges 1 and 2: pass → majority pass.
        responses = ["not-json", _VALID_PASS_JSON, _VALID_PASS_JSON]
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert data["consensus_votes"]["unsupported"] == 1
        assert data["consensus_votes"]["pass"] == 2
        assert diag.status is Status.PASS

    # ---- quote fabrication is counted as unsupported ----

    def test_quote_fabrication_counted_as_unsupported_vote(self, monkeypatch, tmp_path):
        """A judge with fabricated quotes contributes an unsupported vote."""
        fabricated_response = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Looks good.",
                "supporting_evidence_quotes": ["this quote does not appear in the artifact at all xyz123"],
                "suggested_action": None,
            }
        )
        _patch_env(monkeypatch, self._ENV)
        responses = [fabricated_response, _VALID_PASS_JSON, _VALID_PASS_JSON]
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert data["consensus_votes"]["unsupported"] >= 1
        assert diag.status is Status.PASS

    # ---- supporting quotes are merged from majority judges ----

    def test_supporting_quotes_merged_from_majority_judges(self, monkeypatch, tmp_path):
        """Quotes from majority-voting judges are merged and deduplicated."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=3), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        quotes = data["supporting_evidence_quotes"]
        # All three judges use the same quote; deduplicated to exactly one entry.
        assert len(quotes) >= 1
        assert len(quotes) == len(set(quotes))  # no duplicates

    # ---- panel size validation ----

    def test_default_panel_size_is_3(self, monkeypatch, tmp_path):
        """When ``consensus_panel_size`` is omitted the default is 3."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON] * 3)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.evidence[0].data["consensus_panel_size"] == 3
        assert len(calls) == 3

    def test_panel_size_clamped_to_max(self, monkeypatch, tmp_path):
        """``consensus_panel_size`` > 5 is clamped to 5."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON] * 10)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=99), _target_with_artifact(tmp_path))
        assert diag.evidence[0].data["consensus_panel_size"] == 5
        assert len(calls) == 5

    def test_panel_size_clamped_to_min(self, monkeypatch, tmp_path):
        """``consensus_panel_size`` < 2 is clamped to 2."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON] * 2)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(panel_size=1), _target_with_artifact(tmp_path))
        assert diag.evidence[0].data["consensus_panel_size"] == 2
        assert len(calls) == 2

    # ---- unconfigured falls through to unavailable ----

    def test_unconfigured_returns_unavailable(self, monkeypatch, tmp_path):
        """Consensus path respects the unconfigured check the same as single."""
        # Force unconfigured state regardless of the host environment by
        # returning an empty env dict from _load_env_file.
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *_a, **_k: {})
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_unconfigured"


class TestReviewStrategy:
    """Issue #185 — two-pass primary+reviewer strategy.

    All tests use fake provider stubs; no live LLM calls are made.
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    def _rule(self) -> "Rule":
        return Rule(
            id="stub-review-rule",
            title="Stub review rule",
            source=SourceLocation(path="rules.md", line=1),
            text="The documentation should be clear and comprehensive",
            kind=RuleKind.SEMANTIC_RUBRIC,
            severity=Severity.ERROR,
            backend_hint=Backend.LLM_RUBRIC,
            confidence=Confidence.LOW,
            params={"strategy": "review"},
        )

    def _make_two_call_spy(self, primary_response: str, reviewer_response: str) -> "tuple[list[str], object]":
        """Return (call_log, spy) for two sequential provider calls."""
        calls: list[str] = []
        responses = [primary_response, reviewer_response]
        idx = {"i": 0}

        def _spy(api_key, system, user, model):
            calls.append(system[:40])
            text = responses[idx["i"] % len(responses)]
            idx["i"] += 1
            return _stub_response(text, {"latency_ms": 100, "tokens_in": 200, "tokens_out": 50})

        return calls, _spy

    # ---- reviewer agree JSON helpers ----

    @staticmethod
    def _reviewer_agree(reason: str = "The primary judgment is well-grounded.") -> str:
        return json.dumps({"review_verdict": "agree", "review_reason": reason})

    @staticmethod
    def _reviewer_disagree_pass_should_fail(reason: str = "The artifact clearly fails.") -> str:
        return json.dumps({"review_verdict": "disagree-pass-should-fail", "review_reason": reason})

    @staticmethod
    def _reviewer_disagree_fail_should_pass(reason: str = "The artifact clearly passes.") -> str:
        return json.dumps({"review_verdict": "disagree-fail-should-pass", "review_reason": reason})

    @staticmethod
    def _reviewer_abstain(reason: str = "Cannot determine either way.") -> str:
        return json.dumps({"review_verdict": "abstain", "review_reason": reason})

    # ---- dispatcher routing ----

    def test_review_routes_to_review_strategy(self, monkeypatch, tmp_path):
        """``params.strategy=review`` dispatches to ``_run_review_strategy``."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        # Two provider calls should have been made (primary + reviewer).
        assert len(calls) == 2
        assert diag.evidence[0].kind == "llm_review"
        assert diag.evidence[0].data["llm_strategy"] == "review"

    # ---- reviewer agree → primary verdict ----

    def test_reviewer_agree_primary_pass_returns_pass(self, monkeypatch, tmp_path):
        """Reviewer agrees with a pass → PASS with llm_review evidence."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["review_reviewer_verdict"] == "agree"
        assert data["review_disagreement"] is False
        assert data["reviewer_abstained"] is False
        assert data["llm_strategy"] == "review"

    def test_reviewer_agree_primary_fail_returns_fail(self, monkeypatch, tmp_path):
        """Reviewer agrees with a fail → FAIL with llm_review evidence."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_FAIL_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.FAIL
        data = diag.evidence[0].data
        assert data["review_reviewer_verdict"] == "agree"
        assert data["review_disagreement"] is False
        assert diag.remediation is not None

    # ---- reviewer disagree → UNSUPPORTED (fail-closed) ----

    def test_reviewer_disagree_pass_should_fail_returns_unsupported(self, monkeypatch, tmp_path):
        """Reviewer disagrees (pass-should-fail) → UNSUPPORTED with review_disagreement."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_disagree_pass_should_fail())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "review_disagreement"
        data = diag.evidence[0].data
        assert data["review_disagreement"] is True
        assert data["review_reviewer_verdict"] == "disagree-pass-should-fail"
        assert data["supporting_evidence_quotes"] == []
        assert diag.remediation is not None

    def test_reviewer_disagree_fail_should_pass_returns_unsupported(self, monkeypatch, tmp_path):
        """Reviewer disagrees (fail-should-pass) → UNSUPPORTED with review_disagreement."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_FAIL_JSON, self._reviewer_disagree_fail_should_pass())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNSUPPORTED
        assert diag.evidence[0].kind == "review_disagreement"
        data = diag.evidence[0].data
        assert data["review_disagreement"] is True
        assert data["review_reviewer_verdict"] == "disagree-fail-should-pass"

    # ---- reviewer abstain → primary verdict + flag ----

    def test_reviewer_abstain_primary_pass_returns_pass_with_flag(self, monkeypatch, tmp_path):
        """Reviewer abstains → primary PASS emitted with reviewer_abstained=True."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_abstain())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["reviewer_abstained"] is True
        assert data["review_disagreement"] is False
        assert data["review_reviewer_verdict"] == "abstain"

    def test_reviewer_abstain_primary_fail_returns_fail_with_flag(self, monkeypatch, tmp_path):
        """Reviewer abstains → primary FAIL emitted with reviewer_abstained=True."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_FAIL_JSON, self._reviewer_abstain())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.FAIL
        data = diag.evidence[0].data
        assert data["reviewer_abstained"] is True
        assert data["review_disagreement"] is False

    # ---- reviewer parse failure → treated as abstain ----

    def test_reviewer_parse_failure_treated_as_abstain(self, monkeypatch, tmp_path):
        """A reviewer response that fails to parse is treated as abstain (primary preserved)."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, "not-valid-json")
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        # Primary verdict is preserved when reviewer cannot be parsed.
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["reviewer_abstained"] is True

    def test_reviewer_fenced_json_is_parsed(self, monkeypatch, tmp_path):
        """Reviewer response wrapped in a markdown code fence is parsed correctly (#217 P1)."""
        _patch_env(monkeypatch, self._ENV)
        fenced = "```json\n" + self._reviewer_agree() + "\n```"
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, fenced)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        # Fenced reviewer agree → primary verdict preserved, not abstained.
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["reviewer_abstained"] is False

    def test_reviewer_provider_error_call_count_is_2(self, monkeypatch, tmp_path):
        """``llm_call_count`` is 2 even when the reviewer provider call raises (#217 P2)."""
        _patch_env(monkeypatch, self._ENV)
        call_log: list[str] = []

        def _spy_raise_on_second(api_key, system, user, model):
            call_log.append("call")
            if len(call_log) == 1:
                telem = {"latency_ms": 100, "tokens_in": 200, "tokens_out": 50}
                return _stub_response(_VALID_PASS_JSON, telem)
            raise RuntimeError("simulated reviewer provider error")

        monkeypatch.setattr(llm_backend, "_call_openai", _spy_raise_on_second)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert len(call_log) == 2
        data = diag.evidence[0].data
        assert data["llm_call_count"] == 2
        assert data["reviewer_abstained"] is True

    # ---- telemetry ----

    def test_telemetry_call_count_is_2(self, monkeypatch, tmp_path):
        """``llm_call_count`` is 2 for the review strategy (primary + reviewer)."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert len(calls) == 2
        assert diag.evidence[0].data["llm_call_count"] == 2

    def test_telemetry_models_list_has_two_entries(self, monkeypatch, tmp_path):
        """``models`` list has two entries (primary model + reviewer model)."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert len(diag.evidence[0].data["models"]) == 2

    def test_telemetry_latency_is_sum_of_both_calls(self, monkeypatch, tmp_path):
        """``latency_ms_total`` sums primary and reviewer latencies."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        # Each stub call returns latency_ms=100; total should be 200.
        assert diag.evidence[0].data["latency_ms_total"] == 200

    def test_telemetry_cost_is_sum_of_both_calls(self, monkeypatch, tmp_path):
        """``cost_estimate_usd_total`` equals the sum of primary and reviewer costs."""
        _patch_env(monkeypatch, self._ENV)
        stub_telem = {"latency_ms": 100, "tokens_in": 200, "tokens_out": 50}

        def _spy(*_a, **_k):
            return _stub_response(_VALID_PASS_JSON, stub_telem)

        # We need primary to return PASS, reviewer to return agree.
        responses = [_VALID_PASS_JSON, self._reviewer_agree()]
        idx = {"i": 0}

        def _spy2(api_key, system, user, model):
            text = responses[idx["i"] % len(responses)]
            idx["i"] += 1
            return _stub_response(text, stub_telem)

        monkeypatch.setattr(llm_backend, "_call_openai", _spy2)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        single_cost = llm_backend._estimate_cost(
            llm_backend.OPENAI_DEFAULT_MODEL,
            stub_telem["tokens_in"],
            stub_telem["tokens_out"],
        )
        assert single_cost is not None
        expected_total = single_cost * 2
        assert abs(diag.evidence[0].data["cost_estimate_usd_total"] - expected_total) < 1e-10

    def test_primary_model_and_reviewer_model_recorded(self, monkeypatch, tmp_path):
        """Both ``primary_model`` and ``reviewer_model`` are recorded in evidence."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert data["primary_model"] == llm_backend.OPENAI_DEFAULT_MODEL
        assert data["reviewer_model"] == llm_backend.OPENAI_DEFAULT_MODEL

    # ---- primary judgment record in evidence ----

    def test_evidence_carries_primary_judgment_record(self, monkeypatch, tmp_path):
        """Evidence includes the primary judgment nested under ``review_primary_judgment``."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        pj = data["review_primary_judgment"]
        assert pj["judgment"] == "pass"
        assert isinstance(pj["primary_reason"], str)
        assert isinstance(pj["quotes"], list)

    def test_evidence_quotes_come_from_primary_on_agree(self, monkeypatch, tmp_path):
        """On reviewer agree, supporting_evidence_quotes are taken from the primary."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_agree())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert len(data["supporting_evidence_quotes"]) >= 1

    def test_evidence_quotes_empty_on_disagree(self, monkeypatch, tmp_path):
        """On reviewer disagreement, supporting_evidence_quotes is empty (fail-closed)."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_two_call_spy(_VALID_PASS_JSON, self._reviewer_disagree_pass_should_fail())
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.evidence[0].data["supporting_evidence_quotes"] == []

    # ---- unconfigured ----

    def test_unconfigured_returns_unavailable(self, monkeypatch, tmp_path):
        """Review path respects the unconfigured check the same as single."""
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *_a, **_k: {})
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "provider_unconfigured"


# ---------------------------------------------------------------------------
# Adaptive strategy (#186): single→consensus escalation
# ---------------------------------------------------------------------------


class TestAdaptiveStrategy:
    """Issue #186 — adaptive escalation policy (single → consensus).

    All tests use fake provider stubs; no live LLM calls are made.

    Escalation triggers:
    - Tier 1 evidence kind ``target_kind_mismatch`` → escalate to consensus.
    - Any other Tier 1 outcome → commit directly (no escalation).
    """

    _ENV = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-openai-test",
    }

    # JSON payload the model returns when it declares the rule does not apply
    # to the artifact kind. Requires a rule with target_kind != UNSPECIFIED
    # so that the single-strategy path emits target_kind_mismatch evidence.
    _UNSUPPORTED_JSON = json.dumps(
        {
            "judgment": "unsupported",
            "primary_reason": "The rule targets PR descriptions; this is a commit message.",
            "supporting_evidence_quotes": [],
            "suggested_action": None,
        }
    )

    def _rule(self, *, with_target_kind: bool = False) -> "Rule":
        from gate_keeper.models import TargetKind

        return Rule(
            id="stub-adaptive-rule",
            title="Stub adaptive rule",
            source=SourceLocation(path="rules.md", line=1),
            text="The documentation should be clear and comprehensive",
            kind=RuleKind.SEMANTIC_RUBRIC,
            severity=Severity.ERROR,
            backend_hint=Backend.LLM_RUBRIC,
            confidence=Confidence.LOW,
            params={"strategy": "adaptive"},
            target_kind=TargetKind.PR_DESCRIPTION if with_target_kind else TargetKind.UNSPECIFIED,
        )

    def _make_spy(self, responses: list[str]) -> "tuple[list[tuple], object]":
        """Return (calls_log, spy_fn) where spy_fn cycles through *responses*."""
        calls: list[tuple] = []
        idx = {"i": 0}

        def _spy(api_key, system, user, model):
            calls.append((api_key, model))
            text = responses[idx["i"] % len(responses)]
            idx["i"] += 1
            return _stub_response(text, {"latency_ms": 50, "tokens_in": 100, "tokens_out": 20})

        return calls, _spy

    # ---- dispatcher routing ----

    def test_adaptive_routes_to_adaptive_strategy(self, monkeypatch, tmp_path):
        """``params.strategy=adaptive`` dispatches to ``_run_adaptive_strategy``."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.evidence[0].kind == "llm_adaptive"
        assert diag.evidence[0].data["llm_strategy"] == "adaptive"

    # ---- Tier 1 pass → commit, no escalation ----

    def test_tier1_pass_commits_no_escalation(self, monkeypatch, tmp_path):
        """Tier 1 pass → PASS at adaptive_tier=1, provider called exactly once."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_PASS_JSON])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.PASS
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 1
        assert data["adaptive_escalation_reason"] is None
        assert data["llm_call_count"] == 1
        # Only one provider call was made.
        assert len(calls) == 1

    # ---- Tier 1 fail → commit, no escalation ----

    def test_tier1_fail_commits_no_escalation(self, monkeypatch, tmp_path):
        """Tier 1 fail → FAIL at adaptive_tier=1, provider called exactly once."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([_VALID_FAIL_JSON])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.FAIL
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 1
        assert data["adaptive_escalation_reason"] is None
        assert data["llm_call_count"] == 1
        assert len(calls) == 1
        assert diag.remediation is not None

    # ---- Tier 1 target_kind_mismatch → escalate to consensus ----

    def test_tier1_target_kind_mismatch_escalates(self, monkeypatch, tmp_path):
        """Tier 1 target_kind_mismatch → escalate; 1+3=4 provider calls total."""
        _patch_env(monkeypatch, self._ENV)
        # First call: unsupported (tier 1). Next three: consensus panel (tier 2).
        responses = [self._UNSUPPORTED_JSON] + [_VALID_PASS_JSON] * 3
        calls, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        rule = self._rule(with_target_kind=True)
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        # 4 total calls: 1 for tier-1 single + 3 for tier-2 consensus panel.
        assert len(calls) == 4
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 2
        assert data["adaptive_escalation_reason"] == "tier1_unsupported"
        assert data["llm_call_count"] == 4

    def test_tier1_target_kind_mismatch_escalation_verdict_from_consensus(self, monkeypatch, tmp_path):
        """After escalation, the final verdict comes from the consensus result."""
        _patch_env(monkeypatch, self._ENV)
        responses = [self._UNSUPPORTED_JSON] + [_VALID_FAIL_JSON] * 3
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        rule = self._rule(with_target_kind=True)
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        assert diag.status is Status.FAIL
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 2
        assert data["adaptive_escalation_reason"] == "tier1_unsupported"

    def test_tier1_unsupported_without_target_kind_does_not_escalate(self, monkeypatch, tmp_path):
        """Stray unsupported from unspecified target_kind → UNAVAILABLE, no escalation.

        The single strategy treats unsupported-without-target_kind as a
        provider contract violation (UNAVAILABLE with provider_error evidence).
        Adaptive must not escalate that — it is fail-closed, not ambiguous.
        """
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy([self._UNSUPPORTED_JSON])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        # Rule without target_kind: unsupported verdict is treated as provider error.
        rule = self._rule(with_target_kind=False)
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        # Only one provider call — no escalation.
        assert len(calls) == 1
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 1
        assert data["adaptive_escalation_reason"] is None

    # ---- Tier 1 unavailable (parse error) → fail-closed, no escalation ----

    def test_tier1_parse_failure_failclosed_no_escalation(self, monkeypatch, tmp_path):
        """Tier 1 parse failure → UNAVAILABLE at adaptive_tier=1, no escalation."""
        _patch_env(monkeypatch, self._ENV)
        calls, spy = self._make_spy(["not valid json {{{{"])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        # Only one call — no escalation.
        assert len(calls) == 1
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 1
        assert data["adaptive_escalation_reason"] is None

    # ---- Tier 1 unavailable (fabricated quotes) → fail-closed, no escalation ----

    def test_tier1_fabricated_quotes_failclosed_no_escalation(self, monkeypatch, tmp_path):
        """Tier 1 quote fabrication → UNSUPPORTED(llm_quote_fabrication) at tier=1, no escalation."""
        _patch_env(monkeypatch, self._ENV)
        fabricated_payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "All good.",
                "supporting_evidence_quotes": ["This phrase does not exist in the artifact at all."],
                "suggested_action": None,
            }
        )
        calls, spy = self._make_spy([fabricated_payload])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        # Quote fabrication returns UNSUPPORTED (not UNAVAILABLE), but adaptive
        # must NOT escalate it — evidence kind is llm_quote_fabrication, not
        # target_kind_mismatch.
        assert diag.status is Status.UNSUPPORTED
        # Only one call — no escalation.
        assert len(calls) == 1
        data = diag.evidence[0].data
        assert data["adaptive_tier"] == 1
        assert data["adaptive_escalation_reason"] is None

    # ---- Telemetry accumulation ----

    def test_telemetry_tier1_call_count_is_1(self, monkeypatch, tmp_path):
        """Tier 1 (no escalation): llm_call_count=1, one model entry."""
        _patch_env(monkeypatch, self._ENV)
        _, spy = self._make_spy([_VALID_PASS_JSON])
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert data["llm_call_count"] == 1
        assert len(data["models"]) == 1

    def test_telemetry_tier2_call_count_accumulates_across_tiers(self, monkeypatch, tmp_path):
        """Tier 2 (escalated): llm_call_count=4 (1 single + 3 consensus)."""
        _patch_env(monkeypatch, self._ENV)
        responses = [self._UNSUPPORTED_JSON] + [_VALID_PASS_JSON] * 3
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        rule = self._rule(with_target_kind=True)
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert data["llm_call_count"] == 4
        assert len(data["models"]) == 4

    def test_telemetry_latency_accumulates_across_tiers(self, monkeypatch, tmp_path):
        """latency_ms_total sums Tier 1 and Tier 2 latencies."""
        _patch_env(monkeypatch, self._ENV)
        idx = {"i": 0}
        responses = [self._UNSUPPORTED_JSON] + [_VALID_PASS_JSON] * 3

        def _spy(api_key, system, user, model):
            text = responses[idx["i"] % len(responses)]
            idx["i"] += 1
            return _stub_response(text, {"latency_ms": 100, "tokens_in": 100, "tokens_out": 20})

        monkeypatch.setattr(llm_backend, "_call_openai", _spy)
        rule = self._rule(with_target_kind=True)
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        # 4 calls × 100 ms = 400 ms total.
        assert data["latency_ms_total"] == 400

    def test_tier1_evidence_preserved_in_tier2(self, monkeypatch, tmp_path):
        """When Tier 2 fires, Tier 1 evidence is nested under ``tier1_evidence``."""
        _patch_env(monkeypatch, self._ENV)
        responses = [self._UNSUPPORTED_JSON] + [_VALID_PASS_JSON] * 3
        _, spy = self._make_spy(responses)
        monkeypatch.setattr(llm_backend, "_call_openai", spy)
        rule = self._rule(with_target_kind=True)
        diag = llm_backend.check(rule, _target_with_artifact(tmp_path))
        data = diag.evidence[0].data
        assert "tier1_evidence" in data
        t1 = data["tier1_evidence"]
        assert t1["judgment"] == "unsupported"

    # ---- unconfigured ----

    def test_unconfigured_returns_unavailable_adaptive(self, monkeypatch, tmp_path):
        """Adaptive path respects the unconfigured check the same as single."""
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *_a, **_k: {})
        diag = llm_backend.check(self._rule(), _target_with_artifact(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "llm_adaptive"
