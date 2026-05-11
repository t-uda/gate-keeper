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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status, TargetKind
from gate_keeper.targets import TargetSpec

name = "llm-rubric"

# ---------------------------------------------------------------------------
# Strategy registry (#183)
#
# Strategy ids surface in the rule IR via ``rule.params["strategy"]``. The
# default ``single`` preserves the pre-#183 behaviour byte-for-byte (one
# provider call, one parsed judgment, one diagnostic) and is the only
# concrete strategy shipped in this slice. ``consensus`` / ``review`` /
# ``adaptive`` are reserved ids — declared in :data:`KNOWN_STRATEGIES` so
# the rule IR can validate the value, but invoking them returns
# ``UNAVAILABLE`` with ``strategy_not_implemented`` evidence so callers
# discover the gap explicitly rather than silently falling back to
# ``single``. Higher-effort strategies are opt-in and experimental until
# benchmarked; the seam exists so they can be plugged in without
# duplicating provider-call code, not as a green-light to enable them in
# CI.
# ---------------------------------------------------------------------------

#: All strategy ids the IR layer recognises. Only ``single`` has a concrete
#: implementation in this slice; the others are seam placeholders that
#: dispatch to ``UNAVAILABLE`` with ``strategy_not_implemented`` evidence.
KNOWN_STRATEGIES: frozenset[str] = frozenset({"single", "consensus", "review", "adaptive"})

#: Default strategy id when ``rule.params["strategy"]`` is unset. Preserves
#: the pre-#183 single-call behaviour as the backward-compatible default.
DEFAULT_STRATEGY: Literal["single"] = "single"


@dataclass(frozen=True)
class JudgmentRequest:
    """Inputs every strategy receives (#183 seam).

    Strategies operate on this immutable request rather than the raw
    ``check`` arguments so the seam stays narrow and additive — future
    strategy ids can read the same fields without changing the public
    ``check`` signature. Multi-target inputs are unwrapped at the
    dispatcher boundary; strategies see a single ``target`` and never
    have to handle ``TargetSpec``.
    """

    rule: Rule
    target: str | Path
    artifact_kind: TargetKind | None = None


@dataclass(frozen=True)
class StrategyTelemetry:
    """Aggregated per-strategy telemetry recorded into evidence (#183).

    The ``single`` strategy fills these from one provider call. Future
    multi-call strategies (``consensus`` / ``review`` / ``adaptive``)
    aggregate across calls — ``call_count`` rises above 1, ``models``
    can carry distinct entries, ``cost_estimate_usd_total`` and
    ``latency_ms_total`` sum across calls. ``cost_estimate_usd_total``
    is ``None`` iff any individual call had unknown pricing (preserving
    the existing fail-closed semantics for ``cost_estimate_usd``).
    """

    strategy: str
    call_count: int
    models: list[str] = field(default_factory=list)
    cost_estimate_usd_total: float | None = None
    latency_ms_total: int = 0


class Strategy(Protocol):
    """Strategy seam for LLM-rubric judgment (#183).

    A strategy receives a fully-resolved :class:`JudgmentRequest` and
    returns a :class:`Diagnostic` ready for the caller. The seam exists
    so consensus / review / adaptive can plug in without duplicating
    provider-call helpers, parser code, or evidence construction.

    Strategies own their own provider-call budget, retry policy, and
    aggregation logic. They MUST surface ``llm_strategy``,
    ``llm_call_count``, ``models``, ``prompt_version``,
    ``cost_estimate_usd_total``, and ``latency_ms_total`` on the success
    evidence dict so downstream consumers can observe strategy effort
    uniformly. The ``single`` strategy is the reference implementation.
    """

    def __call__(self, request: JudgmentRequest) -> Diagnostic: ...


DOTENV_PATH = Path("/home/vscode/.config/hermes-projects/gate-keeper.env")

ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

_SUPPORTED_PROVIDERS = ("anthropic", "openai")

# Prompt versioning constant — referenced by issue #68 (reproducibility).
#
# History:
# - v1 (#67): initial structured-judgment schema.
# - v2 (#168): tighten ``supporting_evidence_quotes`` constraints to require
#   non-empty grounding on every verdict (pass and fail), near-verbatim
#   substrings of the artifact text, and representative coverage rather than
#   first-line bias.
# - v3 (#169): when a rule carries ``target_kind`` (PR description / commit
#   message / issue body / documentation / code change), inject an artifact-
#   kind sentence into the prompt and authorise an ``"unsupported"`` verdict
#   for the target-kind-mismatch case so the model can decline rather than
#   parrot the rule's wording onto the wrong artifact.
# - v4 (#175): ground ``target_kind`` more strongly in the prompt. v3 leaked
#   "PR descriptions"/"commit message" into both the artifact-kind block's
#   illustrative example and the canned ``unsupported`` response example,
#   which gpt-4o-mini parroted verbatim regardless of the rule's actual
#   ``target_kind`` annotation. v4 (a) names the rule's annotated kind in
#   the artifact-kind block and instructs the model to echo it back when
#   declining, (b) replaces the hardcoded "PR descriptions / commit
#   message" example with a kind-neutral schema illustration, and (c) adds
#   a checklist step requiring the model to identify which artifact kind
#   the rule's predicate actually targets before deciding.
#
# The ``PROMPT_VERSION`` constant is **not** bumped for #191. The rendered
# template body (schema, instructions, constraints, examples) is unchanged;
# the only change is what string fills the ``Target reference`` slot — when
# the caller declared an ``--artifact-kind`` and the target resolves to a
# real file, the prompt now carries the file *content* rather than the
# file *path* (and the substring grounding check follows the same
# substitution). Reproducibility records keyed on ``prompt_version``
# continue to mean the same thing.
PROMPT_VERSION = "v4"

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
# Structured LLM judgment schema (#67, migrated to Pydantic #187)
# ---------------------------------------------------------------------------


