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

- Concrete adapter implementations (textlint, vale, …).
- Subprocess invocation conventions, version pinning, or tool installation.
- Classifier rules that route Markdown text to `external_check` — the
  classifier still has no `external` branch; rules use `external_check`
  only when authored that way (or compiled from a future adapter-aware
  classification pass).
- Concrete `--backend external` invocation flows; the choice is exposed
  for symmetry with the other backends, but the foundation has no adapters
  registered, so every rule routed there returns `unsupported` /
  `adapter_unknown` until #80 lands at least one adapter.
