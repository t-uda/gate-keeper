#!/usr/bin/env python3
"""Format a `gate-keeper validate --format json` report into a PR comment.

Used by `.github/workflows/dogfooding.yml`. Kept as a standalone script
(not under ``src/``) because it is workflow-glue, not part of the package
public surface — the formatting contract is allowed to change with the
workflow without bumping the library version.

The script accepts a single positional arg (path to the JSON file produced
by ``gate-keeper validate ... --format json``) and writes a Markdown
comment to stdout. It prints a fallback body when the JSON cannot be
parsed; the workflow tolerates that path with ``continue-on-error`` on
the validate step.

User-facing wording rule (issue #261): the rendered comment MUST NOT
contain any "advisory" / "does not gate merge" / equivalent non-gating
disclaimer. Owner directive: remove that wording entirely. The
``test_format_dogfooding_comment.py`` suite pins this with a
forbidden-substring regression test so it cannot silently regress.

Output shape:

  ## gate-keeper dogfooding

  | rule | status | judgment | primary_reason |
  | --- | --- | --- | --- |
  | <title-or-id> | PASS | pass | ... |

  Tokens: in=N out=N, latency: N ms, model: <model>, prompt_version: vX

Structural skips (#240): UNSUPPORTED diagnostics whose primary evidence
kind is ``target_kind_mismatch`` (deterministic precheck, #178) or
``llm_quote_fabrication`` (parser-side reject, #172) are not
author-actionable per PR. They are filtered out of the main table and
collapsed into a ``<details>`` summary at the end.

Provider-error rendering (#263): when a diagnostic carries
``provider_error`` evidence (raised via
``_unavailable_provider_error`` in
``src/gate_keeper/backends/llm_rubric.py``), the ``primary_reason``
column renders ``provider error (<provider>): <failure_mode>: <detail>``
instead of the generic constant message. The renderer re-truncates
``detail`` to ~160 chars on top of the backend's 500-char cap so the PR
comment table stays readable. The backend stores ``str(exc)``, which
for the OpenAI SDK exception classes is the public error message and
does not include the API key; the test suite pins a regression guard
that the rendered output never contains literal ``OPENAI_API_KEY`` or
``sk-`` prefixes.
"""

from __future__ import annotations

import json
import sys
from typing import Any

_FALLBACK_HEADER = "## gate-keeper dogfooding"
_MARKER_BASE = "gate-keeper-dogfooding-comment"
_MARKER_FALLBACK = f"<!-- {_MARKER_BASE}:fallback -->"
_MARKER_REPORT = f"<!-- {_MARKER_BASE}:report -->"
_FALLBACK_BODY = (
    "Evaluation could not be produced for this PR.\n\n"
    "The validate step exited without a usable JSON report. See the "
    "workflow run logs for details."
)
_TRAILER = "<sub>See [`docs/dogfooding.md`](docs/dogfooding.md) for the per-rule promotion path.</sub>"

# UNSUPPORTED diagnostics whose primary evidence falls in this set are
# "structural skips": the system correctly short-circuited or self-defended
# (deterministic target-kind precheck #178, parser-side quote-fabrication
# reject #172). They are not author-actionable per PR, so they are collapsed
# into a <details> block instead of polluting the main table. See #240.
_STRUCTURAL_SKIP_KINDS = frozenset({"target_kind_mismatch", "llm_quote_fabrication"})
_NO_ACTIONABLE_BODY = (
    "No author-actionable findings. All evaluated rules either passed or "
    "were structurally skipped (see below)."
)

# Comment-renderer cap on `provider_error` detail (#263). The backend already
# caps `detail` at 500 chars in `_unavailable_provider_error`; this is an
# additional renderer-side truncation to keep the PR comment table readable.
_PROVIDER_ERROR_DETAIL_LIMIT = 160


def _emit_fallback() -> None:
    print(_MARKER_FALLBACK)
    print()
    print(_FALLBACK_HEADER)
    print()
    print(_FALLBACK_BODY)
    print()
    print(_TRAILER)


def _safe_truncate(text: str, limit: int = 200) -> str:
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _rule_label(diag: dict[str, Any]) -> str:
    rule_id = diag.get("rule_id") or "<unknown>"
    return _safe_truncate(str(rule_id), 80)