class LlmJudgment(BaseModel):
    """Structured output schema for a single LLM rubric evaluation.

    Fields
    ------
    judgment:
        ``"pass"``, ``"fail"``, or ``"unsupported"`` (#169). The
        ``"unsupported"`` verdict is reserved for the target-kind-mismatch
        case: when the rule carries an explicit ``target_kind`` annotation
        and the artifact provided is a different kind, the model returns
        ``"unsupported"`` rather than parroting the rule's wording onto an
        artifact the rule does not address. The backend maps this to
        :data:`Status.UNSUPPORTED` with ``evidence.kind=target_kind_mismatch``.
    primary_reason:
        One-sentence summary of why the target passed, failed, or was
        rejected as unsupported.
    supporting_evidence_quotes:
        Non-empty list of near-verbatim substrings of the target text that
        ground the judgment. Required (non-empty) for both ``"pass"`` and
        ``"fail"`` (#168). For ``"unsupported"`` verdicts the list may be
        empty: when the rule does not address the artifact kind, no
        artifact substring exists that could ground a verdict (#169).
        Quotes must be drawn from the artifact, not paraphrases of
        ``primary_reason``, and at least one entry should reflect the
        strongest evidence for or against the rule's predicate rather than
        only the artifact's opening lines.
    suggested_action:
        Concrete remediation step. Required on fail; MUST be ``None`` on
        pass and on unsupported.
    """

    model_config = ConfigDict(frozen=True)

    judgment: Literal["pass", "fail", "unsupported"]
    primary_reason: str
    supporting_evidence_quotes: list[str]
    suggested_action: str | None

    @field_validator("primary_reason")
    @classmethod
    def _primary_reason_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("primary_reason must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _cross_field_constraints(self) -> "LlmJudgment":
        # pass and fail must have at least one grounding quote (#168).
        if self.judgment in ("pass", "fail") and len(self.supporting_evidence_quotes) == 0:
            raise ValueError(
                "supporting_evidence_quotes must contain at least one entry"
                f" when judgment is {self.judgment!r}"
            )
        # suggested_action must be None on pass/unsupported.
        if self.judgment in ("pass", "unsupported") and self.suggested_action is not None:
            raise ValueError("suggested_action must be None when judgment is not 'fail'")
        # suggested_action must be a non-empty string on fail.
        if self.judgment == "fail" and (self.suggested_action is None or not self.suggested_action.strip()):
            raise ValueError("suggested_action must be a non-empty string when judgment is 'fail'")
        return self


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

# Brief description of each artifact kind, injected into the prompt so the
# model can recognise the target-kind-mismatch case (#169). Kept terse — the
# prompt grows linearly with this dict, and a long taxonomy crowds the
# rubric-evaluation instructions for no marginal gain.
_TARGET_KIND_DESCRIPTIONS: dict[TargetKind, str] = {
    TargetKind.PR_DESCRIPTION: (
        "the body / description of a pull request — narrative prose written to "
        "explain the change to reviewers"
    ),
    TargetKind.COMMIT_MESSAGE: (
        "a Git commit message — typically a short subject line and an optional body explaining the change"
    ),
    TargetKind.ISSUE_BODY: (
        "the body of a GitHub issue — narrative prose describing a bug, feature request, or investigation"
    ),
    TargetKind.DOCUMENTATION: (
        "documentation prose — README, design note, runbook, or reference material intended for human readers"
    ),
    TargetKind.CODE_CHANGE: (
        "source code or a code diff — programming-language text rather than narrative prose"
    ),
}


def _render_target_kind_block(target_kind: TargetKind) -> str:
    """Return the optional artifact-kind block injected before ``## Instructions`` (#169, #175).

    Returns the empty string when *target_kind* is :data:`TargetKind.UNSPECIFIED`
    so the artifact-kind *block* is omitted for unannotated rules — only
    annotated rules receive kind-specific guidance. The surrounding
    template still differs from v2 in places that always render (the
    schema names ``"unsupported"``, the constraints reference it), so v4
    is not byte-identical to v2 globally; see ``_build_prompt`` for the
    full byte-equivalence picture. When set, this function returns a
    block that:

    1. Names the rule's annotated kind (``rule.target_kind``).
    2. Instructs the model to identify whether the artifact above is the
       same kind the rule targets.
    3. Requires the model, when declining as ``"unsupported"``, to echo
       the rule's annotated kind back in ``primary_reason`` so the verdict
       is grounded in the rule's annotation rather than a parroted
       canned phrase (#175 — gpt-4o-mini regression at v3 where every
       mismatch claimed "rule addresses PR descriptions" regardless of
       the rule's actual annotation).
    """
    if target_kind is TargetKind.UNSPECIFIED:
        return ""
    description = _TARGET_KIND_DESCRIPTIONS.get(target_kind, "")
    descriptor = f"`{target_kind.value}`"
    if description:
        descriptor = f"{descriptor} ({description})"
    rule_kind_value = target_kind.value
    return (
        "\n## Artifact kind\n\n"
        f"This rule is annotated `target_kind: {rule_kind_value}` — its "
        f"premise is intended to apply to {descriptor}.\n\n"
        "Decide first whether the **target artifact above** is itself a "
        f"`{rule_kind_value}`. If it is, evaluate the rule normally and "
        'return `"pass"` or `"fail"`. If the artifact is some other kind '
        "(for example, the rule's annotation says it applies to one "
        "artifact kind but the artifact above is a different kind), "
        'return `"unsupported"` instead of rendering a verdict. When you '
        'return `"unsupported"`, your `primary_reason` MUST quote the '
        f"rule's annotated kind (`{rule_kind_value}`) verbatim — for "
        f'example: "The rule is annotated `{rule_kind_value}` but the '
        'artifact provided is a <kind-of-artifact-above>." Do not parrot '
        "the rule's wording onto an artifact the rule does not address, "
        "and do not invent a target_kind value the rule does not claim.\n"
    )


# Optional unsupported-example block. Rendered only when ``rule.target_kind``
# is annotated (#175). At v3, the canned example was always present and
# hardcoded "PR descriptions ... commit message" wording that gpt-4o-mini
# parroted verbatim regardless of the rule's annotation. v4 (a) replaces the
# canned wording with kind-neutral placeholders and (b) gates the example on
# annotation so an unannotated rule never sees an ``unsupported`` shape — at
# v3 some unannotated rules (notably ``completeness-05-rule-doc-has-target-cue``)
# saw the schema's ``unsupported`` option and emitted a stray ``unsupported``
# verdict that the backend then degrades to ``provider_error /
# unsupported_without_target_kind``. Gating the example removes that lure.
_UNSUPPORTED_EXAMPLE_BLOCK = """\

An unsupported verdict — the rule's premise does not apply to this artifact kind.
Use this shape only when the "Artifact kind" block above declares the rule's
target_kind and the target artifact is a different kind. In your response,
replace `<RULE_KIND>` with the literal `target_kind` value the "Artifact kind"
block names for this rule (quote the value verbatim), and replace
`<ARTIFACT_KIND>` with what the target artifact actually is:

{
  "judgment": "unsupported",
  "primary_reason": "The rule is annotated `<RULE_KIND>` but the artifact provided is a <ARTIFACT_KIND>.",
  "supporting_evidence_quotes": [],
  "suggested_action": null
}
"""


def _render_unsupported_example_block(target_kind: TargetKind) -> str:
    """Return the unsupported-example block iff *target_kind* is annotated (#175)."""
    if target_kind is TargetKind.UNSPECIFIED:
        return ""
    return _UNSUPPORTED_EXAMPLE_BLOCK


def _render_unsupported_instruction(target_kind: TargetKind) -> str:
    """Return the artifact-kind-dispatch Instructions step iff *target_kind* is annotated (#175).

    Without an annotation there is no Artifact kind block, so referencing
    one in Instructions confuses the model. Skipping the step keeps the
    unannotated-rule prompt focused on pass/fail. The step is rendered
    as a bullet (no numeral) so the surrounding numbered list stays
    contiguous regardless of whether this block is included — a numbered
    "5." that disappears for unannotated rules would leave a 1, 2, 3, 4,
    6 sequence the model might read as a typo.
    """
    if target_kind is TargetKind.UNSPECIFIED:
        return ""
    return (
        "- Identify whether the target artifact matches the rule's annotated `target_kind`. "
        'If it does not, return `"unsupported"` rather than rendering a verdict, and quote '
        "the rule's annotated `target_kind` value verbatim in `primary_reason` — do not "
        "substitute a different kind name (#175).\n"
    )


RUBRIC_PROMPT_TEMPLATE = """\
You are a rubric evaluator. Your sole task is to judge whether the target \
artifact satisfies the given rule.

## Rule

{rule_text}

## Target reference

{target}
{target_kind_block}
## Instructions

1. Read the rule carefully. It describes a quality requirement.
2. Read the entire target text — not only the opening lines. The strongest
   evidence for or against the rule is often in the middle or later
   sections (rationale paragraphs, body content, trailing details).
3. Judge whether the target (identified by the reference above) satisfies it.
4. If you cannot read the target's content directly, judge from the reference alone.
{unsupported_instruction}\
5. Respond with **only** a JSON object that matches the schema below — no prose outside the JSON.

## Required response schema

{{
  "judgment": "pass" | "fail" | "unsupported",
  "primary_reason": "<one sentence>",
  "supporting_evidence_quotes": ["<near-verbatim substring of the target>", ...],
  "suggested_action": "<concrete step to fix>" | null
}}

Constraints:
- `judgment` must be exactly `"pass"`, `"fail"`, or `"unsupported"`.
- `"unsupported"` is reserved for the target-kind-mismatch case and is
  ONLY valid when an `## Artifact kind` block is present above. If no
  `## Artifact kind` block is rendered, return `"pass"` or `"fail"` only.
- `primary_reason` must be a single sentence (no newlines).
- `supporting_evidence_quotes` must contain **at least one entry** for
  **every pass or fail verdict**. An empty list is invalid for `"pass"`
  and `"fail"`. For an `"unsupported"` verdict the list may be empty:
  when the rule does not address the artifact kind, no artifact
  substring exists that could ground a verdict.
- Each quote must be a **near-verbatim substring of the target text** —
  copy the words from the artifact. Do **not** paraphrase
  `primary_reason`, do **not** invent meta-statements about the artifact,
  and do **not** quote the rule text. Minor whitespace or capitalisation
  normalisation is acceptable; the test is whether a reader could locate
  the quoted phrase in the artifact by ordinary search.
- At least one quote must be **representative of the strongest evidence**
  for or against the rule's predicate. When the artifact is long, do not
  cite only the opening line or the subject; cite the body / rationale /
  trailing content where the rule's predicate is most clearly satisfied
  or violated.
- The grounding requirement does not raise the bar for passing — if the
  artifact plainly satisfies the rule, return `"pass"` and quote the
  passage that demonstrates it.
- `suggested_action` must be a non-empty string when `judgment` is `"fail"`;
  must be `null` when `judgment` is `"pass"` or `"unsupported"`.

## Examples of valid responses

A passing verdict, grounded in the artifact:

{{
  "judgment": "pass",
  "primary_reason": "The body explains the motivation by naming the failure mode and the reproducer.",
  "supporting_evidence_quotes": [
    "Discovered by the umbrella #164 dogfood orchestrator: 4 of 10 validate runs in tick 1 crashed"
  ],
  "suggested_action": null
}}

A failing verdict, grounded in the artifact:

{{
  "judgment": "fail",
  "primary_reason": "The commit body restates the subject without explaining motivation.",
  "supporting_evidence_quotes": [
    "This commit fixes the bug. See the diff for details. Tests updated accordingly."
  ],
  "suggested_action": "Add a paragraph naming the failure mode and why this fix is correct."
}}{unsupported_example_block}\
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

    Keys: ``rule_text``, ``rule_kind``, ``target``, and (when set on the
    rule) ``target_kind`` so the artifact-kind annotation surfaces in
    ``provider_unconfigured`` / ``provider_error`` evidence the same way it
    surfaces in successful ``llm_judgment`` evidence.
    """
    payload: dict[str, Any] = {
        "rule_text": rule.text,
        "rule_kind": rule.kind.value,
        "target": str(target),
    }
    if rule.target_kind is not TargetKind.UNSPECIFIED:
        payload["target_kind"] = rule.target_kind.value
    return payload


def _resolve_artifact_input(
    target: str | Path,
    artifact_kind: TargetKind | None,
) -> str:
    """Return the string to render into the prompt's ``Target reference`` block (#191).

    When *artifact_kind* is declared by the caller (``--artifact-kind`` on
    the CLI surface) **and** *target* refers to a real file on disk, return
    the file's text content so the model sees the artifact body itself
    rather than the path string. The declared kind already tells the model
    what kind of artifact this is — leaking the filename adds no signal and
    actively misleads the model on file-path callers. (Issue #191:
    gpt-4o-mini at v4 parroted ``target-03-commit.txt`` as the artifact
    kind regardless of the declared ``--artifact-kind=commit_message``.)

    When *artifact_kind* is ``None`` (caller omitted the flag), preserve
    the legacy v4 behaviour byte-for-byte: return ``str(target)`` so the
    prompt's ``Target reference`` block carries whatever the caller passed
    in (path string for path targets, inline content for inline targets).
    The legacy prompt-level fallback in #169/#175 is still responsible for
    declining or evaluating in that path.

    When *target* is a non-existent path (or any string that does not
    resolve to a file), return ``str(target)`` regardless of
    *artifact_kind*: there is no file body to substitute, and the model
    will judge from the reference alone per the existing instruction
    ("If you cannot read the target's content directly, judge from the
    reference alone."). A read error (permission denied, decode failure,
    …) also falls back to ``str(target)`` so the behaviour stays
    fail-open at the prompt-builder level — the substring fabrication
    validator in :func:`_resolve_artifact_text` mirrors the same
    substitution so a successful content load is observable in evidence
    even on this branch.

    Note: the helper accepts both ``str`` and ``Path`` because
    :func:`check` is called with whatever the validator forwards. The
    ``str`` branch is the common one (``--target /path/to/file`` ⇒ str);
    we coerce to ``Path`` locally before stat-ing.
    """
    if artifact_kind is None:
        return str(target)
    # Coerce to Path for the existence/read step. Construction itself
    # cannot raise for any reasonable input; ``is_file`` may raise
    # ``OSError`` on filesystems that reject the byte-string (matches
    # the CLI's defensive handling in cli.py).
    try:
        path = target if isinstance(target, Path) else Path(str(target))
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # Fail-open at the prompt-builder level: legacy
                # path-string behaviour is the safe fallback; the
                # diagnostic remains observable via the regular
                # provider response path.
                return str(target)
    except OSError:
        # ``is_file`` itself rejected the path (e.g. ENAMETOOLONG when
        # someone passes a long inline string as --target). Fall back.
        pass
    return str(target)


def _build_prompt(
    rule: Rule,
    target: str | Path,
    artifact_kind: TargetKind | None = None,
) -> tuple[str, str]:
    """Render the system + user messages for *rule* against *target* (#169 / #175 / #191).

    When ``rule.target_kind`` is :data:`TargetKind.UNSPECIFIED` three
    target-kind-related blocks render to the empty string:

    - the ``## Artifact kind`` block (added in v3),
    - the ``unsupported``-handling Instructions step 5 (v4),
    - the ``unsupported`` example response under
      ``## Examples of valid responses`` (v4).

    The remaining template still differs from v2 (the schema's
    ``"judgment"`` line names ``"unsupported"`` and the
    ``supporting_evidence_quotes`` constraint mentions the ``unsupported``
    case), so v4 is **not** byte-identical to v2 even for unannotated
    rules — but the unannotated prompt has no kind-specific guidance,
    matching the v3 design intent and avoiding the v4-regression where
    an unannotated rule (e.g. ``completeness-05-rule-doc-has-target-cue``)
    saw the canned ``unsupported`` example and emitted a stray
    ``unsupported`` verdict that the backend then degraded to
    ``provider_error / unsupported_without_target_kind``.

    The optional *artifact_kind* parameter (#191) selects what fills the
    ``Target reference`` slot. When the caller declares an
    ``--artifact-kind`` and *target* resolves to a real file, the slot
    receives the file content — not the path string — so gpt-4o-mini
    cannot latch onto the filename as a substitute for the declared kind.
    When ``artifact_kind is None`` the legacy ``str(target)`` rendering is
    preserved byte-for-byte (the prompt-level v4 fallback continues to
    apply). See :func:`_resolve_artifact_input` for the substitution
    rules.
    """
    system = RUBRIC_SYSTEM_PROMPT
    user = RUBRIC_PROMPT_TEMPLATE.format(
        rule_text=rule.text,
        target=_resolve_artifact_input(target, artifact_kind),
        target_kind_block=_render_target_kind_block(rule.target_kind),
        unsupported_instruction=_render_unsupported_instruction(rule.target_kind),
        unsupported_example_block=_render_unsupported_example_block(rule.target_kind),
    )
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


# Regex that extracts the body of a fenced code block (``` or ```json).
#
# gpt-4o-mini occasionally wraps its JSON response in a markdown code fence
# despite the prompt instructing it to return *only* a JSON object (#194).
# This pattern matches:
#
#     ```json
#     { ... }
#     ```
#
# or the plain-fence variant (no language tag):
#
#     ```
#     { ... }
#     ```
#
# The regex is used as a fallback in _parse_llm_judgment: if the raw
# response is not valid JSON, we strip any surrounding code fence and retry.
_CODE_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(.*?)\n\s*```",
    re.DOTALL,
)


def _extract_json_candidate(text: str) -> str | None:
    """Return the first code-fenced body from *text*, or ``None``.

    Used as a secondary extraction pass inside :func:`_parse_llm_judgment`
    when the primary ``json.loads`` fails (#194 — gpt-4o-mini sometimes
    wraps its JSON response in a markdown code fence).  Returns the inner
    block's text (stripped) so the caller can retry ``json.loads``; returns
    ``None`` when no fence is found, letting the caller surface the original
    ``invalid_json`` error.
    """
    m = _CODE_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _parse_llm_judgment(text: str) -> LlmJudgment | LlmJudgmentParseError:
    """Parse and validate a raw model response string into ``LlmJudgment``.

    Returns ``LlmJudgmentParseError`` (never raises) for any failure.
    Extra fields in the JSON are silently ignored.

    When the primary ``json.loads`` fails (e.g. because the model wrapped
    the JSON in a markdown code fence), a secondary extraction pass via
    :func:`_extract_json_candidate` strips the fence and retries (#194).
    If the secondary pass also fails, the error from the original text is
    returned so the ``raw_response_excerpt`` always refers to what the
    model actually sent.
    """
    excerpt = text[:200]

    if not text.strip():
        return LlmJudgmentParseError(
            failure_mode="empty_response",
            detail="Model returned an empty string.",
            raw_response_excerpt=excerpt,
        )

    # Primary parse: try the raw response directly.
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        # Secondary pass: strip a surrounding code fence and retry (#194).
        candidate = _extract_json_candidate(text)
        if candidate is not None:
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError as exc2:
                # Both passes failed; report the secondary error for accuracy.
                return LlmJudgmentParseError(
                    failure_mode="invalid_json",
                    detail=f"Response is not valid JSON (code-fence extraction also failed): {exc2}",
                    raw_response_excerpt=excerpt,
                )
        else:
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
    for required_field in ("judgment", "primary_reason", "supporting_evidence_quotes"):
        if required_field not in obj:
            return LlmJudgmentParseError(
                failure_mode="missing_field",
                detail=f"Required field '{required_field}' is absent.",
                raw_response_excerpt=excerpt,
            )

    judgment = obj["judgment"]
    if judgment not in ("pass", "fail", "unsupported"):
        return LlmJudgmentParseError(
            failure_mode="invalid_judgment_value",
            detail=f"judgment must be 'pass', 'fail', or 'unsupported', got {judgment!r}.",
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

    # #168 — both pass and fail must be grounded by at least one quote.
    # #169 — `unsupported` may carry an empty list: when the rule's premise
    # does not address the artifact kind, no artifact substring exists that
    # could ground a verdict.
    if judgment in ("pass", "fail") and len(quotes) == 0:
        return LlmJudgmentParseError(
            failure_mode="missing_field",
            detail=(
                f"supporting_evidence_quotes must contain at least one entry when judgment is {judgment!r}."
            ),
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
        # pass / unsupported — suggested_action must be None/absent
        suggested_action = None

    try:
        return LlmJudgment(
            judgment=judgment,
            primary_reason=primary_reason,
            supporting_evidence_quotes=quotes,
            suggested_action=suggested_action,
        )
    except ValidationError as exc:
        return LlmJudgmentParseError(
            failure_mode="schema_validation_failed",
            detail=str(exc),
            raw_response_excerpt=excerpt,
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
# Quote-fabrication validator (#172)
#
# The v2 prompt (#168) instructs the model that every quote in
# ``supporting_evidence_quotes`` must be drawn as a near-verbatim substring of
# the artifact text. Tick 6 of umbrella #164's dogfood loop (issue #172) showed
# that prompt-side instruction alone is insufficient: on the first post-merge
# sample, the model emitted six fail verdicts whose quotes had **zero overlap**
# with the artifact (generic "fail-shaped" placeholder strings). The parser
# below enforces the contract: when any quote is not a substring of the
# artifact, the verdict is rejected and converted to ``UNSUPPORTED`` with
# ``evidence.kind=llm_quote_fabrication`` so downstream callers can see that
# the model violated the grounding contract rather than acting on a verdict
# whose evidence is fabricated.
# ---------------------------------------------------------------------------


# Smart-quote ↔ ASCII-quote folding table. We accept the model lightly
# normalising punctuation when copying from the artifact (e.g. the artifact
# carries U+2019 RIGHT SINGLE QUOTATION MARK but the model emits a plain
# ASCII apostrophe). We do NOT accept paraphrasing or rewording.
_QUOTE_FOLDING: dict[int, str] = {
    ord("‘"): "'",  # LEFT SINGLE QUOTATION MARK
    ord("’"): "'",  # RIGHT SINGLE QUOTATION MARK
    ord("“"): '"',  # LEFT DOUBLE QUOTATION MARK
    ord("”"): '"',  # RIGHT DOUBLE QUOTATION MARK
    ord("′"): "'",  # PRIME
    ord("″"): '"',  # DOUBLE PRIME
    ord("–"): "-",  # EN DASH
    ord("—"): "-",  # EM DASH
}


def _normalise_for_substring(text: str) -> str:
    """Return *text* normalised for substring containment checks.

    Folds smart quotes and dashes to ASCII counterparts and collapses runs of
    whitespace (including newlines) to single spaces. The result is tolerant
    enough that a quote copied with cosmetic differences (line wrapping,
    curly-quote rendering) still matches; it is **not** tolerant of paraphrase.
    """
    folded = text.translate(_QUOTE_FOLDING)
    # Collapse any run of whitespace (spaces, tabs, newlines) into a single
    # space. ``re`` is imported at module scope; using ``\\s+`` is correct
    # because the ``re`` regex flavour already treats CR/LF/TAB as whitespace.
    collapsed = re.sub(r"\s+", " ", folded)
    return collapsed.strip()


def _resolve_artifact_text(
    target: str | Path,
    artifact_kind: TargetKind | None = None,
) -> str:
    """Return the artifact text used for substring validation.

    Returns the **same string the prompt template renders into the
    ``Target reference`` block** via ``_build_prompt``. The substring
    check must be performed against what the model actually saw, not
    against any out-of-band file contents.

    Concretely (#191 update):

    - When *artifact_kind* is ``None`` (caller did not declare
      ``--artifact-kind``): legacy behaviour. Returns ``str(target)`` —
      the prompt also renders ``str(target)``, so quote substrings are
      checked against the path / inline string the model actually saw.
    - When *artifact_kind* is declared and *target* resolves to a real
      file: the prompt now renders the file content (#191), so this
      function returns the same content. Quotes must be substrings of
      the file body, matching what the model saw.
    - When *artifact_kind* is declared but *target* is inline / a
      non-existent path: ``_resolve_artifact_input`` falls back to
      ``str(target)`` and so does this function — they stay in lockstep.

    Pre-existing properties preserved:

    - For inline-string targets (the dogfood case ``--target "<PR body>"``)
      the model receives the body inline; quotes must be substrings of it.
    - The bench harness pre-resolves path targets to file contents before
      invoking ``check`` (``bench.run_entry`` ⇒ ``Target.resolve``), so for
      bench callers the target string is already the inline content; no
      special-casing is required here.

    Reading file contents off-band when *artifact_kind* is **not** set
    would diverge from what the model sees and would force every
    legitimate verdict on a path target into a false
    ``llm_quote_fabrication`` rejection (Codex review on PR #173). The
    #191 substitution is gated on *artifact_kind* precisely so the
    no-flag legacy path stays untouched.
    """
    return _resolve_artifact_input(target, artifact_kind)


def _find_fabricated_quotes(quotes: list[str], artifact_text: str) -> list[str]:
    """Return quotes from *quotes* that are NOT substrings of *artifact_text*.

    Substring check is performed on the ``_normalise_for_substring`` form of
    both inputs so cosmetic whitespace and smart-quote differences do not
    cause false positives. An empty quote string is treated as fabricated
    (a degenerate / zero-grounding case).
    """
    normalised_artifact = _normalise_for_substring(artifact_text)
    fabricated: list[str] = []
    for quote in quotes:
        if not isinstance(quote, str) or not quote.strip():
            fabricated.append(quote if isinstance(quote, str) else "")
            continue
        if _normalise_for_substring(quote) not in normalised_artifact:
            fabricated.append(quote)
    return fabricated


# ---------------------------------------------------------------------------
# Span resolution for validated quotes (#179)
#
# Once ``_find_fabricated_quotes`` confirms every quote is a normalised
# substring of the artifact, ``_resolve_quote_spans`` locates the **first
# match** of each quote inside the original (un-normalised) artifact text and
# returns deterministic span metadata: character offsets and 1-indexed
# line / column ranges, plus a ``match_normalisation`` tag identifying which
# tolerance step was needed (``"exact"`` / ``"whitespace"`` /
# ``"smart_quotes"``).
#
# Design choices:
#
# - **Character offsets, not byte offsets.** Python strings index by code
#   point and the artifact_text we receive is already a ``str``; computing
#   byte offsets would require nailing an encoding (UTF-8 by convention) and
#   re-encoding for every match, with no extra information for the consumer.
#   Line / column are computed alongside for human-friendly rendering.
#
# - **First match wins on duplicates.** If the same quote appears N times in
#   the artifact, the span points to the first occurrence. This is the
#   simplest deterministic default and the one humans usually want when
#   asking "where did the model find this quote?". Documented in
#   ``docs/llm-rubric.md``.
#
# - **Normalisation-aware fallback.** ``str.find`` is tried first against
#   the raw text (``match_normalisation: "exact"``); if that misses, a
#   smart-quote-folded copy is searched (``"smart_quotes"``); if that still
#   misses, the artifact's whitespace is collapsed and a regex over the
#   collapsed-whitespace artifact locates the run, with the offsets mapped
#   back to the original text (``"whitespace"``). The fabrication validator
#   in ``_find_fabricated_quotes`` already accepts this composite tolerance,
#   so any quote that passed validation is guaranteed to resolve to a span.
#
# - **Single-artifact ``artifact_index: 0``.** Included so the field stays
#   meaningful when multi-artifact attribution arrives in a follow-up
#   (#182 / multi-target semantic context). Single-artifact callers can
#   ignore it without confusion.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteSpan:
    """Span metadata for one validated supporting quote (#179).

    Fields
    ------
    quote:
        The quote string as the model returned it. Preserved verbatim so a
        consumer can correlate ``supporting_evidence_spans[i]`` with
        ``supporting_evidence_quotes[i]`` even when the in-artifact text
        differs (e.g. smart-quote folding).
    artifact_index:
        Index of the artifact this span points into. Always ``0`` in the
        first slice (single-artifact); reserved so future multi-artifact
        evidence attribution does not break the wire format.
    start_offset, end_offset:
        Character offsets (Python ``str`` indices) into the artifact text.
        Half-open: ``artifact_text[start_offset:end_offset]`` is the
        matched substring in the **original** artifact text — even when
        ``match_normalisation`` is ``"whitespace"`` or ``"smart_quotes"``.
    start_line, end_line:
        1-indexed line numbers of the start and end positions. ``end_line``
        is the line containing the last matched character (inclusive).
    start_column, end_column:
        1-indexed column numbers (Unicode code-points within the line).
        ``end_column`` is one past the last matched character (so an empty
        match would have ``start_column == end_column``); this matches the
        half-open-interval convention used for ``end_offset``.
    match_normalisation:
        ``"exact"`` if the quote was found verbatim, ``"smart_quotes"`` if
        smart-quote folding was needed, ``"whitespace"`` if collapsing
        whitespace runs was needed. The tag records the **weakest**
        normalisation the resolver had to apply: an exact match always
        wins over a smart-quote match, and a smart-quote match always wins
        over a whitespace-collapse match.
    """

    quote: str
    artifact_index: int
    start_offset: int
    end_offset: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    match_normalisation: Literal["exact", "smart_quotes", "whitespace"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "quote": self.quote,
            "artifact_index": self.artifact_index,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "match_normalisation": self.match_normalisation,
        }


def _line_col_from_offset(text: str, offset: int) -> tuple[int, int]:
    """Return the 1-indexed (line, column) for *offset* in *text*.

    ``offset`` is clamped into ``[0, len(text)]`` so callers can pass
    ``end_offset`` (which may equal ``len(text)`` for a trailing match) without
    branching. Lines are 1-indexed; columns are 1-indexed within the line and
    counted in Python ``str`` code points.
    """
    if offset < 0:
        offset = 0
    if offset > len(text):
        offset = len(text)
    # Number of newlines before *offset* gives the 0-indexed line; +1 makes it
    # 1-indexed. Column = offset minus the position just after the last
    # preceding newline (or 0 if none), then +1 for 1-indexing.
    last_newline = text.rfind("\n", 0, offset)
    line = text.count("\n", 0, offset) + 1
    column = offset - (last_newline + 1) + 1
    return line, column


def _resolve_quote_span_in(artifact_text: str, quote: str) -> QuoteSpan | None:
    """Return the first-match span for *quote* in *artifact_text*, or ``None``.

    Tries three strategies in escalating tolerance order, recording the
    weakest one that succeeded as ``match_normalisation``:

    1. **Exact** substring search via ``str.find`` against the raw
       artifact text.
    2. **Smart-quote folded** search: fold both artifact and quote through
       ``_QUOTE_FOLDING`` (preserving character count — every entry in the
       table maps to a single ASCII character), then run ``str.find`` on
       the folded artifact. Offsets in the folded form coincide with
       offsets in the original because folding is character-for-character.
    3. **Whitespace-collapsed** search: collapse each whitespace run in the
       artifact to a single space, build an offset-mapping array from
       collapsed-form indices back to original-form indices, then locate
       the (smart-quote-folded, whitespace-collapsed) quote inside the
       (smart-quote-folded, whitespace-collapsed) artifact via regex. The
       returned offsets point into the **original** artifact text, so
       ``artifact_text[start_offset:end_offset]`` always slices something
       meaningful.

    Returns ``None`` only if the quote does not match under any of the
    three strategies. ``_find_fabricated_quotes`` already enforces the
    composite tolerance so a quote that passed validation always resolves.
    """
    if not isinstance(quote, str) or not quote:
        return None

    # Strategy 1: exact match in the raw artifact.
    idx = artifact_text.find(quote)
    if idx >= 0:
        start_line, start_column = _line_col_from_offset(artifact_text, idx)
        end_line, end_column = _line_col_from_offset(artifact_text, idx + len(quote))
        return QuoteSpan(
            quote=quote,
            artifact_index=0,
            start_offset=idx,
            end_offset=idx + len(quote),
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            match_normalisation="exact",
        )

    # Strategy 2: smart-quote folded match. The folding table maps each
    # character to exactly one character, so str.find offsets in the folded
    # artifact correspond byte-for-byte to offsets in the original.
    folded_artifact = artifact_text.translate(_QUOTE_FOLDING)
    folded_quote = quote.translate(_QUOTE_FOLDING)
    if folded_artifact != artifact_text or folded_quote != quote:
        idx = folded_artifact.find(folded_quote)
        if idx >= 0:
            start_line, start_column = _line_col_from_offset(artifact_text, idx)
            end_line, end_column = _line_col_from_offset(artifact_text, idx + len(folded_quote))
            return QuoteSpan(
                quote=quote,
                artifact_index=0,
                start_offset=idx,
                end_offset=idx + len(folded_quote),
                start_line=start_line,
                start_column=start_column,
                end_line=end_line,
                end_column=end_column,
                match_normalisation="smart_quotes",
            )

    # Strategy 3: whitespace-collapsed match. We build a parallel index map
    # so collapsed-form offsets translate back to original-form offsets.
    # The mapping uses the smart-quote-folded artifact so quotes that need
    # both normalisations also resolve.
    collapsed_chars: list[str] = []
    collapsed_to_original: list[int] = []
    in_ws_run = False
    ws_run_start = -1
    for i, ch in enumerate(folded_artifact):
        if ch.isspace():
            if not in_ws_run:
                in_ws_run = True
                ws_run_start = i
                collapsed_chars.append(" ")
                collapsed_to_original.append(i)
        else:
            in_ws_run = False
            ws_run_start = -1
            collapsed_chars.append(ch)
            collapsed_to_original.append(i)
    del ws_run_start  # only used as a sentinel above
    collapsed_artifact = "".join(collapsed_chars)
    collapsed_quote = re.sub(r"\s+", " ", folded_quote).strip()
    if not collapsed_quote:
        return None

    # Locate within the collapsed string. We use str.find on the stripped
    # collapsed_artifact-equivalent: leading/trailing whitespace in the
    # collapsed form does not change the quote's offset since str.find
    # already skips past it; but we must be careful — the collapsed quote
    # is stripped, while the collapsed_artifact retains leading whitespace.
    # str.find naturally skips leading whitespace runs because they don't
    # match the stripped quote, so this is correct.
    cidx = collapsed_artifact.find(collapsed_quote)
    if cidx < 0:
        return None
    cend = cidx + len(collapsed_quote)
    start_offset = collapsed_to_original[cidx]
    # End offset is one past the last matched character in the original.
    # The last collapsed-form index is cend - 1 → maps to one original char.
    # If that char is part of a whitespace run, the run in the original may
    # be longer; advance to one past the run-original character.
    if cend - 1 < len(collapsed_to_original):
        last_original = collapsed_to_original[cend - 1]
        # Determine whether the last collapsed character is a whitespace
        # run representative; if so, end_offset is the original index of
        # the next non-whitespace character (i.e. one past the whitespace
        # run). For a non-whitespace last character, end_offset is one
        # past last_original.
        if folded_artifact[last_original].isspace():
            end_offset = last_original + 1
            while end_offset < len(folded_artifact) and folded_artifact[end_offset].isspace():
                end_offset += 1
        else:
            end_offset = last_original + 1
    else:  # pragma: no cover — defensive; cidx<len means cend-1<len.
        end_offset = len(artifact_text)

    start_line, start_column = _line_col_from_offset(artifact_text, start_offset)
    end_line, end_column = _line_col_from_offset(artifact_text, end_offset)
    return QuoteSpan(
        quote=quote,
        artifact_index=0,
        start_offset=start_offset,
        end_offset=end_offset,
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
        match_normalisation="whitespace",
    )


def _resolve_quote_spans(quotes: list[str], artifact_text: str) -> list[QuoteSpan]:
    """Return ``QuoteSpan`` for every quote in *quotes* (#179).

    Precondition: every quote in *quotes* has already passed
    ``_find_fabricated_quotes`` and is known to be a normalised substring of
    *artifact_text*. Quotes that nevertheless fail to resolve (a guard
    against future drift between the validator and the resolver) are
    silently skipped — the caller still has ``supporting_evidence_quotes``
    as the stable, span-free record. Skipping rather than raising preserves
    the "spans are additive, quotes remain authoritative" invariant from
    #179's compatibility design.
    """
    spans: list[QuoteSpan] = []
    for quote in quotes:
        span = _resolve_quote_span_in(artifact_text, quote)
        if span is not None:
            spans.append(span)
    return spans


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


def _resolve_strategy_id(rule: Rule) -> str:
    """Return the strategy id for *rule* from ``rule.params['strategy']`` (#183).

    Defaults to :data:`DEFAULT_STRATEGY` (``"single"``) when unset. An
    explicit non-string value or unknown id is preserved verbatim and
    surfaced to the dispatcher so it can record an
    ``unknown_strategy`` / ``strategy_not_implemented`` failure with the
    offending id in evidence — silently coercing to ``single`` would
    mask a rule-author typo as a successful single-call verdict.
    """
    raw = rule.params.get("strategy", DEFAULT_STRATEGY)
    if not isinstance(raw, str) or not raw:
        return str(raw)
    return raw


def _strategy_unavailable(
    rule: Rule,
    rubric_input: dict[str, Any],
    strategy_id: str,
    failure_mode: str,
    detail: str,
) -> Diagnostic:
    """Return ``UNAVAILABLE`` for an unknown / unimplemented strategy id (#183).

    Reserved-but-unimplemented ids (``consensus`` / ``review`` /
    ``adaptive``) and unknown strings both go through this path. The
    rule-author sees a fail-closed diagnostic with the offending id and
    a clear failure mode rather than a silent fallback to ``single``.
    """
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message=(
            f"LLM rubric strategy {strategy_id!r} is not available; skipping rule. "
            f"Use one of: {sorted(_STRATEGIES)}."
        ),
        evidence=[
            Evidence(
                kind="strategy_unavailable",
                data={
                    **rubric_input,
                    "requested_strategy": strategy_id,
                    "failure_mode": failure_mode,
                    "detail": detail[:500],
                    "available_strategies": sorted(_STRATEGIES),
                    "known_strategies": sorted(KNOWN_STRATEGIES),
                },
            )
        ],
        remediation=(
            "Set `params.strategy` to a concrete strategy that is "
            f"implemented (currently: {sorted(_STRATEGIES)}), or remove "
            "the `params.strategy` key to fall back to the default "
            f"({DEFAULT_STRATEGY!r})."
        ),
    )


def _run_single_strategy(request: JudgmentRequest) -> Diagnostic:
    """Evaluate *request* with the single-call strategy (#183 reference impl).

    Preserves the pre-#183 ``check`` body byte-for-byte except for the
    additive strategy-metadata fields on the success ``llm_judgment``
    evidence dict (``llm_strategy``, ``llm_call_count``, ``models``,
    ``cost_estimate_usd_total``, ``latency_ms_total``). Existing
    consumers that only read the legacy fields keep working unchanged.
    """
    rule = request.rule
    target = request.target
    artifact_kind = request.artifact_kind

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
    system, user = _build_prompt(rule, target, artifact_kind)

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

    cost = _estimate_cost(model, telemetry["tokens_in"], telemetry["tokens_out"])

    # #183 — strategy-aggregated telemetry. For ``single`` this is just
    # the one provider call; for future multi-call strategies these
    # fields aggregate across calls. The legacy per-call ``latency_ms``
    # / ``tokens_in`` / ``tokens_out`` / ``cost_estimate_usd`` fields
    # remain on the evidence dict so existing consumers don't break.
    strategy_meta: dict[str, Any] = {
        "llm_strategy": "single",
        "llm_call_count": 1,
        "models": [model],
        "cost_estimate_usd_total": cost,
        "latency_ms_total": telemetry["latency_ms"],
    }

    # #169 — when the model declines because the rule does not apply to the
    # artifact kind (target_kind_mismatch), surface UNSUPPORTED with
    # ``target_kind_mismatch`` evidence rather than running the
    # substring-grounding check (an unsupported verdict legitimately carries
    # an empty quote list — see ``LlmJudgment.supporting_evidence_quotes``).
    #
    # We only honour the verdict when the rule actually carries a
    # ``target_kind`` annotation. Otherwise the prompt's "## Artifact kind"
    # block was never rendered and the model has no basis to claim a
    # mismatch — a stray ``"unsupported"`` from a flaky model on an
    # unannotated rule is treated as a contract violation (`provider_error`
    # / `unsupported_without_target_kind`) and degrades the verdict to
    # UNAVAILABLE rather than silently inventing a mismatch.
    if parsed.judgment == "unsupported":
        if rule.target_kind is TargetKind.UNSPECIFIED:
            return _unavailable_provider_error(
                rule,
                rubric_input,
                provider,
                "unsupported_without_target_kind",
                (
                    "Model returned 'unsupported' but the rule carries no "
                    "target_kind annotation; the artifact-kind block was "
                    "never injected into the prompt, so the verdict has "
                    "no grounding. Treat as provider error."
                ),
            )
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=Status.UNSUPPORTED,
            severity=rule.severity,
            message=parsed.primary_reason,
            evidence=[
                Evidence(
                    kind="target_kind_mismatch",
                    data={
                        "model": model,
                        "prompt_version": PROMPT_VERSION,
                        "judgment": parsed.judgment,
                        "primary_reason": parsed.primary_reason,
                        "supporting_evidence_quotes": parsed.supporting_evidence_quotes,
                        "suggested_action": parsed.suggested_action,
                        "rule_target_kind": rule.target_kind.value,
                        "latency_ms": telemetry["latency_ms"],
                        "tokens_in": telemetry["tokens_in"],
                        "tokens_out": telemetry["tokens_out"],
                        "cost_estimate_usd": cost,
                        **strategy_meta,
                    },
                )
            ],
            remediation=(
                "The rule's premise does not apply to this artifact kind. "
                "Either evaluate the rule against an artifact whose kind "
                f"matches its `target_kind` ({rule.target_kind.value}), or "
                "remove / change the `target_kind` annotation on the rule."
            ),
        )

    # #172 — parser-side enforcement of the v2 prompt's substring-grounding
    # contract. If any quote is not a substring of the artifact text, reject
    # the verdict and surface UNSUPPORTED with llm_quote_fabrication evidence.
    # #191 — when ``artifact_kind`` is declared and the target is a real file,
    # ``_resolve_artifact_text`` returns the file content (mirroring what
    # ``_build_prompt`` injected). Quotes are validated against what the
    # model actually saw.
    artifact_text = _resolve_artifact_text(target, artifact_kind)
    fabricated = _find_fabricated_quotes(parsed.supporting_evidence_quotes, artifact_text)
    if fabricated:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=Status.UNSUPPORTED,
            severity=rule.severity,
            message=(
                "LLM rubric verdict rejected: the model returned "
                f"{len(fabricated)} of {len(parsed.supporting_evidence_quotes)} "
                "supporting quotes that are not substrings of the artifact "
                "(quote fabrication)."
            ),
            evidence=[
                Evidence(
                    kind="llm_quote_fabrication",
                    data={
                        "model": model,
                        "prompt_version": PROMPT_VERSION,
                        "claimed_judgment": parsed.judgment,
                        "primary_reason": parsed.primary_reason,
                        "supporting_evidence_quotes": parsed.supporting_evidence_quotes,
                        "fabricated_quotes": fabricated,
                        "suggested_action": parsed.suggested_action,
                        "latency_ms": telemetry["latency_ms"],
                        "tokens_in": telemetry["tokens_in"],
                        "tokens_out": telemetry["tokens_out"],
                        "cost_estimate_usd": cost,
                        **strategy_meta,
                    },
                )
            ],
            remediation=(
                "The model violated the substring-grounding contract for "
                "supporting_evidence_quotes (v2 prompt, #168). Re-run the "
                "rule; if the failure persists, investigate prompt drift or "
                "switch model. Do not act on this verdict."
            ),
        )

    status = Status.PASS if parsed.judgment == "pass" else Status.FAIL
    # #179 — once the fabrication validator has confirmed every quote is a
    # normalised substring of the artifact, resolve each to a span pointing
    # back into the original artifact text. Spans are additive: the
    # ``supporting_evidence_quotes`` field remains the stable compatibility
    # surface, ``supporting_evidence_spans`` is the new offset-bearing field.
    spans = _resolve_quote_spans(parsed.supporting_evidence_quotes, artifact_text)
    evidence = Evidence(
        kind="llm_judgment",
        data={
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "judgment": parsed.judgment,
            "primary_reason": parsed.primary_reason,
            "supporting_evidence_quotes": parsed.supporting_evidence_quotes,
            "supporting_evidence_spans": [span.to_dict() for span in spans],
            "suggested_action": parsed.suggested_action,
            "latency_ms": telemetry["latency_ms"],
            "tokens_in": telemetry["tokens_in"],
            "tokens_out": telemetry["tokens_out"],
            "cost_estimate_usd": cost,
            **strategy_meta,
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


def _run_not_implemented_strategy(request: JudgmentRequest, strategy_id: str) -> Diagnostic:
    """Reserved-id placeholder (#183). Records the gap fail-closed.

    ``review`` / ``adaptive`` are declared in :data:`KNOWN_STRATEGIES` so
    the rule IR layer can validate the value before reaching the backend,
    but their concrete bodies are out of scope for this slice. Invoking
    one returns ``UNAVAILABLE`` with ``strategy_not_implemented`` evidence
    rather than silently falling back to ``single`` — a rule that asks for
    ``review`` should not receive a single-call verdict in disguise.
    """
    rubric_input = _build_rubric_input(request.rule, request.target)
    return _strategy_unavailable(
        request.rule,
        rubric_input,
        strategy_id,
        "strategy_not_implemented",
        (
            f"Strategy {strategy_id!r} is reserved (#183) but its concrete "
            "implementation is deferred to a follow-up issue. Use "
            f"{DEFAULT_STRATEGY!r} for now."
        ),
    )


# ---------------------------------------------------------------------------
# Consensus strategy (#184)
# ---------------------------------------------------------------------------

_CONSENSUS_PANEL_SIZE_DEFAULT = 3
_CONSENSUS_PANEL_SIZE_MIN = 2
_CONSENSUS_PANEL_SIZE_MAX = 5


def _run_consensus_strategy(request: JudgmentRequest) -> Diagnostic:
    """Evaluate *request* with N independent judges and aggregate by majority vote (#184).

    Design choices (slice 1):
    - **Aggregation**: simple majority (>= ceil(N/2) votes). No extra LLM
      chair call — deterministic aggregation keeps cost predictable.
    - **Tie handling** (N=2 with 1-1 split): return ``UNSUPPORTED`` with
      ``consensus_tie`` evidence — fail-closed safety; the caller should
      re-run with odd N or promote to ``single`` with a stronger model.
    - **Panel size**: read from ``rule.params["consensus_panel_size"]``
      (default 3, range 2–5). Values outside the range are clamped and
      recorded in evidence so rule authors can observe the adjustment.
    - **Quote merging**: supporting quotes from majority-voting judges are
      merged and deduplicated by exact string match.
    - **Per-judge parse/fabrication failures**: any judge that returns a
      parse error or quote-fabrication rejection contributes an
      ``"unsupported"`` vote (fail-closed); the failure detail is recorded
      in ``judge_results``.
    - **Chair-LLM aggregation** is deferred to slice 2 if simple majority
      is insufficient for production use cases.

    Evidence shape (``llm_consensus`` kind):

    .. code-block:: json

        {
            "llm_strategy": "consensus",
            "consensus_panel_size": 3,
            "consensus_votes": {"pass": 2, "fail": 1, "unsupported": 0},
            "majority_verdict": "pass",
            "primary_reason": "<from first majority judge>",
            "supporting_evidence_quotes": ["<merged from majority judges>"],
            "prompt_version": "v4",
            "cost_estimate_usd_total": 0.0003,
            "latency_ms_total": 450,
            "models": ["gpt-4o-mini", "gpt-4o-mini", "gpt-4o-mini"],
            "llm_call_count": 3,
            "judge_results": [...]
        }
    """
    rule = request.rule
    target = request.target
    artifact_kind = request.artifact_kind

    rubric_input = _build_rubric_input(rule, target)

    # Load provider config once; if unconfigured bail early.
    env = _load_env_file()
    if not _is_configured(env):
        return _unavailable_unconfigured(rule, rubric_input)

    provider = env["GATE_KEEPER_LLM_PROVIDER"]
    model = _resolve_model(provider, env)

    # Resolve panel size from rule.params, clamp to valid range.
    raw_panel_size = rule.params.get("consensus_panel_size", _CONSENSUS_PANEL_SIZE_DEFAULT)
    try:
        panel_size = int(raw_panel_size)
    except (TypeError, ValueError):
        panel_size = _CONSENSUS_PANEL_SIZE_DEFAULT
    panel_size = max(_CONSENSUS_PANEL_SIZE_MIN, min(_CONSENSUS_PANEL_SIZE_MAX, panel_size))

    system, user = _build_prompt(rule, target, artifact_kind)
    artifact_text = _resolve_artifact_text(target, artifact_kind)

    # --- Run N independent provider calls ---
    judge_results: list[dict[str, object]] = []
    total_cost: float | None = 0.0
    total_latency_ms: int = 0
    models_used: list[str] = []

    for judge_index in range(panel_size):
        try:
            if provider == "anthropic":
                response_text, telemetry = _call_anthropic(env["ANTHROPIC_API_KEY"], system, user, model)
            else:
                response_text, telemetry = _call_openai(env["OPENAI_API_KEY"], system, user, model)
        except Exception as exc:  # noqa: BLE001
            judge_results.append(
                {
                    "judge_index": judge_index,
                    "model": model,
                    "verdict": "unsupported",
                    "failure_mode": "provider_error",
                    "detail": f"{type(exc).__name__}: {exc}"[:500],
                }
            )
            models_used.append(model)
            # Cost/latency unknown for failed call; set total to None (fail-closed).
            total_cost = None
            continue

        assert telemetry.keys() >= {"latency_ms", "tokens_in", "tokens_out"}, (
            f"provider helper returned incomplete telemetry: {sorted(telemetry.keys())}"
        )

        call_cost = _estimate_cost(model, telemetry["tokens_in"], telemetry["tokens_out"])
        if call_cost is None:
            total_cost = None
        elif total_cost is not None:
            total_cost += call_cost

        total_latency_ms += telemetry["latency_ms"]
        models_used.append(model)

        parsed = _parse_llm_judgment(response_text)
        if isinstance(parsed, LlmJudgmentParseError):
            judge_results.append(
                {
                    "judge_index": judge_index,
                    "model": model,
                    "verdict": "unsupported",
                    "failure_mode": "parse_error",
                    "detail": parsed.detail[:500],
                    "latency_ms": telemetry["latency_ms"],
                    "tokens_in": telemetry["tokens_in"],
                    "tokens_out": telemetry["tokens_out"],
                    "cost_estimate_usd": call_cost,
                }
            )
            continue

        # Quote-fabrication check per judge.
        fabricated = _find_fabricated_quotes(parsed.supporting_evidence_quotes, artifact_text)
        if fabricated:
            judge_results.append(
                {
                    "judge_index": judge_index,
                    "model": model,
                    "verdict": "unsupported",
                    "failure_mode": "quote_fabrication",
                    "claimed_judgment": parsed.judgment,
                    "fabricated_quotes": fabricated,
                    "latency_ms": telemetry["latency_ms"],
                    "tokens_in": telemetry["tokens_in"],
                    "tokens_out": telemetry["tokens_out"],
                    "cost_estimate_usd": call_cost,
                }
            )
            continue

        # Mirror the single-strategy contract: an "unsupported" verdict on a
        # rule without ``target_kind`` is a contract violation — the
        # artifact-kind block was never injected, so the model had no basis to
        # claim a mismatch.  Record as a provider error so it counts as an
        # unsupported vote rather than silently aggregating as a valid judgment.
        if parsed.judgment == "unsupported" and rule.target_kind is TargetKind.UNSPECIFIED:
            judge_results.append(
                {
                    "judge_index": judge_index,
                    "model": model,
                    "verdict": "unsupported",
                    "failure_mode": "unsupported_without_target_kind",
                    "detail": (
                        "Model returned 'unsupported' but the rule carries no "
                        "target_kind annotation; the artifact-kind block was "
                        "never injected into the prompt, so the verdict has "
                        "no grounding. Treat as provider error."
                    ),
                    "latency_ms": telemetry["latency_ms"],
                    "tokens_in": telemetry["tokens_in"],
                    "tokens_out": telemetry["tokens_out"],
                    "cost_estimate_usd": call_cost,
                }
            )
            continue

        judge_results.append(
            {
                "judge_index": judge_index,
                "model": model,
                "verdict": parsed.judgment,
                "primary_reason": parsed.primary_reason,
                "supporting_evidence_quotes": parsed.supporting_evidence_quotes,
                "suggested_action": parsed.suggested_action,
                "latency_ms": telemetry["latency_ms"],
                "tokens_in": telemetry["tokens_in"],
                "tokens_out": telemetry["tokens_out"],
                "cost_estimate_usd": call_cost,
            }
        )

    # --- Aggregate ---
    verdicts = [r["verdict"] for r in judge_results]
    pass_count = verdicts.count("pass")
    fail_count = verdicts.count("fail")
    unsupported_count = verdicts.count("unsupported")
    votes = {"pass": pass_count, "fail": fail_count, "unsupported": unsupported_count}

    # Strict majority: a verdict requires more than half the panel (> panel_size/2),
    # not just >= ceil(panel_size/2).  For even N, ceil(N/2) == N/2, so the old
    # threshold allowed a single judge out of two to carry a PASS or FAIL when
    # the other voted unsupported — which is not a majority.  Using > panel_size/2
    # means exactly half is not enough (those cases fall through to tie/unsupported).
    threshold = panel_size / 2
    # Determine majority verdict (fail-closed on tie).
    if pass_count > threshold and pass_count > fail_count:
        majority_verdict: str = "pass"
    elif fail_count > threshold and fail_count > pass_count:
        majority_verdict = "fail"
    elif pass_count == fail_count and unsupported_count == 0:
        # Exact tie between pass and fail (e.g. N=2 → 1-1).
        majority_verdict = "tie"
    else:
        # Plurality is unsupported or no clear majority.
        majority_verdict = "unsupported"

    # Merge supporting quotes from majority judges (dedup by exact match).
    majority_quotes: list[str] = []
    seen_quotes: set[str] = set()
    majority_primary_reason: str = ""
    majority_suggested_action: str | None = None
    for r in judge_results:
        if r["verdict"] != majority_verdict:
            continue
        if not majority_primary_reason:
            majority_primary_reason = str(r.get("primary_reason", ""))
            majority_suggested_action = r.get("suggested_action")  # type: ignore[assignment]
        for q in r.get("supporting_evidence_quotes", []):  # type: ignore[union-attr]
            if isinstance(q, str) and q not in seen_quotes:
                seen_quotes.add(q)
                majority_quotes.append(q)

    consensus_evidence_base: dict[str, object] = {
        "llm_strategy": "consensus",
        "consensus_panel_size": panel_size,
        "consensus_votes": votes,
        "majority_verdict": majority_verdict,
        "prompt_version": PROMPT_VERSION,
        "cost_estimate_usd_total": total_cost,
        "latency_ms_total": total_latency_ms,
        "models": models_used,
        "llm_call_count": panel_size,
        "judge_results": judge_results,
    }

    if majority_verdict == "tie":
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=Status.UNSUPPORTED,
            severity=rule.severity,
            message=(
                f"Consensus panel produced a tie ({pass_count} pass vs {fail_count} fail "
                f"with N={panel_size}); verdict is indeterminate."
            ),
            evidence=[
                Evidence(
                    kind="consensus_tie",
                    data={**consensus_evidence_base},
                )
            ],
            remediation=(
                "Re-run with an odd panel size (e.g. consensus_panel_size: 3) to avoid "
                "ties, or switch to strategy: single with a stronger model."
            ),
        )

    if majority_verdict == "unsupported":
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=Status.UNSUPPORTED,
            severity=rule.severity,
            message=(
                f"Consensus panel did not reach a pass/fail majority "
                f"(pass={pass_count}, fail={fail_count}, unsupported={unsupported_count}, N={panel_size})."
            ),
            evidence=[
                Evidence(
                    kind="consensus_no_majority",
                    data={**consensus_evidence_base},
                )
            ],
            remediation=(
                "Check judge_results in evidence for per-judge failure modes "
                "(parse errors, quote fabrication, target-kind mismatch). "
                "Consider increasing panel size or switching to a more reliable model."
            ),
        )

    # pass or fail majority — build final diagnostic.
    status = Status.PASS if majority_verdict == "pass" else Status.FAIL
    evidence_data: dict[str, object] = {
        **consensus_evidence_base,
        "primary_reason": majority_primary_reason,
        "supporting_evidence_quotes": majority_quotes,
        "suggested_action": majority_suggested_action,
    }
    evidence = Evidence(kind="llm_consensus", data=evidence_data)

    if status is Status.PASS:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.LLM_RUBRIC,
            status=status,
            severity=rule.severity,
            message=majority_primary_reason,
            evidence=[evidence],
        )
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=status,
        severity=rule.severity,
        message=majority_primary_reason,
        evidence=[evidence],
        remediation=majority_suggested_action,
    )


#: Concrete strategy registry (#183 / #184). Maps strategy id to its
#: :class:`Strategy` callable. ``single`` and ``consensus`` are implemented;
#: ``review`` and ``adaptive`` are reserved and dispatch through
#: ``_run_not_implemented_strategy`` so the gap is observable.
_STRATEGIES: dict[str, Strategy] = {
    "single": _run_single_strategy,
    "consensus": _run_consensus_strategy,
}


def check(
    rule: Rule,
    target: str | Path | TargetSpec,
    *,
    artifact_kind: TargetKind | None = None,
) -> Diagnostic:
    """Evaluate a semantic-rubric rule against *target*.

    When no provider is configured (or the env file is absent), returns
    ``UNAVAILABLE`` with ``provider_unconfigured`` evidence. When a provider is
    configured, dispatches to the configured provider and maps the response to
    ``pass``/``fail`` with structured ``llm_judgment`` evidence (see
    ``LlmJudgment``). Provider errors and unparseable responses map to
    ``UNAVAILABLE`` with ``provider_error`` evidence — never to a crash,
    ``pass``, or ``fail``.

    When the rule carries an explicit ``target_kind`` annotation (#169) and
    the model determines the rule's premise does not apply to the artifact
    (e.g. a PR-description rule given a commit-message artifact), the
    judgment maps to :data:`Status.UNSUPPORTED` with
    ``evidence.kind=target_kind_mismatch`` rather than rendering a verdict.

    Multi-target inputs (``TargetSpec`` with ``is_multi=True``) are not
    supported by this backend in the first slice (issue #146); the call
    returns ``UNSUPPORTED`` with a ``multi_target_unsupported`` evidence
    record so callers can see that targets were silently dropped is *not*
    happening — content assembly is deferred to a follow-up. Single-file
    ``TargetSpec`` values are unwrapped to the underlying path so callers
    can mix CLI surfaces freely.

    Strategy dispatch (#183). The judgment strategy is selected from
    ``rule.params["strategy"]`` and defaults to :data:`DEFAULT_STRATEGY`
    (``"single"``) for backward compatibility. Only ``"single"`` is
    implemented in this slice; reserved ids (``"consensus"`` /
    ``"review"`` / ``"adaptive"``) and unknown ids return
    ``UNAVAILABLE`` with ``strategy_unavailable`` evidence so callers
    discover the gap explicitly. Strategy-aggregated metadata
    (``llm_strategy``, ``llm_call_count``, ``models``,
    ``cost_estimate_usd_total``, ``latency_ms_total``) is appended to
    every successful ``llm_judgment`` / ``target_kind_mismatch`` /
    ``llm_quote_fabrication`` evidence dict; the legacy single-call
    fields (``latency_ms``, ``tokens_in``, ``tokens_out``,
    ``cost_estimate_usd``) remain unchanged so existing consumers keep
    working.

    Parameters
    ----------
    rule:
        The semantic-rubric rule to evaluate.
    target:
        File path, PR reference, inline string, or single-file
        :class:`TargetSpec`. When a multi-target spec is supplied the
        call short-circuits to ``UNSUPPORTED`` with
        ``multi_target_unsupported`` evidence (see above).
    artifact_kind:
        Optional caller-declared :class:`TargetKind` for *target* (#191).
        When supplied **and** *target* refers to a real file on disk, the
        prompt's ``Target reference`` block is filled with the file
        content rather than the path string — gpt-4o-mini at v4 was
        observed to parrot filenames as artifact-kind evidence
        (e.g. ``target-03-commit.txt``) when the path was rendered into
        the prompt, undermining the rule's declared ``target_kind``
        annotation. Inline / non-existent targets and ``artifact_kind=None``
        callers preserve the legacy v4 byte-for-byte behaviour. The
        :func:`_resolve_artifact_text` substring grounding follows the
        same substitution so quotes are validated against what the model
        actually saw.

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
    - ``llm_strategy``: strategy id used (#183), e.g. ``"single"``.
    - ``llm_call_count``: number of provider calls the strategy made (#183).
    - ``models``: list of model identifiers used across the strategy's
      calls (#183).
    - ``cost_estimate_usd_total``: aggregated USD cost across the
      strategy's calls (#183); ``None`` if any call had unknown pricing.
    - ``latency_ms_total``: aggregated provider-call wall-clock latency
      in integer milliseconds across the strategy's calls (#183).

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

    # #183 — strategy dispatch. ``rule.params["strategy"]`` selects the
    # judgment strategy; defaults to :data:`DEFAULT_STRATEGY` (``"single"``)
    # so unannotated rules preserve the pre-#183 single-call behaviour.
    strategy_id = _resolve_strategy_id(rule)
    request = JudgmentRequest(rule=rule, target=target, artifact_kind=artifact_kind)
    strategy = _STRATEGIES.get(strategy_id)
    if strategy is not None:
        return strategy(request)
    if strategy_id in KNOWN_STRATEGIES:
        return _run_not_implemented_strategy(request, strategy_id)
    return _strategy_unavailable(
        rule,
        _build_rubric_input(rule, target),
        strategy_id,
        "unknown_strategy",
        (
            f"Strategy {strategy_id!r} is not a recognised id. "
            f"Known ids: {sorted(KNOWN_STRATEGIES)}; implemented in this build: "
            f"{sorted(_STRATEGIES)}."
        ),
    )


# ---------------------------------------------------------------------------
# Reproducibility (#68)
# ---------------------------------------------------------------------------


def run_n(
    rule: Rule,
    target: str | Path,
    n: int,
    *,
    artifact_kind: TargetKind | None = None,
) -> Diagnostic:
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
    - The optional *artifact_kind* keyword is forwarded verbatim to each
      ``check`` call (#191) so reproducibility runs see the same prompt
      substitution as a single-shot call.
    """
    if n < 1:
        raise ValueError(f"reproducibility n must be >= 1, got {n}")

    if n == 1:
        return check(rule, target, artifact_kind=artifact_kind)

    diagnostics: list[Diagnostic] = []
    for _ in range(n):
        diag = check(rule, target, artifact_kind=artifact_kind)
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
