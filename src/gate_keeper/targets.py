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
from collections.abc import MutableMapping, Sequence
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


def resolve_changed_targets(
    changed_posix_paths: frozenset[str],
    repo_root: Path,
    *,
    file_limit: int | None = None,
) -> TargetSpec:
    """Resolve a changed-file set (from ``git diff --name-only``) into a :class:`TargetSpec`.

    Unlike :func:`resolve_targets`, this function applies the text-readable
    filter to each candidate path because ``git diff`` may include non-text
    files (images, compiled binaries) and deleted files that no longer exist.

    Parameters
    ----------
    changed_posix_paths:
        Repo-root-relative POSIX paths returned by
        ``gate_keeper.changed.compute_changed_files``.
    repo_root:
        Absolute path to the repository root (contains ``.git``).
    file_limit:
        Hard cap on the number of resolved files; exceeding it raises
        :class:`TargetExpansionError`.  Defaults to :data:`DEFAULT_FILE_LIMIT`.

    Returns
    -------
    TargetSpec
        A frozen spec carrying the deduplicated, sorted list of resolved paths.
        ``is_multi`` is ``True`` when the resolved count is not exactly one
        (mirrors the semantics of :func:`resolve_targets`).
        ``paths`` may be empty when all changed files are non-text or deleted.

    Raises
    ------
    TargetExpansionError
        When the surviving file count exceeds *file_limit*.
    """
    if file_limit is None:
        file_limit = DEFAULT_FILE_LIMIT

    seen: set[str] = set()
    accumulator: list[Path] = []

    for posix_path in sorted(changed_posix_paths):
        abs_path = (repo_root / posix_path).resolve()
        if not abs_path.is_file():
            continue  # deleted file or directory
        if not _is_text_readable(abs_path):
            continue
        key = os.fspath(abs_path)
        if key in seen:
            continue
        seen.add(key)
        accumulator.append(abs_path)
        _check_cap(seen, file_limit)

    accumulator.sort(key=os.fspath)

    return TargetSpec(
        paths=accumulator,
        raw_targets=sorted(changed_posix_paths),
        is_multi=len(accumulator) != 1,
    )


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


# ---------------------------------------------------------------------------
# Per-rule target scope (issue #279 — S3, docs/design/multi-target.md §9)
# ---------------------------------------------------------------------------
#
# A rule may declare ``params.target_scope`` (a list of repo-relative glob
# strings). The engine computes a per-rule effective set
#
#     effective_set = scope_expansion(target_scope) ∩ run_level_candidate_set
#
# on normalized repo-relative POSIX paths (§9.2) and dispatches the rule against
# its own :class:`TargetSpec`. This is engine-side only: backends never learn a
# scope was applied. See :func:`resolve_rule_scope` for the full contract.


def _to_repo_relative(path: Path, repo_root: Path) -> str | None:
    """Normalize *path* to a repo-relative POSIX string, or ``None`` if outside.

    A candidate path resolving outside *repo_root* can never intersect a
    repo-relative scope glob, so the caller drops it from the candidate set.
    """
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _expand_scope_pattern(pattern: str, repo_root: Path) -> frozenset[str]:
    """Expand one ``target_scope`` glob against *repo_root*.

    Returns the set of repo-relative POSIX paths of the existing, text-readable
    regular files the pattern selects. Directory matches are walked recursively
    (mirroring :func:`resolve_targets`). The expansion is **uncapped by design**
    (§9.6): the ``DEFAULT_FILE_LIMIT`` cap applies to the per-rule *effective
    set*, not to the repo-wide scope expansion, so a broad scope like
    ``src/**/*.py`` in a large tree still narrows correctly once intersected
    with a small candidate set.

    A pattern matching nothing — or only binary / non-existent paths —
    contributes the empty set; the caller folds that into the "expands to zero
    files repo-wide → ``scope_invalid``" determination (§9.4).
    """
    results: set[str] = set()

    def _add_file(candidate: Path) -> None:
        if candidate.is_file() and _is_text_readable(candidate):
            rel = _to_repo_relative(candidate, repo_root)
            if rel is not None:
                results.add(rel)

    def _add_dir(directory: Path) -> None:
        for entry in directory.rglob("*"):
            _add_file(entry)

    if looks_like_glob(pattern):
        search = pattern if Path(pattern).is_absolute() else str(repo_root / pattern)
        for match in glob.glob(search, recursive=True):
            matched = Path(match)
            if matched.is_dir():
                _add_dir(matched)
            else:
                _add_file(matched)
    else:
        literal = Path(pattern) if Path(pattern).is_absolute() else repo_root / pattern
        if literal.is_dir():
            _add_dir(literal)
        else:
            _add_file(literal)

    return frozenset(results)


