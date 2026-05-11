# `Backend.EXTERNAL` — adapter pattern for third-party tools

This document is the contract for the `external` backend: a single dispatcher
that routes rules to per-tool adapters (textlint, vale, ESLint, …). Tracking
umbrella: #80. New tools integrate as adapters here, not as new
`Backend` enum values.

## Why one backend, many adapters

The `Backend` enum is a stable surface in the IR (see
[`docs/rule-ir.md`](rule-ir.md)). Adding a new enum entry per tool would
churn that surface for every integration and force every consumer of the IR
to update. A single `external` backend with a per-tool adapter selector keeps
the IR stable while letting the adapter set evolve independently.

## Selecting an adapter

A rule routed to this backend has:

- `backend_hint = "external"`
- `kind = "external_check"`
- `params.tool = "<adapter id>"` — the registered name of the adapter
  (e.g. `"textlint"`).

All other `params` keys are forwarded verbatim to the adapter, which owns its
own per-tool key set. The dispatcher does not validate adapter-specific keys.

## Adapter protocol

```python
from gate_keeper.backends.external import ExternalAdapter, register
from gate_keeper.models import Diagnostic, Rule
from pathlib import Path

class MyAdapter:
    name = "my-tool"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        ...

register(MyAdapter())
```

Adapters must:

- expose a non-empty string `name` matching the value rules use in `params.tool`;
- implement `check(rule, target) -> Diagnostic` and never raise — return an
  `unavailable` Diagnostic with adapter-defined evidence on internal failure;
- set `Diagnostic.backend = Backend.EXTERNAL` on every returned diagnostic;
- echo `Diagnostic.rule_id`, `Diagnostic.source`, and `Diagnostic.severity`
  from the input rule.

The dispatcher additionally traps any exception an adapter raises (defence in
depth) and converts it to `unavailable` / `adapter_error`, but adapters should
not rely on this — surface a structured diagnostic of your own instead.

## Dispatcher behaviour (fail-closed)

| Condition | Status | Evidence kind |
| --------- | ------ | ------------- |
| `kind` is not `external_check` | `unsupported` | `backend_capability` |
| `params.tool` missing or non-string or empty | `unavailable` | `params_error` |
| `params.tool` is set but no adapter is registered for it | `unsupported` | `adapter_unknown` (lists currently registered adapters) |
| Adapter raises | `unavailable` | `adapter_error` (records exception type and truncated message) |
| Adapter returns a diagnostic | passthrough | _(adapter-defined)_ |

`pass` is only ever produced by the adapter itself. Missing or
malformed routing data never collapses to `pass`.

## Registry lifecycle

