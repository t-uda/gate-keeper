"""LLM-rubric backend for gate-keeper.

Loads provider credentials from a per-project dotenv file (path documented in
``docs/llm-rubric.md``). API keys and ``GATE_KEEPER_LLM_PROVIDER`` are
intentionally **not** read from ``os.environ`` to avoid collisions with the
host container's global API-key environment (the env-passthrough route was
rejected upstream — see hermes-engineering#149 and
``docs/auth-matrix.md`` "Issue #147").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

name = "llm-rubric"

DOTENV_PATH = Path("/home/vscode/.config/hermes-projects/gate-keeper.env")

ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

_SUPPORTED_PROVIDERS = ("anthropic", "openai")


def _load_env_file(path: Path = DOTENV_PATH) -> dict[str, str]:
    """Load the per-project dotenv file without mutating ``os.environ``.

    Returns an empty dict when the file is absent. Uses ``python-dotenv``'s
    ``dotenv_values`` so values stay local to this module.
    """
    if not path.exists():
        return {}
    from dotenv import dotenv_values

    values = dotenv_values(path)
    return {k: v for k, v in values.items() if v is not None}


def _is_configured() -> bool:
    """Return True iff a supported provider and its API key are present in the dotenv."""
    env = _load_env_file()
    provider = env.get("GATE_KEEPER_LLM_PROVIDER")
    if provider not in _SUPPORTED_PROVIDERS:
        return False
    key_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    return bool(env.get(key_var))


def _build_rubric_input(rule: Rule, target: str | Path) -> dict[str, Any]:
    return {
        "rule_text": rule.text,
        "rule_kind": rule.kind.value,
        "target": str(target),
    }


def _build_prompt(rule: Rule, target: str | Path) -> tuple[str, str]:
    system = (
        "You are a reviewer applying a single rule to one artifact. "
        'Reply with ONLY a compact JSON object: {"judgment": "pass" | "fail", '
        '"explanation": "<one-line summary, no newlines>"}. '
        "No prose outside the JSON."
    )
    user = (
        f"Rule:\n{rule.text}\n\n"
        f"Target reference (path or PR id):\n{target}\n\n"
        "Judge whether the target satisfies the rule. "
        "If you cannot read the target's content directly, judge from the reference alone."
    )
    return system, user


def _call_anthropic(api_key: str, system: str, user: str, model: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=400,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts: list[str] = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _call_openai(api_key: str, system: str, user: str, model: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_completion_tokens=400,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def _parse_response(text: str) -> tuple[str, str]:
    """Parse ``{"judgment", "explanation"}`` from model output. Raises ``ValueError``."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("response is not a JSON object")
    judgment = obj.get("judgment")
    explanation = obj.get("explanation")
    if judgment not in ("pass", "fail"):
        raise ValueError(f"judgment must be 'pass' or 'fail', got {judgment!r}")
    if not isinstance(explanation, str) or not explanation:
        raise ValueError("explanation must be a non-empty string")
    return judgment, explanation


def _unavailable_unconfigured(rule: Rule, rubric_input: dict[str, Any]) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message="LLM rubric backend is not configured; skipping rule.",
        evidence=[Evidence(kind="provider_unconfigured", data=rubric_input)],
        remediation=(
            "Configure an LLM provider to enable semantic rule evaluation. "
            "See docs/llm-rubric.md for the host-side dotenv setup."
        ),
    )


def _unavailable_provider_error(
    rule: Rule,
    rubric_input: dict[str, Any],
    provider: str,
    failure_mode: str,
    detail: str,
) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message=f"LLM rubric backend provider error ({provider}); skipping rule.",
        evidence=[
            Evidence(
                kind="provider_error",
                data={
                    **rubric_input,
                    "provider": provider,
                    "failure_mode": failure_mode,
                    "detail": detail[:500],
                },
            )
        ],
        remediation=(
            "Investigate the provider error (see evidence for failure mode) "
            "and rerun once the provider is healthy."
        ),
    )


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Evaluate a semantic-rubric rule against *target*.

    When no provider is configured (or the env file is absent), returns
    ``UNAVAILABLE`` with ``provider_unconfigured`` evidence. When a provider is
    configured, dispatches to the configured provider and maps the response to
    ``pass``/``fail`` with ``llm_judgment`` evidence. Provider errors and
    unparseable responses map to ``UNAVAILABLE`` with ``provider_error``
    evidence — never to a crash, ``pass``, or ``fail``.
    """
    rubric_input = _build_rubric_input(rule, target)

    if not _is_configured():
        return _unavailable_unconfigured(rule, rubric_input)

    env = _load_env_file()
    provider = env["GATE_KEEPER_LLM_PROVIDER"]
    system, user = _build_prompt(rule, target)

    try:
        if provider == "anthropic":
            model = ANTHROPIC_DEFAULT_MODEL
            response_text = _call_anthropic(env["ANTHROPIC_API_KEY"], system, user, model)
        else:
            model = OPENAI_DEFAULT_MODEL
            response_text = _call_openai(env["OPENAI_API_KEY"], system, user, model)
    except Exception as exc:  # noqa: BLE001 — fail-closed: any provider error → unavailable
        return _unavailable_provider_error(rule, rubric_input, provider, type(exc).__name__, str(exc))

    try:
        judgment, explanation = _parse_response(response_text)
    except ValueError as exc:
        return _unavailable_provider_error(rule, rubric_input, provider, "unparseable_response", str(exc))

    status = Status.PASS if judgment == "pass" else Status.FAIL
    evidence = Evidence(
        kind="llm_judgment",
        data={"model": model, "judgment": judgment, "explanation": explanation},
    )
    if status is Status.PASS:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=status,
            severity=rule.severity,
            message=explanation,
            evidence=[evidence],
        )
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=status,
        severity=rule.severity,
        message=explanation,
        evidence=[evidence],
        remediation=(
            "The semantic rubric judged the target as failing; review the "
            "explanation and update the artifact to satisfy the rule's intent."
        ),
    )
