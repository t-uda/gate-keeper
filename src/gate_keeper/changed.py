"""Changed-file resolver for gate-keeper CLI target selection.

Wraps ``git diff --name-only <base_ref>...HEAD`` and exposes helpers used
by:

- ``gate_keeper.cli`` — ``--target-changed / --base-ref`` CLI target
  selection (S1, issue #278).
- ``scripts/dependency_gates/_changed_files`` — Mode A validator-owned diff
  source (unchanged behaviour; the sidecar re-exports from here).

The two use-cases share the same underlying diff command but have different
ownership models:

**Mode A (validator-owned):** the dependency-gates command adapter scripts
compute the diff themselves so they can apply their own changed-set policy
(uncovered reference detection, etc.).  No CLI flag is involved.  Documented
in ``docs/design/dependency-gates.md`` §4.3 "Mode A".

**CLI target selection (Mode C):** ``validate --target-changed`` passes the
changed set through the existing ``resolve_targets`` machinery as the initial
candidate pool, applying the same text-readable and 200-file-cap filters as
any other ``--target`` invocation.  Combined with an explicit ``--target``
the result is the intersection of the two sets.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_BASE_REF_ENV: str = "GATE_KEEPER_BASE_REF"
DEFAULT_BASE_REF: str = "origin/main"


class ChangedFilesError(RuntimeError):
    """Raised when the changed-file set cannot be computed.

    The CLI translates this into a usage error (``EXIT_USAGE``); programmatic
    callers may catch it directly.
    """


def resolve_base_ref(env: dict[str, str] | None = None) -> str:
    """Return the configured base ref (env var with fallback).

    Checks ``GATE_KEEPER_BASE_REF`` in *env* (defaults to
    :data:`os.environ`) and returns :data:`DEFAULT_BASE_REF` when unset.
    """
    source = env if env is not None else os.environ
    value = source.get(DEFAULT_BASE_REF_ENV)
    if value:
        return value
    return DEFAULT_BASE_REF


def compute_changed_files(
    repo_root: str | Path,
    base_ref: str,
) -> frozenset[str]:
    """Return the set of repo-relative POSIX paths changed since *base_ref*.

    Parameters
    ----------
    repo_root:
        Directory that contains the ``.git`` directory.  Must exist.
    base_ref:
        The merge-base ref for ``git diff --name-only <base_ref>...HEAD``.

    Returns
    -------
    frozenset[str]
        Repo-root-relative POSIX paths (the raw ``git diff`` output lines).

    Raises
    ------
    ChangedFilesError
        When *repo_root* does not exist, ``git`` is not on PATH, git exits
        non-zero (e.g. invalid *base_ref*), or any other I/O failure.
    """
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


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from *start* (default: cwd) to find the repository root.

    Returns the first directory that contains a ``.git`` entry.

    Raises
    ------
    ChangedFilesError
        When no ``.git`` directory is found up to the filesystem root.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            raise ChangedFilesError(f"not a git repository (searched from {start or Path.cwd()})")
        current = parent


__all__ = [
    "DEFAULT_BASE_REF",
    "DEFAULT_BASE_REF_ENV",
    "ChangedFilesError",
    "compute_changed_files",
    "find_repo_root",
    "resolve_base_ref",
]