def _judgment_for(diag: dict[str, Any]) -> str:
    """Pull the LLM judgment string from `evidence[*].data.judgment` if present."""
    for ev in diag.get("evidence", []) or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") != "llm_judgment":
            continue
        data = ev.get("data") or {}
        if isinstance(data, dict) and data.get("judgment"):
            return str(data["judgment"])
    return "-"


def _provider_error_reason(diag: dict[str, Any]) -> str | None:
    """Render ``provider_error`` evidence as a compact one-line reason (#263).

    Returns ``"provider error (<provider>): <failure_mode>: <detail>"`` when
    the diagnostic carries ``provider_error`` evidence, else ``None``. The
    backend already caps ``detail`` at 500 chars in
    ``_unavailable_provider_error``; we additionally re-truncate to
    :data:`_PROVIDER_ERROR_DETAIL_LIMIT` (~160 chars) to keep the PR comment
    table readable.

    The backend stores ``str(exc)`` for the SDK exception. For OpenAI's
    public exception classes (``AuthenticationError``,
    ``PermissionDeniedError``, ``BadRequestError``, ``RateLimitError``,
    etc.) that string does NOT include the API key. The
    ``test_format_dogfooding_comment.py`` suite pins a regression guard
    that asserts the rendered output contains neither literal
    ``OPENAI_API_KEY`` nor ``sk-`` prefixes.
    """
    for ev in diag.get("evidence", []) or []:
        if not isinstance(ev, dict) or ev.get("kind") != "provider_error":
            continue
        data = ev.get("data") or {}
        if not isinstance(data, dict):
            continue
        provider = str(data.get("provider") or "unknown")
        failure_mode = str(data.get("failure_mode") or "").strip()
        detail = str(data.get("detail") or "").strip()
        parts: list[str] = [f"provider error ({provider})"]
        tail_segments: list[str] = []
        if failure_mode:
            tail_segments.append(failure_mode)
        if detail:
            # Re-truncate detail BEFORE joining so the cap applies to the
            # detail snippet itself, not the whole composite string. The cap
            # is on the **pre-escape** length: ``_safe_truncate`` later
            # escapes ``|`` to ``\|`` for Markdown-table safety, which can
            # extend a detail full of pipes from 160 to ~320 chars in the
            # final cell. In practice ``str(exc)`` for OpenAI SDK
            # exceptions does not contain pipes, so the approximation is
            # accepted (cosmetic-only; not a correctness or secret-safety
            # concern).
            if len(detail) > _PROVIDER_ERROR_DETAIL_LIMIT:
                detail = detail[: _PROVIDER_ERROR_DETAIL_LIMIT - 1].rstrip() + "..."
            tail_segments.append(detail)
        if tail_segments:
            parts.append(": ".join(tail_segments))
        return _safe_truncate(": ".join(parts), 400)
    return None


def _primary_reason(diag: dict[str, Any]) -> str:
    """Return the diagnostic's `message`, falling back to the LLM primary_reason.

    For ``provider_error`` evidence (#263) the rendered string is built from
    ``failure_mode`` / ``detail`` directly so the operator sees the
    underlying exception type and a short snippet instead of the
    backend's generic ``"LLM rubric backend provider error (<provider>);
    skipping rule."`` message. The check runs BEFORE the
    ``diag["message"]`` fallback because the generic message is precisely
    what we are replacing.
    """
    provider_reason = _provider_error_reason(diag)
    if provider_reason is not None:
        return provider_reason
    msg = diag.get("message")
    if isinstance(msg, str) and msg.strip():
        return _safe_truncate(msg, 200)
    for ev in diag.get("evidence", []) or []:
        if not isinstance(ev, dict):
            continue
        data = ev.get("data") or {}
        if isinstance(data, dict) and data.get("primary_reason"):
            return _safe_truncate(str(data["primary_reason"]), 200)
    return "-"


def _structural_skip_kind(diag: dict[str, Any]) -> str | None:
    """Return the structural-skip evidence kind for ``diag``, or ``None``.

    A diagnostic is classified as a structural skip when status is
    ``unsupported`` AND the first evidence entry's ``kind`` falls in
    :data:`_STRUCTURAL_SKIP_KINDS`. See module docstring / #240.
    """
    status = str(diag.get("status", "")).lower()
    if status != "unsupported":
        return None
    evidence = diag.get("evidence") or []
    if not isinstance(evidence, list) or not evidence:
        return None
    first = evidence[0]
    if not isinstance(first, dict):
        return None
    kind = first.get("kind")
    if isinstance(kind, str) and kind in _STRUCTURAL_SKIP_KINDS:
        return kind
    return None