def _memoized_scope_expansion(
    pattern: str,
    repo_root: Path,
    memo: MutableMapping[tuple[str, str], frozenset[str]] | None,
) -> frozenset[str]:
    """Expand *pattern*, reusing a per-run memo keyed by ``(pattern, repo_root)`` (§9.6)."""
    if memo is None:
        return _expand_scope_pattern(pattern, repo_root)
    key = (pattern, os.fspath(repo_root))
    cached = memo.get(key)
    if cached is None:
        cached = _expand_scope_pattern(pattern, repo_root)
        memo[key] = cached
    return cached


@dataclass(frozen=True)
class ScopeResolution:
    """Outcome of resolving a rule's ``target_scope`` against the candidate set.

    Attributes
    ----------
    status:
        One of ``"dispatch"``, ``"empty"``, ``"invalid"``, or
        ``"file_limit_exceeded"`` — the engine maps each to a verdict (§9.4/§9.5,
        §9.8): ``dispatch`` runs the rule against :attr:`spec`; ``empty`` is a
        ``scope_empty`` PASS; ``invalid`` a ``scope_invalid`` UNAVAILABLE;
        ``file_limit_exceeded`` a ``scope_file_limit_exceeded`` UNAVAILABLE.
    spec:
        The per-rule :class:`TargetSpec` to dispatch against — set only when
        :attr:`status` is ``"dispatch"``.
    scope_size / candidate_size / effective_size:
        Sizes of the repo-wide scope expansion, the run-level candidate set, and
        their intersection, for auditable evidence.
    effective_relpaths:
        Sorted repo-relative POSIX paths of the effective set (evidence).
    file_limit:
        The per-rule cap in force for this resolution.
    detail:
        Human-readable reason — set only when :attr:`status` is ``"invalid"``.
    """

    status: str
    spec: TargetSpec | None
    scope_size: int
    candidate_size: int
    effective_size: int
    effective_relpaths: list[str]
    file_limit: int
    detail: str | None = None


