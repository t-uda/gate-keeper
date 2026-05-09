"""Tests for ``gate-keeper diagnose`` (issue #129).

The ``diagnose`` subcommand reports LLM-rubric provider credential state
(dotenv path, file existence, ``GATE_KEEPER_LLM_PROVIDER``, API-key length,
and ``_is_configured()`` result). It must never print the API key value.

Tests monkeypatch ``llm_rubric._load_env_file`` so the host dotenv state
does not leak into the test suite (matching the pattern in
``tests/test_llm_rubric_backend.py`` and ``tests/test_dogfooding_rules.py``).
"""

from __future__ import annotations

from gate_keeper.backends import llm_rubric as llm_backend
from gate_keeper.cli import main


def test_diagnose_unconfigured_reports_no(monkeypatch, capsys):
    """An empty dotenv yields ``provider configured: no``.

    When ``GATE_KEEPER_LLM_PROVIDER`` is unset, the api-key line must
    say ``<unset>`` rather than ``<unsupported provider...>`` —
    "unsupported" is reserved for *recognised-as-not-supported* values
    (e.g. ``google``). Asserting the exact api-key line locks the
    documented UX and prevents accidental key-leak regressions.
    """
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: {})

    rc = main(["diagnose"])
    assert rc == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "dotenv path:" in out
    assert "GATE_KEEPER_LLM_PROVIDER: <unset>" in out
    assert "api key:            <unset>" in out
    # The "unsupported provider" wording is reserved for recognised-as-
    # unsupported values; an unset provider must not surface it.
    assert "unsupported provider" not in out
    assert "provider configured: no" in out


def test_diagnose_configured_openai_reports_yes_and_length(monkeypatch, capsys):
    """A populated openai dotenv yields ``yes`` and length-only key reporting."""
    secret = "sk-test-abcdef0123456789-DO-NOT-LEAK"
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": secret,
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "GATE_KEEPER_LLM_PROVIDER: openai" in out
    assert f"OPENAI_API_KEY: <{len(secret)} chars>" in out
    assert "provider configured: yes" in out

    # The literal key value MUST NOT appear in stdout or stderr.
    assert secret not in out
    assert secret not in captured.err


def test_diagnose_configured_anthropic_reports_yes_and_length(monkeypatch, capsys):
    """A populated anthropic dotenv yields ``yes`` and length-only key reporting."""
    secret = "sk-ant-anthropic-test-DO-NOT-LEAK"
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": secret,
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "GATE_KEEPER_LLM_PROVIDER: anthropic" in out
    assert f"ANTHROPIC_API_KEY: <{len(secret)} chars>" in out
    assert "provider configured: yes" in out

    assert secret not in out
    assert secret not in captured.err


def test_diagnose_unsupported_provider_reports_no(monkeypatch, capsys):
    """An unrecognised provider yields ``no`` regardless of any key entries."""
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "google",
        "GOOGLE_API_KEY": "ignored",
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "GATE_KEEPER_LLM_PROVIDER: google" in out
    assert "<unsupported provider" in out
    assert "provider configured: no" in out


def test_diagnose_provider_set_but_key_unset_reports_no(monkeypatch, capsys):
    """``GATE_KEEPER_LLM_PROVIDER=openai`` without ``OPENAI_API_KEY`` is ``no``."""
    env = {"GATE_KEEPER_LLM_PROVIDER": "openai"}
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "OPENAI_API_KEY: <unset>" in out
    assert "provider configured: no" in out


def test_diagnose_reports_default_model_openai(monkeypatch, capsys):
    """Without an override, ``diagnose`` reports the default OpenAI model."""
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    out = capsys.readouterr().out
    assert f"model:              {llm_backend.OPENAI_DEFAULT_MODEL} (default)" in out


def test_diagnose_reports_override_model_openai(monkeypatch, capsys):
    """``GATE_KEEPER_OPENAI_MODEL`` is surfaced in the ``diagnose`` model line."""
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
        "GATE_KEEPER_OPENAI_MODEL": "gpt-4o",
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "model:              gpt-4o (override)" in out


def test_diagnose_reports_default_model_anthropic(monkeypatch, capsys):
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    out = capsys.readouterr().out
    assert f"model:              {llm_backend.ANTHROPIC_DEFAULT_MODEL} (default)" in out


def test_diagnose_reports_override_model_anthropic(monkeypatch, capsys):
    env = {
        "GATE_KEEPER_LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "GATE_KEEPER_ANTHROPIC_MODEL": "claude-opus-4-7",
    }
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **kw: env)

    rc = main(["diagnose"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "model:              claude-opus-4-7 (override)" in out
