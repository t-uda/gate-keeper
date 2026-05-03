# `Backend.EXTERNAL` — adapter pattern for third-party tools

This document is the contract for the `external` backend: a single dispatcher
that routes rules to per-tool adapters (textlint, vale, eslint, …). Tracking
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
Adapter registration is intended to happen during application setup. The
foundation ships with **no** adapters registered — the first concrete
adapter (textlint) lands in a follow-up issue under #80.

For test isolation, the module exposes:

- `clear_adapters()` — drop everything;
- `snapshot_adapters()` / `restore_adapters(snapshot)` — save/restore around
  a test that mutates the registry.

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
