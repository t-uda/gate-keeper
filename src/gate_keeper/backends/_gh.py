"""Internal gh CLI adapter for gate-keeper's GitHub backend.

Provides:
- GhResult        — typed result container for gh invocations
- run_gh()        — subprocess wrapper; never raises; redacts secrets
- parse_json()    — JSON parser with truncated error messages
- classify_gh_failure() — triage helper for gh failures
- Diagnostic builders  — gh_missing_diag, gh_failed_diag, gh_auth_diag,
                          gh_json_diag, gh_pagination_diag, gh_missing_field_diag

None of these functions perform per-rule logic; they are shared infrastructure
consumed by per-rule checks landing in issues #9-#13.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

# ---------------------------------------------------------------------------
# Secret-redaction patterns applied to stderr before it is stored anywhere.
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(
    # Classic GitHub token prefixes (ghp/gho/ghu/ghs/ghr) plus bare "gh_" tokens
    # and the fine-grained PAT prefix `github_pat_`. Order matters only for
    # readability — re.sub applies the alternation greedily by position.
    r"github_pat_[A-Za-z0-9_]{20,}"
    r"|ghp_[A-Za-z0-9_]{10,}"
    r"|gho_[A-Za-z0-9_]{10,}"
    r"|ghu_[A-Za-z0-9_]{10,}"
    r"|ghs_[A-Za-z0-9_]{10,}"
    r"|ghr_[A-Za-z0-9_]{10,}"
    r"|gh_[A-Za-z0-9_]{16,}",
)

_AUTH_HEADER_LINE_RE = re.compile(
    r"^.*(?:Authorization:|token=).*$",
    re.MULTILINE | re.IGNORECASE,
)

# Markers that indicate an authentication/credential problem in stderr.
_AUTH_MARKERS = (
    "not logged in",
    "gh auth login",
    "bad credentials",
    "401",
    "unauthorized",
)


def _redact(text: str) -> str:
    """Strip obvious GitHub tokens and auth headers from *text*."""
    text = _AUTH_HEADER_LINE_RE.sub("[redacted-auth-header]", text)
    text = _SECRET_RE.sub("[redacted-token]", text)
    return text


# ---------------------------------------------------------------------------
# GhResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GhResult:
    """Typed result of a ``gh`` CLI invocation.

    ``binary_missing`` is True only when the OS could not locate the ``gh``
    executable. This is tracked separately from ``returncode`` because Unix
    reports signal-terminated processes with negative return codes too — for
    example, SIGHUP yields ``returncode == -1`` — so a sentinel return code
    would conflate the two.
    """

    ok: bool
    stdout: str
    stderr: str  # already redacted
    returncode: int
    cmd: tuple[str, ...]  # the actual argv, for diagnostics
    binary_missing: bool = False


# ---------------------------------------------------------------------------
# run_gh
# ---------------------------------------------------------------------------


def run_gh(
    args: Sequence[str],
    *,
    input: str | None = None,
    timeout: float | None = None,
) -> GhResult:
    """Run ``gh <args>`` and return a GhResult; never raises.

    - Captures stdout/stderr as UTF-8 (errors="replace").
    - Non-zero exit codes are reflected in ``GhResult.ok = False``; no exception.
    - ``FileNotFoundError``, ``TimeoutExpired``, and other ``OSError`` variants
      are caught and returned as failed GhResult objects.
    - Secrets are redacted from stderr before storing.
    """
    argv: tuple[str, ...] = ("gh", *args)
    try:
        completed = subprocess.run(
            list(argv),
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        ok = completed.returncode == 0
        return GhResult(
            ok=ok,
            stdout=completed.stdout,
            stderr=_redact(completed.stderr),
            returncode=completed.returncode,
            cmd=argv,
        )
    except FileNotFoundError:
        return GhResult(
            ok=False,
            stdout="",
            stderr="gh binary not found",
            returncode=127,  # POSIX convention for "command not found"
            cmd=argv,
            binary_missing=True,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stderr = _redact(exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return GhResult(
            ok=False,
            stdout="",
            stderr=f"timed out after {exc.timeout}s; {partial_stderr}".strip("; "),
            returncode=-2,
            cmd=argv,
        )
    except OSError as exc:
        return GhResult(
            ok=False,
            stdout="",
            stderr=f"OSError: {exc}",
            returncode=-3,
            cmd=argv,
        )


# ---------------------------------------------------------------------------
# parse_json
# ---------------------------------------------------------------------------

_TRUNCATE_AT = 200


def parse_json(stdout: str) -> tuple[Any, str | None]:
    """Parse *stdout* as JSON.

    Returns ``(value, None)`` on success.
    Returns ``(None, error_message)`` on failure; the error message is one
    short line and never echoes the entire input.
    """
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as exc:
        preview = stdout[:_TRUNCATE_AT]
        if len(stdout) > _TRUNCATE_AT:
            preview += "…"
        return None, f"JSON parse error at pos {exc.pos}: {exc.msg!r}; input preview: {preview!r}"


# ---------------------------------------------------------------------------
# classify_gh_failure
# ---------------------------------------------------------------------------


def classify_gh_failure(result: GhResult) -> Literal["missing", "auth", "failure"]:
    """Classify a failed GhResult into one of three categories.

    - ``"missing"`` — the ``gh`` binary was not found.
    - ``"auth"``    — stderr contains markers indicating a credentials problem.
    - ``"failure"`` — any other non-zero exit (including signal-terminated runs).
    """
    if result.binary_missing:
        return "missing"
    stderr_lower = result.stderr.lower()
    if any(marker in stderr_lower for marker in _AUTH_MARKERS):
        return "auth"
    return "failure"


# ---------------------------------------------------------------------------
# Diagnostic builders
# ---------------------------------------------------------------------------


def _base_diag(rule: Rule, status: Status, message: str, evidence: list[Evidence]) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.GITHUB,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


def gh_missing_diag(rule: Rule) -> Diagnostic:
    """Produce an UNAVAILABLE diagnostic for a missing ``gh`` binary."""
    return _base_diag(
        rule,
        Status.UNAVAILABLE,
        "gh CLI is not installed or not found on PATH; cannot evaluate GitHub rule.",
        [
            Evidence(
                kind="gh_missing",
                data={"path": "gh"},
            )
        ],
    )


def _stderr_excerpt(result: GhResult, limit: int = 300) -> str:
    """Return the first *limit* chars of (already-redacted) stderr."""
    text = result.stderr
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _safe_cmd(result: GhResult) -> str:
    """Return a space-joined command string with secrets redacted from each argv element.

    A future caller could pass an auth header or token as an argument (for example
    via ``-H "Authorization: Bearer <token>"`` or ``-f token=<token>``); without
    redaction those would land verbatim in diagnostic evidence. The same redaction
    applied to stderr is applied per-arg here.
    """
    return " ".join(_redact(arg) for arg in result.cmd)


def gh_failed_diag(rule: Rule, op: str, result: GhResult) -> Diagnostic:
    """Produce a diagnostic for a non-zero gh exit that is not auth-related.

    Status is UNAVAILABLE for expected failures (bad args, rate-limit, …) and
    ERROR for unexpected internals (returncode < 0 from OSError/timeout).
    """
    if result.returncode < 0:
        status = Status.ERROR
    else:
        status = Status.UNAVAILABLE

    return _base_diag(
        rule,
        status,
        f"gh {op!r} exited with returncode {result.returncode}.",
        [
            Evidence(
                kind="gh_failure",
                data={
                    "op": op,
                    "returncode": result.returncode,
                    "stderr_excerpt": _stderr_excerpt(result),
                    "cmd": _safe_cmd(result),
                },
            )
        ],
    )


def gh_auth_diag(rule: Rule, op: str, result: GhResult) -> Diagnostic:
    """Produce an UNAVAILABLE diagnostic when stderr indicates an auth failure."""
    return _base_diag(
        rule,
        Status.UNAVAILABLE,
        f"gh {op!r} failed due to authentication or authorization error (returncode {result.returncode}).",
        [
            Evidence(
                kind="gh_auth_failure",
                data={
                    "op": op,
                    "returncode": result.returncode,
                    "stderr_excerpt": _stderr_excerpt(result),
                    "cmd": _safe_cmd(result),
                },
            )
        ],
    )


def gh_json_diag(rule: Rule, op: str, message: str) -> Diagnostic:
    """Produce an UNAVAILABLE diagnostic when gh output cannot be parsed as JSON."""
    return _base_diag(
        rule,
        Status.UNAVAILABLE,
        f"gh {op!r} returned malformed JSON.",
        [
            Evidence(
                kind="gh_json_error",
                data={"op": op, "error": message},
            )
        ],
    )


def gh_pagination_diag(rule: Rule, op: str, *, end_cursor: str | None) -> Diagnostic:
    """Produce an UNAVAILABLE diagnostic when a paginated result has more pages."""
    return _base_diag(
        rule,
        Status.UNAVAILABLE,
        f"gh {op!r} returned a paginated result with additional pages; full evaluation is unavailable.",
        [
            Evidence(
                kind="gh_pagination_unavailable",
                data={
                    "op": op,
                    "has_next_page": True,
                    "end_cursor": end_cursor,
                },
            )
        ],
    )


def gh_missing_field_diag(rule: Rule, op: str, field: str) -> Diagnostic:
    """Produce an UNAVAILABLE diagnostic when a required field is absent from gh output."""
    return _base_diag(
        rule,
        Status.UNAVAILABLE,
        f"gh {op!r} response is missing required field {field!r}.",
        [
            Evidence(
                kind="gh_missing_field",
                data={"op": op, "field": field},
            )
        ],
    )


def failure_diag(rule: Rule, op: str, result: GhResult) -> Diagnostic:
    """Dispatch to the appropriate failure diagnostic builder based on GhResult.

    This is the single entry point for per-rule checks to call after a failed
    run_gh() — it classifies the failure and returns the right Diagnostic.
    """
    category = classify_gh_failure(result)
    if category == "missing":
        return gh_missing_diag(rule)
    if category == "auth":
        return gh_auth_diag(rule, op, result)
    return gh_failed_diag(rule, op, result)


__all__ = [
    "GhResult",
    "run_gh",
    "parse_json",
    "classify_gh_failure",
    "failure_diag",
    "gh_missing_diag",
    "gh_failed_diag",
    "gh_auth_diag",
    "gh_json_diag",
    "gh_pagination_diag",
    "gh_missing_field_diag",
]
