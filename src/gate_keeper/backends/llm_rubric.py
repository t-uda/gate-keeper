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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

name = "llm-rubric"

DOTENV_PATH = Path("/home/vscode/.config/hermes-projects/gate-keeper.env")

ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

_SUPPORTED_PROVIDERS = ("anthropic", "openai")

# Prompt versioning constant — referenced by issue #68 (reproducibility).
PROMPT_VERSION = "v1"

# ---------------------------------------------------------------------------
# Structured LLM judgment schema (#67)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LlmJudgment:
    """Structured output schema for a single LLM rubric evaluation.

    Fields
    ------
    judgment:
        ``"pass"`` or ``"fail"``.
    primary_reason:
        One-sentence summary of why the target passed or failed.
    supporting_evidence_quotes:
        List of verbatim quotes from the target supporting the judgment.
        Required (non-empty) on fail; optional (may be empty) on pass.
    suggested_action:
        Concrete remediation step. Required on fail; MUST be ``None`` on pass.
    """

    judgment: Literal["pass", "fail"]
    primary_reason: str
    supporting_evidence_quotes: list[str]
    suggested_action: str | None


@dataclass
class LlmJudgmentParseError:
    """Carries structured parse-failure information for #71 (failure rendering).

    Attributes
    ----------
    failure_mode:
        Short tag, e.g. ``"invalid_json"``, ``"missing_field"``,
        ``"invalid_judgment_value"``.
    detail:
        Human-readable explanation of the failure.
    raw_response_excerpt:
        First ~200 characters of the raw model response.
    """

    failure_mode: str
    detail: str
    raw_response_excerpt: str


# ---------------------------------------------------------------------------
# Prompt template (#67)
# ---------------------------------------------------------------------------

RUBRIC_PROMPT_TEMPLATE = """\
You are a rubric evaluator. Your sole task is to judge whether the target \
artifact satisfies the given rule.

## Rule

{rule_text}

## Target reference

{target}

## Instructions

1. Read the rule carefully. It describes a quality requirement.
2. Judge whether the target (identified by the reference above) satisfies it.
3. If you cannot read the target's content directly, judge from the reference alone.
4. Respond with **only** a JSON object that matches the schema below — no prose outside the JSON.

## Required response schema

{{
  "judgment": "pass" | "fail",
  "primary_reason": "<one sentence>",
  "supporting_evidence_quotes": ["<verbatim quote>", ...],
  "suggested_action": "<concrete step to fix>" | null
}}

Constraints:
- `judgment` must be exactly `"pass"` or `"fail"`.
- `primary_reason` must be a single sentence (no newlines).
- `supporting_evidence_quotes` must contain at least one entry when `judgment` is `"fail"`.
- `suggested_action` must be a non-empty string when `judgment` is `"fail"`;
  must be `null` when `judgment` is `"pass"`.

## Example of a valid response

{{
  "judgment": "fail",
  "primary_reason": "The README lacks a usage section.",
  "supporting_evidence_quotes": ["README.md: no '## Usage' heading found"],
  "suggested_action": "Add a '## Usage' section with at least one code example."
}}
"""

RUBRIC_SYSTEM_PROMPT = (
    "You are a rubric evaluator applying a single quality rule to one artifact. "
    "Respond with ONLY a JSON object matching the required schema. No prose outside the JSON."
)


# ---------------------------------------------------------------------------
# dotenv loader
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Rubric input builder
# ---------------------------------------------------------------------------

def _build_rubric_input(rule: Rule, target: str | Path) -> dict[str, Any]:
    """Return the context dict passed to the model and recorded in evidence.

    Keys: ``rule_text``, ``rule_kind``, ``target``.
    """
    return {
        "rule_text": rule.text,
        "rule_kind": rule.kind.value,
        "target": str(target),
    }


def _build_prompt(rule: Rule, target: str | Path) -> tuple[str, str]:
    system = RUBRIC_SYSTEM_PROMPT
    user = RUBRIC_PROMPT_TEMPLATE.format(rule_text=rule.text, target=str(target))
    return system, user


# ---------------------------------------------------------------------------
# Provider call helpers
# ---------------------------------------------------------------------------

