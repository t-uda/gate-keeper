"""Target specification helpers for gate-keeper.

This module introduces ``TargetSpec``, the internal representation passed from
the CLI / validator to backends when more than one filesystem path needs to be
evaluated against a single rule (issue #146 — first slice of the multi-target
design at ``docs/design/multi-target.md``).

Scope
-----
This first slice is deliberately filesystem-only:

- Each ``--target`` argument is treated as a literal path, a directory, or a
  shell-style glob.
- Directories expand to all *text-readable regular files* below them.
- Globs expand deterministically.
- Resolved paths are deduplicated and sorted lexicographically (using
  ``os.fspath`` order) so backend output is stable.
- A file-count cap (``DEFAULT_FILE_LIMIT`` = 200) protects callers from
  accidentally selecting an unbounded set; exceeding the cap is a hard
  error, not a silent truncation.

Out of scope (deferred to later issues):

- LLM-rubric multi-file content assembly and token budgeting.
- ``params.targets`` rule-level glob lists.
- Heuristic file-relevance ranking.
- Remote / non-filesystem URIs.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Cap and error types
# ---------------------------------------------------------------------------

#: Default cap on the number of files a multi-target expansion may resolve to.
#: Mirrors ``docs/design/multi-target.md`` §6.  Hardcoded for the first slice;
#: a dotenv override (``GATE_KEEPER_TARGET_FILE_LIMIT``) is reserved as a
#: follow-up.
DEFAULT_FILE_LIMIT: int = 200


class TargetExpansionError(ValueError):
    """Raised when a target spec cannot be resolved into a usable file set.

    Cases:

    - File-count cap exceeded.
    - (Reserved) other unrecoverable expansion errors.

    The CLI translates this into a usage error (``EXIT_USAGE``); programmatic
    callers may catch it directly when invoking :func:`resolve_targets`.
    """


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetSpec:
    """Resolved file set for a multi-target evaluation.

    Attributes
    ----------
    paths:
        Deduplicated, lexicographically sorted list of resolved paths.  Each
        entry is a regular file (directories expand recursively into the
        regular files below them).
    raw_targets:
        The original ``--target`` arguments, in the order they were supplied,
        for diagnostic reporting.
    is_multi:
        ``True`` when more than one ``--target`` argument was supplied, when a
        directory was supplied, or when a glob was supplied (even if it
        happened to expand to a single file).  ``False`` when exactly one raw
        argument was supplied and it pointed at a single regular file with no
        glob metacharacters.
    """

    paths: list[Path]
    raw_targets: list[str] = field(default_factory=list)
    is_multi: bool = False

    def __post_init__(self) -> None:  # pragma: no cover - trivial guard
        # Defensive: callers should construct via ``resolve_targets``.  Detect
        # accidental misuse rather than letting downstream code de-reference
        # an unsorted list.
        if list(self.paths) != sorted(self.paths, key=os.fspath):
            raise ValueError("TargetSpec.paths must be sorted lexicographically")


# ---------------------------------------------------------------------------
# Glob detection
# ---------------------------------------------------------------------------

_GLOB_METACHARS = set("*?[")


def looks_like_glob(token: str) -> bool:
    """Return ``True`` when *token* contains shell-glob metacharacters."""
    return any(ch in token for ch in _GLOB_METACHARS)


# Backwards-compatible alias for any internal caller; the leading underscore
# was an oversight — the helper is part of the public ``targets`` surface.
_looks_like_glob = looks_like_glob


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------


def _is_text_readable(path: Path) -> bool:
    """Heuristic: return ``True`` when *path* looks like a UTF-8 text file.

    Filesystem rules read targets via ``Path.read_text(encoding="utf-8")``; a
    binary or undecodable file would surface as ``UNAVAILABLE``.  When a
    directory is expanded by ``--target``, we therefore filter to files that
    decode cleanly — otherwise a single binary asset could turn an entire
    multi-target run unavailable.

    The check reads up to the first 4 KiB of the file and rejects any blob
    that:

    - cannot be opened (permission errors, broken symlinks, etc.);
    - contains a NUL byte in the sampled prefix;
    - fails strict UTF-8 decoding of the sampled prefix.

    Any other I/O error is treated as non-text.  This is intentionally
    conservative: a false-negative means the file is silently skipped, which
    is acceptable for an opt-in directory walk; a false-positive would surface
    later as an ``UNAVAILABLE`` diagnostic, which is also fail-closed.
    """
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def _check_cap(seen: set[str], file_limit: int) -> None:
    """Raise :class:`TargetExpansionError` once the unique-path count exceeds *file_limit*.

    Used by the incremental expansion path so a broad target cannot consume
    unbounded I/O before failing closed.
    """
    if len(seen) > file_limit:
        raise TargetExpansionError(
            f"target expansion produced more than {file_limit} files; "
            f"exceeds limit of {file_limit}. "
            f"Narrow the target set or raise the cap (default {DEFAULT_FILE_LIMIT})."
        )


def _walk_directory_into(
    directory: Path,
    seen: set[str],
    accumulator: list[Path],
    file_limit: int,
) -> None:
    """Append text-readable regular files under *directory* to *accumulator*.

    Stops as soon as *seen* exceeds *file_limit* (raises
    :class:`TargetExpansionError`) so traversal of large trees does not
    continue past the cap.  Sorted by ``os.fspath`` for deterministic order.
    """
    for entry in sorted(directory.rglob("*"), key=os.fspath):
        if not entry.is_file() or not _is_text_readable(entry):
            continue
        key = os.fspath(entry)
        if key in seen:
            continue
        seen.add(key)
        accumulator.append(entry)
        _check_cap(seen, file_limit)


def _expand_token_into(
    token: str,
    base: Path,
    seen: set[str],
    accumulator: list[Path],
    file_limit: int,
) -> None:
    """Expand *token* into *accumulator*, enforcing *file_limit* incrementally.

    - Globs (tokens with ``*``, ``?``, ``[``) are expanded via :mod:`glob`
      with ``recursive=True``; matched directories are walked into.
    - Directories are walked recursively for text-readable files.
    - Existing or missing literal files are appended as-is — the filesystem
      backend handles the unavailable case.
    """
    if looks_like_glob(token):
        # Resolve relative globs against ``base`` for stability across cwd
        # changes.  ``Path.is_absolute`` handles already-absolute glob bases.
        if not Path(token).is_absolute():
            search = str(base / token)
        else:
            search = token
        matches = sorted(glob.glob(search, recursive=True))
        for match in matches:
            p = Path(match)
            if p.is_dir():
                _walk_directory_into(p, seen, accumulator, file_limit)
            elif p.is_file():
                # Globs may match binary blobs (e.g. ``*`` against a build
                # output dir).  We trust the caller's pattern but still
                # apply the text-readable filter so a single accidental
                # binary match does not poison the entire run.
                if not _is_text_readable(p):
                    continue
                key = os.fspath(p)
                if key in seen:
                    continue
                seen.add(key)
                accumulator.append(p)
                _check_cap(seen, file_limit)
        return

    path = Path(token)
    if path.is_dir():
        _walk_directory_into(path, seen, accumulator, file_limit)
        return
    # Either an existing file or a missing path — let the backend decide.
    key = os.fspath(path)
    if key in seen:
        return
    seen.add(key)
    accumulator.append(path)
    _check_cap(seen, file_limit)


def resolve_targets(
    raw_targets: list[str],
    *,
    base: Path | None = None,
    file_limit: int | None = None,
) -> TargetSpec:
    """Resolve raw ``--target`` arguments into a deterministic ``TargetSpec``.

    Parameters
    ----------
    raw_targets:
        The list of ``--target`` strings, in the order they were supplied.
        Must be non-empty (the CLI enforces this via ``argparse``).
    base:
        Base directory for relative globs.  Defaults to the process cwd.
    file_limit:
        Hard cap on the number of resolved files; exceeding it raises
        :class:`TargetExpansionError` *during* expansion, before the full
        tree is walked.  Defaults to the module-level
        :data:`DEFAULT_FILE_LIMIT` (looked up at call time so tests can
        monkeypatch the constant).

    Returns
    -------
    TargetSpec
        A frozen spec carrying the deduplicated, sorted file list and the
        ``is_multi`` flag.

    Raises
    ------
    ValueError
        When *raw_targets* is empty.
    TargetExpansionError
        When the resolved file set exceeds *file_limit*.  Detection is
        incremental: traversal stops as soon as the cap is exceeded so a
        broad target cannot consume unbounded I/O.
    """
    if not raw_targets:
        raise ValueError("resolve_targets: at least one target is required")

    if base is None:
        base = Path.cwd()

    if file_limit is None:
        file_limit = DEFAULT_FILE_LIMIT

    # Track multi-ness: a single literal file is the only single-target case.
    is_multi = len(raw_targets) > 1
    if not is_multi:
        token = raw_targets[0]
        if looks_like_glob(token) or Path(token).is_dir():
            is_multi = True

    seen: set[str] = set()
    accumulator: list[Path] = []
    for token in raw_targets:
        _expand_token_into(token, base, seen, accumulator, file_limit)

    # Lexicographic sort is deterministic regardless of caller-supplied
    # input order (deduplication already happened during expansion).
    accumulator.sort(key=os.fspath)

    return TargetSpec(paths=accumulator, raw_targets=list(raw_targets), is_multi=is_multi)


__all__ = [
    "DEFAULT_FILE_LIMIT",
    "TargetExpansionError",
    "TargetSpec",
    "looks_like_glob",
    "resolve_targets",
]
