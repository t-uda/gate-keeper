"""Changed-file resolver for Mode A (PR-affected-set) validators.

Wraps ``git diff --name-only <base_ref>...HEAD``. The validator owns its diff
source rather than going through a CLI flag — see
docs/design/dependency-gates.md §4.3.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_BASE_REF_ENV = "GATE_KEEPER_BASE_REF"
DEFAULT_BASE_REF = "origin/main"


class ChangedFilesError(RuntimeError):
    """Raised when the changed-file set cannot be computed."""


def resolve_base_ref(env: dict[str, str] | None = None) -> str:
    """Return the configured base ref (env var with fallback)."""
    source = env if env is not None else os.environ
    value = source.get(DEFAULT_BASE_REF_ENV)
    if value:
        return value
    return DEFAULT_BASE_REF


def compute_changed_files(
    repo_root: str | Path,
    base_ref: str,
) -> frozenset[str]:
    """Return the set of repo-relative POSIX paths changed since *base_ref*."""
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        raise ChangedFilesError(f"repo root does not exist: {repo_root}")
    try:
        result = subprocess.run(  # noqa: S603 -- explicit argv, shell=False
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ChangedFilesError("git executable not found on PATH") from exc
    except OSError as exc:
        raise ChangedFilesError(f"git invocation failed: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ChangedFilesError(f"git diff exited {result.returncode}: {stderr or '(no stderr)'}")

    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return frozenset(changed)
