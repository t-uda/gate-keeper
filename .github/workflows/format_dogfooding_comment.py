#!/usr/bin/env python3
"""Format a `gate-keeper validate --format json` report into a PR comment.

Used by `.github/workflows/dogfooding.yml`. Kept as a standalone script
(not under ``src/``) because it is workflow-glue, not part of the package
public surface — the formatting contract is allowed to change with the
workflow without bumping the library version.

The script accepts a single positional arg (path to the JSON file produced
by ``gate-keeper validate ... --format json``) and writes a Markdown
comment to stdout. It prints a fallback "no advisory" body when the JSON
cannot be parsed; the workflow tolerates that path with
``continue-on-error`` on the validate step.

Output shape (matches issue #131 acceptance criteria):

  ## gate-keeper dogfooding (advisory)

  | rule | status | judgment | primary_reason |
  | --- | --- | --- | --- |
  | <title-or-id> | PASS | pass | ... |

  Tokens: in=N out=N, latency: N ms, model: <model>, prompt_version: vX

  <small>This comment is advisory and does not gate merge. See
  docs/dogfooding.md.</small>
"""

from __future__ import annotations

import json
import sys
from typing import Any

_FALLBACK_HEADER = "## gate-keeper dogfooding (advisory)"
_MARKER = "<!-- gate-keeper-dogfooding-comment -->"
_FALLBACK_BODY = (
    "Advisory evaluation could not be produced for this PR.\n\n"
    "The validate step exited without a usable JSON report. This does not "
    "block merge. See the workflow run logs for details."
)
_TRAILER = (
    "<sub>This comment is advisory and does not gate merge. "
    "See [`docs/dogfooding.md`](docs/dogfooding.md) for the promotion path.</sub>"
)


def _emit_fallback() -> None:
    print(_MARKER)
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


def _primary_reason(diag: dict[str, Any]) -> str:
    """Return the diagnostic's `message`, falling back to the LLM primary_reason."""
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

    print(_MARKER)
    print()
    print(_FALLBACK_HEADER)
    print()
    print("| rule | status | judgment | primary_reason |")
    print("| --- | --- | --- | --- |")
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        label = _rule_label(diag)
        status = str(diag.get("status", "-")).upper()
        judgment = _judgment_for(diag)
        reason = _primary_reason(diag)
        print(f"| {label} | {status} | {judgment} | {reason} |")
    print()

    telemetry = _telemetry_summary(diagnostics)
    if telemetry:
        print(telemetry)
        print()

    print(_TRAILER)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
