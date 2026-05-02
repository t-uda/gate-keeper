"""LLM-rubric backend for gate-keeper (MVP stub).

Provider-specific implementation is out of scope for the MVP. This module is
registered so that ``--backend llm-rubric`` is a valid choice and rules that
fall through classifier heuristics have a defined home.

Extension point: when adding a real provider, implement ``_is_configured()``
to detect provider credentials and replace the stub body in ``check()`` with
provider invocation and response-parsing logic. The ``Diagnostic`` contract is
unchanged.
"""

from __future__ import annotations

from pathlib import Path

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

name = "llm-rubric"


def _is_configured() -> bool:
    """Return True when an LLM provider is configured.

    Always False for the MVP.  A real implementation would inspect env vars
    or a config file (e.g. GATE_KEEPER_LLM_PROVIDER, OPENAI_API_KEY, etc.).
    """
    return False


def _build_rubric_input(rule: Rule, target: str | Path) -> dict:
    """Build the context a future provider call would consume.

    Shape:
      rule_text  — verbatim normative text for the model to evaluate.
      rule_kind  — classifier-assigned kind (``semantic_rubric`` in practice).
      target     — artifact under evaluation (filesystem path or PR reference).

    A provider implementation passes this to the model and parses the
    pass/fail judgment and explanation from the response.
    """
    return {
        "rule_text": rule.text,
        "rule_kind": rule.kind.value,
        "target": str(target),
    }


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Evaluate a semantic-rubric rule against *target*.

    Returns UNAVAILABLE (exits non-zero) until a provider is configured.
    The evidence block carries the rubric input so downstream consumers can
    see exactly what would be sent to the provider.
    """
    if _is_configured():
        # Provider call would go here; out of scope for the MVP.
        raise NotImplementedError("LLM provider integration is out of scope for the MVP")

    rubric_input = _build_rubric_input(rule, target)
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.LLM_RUBRIC,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message="LLM rubric backend is not configured; skipping rule.",
        evidence=[
            Evidence(
                kind="provider_unconfigured",
                data=rubric_input,
            )
        ],
        remediation=(
            "Configure an LLM provider to enable semantic rule evaluation. "
            "See docs/llm-rubric.md for the extension point."
        ),
    )
