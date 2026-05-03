"""Tool-agnostic CLI runner for external-CLI backend adapters.

Provides:
- CliResult        — typed result container for arbitrary CLI invocations
- CliFault         — coarse failure category enum
- run_cli()        — subprocess wrapper; never raises
- classify_cli_failure() — triage helper for CliResult failures
- Diagnostic builders  — cli_missing_diag, cli_failed_diag, cli_timeout_diag
- failure_diag()   — single dispatch entry point

Mirrors the shape of ``backends/_gh.py`` but holds no GitHub-specific knowledge:
no auth-marker detection, no built-in token regex. Per-tool adapters wire this
runner into a backend (e.g. the planned textlint adapter under
``Backend.EXTERNAL``) and supply their own redaction policy via the optional
``redactor`` parameter.
"""

from __future__ import annotations

import enum
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Sequence

from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

Redactor = Callable[[str], str]


def _identity(text: str) -> str:
    return text


# ---------------------------------------------------------------------------
# CliFault
# ---------------------------------------------------------------------------


class CliFault(str, enum.Enum):
    """Coarse classification of a failed CliResult.

    ``MISSING``      — the executable was not found on PATH.
    ``TIMEOUT``      — subprocess.TimeoutExpired raised by the OS.
    ``OS_ERROR``     — any other OSError raised before/while spawning.
    ``NONZERO_EXIT`` — the process ran to completion with a non-zero status
                       (this also covers signal-terminated runs reported as
                       a negative returncode by the OS).
    """

    MISSING = "missing"
    TIMEOUT = "timeout"
    OS_ERROR = "os_error"
    NONZERO_EXIT = "nonzero_exit"


# Sentinel returncodes used when no real OS-level returncode is available.
# Negative values cannot collide with a real process exit (0–255) and let
# ``classify_cli_failure`` reconstruct the fault from a CliResult alone.
_RETURNCODE_BINARY_MISSING = 127  # POSIX convention for "command not found"
_RETURNCODE_TIMEOUT = -2
_RETURNCODE_OS_ERROR = -3


# ---------------------------------------------------------------------------
# CliResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliResult:
    """Typed result of a CLI invocation.

    ``binary_missing`` and ``timed_out`` are tracked as explicit flags rather
    than inferred from ``returncode`` because Unix reports signal-terminated
    processes with negative return codes too — for example, SIGHUP yields
    ``returncode == -1`` — so a sentinel return code would conflate the two.
    """

    ok: bool
    stdout: str
    stderr: str  # already passed through the caller's redactor
    returncode: int
    cmd: tuple[str, ...]  # the actual argv, for diagnostics
    binary_missing: bool = False
    timed_out: bool = False


# ---------------------------------------------------------------------------
# run_cli
# ---------------------------------------------------------------------------


