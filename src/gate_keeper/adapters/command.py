"""Generic project-local ``command`` external adapter (#149).

Lets a trusted local rule document delegate a check to a project-local
executable. The adapter passes the rule and target to the command as a
JSON object on stdin and parses a single ``Diagnostic`` from stdout.

Trust model
-----------
This adapter executes arbitrary local commands. It is disabled by default;
the CLI flag ``--allow-command-adapter`` enables it for the current process
only. Without the flag, every ``external_check`` rule whose
``params.tool == "command"`` returns ``Status.UNAVAILABLE`` with a
``command_adapter_disabled`` evidence record. Documentation MUST warn that
running this flag against an untrusted rule document is a remote-code-
execution vector — the rule document supplies the argv.

Security constraints
--------------------
- ``argv`` is a non-empty list of strings; shell strings are rejected.
- ``subprocess.run`` is invoked with ``shell=False`` (via
  ``backends._cli.run_cli``); no shell expansion or globbing is performed.
- No ``argv`` element is ever passed through string formatting,
  ``os.path.expandvars``, or ``os.path.expanduser``.
- Target is forwarded only via stdin JSON (and never via shell
  interpolation or environment substitution into ``argv``).
- A timeout (default 30s, hard maximum 300s) bounds runtime; expiry
  produces a fail-closed ``Diagnostic`` and the OS kills the subprocess
  via ``subprocess.run``'s timeout machinery.
- ``stderr`` is truncated in evidence to bounded size for forensic use.

Output contract
---------------
The command must emit exactly one of the following JSON shapes on stdout:

- a ``Diagnostic`` object (single rule result), OR
- a ``DiagnosticReport`` with one diagnostic in its ``diagnostics`` array.

Either way the adapter:

- echoes ``rule.id``, ``rule.source``, and ``rule.severity`` from the
  input rule onto whatever the command produced (the command's own copies
  of those fields are ignored — the adapter is the source of truth);
- forces ``backend == external``.

Malformed JSON, an output that does not match the expected diagnostic
shape, or a status outside the allowed enum produces an
``unavailable`` / ``parse_error`` diagnostic.

See ``docs/backend-external.md`` and the security-warning callout in
``docs/cli-reference.md`` for the user-facing contract.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from gate_keeper.backends._cli import (
    CliFault as _CliFault,
)
from gate_keeper.backends._cli import (
    classify_cli_failure,
    cli_missing_diag,
    cli_os_error_diag,
    cli_timeout_diag,
    run_cli,
)
from gate_keeper.models import (
    Backend,
    Diagnostic,
    Evidence,
    Rule,
    Status,
)

#: Default subprocess timeout (seconds) when ``params.timeout_seconds`` is unset.
DEFAULT_TIMEOUT_SECONDS = 30
#: Hard upper bound on ``params.timeout_seconds`` (seconds).
MAX_TIMEOUT_SECONDS = 300

#: Cap on stderr characters retained in evidence to keep diagnostics bounded.
_STDERR_EXCERPT_LIMIT = 1000

# ---------------------------------------------------------------------------
# Process-local enable flag
# ---------------------------------------------------------------------------
#
# The flag is stored in a one-element mutable list rather than a plain module
# attribute so callers can use ``contextlib.contextmanager`` patterns and so
# tests can ``snapshot`` / ``restore`` predictably without monkeypatching the
# module attribute (which can collide with reload / re-import semantics).

_ENABLED: list[bool] = [False]


def is_enabled() -> bool:
    """Return whether the ``command`` adapter is allowed to spawn subprocesses."""
    return _ENABLED[0]


def set_enabled(value: bool) -> None:
    """Set the process-local enable flag. CLI use only."""
    _ENABLED[0] = bool(value)


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------


def _diag(
    rule: Rule,
    status: Status,
    message: str,
    evidence: list[Evidence],
    remediation: str | None = None,
) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.EXTERNAL,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
        remediation=remediation,
    )


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Params:
    argv: list[str]
    timeout_seconds: float


def _validate_params(rule: Rule) -> _Params | Diagnostic:
    """Return parsed params or a fail-closed ``Diagnostic`` with the reason."""
    raw_argv = rule.params.get("argv")
    if raw_argv is None:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires params.argv (non-empty list of strings)",
            [Evidence(kind="params_error", data={"missing": "argv"})],
            remediation=(
                'Set params.argv to a non-empty list of strings, e.g. ["python", "tools/policy/check.py"].'
            ),
        )
    if not isinstance(raw_argv, list):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires params.argv to be a list of strings (no shell strings)",
            [
                Evidence(
                    kind="params_error",
                    data={"field": "argv", "type": type(raw_argv).__name__},
                )
            ],
            remediation=(
                "Replace any shell-string argv with a list, e.g. "
                '["python", "tools/check.py"]; shell strings are not supported.'
            ),
        )
    if not raw_argv:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires params.argv to be a non-empty list",
            [Evidence(kind="params_error", data={"field": "argv", "reason": "empty"})],
            remediation=("Provide at least the executable as the first argv element."),
        )
    for index, item in enumerate(raw_argv):
        if not isinstance(item, str):
            return _diag(
                rule,
                Status.UNAVAILABLE,
                "command adapter requires every argv element to be a string",
                [
                    Evidence(
                        kind="params_error",
                        data={
                            "field": "argv",
                            "index": index,
                            "type": type(item).__name__,
                        },
                    )
                ],
                remediation=(
                    "Argv elements must be strings; coerce numeric / path "
                    "values to str at rule-authoring time."
                ),
            )

    raw_timeout = rule.params.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires params.timeout_seconds to be a positive number",
            [
                Evidence(
                    kind="params_error",
                    data={
                        "field": "timeout_seconds",
                        "type": type(raw_timeout).__name__,
                    },
                )
            ],
        )
    timeout_value = float(raw_timeout)
    if not (0 < timeout_value <= MAX_TIMEOUT_SECONDS):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            (f"command adapter requires 0 < params.timeout_seconds <= {MAX_TIMEOUT_SECONDS}"),
            [
                Evidence(
                    kind="params_error",
                    data={
                        "field": "timeout_seconds",
                        "value": timeout_value,
                        "max": MAX_TIMEOUT_SECONDS,
                    },
                )
            ],
        )

    return _Params(argv=list(raw_argv), timeout_seconds=timeout_value)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

_ALLOWED_STATUS_VALUES = frozenset(s.value for s in Status)


def _stderr_excerpt(stderr: str) -> str:
    if len(stderr) <= _STDERR_EXCERPT_LIMIT:
        return stderr
    return stderr[:_STDERR_EXCERPT_LIMIT] + "…"


def _parse_output(
    rule: Rule,
    stdout: str,
    stderr: str,
) -> Diagnostic:
    """Parse *stdout* into a Diagnostic, or return a fail-closed parse_error.

    Accepts either a single ``Diagnostic`` JSON object or a
    ``DiagnosticReport`` containing exactly one diagnostic. The adapter
    overwrites ``rule_id``, ``source``, ``severity``, and ``backend`` from
    the input rule so a misbehaving command cannot spoof another rule.
    """
    if not stdout.strip():
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter received empty stdout from the project-local command",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "empty_stdout",
                        "stderr_excerpt": _stderr_excerpt(stderr),
                    },
                )
            ],
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter could not parse stdout as JSON",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "json_decode_error",
                        "error": str(exc),
                        "stdout_excerpt": stdout[:300],
                        "stderr_excerpt": _stderr_excerpt(stderr),
                    },
                )
            ],
        )

    if not isinstance(payload, dict):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter expected a JSON object on stdout (Diagnostic or DiagnosticReport)",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "non_object_top_level",
                        "type": type(payload).__name__,
                    },
                )
            ],
        )

    diagnostic_payload: Any = payload
    if "diagnostics" in payload and "status" not in payload:
        diagnostics = payload.get("diagnostics")
        if not isinstance(diagnostics, list) or len(diagnostics) != 1:
            return _diag(
                rule,
                Status.UNAVAILABLE,
                "command adapter expected exactly one diagnostic in DiagnosticReport.diagnostics",
                [
                    Evidence(
                        kind="parse_error",
                        data={
                            "reason": "diagnostic_count",
                            "count": len(diagnostics) if isinstance(diagnostics, list) else None,
                        },
                    )
                ],
            )
        diagnostic_payload = diagnostics[0]

    if not isinstance(diagnostic_payload, dict):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter expected a Diagnostic object",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "non_object_diagnostic",
                        "type": type(diagnostic_payload).__name__,
                    },
                )
            ],
        )

    raw_status = diagnostic_payload.get("status")
    if raw_status not in _ALLOWED_STATUS_VALUES:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter received an invalid or missing diagnostic status",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "invalid_status",
                        "status": raw_status,
                        "allowed": sorted(_ALLOWED_STATUS_VALUES),
                    },
                )
            ],
        )

    raw_message = diagnostic_payload.get("message")
    if not isinstance(raw_message, str):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires diagnostic.message to be a string",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "invalid_message",
                        "type": type(raw_message).__name__,
                    },
                )
            ],
        )

    raw_evidence = diagnostic_payload.get("evidence", [])
    parsed_evidence: list[Evidence] = []
    if isinstance(raw_evidence, list):
        for index, item in enumerate(raw_evidence):
            if not isinstance(item, dict):
                return _diag(
                    rule,
                    Status.UNAVAILABLE,
                    "command adapter requires each evidence entry to be an object",
                    [
                        Evidence(
                            kind="parse_error",
                            data={
                                "reason": "invalid_evidence_item",
                                "index": index,
                                "type": type(item).__name__,
                            },
                        )
                    ],
                )
            kind = item.get("kind")
            data = item.get("data", {})
            if not isinstance(kind, str) or not kind:
                return _diag(
                    rule,
                    Status.UNAVAILABLE,
                    "command adapter requires each evidence.kind to be a non-empty string",
                    [
                        Evidence(
                            kind="parse_error",
                            data={
                                "reason": "invalid_evidence_kind",
                                "index": index,
                            },
                        )
                    ],
                )
            if not isinstance(data, dict):
                return _diag(
                    rule,
                    Status.UNAVAILABLE,
                    "command adapter requires each evidence.data to be an object",
                    [
                        Evidence(
                            kind="parse_error",
                            data={
                                "reason": "invalid_evidence_data",
                                "index": index,
                                "type": type(data).__name__,
                            },
                        )
                    ],
                )
            parsed_evidence.append(Evidence(kind=kind, data=dict(data)))
    else:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires diagnostic.evidence to be a list",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "invalid_evidence_list",
                        "type": type(raw_evidence).__name__,
                    },
                )
            ],
        )

    raw_remediation = diagnostic_payload.get("remediation")
    if raw_remediation is not None and not isinstance(raw_remediation, str):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "command adapter requires diagnostic.remediation to be a string or null",
            [
                Evidence(
                    kind="parse_error",
                    data={
                        "reason": "invalid_remediation",
                        "type": type(raw_remediation).__name__,
                    },
                )
            ],
        )

    # Adapter is the source of truth for rule_id / source / severity / backend;
    # untrusted output cannot rebrand someone else's rule.
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.EXTERNAL,
        status=Status(raw_status),
        severity=rule.severity,
        message=raw_message,
        evidence=parsed_evidence,
        remediation=raw_remediation,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CommandAdapter:
    """Run a project-local command and parse its JSON Diagnostic from stdout.

    See module docstring for the trust model and contract.
    """

    name = "command"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        if not is_enabled():
            return _diag(
                rule,
                Status.UNAVAILABLE,
                (
                    "command adapter is disabled by default for security; pass "
                    "--allow-command-adapter to enable it for trusted rule documents"
                ),
                [
                    Evidence(
                        kind="command_adapter_disabled",
                        data={"flag": "--allow-command-adapter"},
                    )
                ],
                remediation=(
                    "Pass --allow-command-adapter ONLY when validating with a "
                    "rule document you fully trust. The command adapter executes "
                    "arbitrary local commands defined by the rule document."
                ),
            )

        params_or_diag = _validate_params(rule)
        if isinstance(params_or_diag, Diagnostic):
            return params_or_diag
        params = params_or_diag

        target_str = str(target)
        stdin_payload = json.dumps(
            {
                "rule": {
                    "id": rule.id,
                    "text": rule.text,
                    "params": dict(rule.params),
                },
                "target": target_str,
            },
            sort_keys=True,
        )

        executable = params.argv[0]
        rest = params.argv[1:]
        result = run_cli(
            executable,
            rest,
            input=stdin_payload,
            timeout=params.timeout_seconds,
        )

        # Sub-process-level failures (binary missing / timeout / OS error) are
        # mapped to the shared cli_* diagnostic vocabulary. A non-zero exit is
        # a strict, fail-closed signal in this adapter — the contract says the
        # command must emit a JSON diagnostic on stdout regardless of pass/fail
        # outcome, so non-zero exit means the command itself faulted.
        if not result.ok:
            fault = classify_cli_failure(result)
            if fault is _CliFault.MISSING:
                return cli_missing_diag(rule, Backend.EXTERNAL, executable)
            if fault is _CliFault.TIMEOUT:
                return cli_timeout_diag(rule, Backend.EXTERNAL, "command", result)
            if fault is _CliFault.OS_ERROR:
                return cli_os_error_diag(rule, Backend.EXTERNAL, "command", result)
            # NONZERO_EXIT: surface stderr excerpt for debugging.
            return _diag(
                rule,
                Status.UNAVAILABLE,
                f"command adapter subprocess exited non-zero ({result.returncode})",
                [
                    Evidence(
                        kind="command_failure",
                        data={
                            "executable": executable,
                            "returncode": result.returncode,
                            "stderr_excerpt": _stderr_excerpt(result.stderr),
                            "stdout_excerpt": result.stdout[:300],
                        },
                    )
                ],
                remediation=(
                    "The project-local command must emit a Diagnostic JSON on "
                    "stdout and exit zero on both pass and fail. Investigate the "
                    "stderr excerpt and rerun once the command is healthy."
                ),
            )

        return _parse_output(rule, result.stdout or "", result.stderr or "")


__all__ = [
    "CommandAdapter",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "is_enabled",
    "set_enabled",
]
