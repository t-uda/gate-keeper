"""Backend registry for gate-keeper.

Maps backend name strings to callable check functions.  Each entry wraps a
module-level ``check(rule, target) -> Diagnostic`` function so callers can
look up backends by name without importing modules directly.

Registered names (all four must be present for `--backend` choices):
  ``filesystem``, ``github``, ``llm-rubric``, ``external``

The ``external`` backend is a dispatcher for third-party tool adapters
(textlint, vale, …); see ``gate_keeper.backends.external`` and
``docs/backend-external.md`` for the adapter contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

# Import backend modules — avoid circular deps by keeping these as plain imports.
from gate_keeper.backends import external as _ext
from gate_keeper.backends import filesystem as _fs
from gate_keeper.backends import github as _gh
from gate_keeper.backends import llm_rubric as _llm
from gate_keeper.models import Diagnostic, Rule

# Registry: name -> check callable
_REGISTRY: dict[str, Callable[[Rule, str | Path], Diagnostic]] = {
    _fs.name: _fs.check,
    _gh.name: _gh.check,
    _llm.name: _llm.check,
    _ext.name: _ext.check,
}

#: Sorted list of all registered backend names (used for argparse ``choices``).
BACKEND_NAMES: list[str] = sorted(_REGISTRY)


def get(name: str) -> Callable[[Rule, str | Path], Diagnostic] | None:
    """Return the check callable for *name*, or ``None`` if not registered."""
    return _REGISTRY.get(name)


def is_registered(name: str) -> bool:
    """Return True if *name* is a known backend."""
    return name in _REGISTRY


__all__ = ["BACKEND_NAMES", "get", "is_registered"]
