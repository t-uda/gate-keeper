"""LLM-rubric backend for gate-keeper.

Loads provider credentials from a per-project dotenv file (path documented in
``docs/llm-rubric.md``). API keys and ``GATE_KEEPER_LLM_PROVIDER`` are
intentionally **not** read from ``os.environ`` to avoid collisions with the
host container's global API-key environment (the env-passthrough route was
rejected upstream — see hermes-engineering#149 and
``docs/auth-matrix.md`` "Issue #147").
"""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status
from gate_keeper.targets import TargetSpec

name = "llm-rubric"

DOTENV_PATH = Path("/home/vscode/.config/hermes-projects/gate-keeper.env")

ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

_SUPPORTED_PROVIDERS = ("anthropic", "openai")

# Prompt versioning constant — referenced by issue #68 (reproducibility).
PROMPT_VERSION = "v1"

# ---------------------------------------------------------------------------
# Per-model pricing table (#133)
#
# Snapshot date: 2026-05-08.
# Source: https://openai.com/api/pricing/ (gpt-4o-mini) and
#         https://www.anthropic.com/pricing#anthropic-api (claude-haiku-4-5).
# Units: USD per 1 million tokens.
# Update this dict when pricing changes; add ``snapshot_date`` to docs/llm-rubric.md.
# ---------------------------------------------------------------------------

_MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI — https://openai.com/api/pricing/
    "gpt-4o-mini": {"input_per_1m": 0.15, "output_per_1m": 0.60},
    # Anthropic — https://www.anthropic.com/pricing#anthropic-api
    "claude-haiku-4-5": {"input_per_1m": 0.80, "output_per_1m": 4.00},
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Return estimated USD cost for *model* given token counts.

    Uses the static ``_MODEL_PRICING`` snapshot (2026-05-08).  Returns ``None``
    for any model not in the table — fail-closed: unknown cost is not guessed.

    Parameters
    ----------
    model:
        Model identifier as reported by the provider (e.g. ``"gpt-4o-mini"``).
    tokens_in:
        Provider-reported prompt / input token count.
    tokens_out:
        Provider-reported completion / output token count.
    """
    pricing = _MODEL_PRICING.get(model)
    if pricing is None:
        return None
    cost = (tokens_in * pricing["input_per_1m"] + tokens_out * pricing["output_per_1m"]) / 1_000_000
    return cost


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


def _is_configured(env: dict[str, str] | None = None) -> bool:
    """Return True iff a supported provider and its API key are present.

    When *env* is ``None`` the dotenv is read fresh; callers that already
    hold a snapshot should pass it in so the configured decision and any
    follow-up access (provider, key, model override) all read from the
    same snapshot — otherwise a concurrent dotenv edit could split the
    decision across two reads.
    """
    if env is None:
        env = _load_env_file()
    provider = env.get("GATE_KEEPER_LLM_PROVIDER")
    if provider not in _SUPPORTED_PROVIDERS:
        return False
    key_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    return bool(env.get(key_var))


def _resolve_model(provider: str, env: dict[str, str]) -> str:
    """Return the model identifier to use for *provider*.

    Reads ``GATE_KEEPER_<PROVIDER>_MODEL`` from the dotenv snapshot when set
    and non-empty (after stripping); otherwise falls back to the source
    default constant. Empty / whitespace-only overrides are ignored so a
    blank line in the dotenv does not silently produce an invalid model id.
    """
    if provider == "anthropic":
        override = env.get("GATE_KEEPER_ANTHROPIC_MODEL", "").strip()
        return override or ANTHROPIC_DEFAULT_MODEL
    if provider == "openai":
        override = env.get("GATE_KEEPER_OPENAI_MODEL", "").strip()
        return override or OPENAI_DEFAULT_MODEL
    raise ValueError(f"unsupported provider: {provider!r}")


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


def _call_anthropic(api_key: str, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
    """Call Anthropic and return ``(response_text, telemetry)``.

    Telemetry keys (#76):

    - ``latency_ms``: wall-clock provider call duration, integer milliseconds.
    - ``tokens_in``: provider-reported prompt token count
      (Anthropic ``usage.input_tokens``).
    - ``tokens_out``: provider-reported completion token count
      (Anthropic ``usage.output_tokens``).

    Fail-closed contract (#76 follow-up): if the provider response omits
    ``usage.input_tokens`` or ``usage.output_tokens`` (or the entire ``usage``
    object), raise :class:`RuntimeError`. The caller in :func:`check`
    catches this and dispatches a ``provider_error`` diagnostic; we never
    synthesise a zero token count.
    """
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    start = time.perf_counter()
    msg = client.messages.create(
        model=model,
        max_tokens=600,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    latency_ms = int(round((time.perf_counter() - start) * 1000))
    parts: list[str] = []
    for block in msg.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    usage = getattr(msg, "usage", None)
    if usage is None:
        raise RuntimeError("Anthropic response missing usage block; cannot record token telemetry.")
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None:
        raise RuntimeError("Anthropic response missing usage.input_tokens; cannot record token telemetry.")
    if output_tokens is None:
        raise RuntimeError("Anthropic response missing usage.output_tokens; cannot record token telemetry.")
    return "".join(parts), {
        "latency_ms": latency_ms,
        "tokens_in": int(input_tokens),
        "tokens_out": int(output_tokens),
    }


def _call_openai(api_key: str, system: str, user: str, model: str) -> tuple[str, dict[str, int]]:
    """Call OpenAI and return ``(response_text, telemetry)``.

    Telemetry keys (#76):

    - ``latency_ms``: wall-clock provider call duration, integer milliseconds.
    - ``tokens_in``: provider-reported prompt token count
      (OpenAI ``usage.prompt_tokens``).
    - ``tokens_out``: provider-reported completion token count
      (OpenAI ``usage.completion_tokens``).

    Fail-closed contract (#76 follow-up): if the provider response omits
    ``usage.prompt_tokens`` or ``usage.completion_tokens`` (or the entire
    ``usage`` object), raise :class:`RuntimeError`. The caller in
    :func:`check` catches this and dispatches a ``provider_error``
    diagnostic; we never synthesise a zero token count.
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    start = time.perf_counter()
    resp = client.chat.completions.create(
        model=model,
        max_completion_tokens=600,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    latency_ms = int(round((time.perf_counter() - start) * 1000))
    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    if usage is None:
        raise RuntimeError("OpenAI response missing usage block; cannot record token telemetry.")
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None:
        raise RuntimeError("OpenAI response missing usage.prompt_tokens; cannot record token telemetry.")
    if completion_tokens is None:
        raise RuntimeError("OpenAI response missing usage.completion_tokens; cannot record token telemetry.")
    return text, {
        "latency_ms": latency_ms,
        "tokens_in": int(prompt_tokens),
        "tokens_out": int(completion_tokens),
    }


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


def check(rule: Rule, target: str | Path | TargetSpec) -> Diagnostic:
    """Evaluate a semantic-rubric rule against *target*.

    When no provider is configured (or the env file is absent), returns
    ``UNAVAILABLE`` with ``provider_unconfigured`` evidence. When a provider is
    configured, dispatches to the configured provider and maps the response to
    ``pass``/``fail`` with structured ``llm_judgment`` evidence (see
    ``LlmJudgment``). Provider errors and unparseable responses map to
    ``UNAVAILABLE`` with ``provider_error`` evidence — never to a crash,
    ``pass``, or ``fail``.

    Multi-target inputs (``TargetSpec`` with ``is_multi=True``) are not
    supported by this backend in the first slice (issue #146); the call
    returns ``UNSUPPORTED`` with a ``multi_target_unsupported`` evidence
    record so callers can see that targets were silently dropped is *not*
    happening — content assembly is deferred to a follow-up. Single-file
    ``TargetSpec`` values are unwrapped to the underlying path so callers
    can mix CLI surfaces freely.

    On success the ``evidence[0].data`` dict contains:

    - ``model``: the model identifier used.
    - ``prompt_version``: ``PROMPT_VERSION`` constant (for #68 reproducibility).
    - ``judgment``: ``"pass"`` or ``"fail"``.
    - ``primary_reason``: one-sentence summary.
    - ``supporting_evidence_quotes``: list of verbatim quotes.
    - ``suggested_action``: remediation string (fail only) or ``None`` (pass).
    - ``latency_ms``: wall-clock provider call duration in integer
      milliseconds (#76).
    - ``tokens_in``: provider-reported prompt token count (#76).
    - ``tokens_out``: provider-reported completion token count (#76).
    - ``cost_estimate_usd``: estimated USD cost based on static per-model
      pricing snapshot (#133); ``None`` for unknown models.

    ``Diagnostic.remediation`` is set to ``suggested_action`` on fail.
    """
    if isinstance(target, TargetSpec):
        if target.is_multi:
            return Diagnostic(
                rule_id=rule.id,
                source=rule.source,
                backend=Backend.LLM_RUBRIC,
                status=Status.UNSUPPORTED,
                severity=rule.severity,
                message=(
                    "llm-rubric backend does not support multi-target evaluation; "
                    "content assembly is deferred to a follow-up."
                ),
                evidence=[
                    Evidence(
                        kind="multi_target_unsupported",
                        data={
                            "backend": "llm-rubric",
                            "raw_targets": list(target.raw_targets),
                            "file_count": len(target.paths),
                        },
                    )
                ],
            )
        # Single-file spec: unwrap so the rest of the function operates on
        # the underlying path exactly as it did before #146.
        target = target.paths[0] if target.paths else ""

    rubric_input = _build_rubric_input(rule, target)

    # Single-snapshot read: load the dotenv once and drive the configured
    # check, provider lookup, and model resolution all from the same dict.
    # Reading twice would let a concurrent dotenv edit split the decision
    # (e.g. configured=True at first read, provider=None at second read).
    env = _load_env_file()
    if not _is_configured(env):
        return _unavailable_unconfigured(rule, rubric_input)

    provider = env["GATE_KEEPER_LLM_PROVIDER"]
    model = _resolve_model(provider, env)
    system, user = _build_prompt(rule, target)

    try:
        if provider == "anthropic":
            response_text, telemetry = _call_anthropic(env["ANTHROPIC_API_KEY"], system, user, model)
        else:
            response_text, telemetry = _call_openai(env["OPENAI_API_KEY"], system, user, model)
    except Exception as exc:  # noqa: BLE001 — fail-closed: any provider error → unavailable
        return _unavailable_provider_error(rule, rubric_input, provider, type(exc).__name__, str(exc))

    parsed = _parse_llm_judgment(response_text)
    if isinstance(parsed, LlmJudgmentParseError):
        return _unavailable_provider_error(
            rule, rubric_input, provider, "unparseable_response", parsed.detail
        )

    # Invariant: when control reaches here the provider helper completed without
    # raising, so telemetry must contain all three required keys (#76 fail-closed
    # contract — helpers raise when usage is missing rather than defaulting).
    # Assert explicitly so a future helper bug surfaces as a programmer error
    # instead of a silent KeyError below.
    assert telemetry.keys() >= {"latency_ms", "tokens_in", "tokens_out"}, (
        f"provider helper returned incomplete telemetry: {sorted(telemetry.keys())}"
    )

    status = Status.PASS if parsed.judgment == "pass" else Status.FAIL
    cost = _estimate_cost(model, telemetry["tokens_in"], telemetry["tokens_out"])
    evidence = Evidence(
        kind="llm_judgment",
        data={
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "judgment": parsed.judgment,
            "primary_reason": parsed.primary_reason,
            "supporting_evidence_quotes": parsed.supporting_evidence_quotes,
            "suggested_action": parsed.suggested_action,
            "latency_ms": telemetry["latency_ms"],
            "tokens_in": telemetry["tokens_in"],
            "tokens_out": telemetry["tokens_out"],
            "cost_estimate_usd": cost,
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


# ---------------------------------------------------------------------------
# Reproducibility (#68)
# ---------------------------------------------------------------------------


def run_n(rule: Rule, target: str | Path, n: int) -> Diagnostic:
    """Evaluate *rule* against *target* ``n`` times and aggregate the result.

    Reproducibility metric (#68): runs ``check`` ``n`` times, then aggregates the
    structured ``pass`` / ``fail`` outcomes via simple majority vote. Ties break
    toward ``fail`` (fail-closed). The returned diagnostic is the first run that
    matches the majority judgment, with an additional
    ``Evidence(kind="reproducibility_score", ...)`` appended carrying:

    - ``score``: agreement rate of the majority judgment, in ``[0.0, 1.0]``;
    - ``n``: the requested number of runs;
    - ``pass_count``: how many runs returned ``pass``;
    - ``majority_judgment``: ``"pass"`` or ``"fail"``.

    Behaviour rules
    ---------------
    - ``n == 1`` is equivalent to ``check`` (no extra evidence appended).
    - ``n < 1`` raises ``ValueError`` (caller must validate).
    - If *any* run returns a non-pass/fail diagnostic (``UNAVAILABLE`` /
      ``ERROR``), aggregation is abandoned and that diagnostic is returned
      unchanged - fail-closed for unconfigured / error states.
    """
    if n < 1:
        raise ValueError(f"reproducibility n must be >= 1, got {n}")

    if n == 1:
        return check(rule, target)

    diagnostics: list[Diagnostic] = []
    for _ in range(n):
        diag = check(rule, target)
        # Fail-closed on non-deterministic outcomes: if any run is unavailable
        # or errored, don't synthesise a misleading reproducibility score.
        if diag.status not in (Status.PASS, Status.FAIL):
            return diag
        diagnostics.append(diag)

    pass_count = sum(1 for d in diagnostics if d.status is Status.PASS)
    fail_count = n - pass_count
    # Tie-break toward fail (fail-closed).
    majority_is_pass = pass_count > fail_count
    majority_judgment = "pass" if majority_is_pass else "fail"
    majority_count = pass_count if majority_is_pass else fail_count
    score = majority_count / n

    # Pick the first diagnostic whose status matches the majority judgment so the
    # rendered message and llm_judgment evidence are consistent with the score.
    target_status = Status.PASS if majority_is_pass else Status.FAIL
    representative = next(d for d in diagnostics if d.status is target_status)

    repro_evidence = Evidence(
        kind="reproducibility_score",
        data={
            "score": score,
            "n": n,
            "pass_count": pass_count,
            "majority_judgment": majority_judgment,
        },
    )
    return dataclasses.replace(
        representative,
        evidence=[*representative.evidence, repro_evidence],
    )
