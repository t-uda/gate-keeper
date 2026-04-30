"""GitHub backend for gate-keeper.

Per-rule checks land in issues #9-#13. This module provides the registered
``check()`` entry point and re-exports the public adapter API from ``_gh``
so downstream callers only need to import ``gate_keeper.backends.github``.

All rule kinds currently return ``Status.UNAVAILABLE`` via the shared
diagnostic builders in ``_gh``, so the message shape is consistent with
what future per-rule checks will produce on auth/network failure.
"""
from __future__ import annotations

from pathlib import Path

from gate_keeper.backends._gh import (  # noqa: F401  (re-export)
    GhResult,
    classify_gh_failure,
    failure_diag,
    gh_auth_diag,
    gh_failed_diag,
    gh_json_diag,
    gh_missing_diag,
    gh_missing_field_diag,
    gh_pagination_diag,
    parse_json,
    run_gh,
)
from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

name = "github"


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Return UNAVAILABLE; per-rule GitHub checks land in issues #9-#13.

    The message follows the same shape as future auth/network failures so
    callers can recognise the pattern without special-casing the stub.
    """
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.GITHUB,
        status=Status.UNAVAILABLE,
        severity=rule.severity,
        message=f"github backend rule kind {rule.kind.value!r} not yet implemented (#9-#13)",
        evidence=[
            Evidence(
                kind="backend_stub",
                data={"backend": "github", "rule_kind": rule.kind.value},
            )
        ],
    )