def _call_anthropic(api_key: str, system: str, user: str, model: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=600,
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
        max_completion_tokens=600,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Parser / validator (#67)
# ---------------------------------------------------------------------------

def _parse_llm_judgment(text: str) -> LlmJudgment | LlmJudgmentParseError:
    """Parse and validate a raw model response string into ``LlmJudgment``.

    Returns ``LlmJudgmentParseError`` (never raises) for any failure.
    Extra fields in the JSON are silently ignored.
    """
    excerpt = text[:200]

    if not text.strip():
        return LlmJudgmentParseError(
            failure_mode="empty_response",
            detail="Model returned an empty string.",
            raw_response_excerpt=excerpt,
        )

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return LlmJudgmentParseError(
            failure_mode="invalid_json",
            detail=f"Response is not valid JSON: {exc}",
            raw_response_excerpt=excerpt,
        )

    if not isinstance(obj, dict):
        return LlmJudgmentParseError(
            failure_mode="invalid_json",
            detail="Response is not a JSON object.",
            raw_response_excerpt=excerpt,
        )

    # Required fields
    for field in ("judgment", "primary_reason", "supporting_evidence_quotes"):
        if field not in obj:
            return LlmJudgmentParseError(
                failure_mode="missing_field",
                detail=f"Required field '{field}' is absent.",
                raw_response_excerpt=excerpt,
            )

    judgment = obj["judgment"]
    if judgment not in ("pass", "fail"):
        return LlmJudgmentParseError(
            failure_mode="invalid_judgment_value",
            detail=f"judgment must be 'pass' or 'fail', got {judgment!r}.",
            raw_response_excerpt=excerpt,
        )

    primary_reason = obj["primary_reason"]
    if not isinstance(primary_reason, str) or not primary_reason.strip():
        return LlmJudgmentParseError(
            failure_mode="missing_field",
            detail="primary_reason must be a non-empty string.",
            raw_response_excerpt=excerpt,
        )

    quotes = obj["supporting_evidence_quotes"]
    if not isinstance(quotes, list):
        return LlmJudgmentParseError(
            failure_mode="missing_field",
            detail="supporting_evidence_quotes must be a list.",
            raw_response_excerpt=excerpt,
        )

    if judgment == "fail" and len(quotes) == 0:
        return LlmJudgmentParseError(
            failure_mode="missing_field",
            detail="supporting_evidence_quotes must contain at least one entry when judgment is 'fail'.",
            raw_response_excerpt=excerpt,
        )

    suggested_action = obj.get("suggested_action")
    if judgment == "fail":
        if not isinstance(suggested_action, str) or not suggested_action.strip():
            return LlmJudgmentParseError(
                failure_mode="missing_field",
                detail="suggested_action must be a non-empty string when judgment is 'fail'.",
                raw_response_excerpt=excerpt,
            )
    else:
        # pass judgment — suggested_action must be None/absent
        suggested_action = None

    return LlmJudgment(
        judgment=judgment,
        primary_reason=primary_reason,
        supporting_evidence_quotes=quotes,
        suggested_action=suggested_action,
    )


# ---------------------------------------------------------------------------
# Legacy thin wrapper — kept for backward compat with existing tests that
# call _parse_response directly.  New code should call _parse_llm_judgment.
# ---------------------------------------------------------------------------

def _parse_response(text: str) -> tuple[str, str]:
    """Parse ``{"judgment", ...}`` from model output. Raises ``ValueError``.

    .. deprecated::
        Use ``_parse_llm_judgment`` for structured output.  This shim is
        retained so existing callers that expect ``(judgment, primary_reason)``
        continue to work.
    """
    result = _parse_llm_judgment(text)
    if isinstance(result, LlmJudgmentParseError):
        raise ValueError(result.detail)
    return result.judgment, result.primary_reason


# ---------------------------------------------------------------------------
# Diagnostic constructors — #51 contract preserved byte-for-byte
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Evaluate a semantic-rubric rule against *target*.

    When no provider is configured (or the env file is absent), returns
    ``UNAVAILABLE`` with ``provider_unconfigured`` evidence. When a provider is
    configured, dispatches to the configured provider and maps the response to
    ``pass``/``fail`` with structured ``llm_judgment`` evidence (see
    ``LlmJudgment``). Provider errors and unparseable responses map to
    ``UNAVAILABLE`` with ``provider_error`` evidence — never to a crash,
    ``pass``, or ``fail``.

    On success the ``evidence[0].data`` dict contains:

    - ``model``: the model identifier used.
    - ``prompt_version``: ``PROMPT_VERSION`` constant (for #68 reproducibility).
    - ``judgment``: ``"pass"`` or ``"fail"``.
    - ``primary_reason``: one-sentence summary.
    - ``supporting_evidence_quotes``: list of verbatim quotes.
    - ``suggested_action``: remediation string (fail only) or ``None`` (pass).

    ``Diagnostic.remediation`` is set to ``suggested_action`` on fail.
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

    parsed = _parse_llm_judgment(response_text)
    if isinstance(parsed, LlmJudgmentParseError):
        return _unavailable_provider_error(
            rule, rubric_input, provider, "unparseable_response", parsed.detail
        )

    status = Status.PASS if parsed.judgment == "pass" else Status.FAIL
    evidence = Evidence(
        kind="llm_judgment",
        data={
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "judgment": parsed.judgment,
            "primary_reason": parsed.primary_reason,
            "supporting_evidence_quotes": parsed.supporting_evidence_quotes,
            "suggested_action": parsed.suggested_action,
        },
    )
    if status is Status.PASS:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=status,
            severity=rule.severity,
            message=parsed.primary_reason,
            evidence=[evidence],
        )
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=status,
        severity=rule.severity,
        message=parsed.primary_reason,
        evidence=[evidence],
        remediation=parsed.suggested_action,
    )
