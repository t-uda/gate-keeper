"""textlint adapter — first concrete consumer of ``Backend.EXTERNAL``.

Invokes ``npx textlint --format json <target>`` and maps each finding to a
single Diagnostic with one Evidence entry per finding. Resolves the open
adapter-implementation decision tracked in
``docs/textlint/severity-policy.md §6``: any finding (regardless of textlint
message-level severity integer) yields ``Status.FAIL``; the integer severity
is preserved in evidence for forensic use.

Issue: #94. Umbrella: #80.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gate_keeper.backends._cli import failure_diag, run_cli
from gate_keeper.models import (
    Backend,
    Diagnostic,
    Evidence,
    Rule,
    Severity,
    Status,
)

# Cap evidence entries per Diagnostic to avoid pathological JSON sizes when
# textlint reports many findings (e.g. running against a corpus). Excess
# findings are summarised in a single ``textlint_truncated`` evidence entry.
_FINDINGS_LIMIT = 50

# Default subprocess timeout for textlint runs; overridable via
# ``rule.params["timeout"]``.
_DEFAULT_TIMEOUT_SECONDS = 60


def textlint_severity_to_gate_keeper(textlint_severity: str) -> Severity:
    """Map a textlint rule severity string to a gate-keeper ``Severity``.

    Per ``docs/textlint/severity-policy.md §5``. textlint configures rule
    severity as one of ``"error"`` / ``"warning"`` / ``"info"`` in
    ``.textlintrc``; this helper converts that string. Unrecognised input
    falls back to ``Severity.WARNING`` (conservative anomaly marker, not
    fail-closed).

    NOTE: this helper exists for future per-textlint-rule severity wiring.
    The current adapter sets ``Diagnostic.severity = rule.severity`` and does
    not call this helper on the runtime per-message severity integer — see
    the policy doc for the rationale.
    """
    mapping = {
        "error": Severity.ERROR,
        "warning": Severity.WARNING,
        "info": Severity.ADVISORY,
    }
    return mapping.get(textlint_severity, Severity.WARNING)


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


def _finding_evidence(finding: dict[str, Any], file_path: str) -> Evidence:
    return Evidence(
        kind="textlint_finding",
        data={
            "file": file_path,
            "line": finding.get("line"),
            "column": finding.get("column"),
            "rule_id": finding.get("ruleId"),
            "severity_int": finding.get("severity"),
            "message": finding.get("message"),
            "fixable": bool(finding.get("fix")),
        },
    )


class TextlintAdapter:
    """Run ``npx textlint --format json`` and report findings as Evidence.

    Status mapping (resolves ``docs/textlint/severity-policy.md §6``):

    - 0 findings → ``Status.PASS``;
    - >=1 finding (any severity int) → ``Status.FAIL``;
    - non-zero exit with empty or unparseable stdout and a CLI-level fault →
      ``Status.UNAVAILABLE`` (or ``Status.ERROR`` for timeout / OS error)
      per ``failure_diag`` dispatch;
    - parseable but non-list / non-dict JSON → ``Status.UNAVAILABLE`` /
      ``parse_error``.

    ``Diagnostic.severity`` always equals ``rule.severity``. The textlint
    per-message severity integer is preserved in
    ``Evidence(kind="textlint_finding").data["severity_int"]``.
    """

    name = "textlint"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        target_str = str(target)
        # `--no` makes npx fail rather than silently install textlint when it
        # is not present; gate-keeper runs are deterministic against the
        # already-installed toolchain, never the registry.
        args: list[str] = ["--no", "textlint", "--format", "json"]
        config = rule.params.get("config")
        if isinstance(config, str) and config:
            args.extend(["--config", config])
        args.append(target_str)

        timeout_value = rule.params.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
        try:
            timeout_seconds: float = float(timeout_value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            timeout_seconds = float(_DEFAULT_TIMEOUT_SECONDS)

        result = run_cli("npx", args, timeout=timeout_seconds)

        # textlint exits non-zero when violations are present, but stdout
        # still contains the JSON report. Try parsing first; only fall back
        # to the CLI-failure path when stdout is empty or unparseable AND the
        # CLI failed (e.g. binary missing, timeout, OS error, or runtime
        # crash with no JSON output).
        stdout = result.stdout or ""
        parsed: list[dict[str, Any]] | None = None
        malformed = False
        if stdout.strip():
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                # Reject lists that contain non-dict items rather than
                # silently filtering — a list of mixed types means textlint
                # produced a payload we do not understand, which must
                # surface as a parse_error rather than collapsing to PASS.
                if all(isinstance(item, dict) for item in payload):
                    parsed = [item for item in payload]
                else:
                    parsed = None
                    malformed = True
            else:
                malformed = True

        if parsed is None:
            # Prefer parse_error when stdout was non-empty but malformed (the
            # CLI ran successfully enough to emit *something* but the payload
            # is not the expected list-of-file-reports shape). Only fall
            # through to failure_diag when stdout is genuinely empty AND the
            # subprocess itself failed (binary missing, timeout, OS error,
            # nonzero exit with no JSON).
            if malformed:
                return _diag(
                    rule,
                    Status.UNAVAILABLE,
                    "textlint produced JSON that does not match the expected list-of-file-reports shape",
                    [
                        Evidence(
                            kind="parse_error",
                            data={
                                "stdout_excerpt": stdout[:300],
                                "stderr_excerpt": result.stderr[:300],
                            },
                        )
                    ],
                )
            if not result.ok:
                # binary_missing → cli_missing_diag (UNAVAILABLE),
                # timeout       → cli_timeout_diag (ERROR),
                # OS error      → cli_os_error_diag (ERROR),
                # nonzero exit  → cli_failed_diag (UNAVAILABLE).
                return failure_diag(rule, Backend.EXTERNAL, "textlint", result)
            return _diag(
                rule,
                Status.UNAVAILABLE,
                "textlint produced no parseable JSON output",
                [
                    Evidence(
                        kind="parse_error",
                        data={
                            "stdout_excerpt": stdout[:300],
                            "stderr_excerpt": result.stderr[:300],
                        },
                    )
                ],
            )

        evidence: list[Evidence] = []
        total_findings = 0
        truncated = 0
        for file_report in parsed:
            file_path = str(file_report.get("filePath") or target_str)
            messages = file_report.get("messages")
            if not isinstance(messages, list):
                continue
            for finding in messages:
                if not isinstance(finding, dict):
                    continue
                total_findings += 1
                if len(evidence) >= _FINDINGS_LIMIT:
                    truncated += 1
                    continue
                evidence.append(_finding_evidence(finding, file_path))

        if truncated:
            evidence.append(Evidence(kind="textlint_truncated", data={"omitted": truncated}))

        if total_findings == 0:
            return _diag(
                rule,
                Status.PASS,
                f"textlint reported no findings against {target_str}",
                [],
            )

        # >=1 finding ⇒ FAIL (severity-policy §7 resolution: any finding
        # fails, regardless of message-level severity integer).
        suffix = f" (+{truncated} truncated)" if truncated else ""
        return _diag(
            rule,
            Status.FAIL,
            f"textlint reported {total_findings} finding(s){suffix}",
            evidence,
            remediation=(
                "Run `npx textlint --fix <file>` where applicable, or "
                "address individual findings listed in evidence."
            ),
        )


__all__ = ["TextlintAdapter", "textlint_severity_to_gate_keeper"]