The registry is a process-local dict in `gate_keeper.backends.external`.
Adapter registration is intended to happen during application setup; the
backing module is intentionally empty by default. The first concrete
adapter (textlint, #94) is shipped in `src/gate_keeper/adapters/textlint.py`
and registered lazily at `gate_keeper.cli.main()` entry — see "Registered
adapters" below.

For test isolation, the module exposes:

- `clear_adapters()` — drop everything;
- `snapshot_adapters()` / `restore_adapters(snapshot)` — save/restore around
  a test that mutates the registry.

## Registered adapters

These adapters ship with gate-keeper and self-register at CLI entry
(`gate_keeper.cli.main`):

- **textlint** — `src/gate_keeper/adapters/textlint.py` (#94)
  - Params: `tool="textlint"` (required); `config` (optional, path to a
    `.textlintrc`); `timeout` (optional, seconds; default 60).
  - Adapter-specific Evidence kinds: `textlint_finding`,
    `textlint_truncated`, `parse_error`. The adapter also surfaces the
    full `cli_*` evidence vocabulary from
    `src/gate_keeper/backends/_cli.py` (`cli_missing`, `cli_failure`,
    `cli_timeout`, `cli_os_error`) via `failure_diag` when the
    subprocess itself fails.
  - Status mapping: clean target → `pass`; one or more findings → `fail`;
    missing `npx` / unparseable output → `unavailable`; subprocess timeout or
    OS error → `error` (per the shared CLI-failure builders in
    `src/gate_keeper/backends/_cli.py`).
  - Severity-policy alignment: see
    [`docs/textlint/severity-policy.md §7`](textlint/severity-policy.md) for
    the resolution of the §6 deferred adapter-implementation decision.

- **command** — `src/gate_keeper/adapters/command.py` (#149)
  - Disabled by default for security. Enable per-process with
    `gate-keeper validate ... --allow-command-adapter`. The adapter
    executes arbitrary local commands defined by the rule document, so
    only enable for rule documents you fully trust. See the trust-model
    callout in [`docs/cli-reference.md`](cli-reference.md) for the full
    warning.
  - Params: `tool="command"` (required); `argv` (required, non-empty
    list of strings — shell strings rejected; `subprocess.run` runs with
    `shell=False`); `timeout_seconds` (optional, default `30`, hard
    maximum `300`).
  - Input contract: the adapter passes a single JSON object to the
    command on stdin:

    ```json
    { "rule": { "id": "...", "text": "...", "params": { ... } }, "target": "..." }
    ```

  - Output contract: the command must print a `Diagnostic` (or a
    `DiagnosticReport` containing exactly one diagnostic) to stdout and
    exit `0` for both pass and fail outcomes. The adapter overrides
    `rule_id`, `source`, `severity`, and `backend` on the returned
    Diagnostic so a misbehaving command cannot spoof another rule's
    result.
  - Adapter-specific Evidence kinds: `command_adapter_disabled` (default
    state when the flag is not passed), `params_error`,
    `command_failure` (non-zero exit), `parse_error` (malformed or
    missing JSON output). Subprocess-level faults (binary missing /
    timeout / OS error) produce the shared `cli_missing`, `cli_timeout`,
    and `cli_os_error` evidence kinds via `failure_diag`.
  - Status mapping: adapter disabled → `unavailable` /
    `command_adapter_disabled`; non-zero exit → `unavailable` /
    `command_failure`; missing executable → `unavailable` / `cli_missing`;
    timeout → `error` / `cli_timeout`; OS error → `error` / `cli_os_error`;
    malformed stdout → `unavailable` / `parse_error`; otherwise the
    parsed Diagnostic status from the command (typically `pass` or `fail`).

## Out of scope for the foundation

- Concrete adapter implementations (textlint, vale, …). See
  [Authoring new adapters](#authoring-new-adapters) below for the skeleton
  and worked examples.
- Subprocess invocation conventions, version pinning, or tool installation.
- Classifier rules that route Markdown text to `external_check` — the
  classifier still has no `external` branch; rules use `external_check`
  only when authored that way (or compiled from a future adapter-aware
  classification pass). The classifier *can* emit a non-binding
  suggestion pointing authors at this backend; see
  [Classifier suggestion (#215)](#classifier-suggestion-215) below.
- Concrete `--backend external` invocation flows; the choice is exposed
  for symmetry with the other backends, but the foundation has no adapters
  registered, so every rule routed there returns `unsupported` /
  `adapter_unknown` until #80 lands at least one adapter.

## Classifier suggestion (#215)

The classifier detects textlint-suitable patterns and reports them as
**non-binding suggestions** on the Rule IR. The effective backend
(`backend_hint` / `kind`) is **never** rewritten by the suggestion — the
author keeps control of routing.

When a heuristic matches (`use X instead of Y`, `must not use the term`,
`passive voice`, `first-person pronouns`, `sentence length`,
`capitalized as`, etc.), the classifier populates three advisory fields
on the Rule:

| Field | Type | Meaning |
| --- | --- | --- |
| `suggested_backend` | `str` | e.g. `"external+textlint"` |
| `suggestion_confidence` | `float` in `[0.0, 1.0]` | heuristic confidence |
| `suggestion_rationale` | `str` | short human-readable explanation |

These fields are omitted from `to_dict()` output when absent so existing
IR fixtures stay byte-identical.

**Adopting a suggestion (author opt-in).** To actually route a rule to
the external adapter, edit the compiled IR JSON to set:

```json
{
  "kind": "external_check",
  "backend_hint": "external",
  "params": { "tool": "textlint" }
}
```

Then run `gate-keeper validate --rules-format ir compiled.json --target …`.
The validator uses the explicit `kind` / `backend_hint`, not
`suggested_backend`. There is no auto-promotion: the suggestion is a hint
only, the hand-edited IR is the source of truth. This preserves the
fail-closed behaviour for authors who have not installed textlint while
still surfacing the opportunity.

---

## Authoring new adapters

This section is a practical guide for adding a new adapter (e.g. Vale,
ESLint, shellcheck). It covers the skeleton contract, subprocess patterns,
evidence-kind naming, and a checklist of error cases you must handle.

### Adapter skeleton

Every adapter must satisfy the `ExternalAdapter` protocol
(`src/gate_keeper/backends/external.py`):

```python
from __future__ import annotations

import json
from pathlib import Path

from gate_keeper.backends._cli import failure_diag, run_cli
from gate_keeper.backends.external import register
from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status


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


class MyToolAdapter:
    name = "my-tool"   # must match params.tool in the rule document

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        target_str = str(target)
        try:
            timeout = float(rule.params.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        result = run_cli("my-tool", ["--format", "json", target_str],
                         timeout=timeout)

        # Many linters exit non-zero when findings exist but still emit valid
        # JSON on stdout.  Attempt to parse stdout first; only treat a non-zero
        # exit as a hard failure when stdout is empty/unparseable and the result
        # indicates a real execution problem (missing binary, timeout, OS error,
        # or non-zero exit with no report).
        payload = None
        if result.stdout:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                pass

        if payload is None:
            if not result.ok:
                # Real execution failure: missing binary, timeout, OS error, or
                # non-zero exit with no usable output.
                return failure_diag(rule, Backend.EXTERNAL, "my-tool", result)
            return _diag(rule, Status.UNAVAILABLE, "my-tool produced unparseable JSON",
                         [Evidence(kind="parse_error",
                                   data={"stdout_excerpt": result.stdout[:300]})])

        findings = _extract_findings(payload)   # tool-specific; see examples below
        if not findings:
            return _diag(rule, Status.PASS,
                         f"my-tool reported no findings against {target_str}", [])

        evidence = [_finding_evidence(f) for f in findings]
        return _diag(rule, Status.FAIL,
                     f"my-tool reported {len(findings)} finding(s)", evidence,
                     remediation="Run `my-tool --fix <file>` where applicable.")


# Register inside _register_default_adapters() in gate_keeper/cli.py rather
# than at module import time, so that test isolation helpers (clear_adapters /
# snapshot_adapters) can control the registry without import-order side effects.
# See how the textlint adapter is wired in gate_keeper.cli._register_default_adapters().
#
# register(MyToolAdapter())
```

Call `register(MyToolAdapter())` at application setup, mirroring how the
textlint adapter is registered in `gate_keeper.cli.main()`.

### Subprocess invocation patterns

Use `run_cli` from `gate_keeper.backends._cli` — it never raises and returns
a `CliResult` with `ok`, `stdout`, `stderr`, `returncode`, `binary_missing`,
and `timed_out` flags.

| Tool output format | Parse strategy |
| --- | --- |
| **JSON** | `json.loads(result.stdout)`; validate shape before accessing keys. |
| **JSON-per-line (NDJSON)** | Split on newlines, `json.loads` each non-empty line, collect failures. |
| **Plain-text** | Parse line-by-line; use a regular expression to capture file/line/column/message. |
| **Severity-coded** | Store tool-native severity strings in `Evidence.data` for forensics. Do not map them to `Diagnostic.severity` — that field must always mirror `rule.severity` so that rule-level enforcement policy is preserved. |

Always test the parsed shape before trusting it. A non-zero exit code does
not always mean the run failed — many linters exit non-zero when violations
exist but stdout still contains the JSON report (textlint does this; check
your tool's documentation).

### Evidence-kind naming convention

Evidence kinds are free strings; keep them scoped to the adapter with a
`<tool>_` prefix so evidence records are self-describing in JSON reports.

| Situation | Recommended kind |
| --- | --- |
| One lint finding | `<tool>_finding` |
| Findings list was truncated | `<tool>_truncated` |
| Subprocess binary absent | `cli_missing` _(shared, from `failure_diag`)_ |
| Subprocess timed out | `cli_timeout` _(shared)_ |
| OS error spawning subprocess | `cli_os_error` _(shared)_ |
| Non-zero exit with no useful output | `cli_failure` _(shared)_ |
| Malformed / unexpected JSON shape | `parse_error` |
| Required `params` key absent | `params_error` |

The shared `cli_*` kinds are produced by `failure_diag` / the individual
`cli_*_diag` builders in `backends/_cli.py`; you get them for free by
calling `failure_diag` on a failed `CliResult`.

### Error-case handling checklist

Before marking an adapter complete, verify every branch below returns a
well-formed `Diagnostic` and never raises:

- [ ] Tool binary is not on PATH → `UNAVAILABLE` / `cli_missing`
- [ ] Tool exits non-zero with empty stdout (runtime crash) → `UNAVAILABLE` / `cli_failure`
- [ ] Tool exits non-zero but stdout contains valid JSON (linter-style) → parse normally
- [ ] Subprocess times out → `ERROR` / `cli_timeout`
- [ ] Subprocess raises `OSError` (permissions, bad interpreter) → `ERROR` / `cli_os_error`
- [ ] stdout is non-empty but not valid JSON → `UNAVAILABLE` / `parse_error`
- [ ] stdout is valid JSON but wrong shape (not expected list/dict) → `UNAVAILABLE` / `parse_error`
- [ ] `params.tool` or a required adapter param is missing → `UNAVAILABLE` / `params_error`
- [ ] Findings list is very large (>50 items) → cap evidence entries; add a `<tool>_truncated` entry
- [ ] `target` path does not exist (passed through to tool) → let the tool report it; surface as a finding or `cli_failure`

### Example: Vale adapter (prose style linting)

Vale lints prose against style guide rules and exits non-zero when violations
exist. Its `--output=JSON` flag emits a `{"<file>": [{...}, ...]}` map.

```python
"""Vale adapter — prose style guide linting via Backend.EXTERNAL."""

from __future__ import annotations

import json
from pathlib import Path

from gate_keeper.backends._cli import failure_diag, run_cli
from gate_keeper.backends.external import register
from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

_FINDINGS_LIMIT = 50


def _diag(rule, status, message, evidence, remediation=None):
    return Diagnostic(rule_id=rule.id, source=rule.source, backend=Backend.EXTERNAL,
                      status=status, severity=rule.severity, message=message,
                      evidence=evidence, remediation=remediation)


class ValeAdapter:
    name = "vale"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        target_str = str(target)
        style = rule.params.get("style")          # e.g. "Google" or "Microsoft"
        args = ["--output=JSON"]
        if style:
            args += [f"--config=/dev/null", f"--filter=Style=={style}"]
        args.append(target_str)

        try:
            timeout = float(rule.params.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        result = run_cli("vale", args, timeout=timeout)

        # Vale exits 1 when violations exist; stdout still contains the JSON
        # report.  Attempt to parse stdout first; only treat a non-zero exit
        # as a hard failure when stdout is absent or unparseable.
        payload: dict | None = None
        if result.stdout:
            try:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                pass

        if payload is None:
            if not result.ok:
                # Real execution failure: binary missing, timeout, OS error, or
                # non-zero exit with no usable JSON output.
                return failure_diag(rule, Backend.EXTERNAL, "vale", result)
            return _diag(rule, Status.UNAVAILABLE, "vale produced unparseable JSON",
                         [Evidence(kind="parse_error",
                                   data={"stdout_excerpt": result.stdout[:300]})])

        evidence: list[Evidence] = []
        total = truncated = 0
        for file_path, findings in payload.items():
            for f in (findings or []):
                total += 1
                if len(evidence) >= _FINDINGS_LIMIT:
                    truncated += 1
                    continue
                evidence.append(Evidence(kind="vale_finding", data={
                    "file": file_path, "line": f.get("Line"), "check": f.get("Check"),
                    "severity": f.get("Severity"), "message": f.get("Message"),
                }))
        if truncated:
            evidence.append(Evidence(kind="vale_truncated", data={"omitted": truncated}))

        if total == 0:
            return _diag(rule, Status.PASS, f"vale reported no findings in {target_str}", [])
        return _diag(rule, Status.FAIL, f"vale reported {total} finding(s)", evidence,
                     remediation="Correct prose issues flagged in evidence.")


# Wire ValeAdapter inside gate_keeper.cli._register_default_adapters() rather
# than at module import time — see skeleton note above.
# register(ValeAdapter())
```

### Example: ESLint adapter (JavaScript / TypeScript linting)

ESLint's `--format=json` flag emits a list of file-result objects, each with
a `messages` array. It exits non-zero when errors are found.

```python
"""ESLint adapter — JS/TS static analysis via Backend.EXTERNAL."""

from __future__ import annotations

import json
from pathlib import Path

from gate_keeper.backends._cli import failure_diag, run_cli
from gate_keeper.backends.external import register
from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

_FINDINGS_LIMIT = 50
_SEVERITY_MAP = {1: "warning", 2: "error"}


def _diag(rule, status, message, evidence, remediation=None):
    return Diagnostic(rule_id=rule.id, source=rule.source, backend=Backend.EXTERNAL,
                      status=status, severity=rule.severity, message=message,
                      evidence=evidence, remediation=remediation)


class ESLintAdapter:
    name = "eslint"

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        target_str = str(target)
        config = rule.params.get("config")        # optional --config path
        args = ["--format=json"]
        if config:
            args += ["--config", config]
        args.append(target_str)

        try:
            timeout = float(rule.params.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        result = run_cli("npx", ["--no", "eslint"] + args, timeout=timeout)

        # ESLint exits 1 on lint errors, 2 on internal failure.
        # exit 1 with JSON stdout is normal; exit 2 or binary missing is not.
        # Parse stdout first; only treat non-zero as a hard failure when no
        # usable JSON report was produced.
        file_reports: list[dict] | None = None
        if result.stdout:
            try:
                parsed = json.loads(result.stdout)
                if isinstance(parsed, list):
                    file_reports = parsed
            except json.JSONDecodeError:
                pass

        if file_reports is None:
            if not result.ok:
                return failure_diag(rule, Backend.EXTERNAL, "eslint", result)
            return _diag(rule, Status.UNAVAILABLE, "eslint JSON has unexpected shape",
                         [Evidence(kind="parse_error",
                                   data={"stdout_excerpt": result.stdout[:300]})])

        evidence: list[Evidence] = []
        total = truncated = 0
        for file_report in file_reports:
            if not isinstance(file_report, dict):
                return _diag(rule, Status.UNAVAILABLE,
                             "eslint JSON element has unexpected type",
                             [Evidence(kind="parse_error",
                                       data={"stdout_excerpt": result.stdout[:300]})])
            file_path = str(file_report.get("filePath") or target_str)
            for msg in (file_report.get("messages") or []):
                total += 1
                if len(evidence) >= _FINDINGS_LIMIT:
                    truncated += 1
                    continue
                evidence.append(Evidence(kind="eslint_violation", data={
                    "file": file_path, "line": msg.get("line"), "column": msg.get("column"),
                    "rule_id": msg.get("ruleId"), "severity": _SEVERITY_MAP.get(msg.get("severity"), "unknown"),
                    "message": msg.get("message"),
                }))
        if truncated:
            evidence.append(Evidence(kind="eslint_truncated", data={"omitted": truncated}))

        if total == 0:
            return _diag(rule, Status.PASS, f"eslint reported no violations in {target_str}", [])
        return _diag(rule, Status.FAIL, f"eslint reported {total} violation(s)", evidence,
                     remediation="Run `npx eslint --fix <file>` where auto-fix applies.")


# Wire ESLintAdapter inside gate_keeper.cli._register_default_adapters() rather
# than at module import time — see skeleton note above.
# register(ESLintAdapter())
```