def _structural_skip_summary(skips: list[tuple[dict[str, Any], str]]) -> str:
    """Render the collapsed ``<details>`` block for structurally-skipped rules."""
    counts: dict[str, int] = {}
    for _, kind in skips:
        counts[kind] = counts.get(kind, 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    lines: list[str] = []
    lines.append("<details>")
    lines.append(
        f"<summary>{len(skips)} rule(s) structurally skipped "
        f"(no author action required): {breakdown}</summary>"
    )
    lines.append("")
    lines.append("| rule | status | evidence.kind | primary_reason |")
    lines.append("| --- | --- | --- | --- |")
    for diag, kind in skips:
        label = _rule_label(diag)
        status = str(diag.get("status", "-")).upper()
        reason = _primary_reason(diag)
        lines.append(f"| {label} | {status} | {kind} | {reason} |")
    lines.append("")
    lines.append(
        "These are non-actionable: deterministic precheck (#178) or "
        "parser-side quote-fabrication reject (#172) — the system "
        "correctly defended. See `docs/dogfooding.md`."
    )
    lines.append("</details>")
    return "\n".join(lines)


def _telemetry_summary(diagnostics: list[dict[str, Any]]) -> str | None:
    """Sum tokens / latency across `llm_judgment` evidence; return one summary line.

    Returns None when no telemetry is available (e.g. all rules unavailable).
    """
    tokens_in = 0
    tokens_out = 0
    latency_ms = 0
    models: list[str] = []
    prompt_versions: list[str] = []
    saw_telemetry = False

    for diag in diagnostics:
        for ev in diag.get("evidence", []) or []:
            if not isinstance(ev, dict) or ev.get("kind") != "llm_judgment":
                continue
            data = ev.get("data") or {}
            if not isinstance(data, dict):
                continue
            ti = data.get("tokens_in")
            to = data.get("tokens_out")
            lt = data.get("latency_ms")
            if isinstance(ti, int):
                tokens_in += ti
                saw_telemetry = True
            if isinstance(to, int):
                tokens_out += to
                saw_telemetry = True
            if isinstance(lt, int):
                latency_ms += lt
                saw_telemetry = True
            mdl = data.get("model")
            if isinstance(mdl, str) and mdl not in models:
                models.append(mdl)
            pv = data.get("prompt_version")
            if isinstance(pv, str) and pv not in prompt_versions:
                prompt_versions.append(pv)

    if not saw_telemetry:
        return None

    model_str = ", ".join(models) if models else "-"
    prompt_str = ", ".join(prompt_versions) if prompt_versions else "-"
    return (
        f"Tokens: in={tokens_in} out={tokens_out}, "
        f"latency: {latency_ms} ms, "
        f"model: {model_str}, "
        f"prompt_version: {prompt_str}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <path-to-json>", file=sys.stderr)
        _emit_fallback()
        return 0

    path = argv[1]
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not load {path!r}: {exc}", file=sys.stderr)
        _emit_fallback()
        return 0

    diagnostics = payload.get("diagnostics") if isinstance(payload, dict) else None
    if not isinstance(diagnostics, list) or not diagnostics:
        _emit_fallback()
        return 0

    actionable: list[dict[str, Any]] = []
    structural_skips: list[tuple[dict[str, Any], str]] = []
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        kind = _structural_skip_kind(diag)
        if kind is not None:
            structural_skips.append((diag, kind))
        else:
            actionable.append(diag)

    print(_MARKER_REPORT)
    print()
    print(_FALLBACK_HEADER)
    print()
    if actionable:
        print("| rule | status | judgment | primary_reason |")
        print("| --- | --- | --- | --- |")
        for diag in actionable:
            label = _rule_label(diag)
            status = str(diag.get("status", "-")).upper()
            judgment = _judgment_for(diag)
            reason = _primary_reason(diag)
            print(f"| {label} | {status} | {judgment} | {reason} |")
    else:
        print(_NO_ACTIONABLE_BODY)
    print()

    if structural_skips:
        print(_structural_skip_summary(structural_skips))
        print()

    telemetry = _telemetry_summary(diagnostics)
    if telemetry:
        print(telemetry)
        print()

    print(_TRAILER)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
