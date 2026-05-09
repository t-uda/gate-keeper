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
        """A rule without target_kind must render byte-identically to v2 plus version bump."""
        from gate_keeper.models import TargetKind

        rule = _semantic_rule_with_target_kind(TargetKind.UNSPECIFIED)
        _system, user = llm_backend._build_prompt(rule, "an inline target string")
        assert "## Artifact kind" not in user
        # The dedicated artifact-kind instruction block must not be
        # rendered. The unsupported-verdict shape documented in the
        # ``Examples of valid responses`` block is gated below in
        # ``test_unsupported_example_uses_neutral_placeholders`` — it is
        # acceptable for that example to mention the schema variant.
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
        """The kind-neutral example placeholders must be present (#175)."""
        from gate_keeper.models import TargetKind

        # Pick any target_kind — the example block is the same across all
        # of them by design (the kind-specific guidance is in the
        # artifact-kind block, not the example).
        rendered = self._render(TargetKind.PR_DESCRIPTION)
        # Both placeholders must appear so the model sees the substitution
        # contract instead of a copy-pasteable canned phrase.
        assert "<RULE_KIND>" in rendered
        assert "<ARTIFACT_KIND>" in rendered

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
