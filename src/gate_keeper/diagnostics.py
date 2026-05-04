"""Diagnostic renderers and exit-code policy for gate-keeper."""

from __future__ import annotations

import json
from typing import Any, Sequence

from gate_keeper.models import Backend, Diagnostic, DiagnosticReport, Rule, Status

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_NON_PASS = frozenset({Status.FAIL, Status.UNAVAILABLE, Status.UNSUPPORTED, Status.ERROR})

# Evidence kinds whose data is expanded into a human-readable phrase rather than
# raw key=value pairs in the compact text renderer.
_HUMAN_READABLE_KINDS = frozenset(
    {
        "backend_capability",
        "provider_unconfigured",
        "provider_error",
        "adapter_unknown",
        "params_error",
    }
)


def compute_exit_code(diagnostics: Sequence[Diagnostic]) -> int:
    """Return EXIT_OK if all diagnostics pass, EXIT_FAIL if any do not."""
    for d in diagnostics:
        if d.status in _NON_PASS:
            return EXIT_FAIL
    return EXIT_OK


def usage_error(message: str) -> tuple[str, int]:
    """Format a CLI usage/input error and return (text, EXIT_USAGE)."""
    return f"error: {message}", EXIT_USAGE


def _safe_value(v: object) -> str:
    return str(v).replace("\r", "\\r").replace("\n", "\\n")


def _human_readable_evidence(kind: str, data: dict[str, Any]) -> str:
    """Return a concise human-readable string for well-known failure-mode evidence kinds.

    Falls back to the raw key=value form for unrecognised kinds.
    """
    if kind == "backend_capability":
        backend = data.get("backend", "?")
        rule_kind = data.get("kind", "?")
        return f"{backend} backend does not support rule kind '{rule_kind}'"
    if kind == "provider_unconfigured":
        return "LLM provider not configured (see docs/llm-rubric.md)"
    if kind == "provider_error":
        failure_mode = data.get("failure_mode", "unknown")
        provider = data.get("provider", "?")
        detail = _safe_value(data.get("detail", ""))
        return f"LLM provider error ({provider}): {failure_mode} — {detail}"
    if kind == "adapter_unknown":
        tool = _safe_value(data.get("tool", "?"))
        registered = data.get("registered_adapters", [])
        if registered:
            return f"adapter '{tool}' not registered (known: {', '.join(str(a) for a in registered)})"
        return f"adapter '{tool}' not registered"
    if kind == "params_error":
        missing = _safe_value(data.get("missing", "?"))
        return f"rule params missing required field '{missing}'"
    # Generic fallback: key=value pairs
    return ", ".join(f"{k}={_safe_value(v)}" for k, v in data.items())


def _compact_evidence(diagnostic: Diagnostic) -> str:
    parts = []
    for e in diagnostic.evidence:
        # llm_judgment evidence from LLM_RUBRIC is rendered in the verbose block;
        # skip it here so it doesn't double-render on the compact line.
        # Scope by backend to avoid silently dropping evidence from other backends
        # that might coincidentally use the same kind name.
        if e.kind == "llm_judgment" and diagnostic.backend == Backend.LLM_RUBRIC:
            continue
        if e.kind in _HUMAN_READABLE_KINDS:
            parts.append(_human_readable_evidence(e.kind, e.data))
        elif e.data:
            pairs = ", ".join(f"{k}={_safe_value(v)}" for k, v in e.data.items())
            parts.append(f"{e.kind}({pairs})")
        else:
            parts.append(e.kind)
    return "; ".join(parts)


def _derive_failure_mode(diagnostic: Diagnostic) -> str | None:
    """Derive a short failure_mode tag from a diagnostic's evidence, or None for pass.

    Used by ``render_json`` to add a ``failure_mode`` field (#71).
    """
    if diagnostic.status is Status.PASS:
        return None
    for e in diagnostic.evidence:
        if e.kind == "backend_capability":
            return "unsupported_rule_kind"
        if e.kind == "provider_unconfigured":
            return "provider_unconfigured"
        if e.kind == "provider_error":
            return str(e.data.get("failure_mode", "provider_error"))
        if e.kind == "adapter_unknown":
            return "adapter_unknown"
        if e.kind == "params_error":
            return "params_error"
        if e.kind == "llm_judgment":
            judgment = e.data.get("judgment", "")
            return f"llm_{judgment}" if judgment else "llm_fail"
        if e.kind in ("exception", "io_error"):
            return e.kind
    # Fall back to status value if no specific evidence kind matched
    return diagnostic.status.value