def run_cli(
    executable: str,
    args: Sequence[str],
    *,
    input: str | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    redactor: Redactor | None = None,
) -> CliResult:
    """Run ``executable <args>`` and return a CliResult; never raises.

    - Captures stdout/stderr as UTF-8 (errors="replace").
    - Non-zero exit codes are reflected in ``CliResult.ok = False``; no exception.
    - ``FileNotFoundError``, ``TimeoutExpired``, and other ``OSError`` variants
      are caught and returned as failed CliResult objects.
    - ``redactor`` (if provided) is applied to stderr before storing. Callers
      that handle credentials are expected to supply one; the default is the
      identity function so no redaction is performed.
    """
    redact = redactor or _identity
    argv: tuple[str, ...] = (executable, *args)
    try:
        completed = subprocess.run(
            list(argv),
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        return CliResult(
            ok=completed.returncode == 0,
            stdout=completed.stdout,
            stderr=redact(completed.stderr),
            returncode=completed.returncode,
            cmd=argv,
        )
    except FileNotFoundError:
        return CliResult(
            ok=False,
            stdout="",
            stderr=f"{executable!r} binary not found",
            returncode=_RETURNCODE_BINARY_MISSING,
            cmd=argv,
            binary_missing=True,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stderr = redact(exc.stderr) if isinstance(exc.stderr, str) else ""
        return CliResult(
            ok=False,
            stdout="",
            stderr=f"timed out after {exc.timeout}s; {partial_stderr}".strip("; "),
            returncode=_RETURNCODE_TIMEOUT,
            cmd=argv,
            timed_out=True,
        )
    except OSError as exc:
        return CliResult(
            ok=False,
            stdout="",
            stderr=f"OSError: {exc}",
            returncode=_RETURNCODE_OS_ERROR,
            cmd=argv,
        )


# ---------------------------------------------------------------------------
# classify_cli_failure
# ---------------------------------------------------------------------------


def classify_cli_failure(result: CliResult) -> CliFault:
    """Classify a failed CliResult into a CliFault category.

    Trusts the explicit ``binary_missing`` / ``timed_out`` flags rather than
    the returncode value — see CliResult docstring.
    """
    if result.binary_missing:
        return CliFault.MISSING
    if result.timed_out:
        return CliFault.TIMEOUT
    if result.returncode == _RETURNCODE_OS_ERROR:
        return CliFault.OS_ERROR
    return CliFault.NONZERO_EXIT


# ---------------------------------------------------------------------------
# Diagnostic builders
# ---------------------------------------------------------------------------


_STDERR_EXCERPT_LIMIT = 300


def _stderr_excerpt(result: CliResult, limit: int = _STDERR_EXCERPT_LIMIT) -> str:
    text = result.stderr
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _safe_cmd(result: CliResult, redactor: Redactor | None) -> str:
    """Return a space-joined command string with the caller's redactor applied per-arg."""
    redact = redactor or _identity
    return " ".join(redact(arg) for arg in result.cmd)


def _base_diag(
    rule: Rule,
    backend: Backend,
    status: Status,
    message: str,
    evidence: list[Evidence],
) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=backend,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


def cli_missing_diag(rule: Rule, backend: Backend, executable: str) -> Diagnostic:
    """Produce an UNAVAILABLE diagnostic when the CLI binary is not on PATH."""
    return _base_diag(
        rule,
        backend,
        Status.UNAVAILABLE,
        f"{executable!r} CLI is not installed or not found on PATH; cannot evaluate rule.",
        [
            Evidence(
                kind="cli_missing",
                data={"executable": executable},
            )
        ],
    )


def cli_failed_diag(
    rule: Rule,
    backend: Backend,
    op: str,
    result: CliResult,
    *,
    redactor: Redactor | None = None,
) -> Diagnostic:
    """Produce a diagnostic for a non-zero CLI exit (not timeout, not OS error).

    Status is UNAVAILABLE — a non-zero exit means we could not produce a
    pass/fail answer, which is the fail-closed convention used elsewhere in
    the codebase.
    """
    return _base_diag(
        rule,
        backend,
        Status.UNAVAILABLE,
        f"{op!r} exited with returncode {result.returncode}.",
        [
            Evidence(
                kind="cli_failure",
                data={
                    "op": op,
                    "executable": result.cmd[0] if result.cmd else "",
                    "returncode": result.returncode,
                    "stderr_excerpt": _stderr_excerpt(result),
                    "cmd": _safe_cmd(result, redactor),
                },
            )
        ],
    )


def cli_timeout_diag(
    rule: Rule,
    backend: Backend,
    op: str,
    result: CliResult,
    *,
    redactor: Redactor | None = None,
) -> Diagnostic:
    """Produce an ERROR diagnostic when a CLI invocation timed out.

    Timeout is treated as ERROR (not UNAVAILABLE) because it indicates an
    abnormal local-environment condition rather than a remote rate-limit /
    bad-args / missing-data signal. This mirrors how ``_gh.py`` maps negative
    returncodes to ``Status.ERROR``.
    """
    return _base_diag(
        rule,
        backend,
        Status.ERROR,
        f"{op!r} timed out (returncode {result.returncode}).",
        [
            Evidence(
                kind="cli_timeout",
                data={
                    "op": op,
                    "executable": result.cmd[0] if result.cmd else "",
                    "returncode": result.returncode,
                    "stderr_excerpt": _stderr_excerpt(result),
                    "cmd": _safe_cmd(result, redactor),
                },
            )
        ],
    )


def cli_os_error_diag(
    rule: Rule,
    backend: Backend,
    op: str,
    result: CliResult,
    *,
    redactor: Redactor | None = None,
) -> Diagnostic:
    """Produce an ERROR diagnostic for an OSError raised while spawning the CLI."""
    return _base_diag(
        rule,
        backend,
        Status.ERROR,
        f"{op!r} failed with an OS error before completion.",
        [
            Evidence(
                kind="cli_os_error",
                data={
                    "op": op,
                    "executable": result.cmd[0] if result.cmd else "",
                    "returncode": result.returncode,
                    "stderr_excerpt": _stderr_excerpt(result),
                    "cmd": _safe_cmd(result, redactor),
                },
            )
        ],
    )


def failure_diag(
    rule: Rule,
    backend: Backend,
    op: str,
    result: CliResult,
    *,
    redactor: Redactor | None = None,
) -> Diagnostic:
    """Dispatch to the appropriate failure diagnostic builder based on CliResult.

    This is the single entry point for per-rule checks to call after a failed
    ``run_cli()`` — it classifies the failure and returns the right Diagnostic.
    """
    fault = classify_cli_failure(result)
    if fault is CliFault.MISSING:
        executable = result.cmd[0] if result.cmd else ""
        return cli_missing_diag(rule, backend, executable)
    if fault is CliFault.TIMEOUT:
        return cli_timeout_diag(rule, backend, op, result, redactor=redactor)
    if fault is CliFault.OS_ERROR:
        return cli_os_error_diag(rule, backend, op, result, redactor=redactor)
    return cli_failed_diag(rule, backend, op, result, redactor=redactor)


__all__ = [
    "CliFault",
    "CliResult",
    "Redactor",
    "run_cli",
    "classify_cli_failure",
    "failure_diag",
    "cli_missing_diag",
    "cli_failed_diag",
    "cli_timeout_diag",
    "cli_os_error_diag",
]
