"""Backend protocol for gate-keeper validation backends.

Each backend receives a Rule and a target, and returns a Diagnostic.
Implementations must never raise — unexpected errors become ERROR diagnostics.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from gate_keeper.models import Diagnostic, Rule


class BackendImpl(Protocol):
    """Protocol that every validation backend must satisfy."""

    name: str

    def check(self, rule: Rule, target: str | Path) -> Diagnostic:
        """Evaluate *rule* against *target* and return a Diagnostic; never raises."""
        ...


__all__ = ["BackendImpl"]