def _render_llm_judgment_verbose(data: dict[str, Any]) -> list[str]:
    """Return indented lines expanding llm_judgment evidence for --verbose."""
    lines: list[str] = []
    lines.append("  [llm-rubric]")
    judgment = data.get("judgment", "")
    if judgment:
        lines.append(f"    judgment  : {judgment}")
    primary_reason = data.get("primary_reason", "")
    if primary_reason:
        lines.append(f"    reason    : {_safe_value(primary_reason)}")
    quotes: list[Any] = data.get("supporting_evidence_quotes") or []
    for quote in quotes:
        lines.append(f'    evidence  : "{_safe_value(quote).replace(chr(34), chr(92) + chr(34))}"')
    suggested_action = data.get("suggested_action")
    if suggested_action:
        lines.append(f"    action    : {_safe_value(suggested_action)}")
    model = data.get("model")
    if model:
        lines.append(f"    model     : {model}")
    return lines


def render_text(diagnostics: Sequence[Diagnostic], *, verbose: bool = False) -> str:
    """Render diagnostics as compiler-style text lines.

    Each line: path:line: severity: [backend/status] rule_id: message [evidence]

    Known failure-mode evidence kinds (backend_capability, provider_unconfigured,
    provider_error, adapter_unknown, params_error) are rendered as concise
    human-readable phrases rather than raw key=value pairs.

    When *verbose* is True and a diagnostic carries ``llm_judgment`` evidence,
    the structured rationale is expanded as indented lines below the main line.
    """
    lines = []
    for d in diagnostics:
        evidence_str = _compact_evidence(d)
        evidence_part = f" [{evidence_str}]" if evidence_str else ""
        lines.append(
            f"{d.source.path}:{d.source.line}: "
            f"{d.severity.value}: "
            f"[{d.backend.value}/{d.status.value}] "
            f"{d.rule_id}: "
            f"{_safe_value(d.message)}"
            f"{evidence_part}"
        )
        if verbose:
            for e in d.evidence:
                if e.kind == "llm_judgment" and d.backend == Backend.LLM_RUBRIC:
                    lines.extend(_render_llm_judgment_verbose(e.data))
    return "\n".join(lines)


def render_explain_text(rules: Sequence[Rule]) -> str:
    """Render rule-to-backend routing decisions as human-readable text.

    Each rule block: path:line: [backend/confidence] rule_id: kind
      <rule text>
      reason: <classifier explanation>
    """
    lines = []
    for rule in rules:
        explanation = rule.params.get("classifier_explanation", "no explanation available")
        heading_part = f" ({rule.source.heading})" if rule.source.heading else ""
        lines.append(
            f"{rule.source.path}:{rule.source.line}:{heading_part} "
            f"[{rule.backend_hint.value}/{rule.confidence.value}] "
            f"{rule.id}: {rule.kind.value}"
        )
        lines.append(f"  {rule.text}")
        lines.append(f"  reason: {explanation}")
    return "\n".join(lines)


def render_json(diagnostics: Sequence[Diagnostic]) -> str:
    """Render diagnostics as deterministic JSON for CI consumers.

    The status field is preserved exactly — unavailable is distinct from fail.
    A ``failure_mode`` field is added to each diagnostic: ``null`` for pass,
    a short descriptive tag for fail/unavailable/unsupported/error (#71).
    Existing fields are unchanged (backwards-compatible addition).
    """
    report = DiagnosticReport(diagnostics=list(diagnostics))
    data = report.to_dict()
    for diag_dict, diag in zip(data["diagnostics"], diagnostics):
        diag_dict["failure_mode"] = _derive_failure_mode(diag)
    return json.dumps(data, sort_keys=True, indent=2)