def resolve_rule_scope(
    target_scope: object,
    candidate_paths: Sequence[Path],
    repo_root: Path,
    *,
    file_limit: int | None = None,
    memo: MutableMapping[tuple[str, str], frozenset[str]] | None = None,
) -> ScopeResolution:
    """Compute a rule's per-rule effective target set (``docs/design/multi-target.md`` §9).

    Parameters
    ----------
    target_scope:
        The raw ``rule.params["target_scope"]`` value. Must be a non-empty list
        of glob strings; any other shape is a rule misconfiguration and yields a
        ``"invalid"`` resolution (fail-closed at the parse seam — §9.1).
    candidate_paths:
        The run-level candidate file set (``TargetSpec.paths`` — §9.3), the same
        pool every rule sees today.
    repo_root:
        Repository root the scope globs expand against; also the base for the
        repo-relative normalization that lets scope and candidate meet in one
        path vocabulary (§9.2).
    file_limit:
        Per-rule cap on the effective set. Defaults to :data:`DEFAULT_FILE_LIMIT`
        (looked up at call time so tests can monkeypatch the constant). Exceeding
        it yields ``"file_limit_exceeded"`` — a per-rule UNAVAILABLE, never a run
        abort (§9.5).
    memo:
        Optional per-run expansion cache keyed by ``(pattern, repo_root)`` (§9.6).

    Returns
    -------
    ScopeResolution
        A discriminated result the engine maps to a per-rule dispatch or a
        synthetic diagnostic.
    """
    if file_limit is None:
        file_limit = DEFAULT_FILE_LIMIT

    candidate_size = len(candidate_paths)

    # §9.1 grammar: a non-empty list of glob strings, or it is a misconfiguration.
    if (
        not isinstance(target_scope, list)
        or not target_scope
        or not all(isinstance(entry, str) for entry in target_scope)
    ):
        return ScopeResolution(
            status="invalid",
            spec=None,
            scope_size=0,
            candidate_size=candidate_size,
            effective_size=0,
            effective_relpaths=[],
            file_limit=file_limit,
            detail=(
                "params.target_scope must be a non-empty list of glob strings; "
                f"got {type(target_scope).__name__}"
            ),
        )

    root = repo_root.resolve()

    scope_set: set[str] = set()
    for pattern in target_scope:
        scope_set |= _memoized_scope_expansion(pattern, root, memo)
    scope_size = len(scope_set)

    # §9.4: a scope that matches nothing anywhere in the tree is a
    # misconfiguration → scope_invalid (fail-closed), distinct from a valid
    # scope that simply does not intersect this run's candidates.
    if scope_size == 0:
        return ScopeResolution(
            status="invalid",
            spec=None,
            scope_size=0,
            candidate_size=candidate_size,
            effective_size=0,
            effective_relpaths=[],
            file_limit=file_limit,
            detail="params.target_scope expands to zero files under the repository root",
        )

    # Normalize candidates to repo-relative POSIX, keeping the original Path so
    # the per-rule TargetSpec dispatches with the exact path vocabulary the
    # run-level spec used (byte-compatible with legacy single-target dispatch).
    candidate_rel: dict[str, Path] = {}
    for path in candidate_paths:
        rel = _to_repo_relative(path, root)
        if rel is not None:
            candidate_rel.setdefault(rel, path)

    effective_relpaths = sorted(scope_set & candidate_rel.keys())
    effective_size = len(effective_relpaths)

    # §9.4: valid scope, empty intersection → scope_empty PASS (steady state of
    # incremental auditing), never a run abort and never a silent pass.
    if effective_size == 0:
        return ScopeResolution(
            status="empty",
            spec=None,
            scope_size=scope_size,
            candidate_size=candidate_size,
            effective_size=0,
            effective_relpaths=[],
            file_limit=file_limit,
        )

    # §9.5: a per-rule effective set over the cap is a per-rule UNAVAILABLE, not
    # a run abort — one over-broad rule must not sink the audit of every other.
    if effective_size > file_limit:
        return ScopeResolution(
            status="file_limit_exceeded",
            spec=None,
            scope_size=scope_size,
            candidate_size=candidate_size,
            effective_size=effective_size,
            effective_relpaths=effective_relpaths,
            file_limit=file_limit,
        )

    effective_paths = sorted(
        (candidate_rel[rel] for rel in effective_relpaths), key=os.fspath
    )
    spec = TargetSpec(
        paths=effective_paths,
        raw_targets=list(effective_relpaths),
        is_multi=len(effective_paths) != 1,
    )
    return ScopeResolution(
        status="dispatch",
        spec=spec,
        scope_size=scope_size,
        candidate_size=candidate_size,
        effective_size=effective_size,
        effective_relpaths=effective_relpaths,
        file_limit=file_limit,
    )


__all__ = [
    "DEFAULT_FILE_LIMIT",
    "ScopeResolution",
    "TargetExpansionError",
    "TargetSpec",
    "looks_like_glob",
    "resolve_changed_targets",
    "resolve_rule_scope",
    "resolve_targets",
]
