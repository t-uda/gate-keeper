"""Changed-file resolver for Mode A (PR-affected-set) validators.

Thin re-export shim.  Logic now lives in ``gate_keeper.changed`` (core) so
both the dependency-gates command adapter scripts and the CLI ``--target-changed``
flag share one implementation.  See ``docs/design/dependency-gates.md`` §4.3.

The public names exported here (``ChangedFilesError``, ``compute_changed_files``,
``resolve_base_ref``, ``DEFAULT_BASE_REF_ENV``, ``DEFAULT_BASE_REF``) are
identical to what was defined locally before this refactor, so all callers
(``check_repo_refs.py``, ``check_cli_reference.py``) continue to work unchanged.
"""

from __future__ import annotations

from gate_keeper.changed import (
    DEFAULT_BASE_REF,
    DEFAULT_BASE_REF_ENV,
    ChangedFilesError,
    compute_changed_files,
    resolve_base_ref,
)

__all__ = [
    "DEFAULT_BASE_REF",
    "DEFAULT_BASE_REF_ENV",
    "ChangedFilesError",
    "compute_changed_files",
    "resolve_base_ref",
]
