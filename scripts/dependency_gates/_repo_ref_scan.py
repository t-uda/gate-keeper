"""Deterministic repo-local reference extraction (umbrella #269, slice 1).

Given a single text-bearing file (README/docs/config/rule file), enumerate the
explicit repo-local file/path references it contains. The extractor is
syntactic and deterministic — it never invokes a language model and never
guesses semantic intent.

Recognised reference shapes:

- ``md_link``: Markdown inline link target — ``[text](path)``.
- ``md_image``: Markdown inline image target — ``![alt](path)``.
- ``md_ref_def``: Markdown reference-style link definition — ``[id]: path``.
- ``inline_code``: single-backtick code span whose body matches a tracked
  path shape (contains ``/`` AND either starts with a known top-level
  directory or carries a known file extension).
- ``bare_path``: a bare path-like token prefixed with a known top-level
  directory, appearing outside Markdown link syntax.

Reference targets that look like URLs (scheme prefix, ``//`` host, ``mailto:``
prefix) or in-page anchors (``#frag``) are intentionally ignored — those are
not repo-local references. Empty link targets and pure-fragment links are
also ignored.

The scanner takes the raw text of one file and returns a list of
``Reference`` records. Classification against the working tree and the
changed-file set lives in ``check_repo_refs.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Top-level directories that, when they appear as the first path segment of a
# reference, mark the token as a candidate repo-local path. Keeping the list
# closed (rather than "anything that looks like a path") keeps the scanner
# deterministic and avoids matching narrative prose like "config/json" used
# as a noun phrase.
KNOWN_TOP_LEVEL_DIRS: frozenset[str] = frozenset(
    {
        "src",
        "docs",
        "scripts",
        "tests",
        ".github",
        ".gate-keeper",
    }
)

# File extensions that, when present, mark a token as path-like even without
# a known top-level prefix. Used by ``inline_code`` classification.
KNOWN_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".py",
        ".json",
        ".yml",
        ".yaml",
        ".toml",
        ".cfg",
        ".ini",
        ".txt",
        ".sh",
        ".lock",
    }
)


@dataclass(frozen=True)
class Reference:
    """One extracted repo-local reference candidate.

    Attributes:
        path: The reference target as it appeared in the text, with any
            in-page anchor (``#frag``) and query string stripped. Always
            POSIX-style separators (Markdown links use ``/``).
        line: 1-based line number where the reference appeared.
        column: 1-based column where the reference's path token starts.
        shape: One of ``md_link``, ``md_image``, ``md_ref_def``,
            ``inline_code``, ``bare_path``.
        raw: The originally matched substring, useful for diagnostics.
    """

    path: str
    line: int
    column: int
    shape: str
    raw: str


# Markdown inline link: [text](target). The target can be wrapped in <…> but we
# only accept plain targets for now. Captures the URL up to a closing ')' or
# a space-quoted title.
_MD_LINK_RE = re.compile(
    r"(?P<bang>!?)\[(?P<text>(?:\\.|[^\[\]])*?)\]\((?P<url>[^()\s]+?)(?:\s+\"[^\"]*\")?\)"
)

# Markdown reference-style link definition: [id]: target  ["optional title"]
# Anchored to the start of a (possibly indented) line so we don't catch the
# common ``[id]: text`` shape inside narrative prose.
_MD_REF_DEF_RE = re.compile(r"^(?P<indent>[ \t]{0,3})\[(?P<id>(?:\\.|[^\[\]])+?)\]:\s+(?P<url>\S+)")

# Single-backtick inline code span (does not match triple-backtick fenced
# code blocks; those are stripped first).
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?P<body>[^`\n]+?)`(?!`)")

# A bare path-like token: starts with a known top-level directory, possibly
# trailed by additional path segments. The lookbehind/lookahead keep us from
# matching path-fragments embedded in larger identifiers. The ``[`` in the
# lookbehind specifically blocks the bare-path matcher from firing inside
# Markdown link *text* (``[docs/old.md](docs/new.md)``) — without it the
# link-text fragment would be re-extracted as a `bare_path` reference and
# could produce a spurious ``broken_reference`` FAIL when the text-side
# path no longer exists (e.g. after a rename).
_BARE_PATH_RE = re.compile(
    r"(?<![\w./\[])(?P<path>(?:" + "|".join(re.escape(d) for d in KNOWN_TOP_LEVEL_DIRS) + r")/[\w./*?-]*)"
)

# Fenced code block boundary (``` or ~~~ with optional info string).
_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>```+|~~~+)")

# TODO(#269-later): strip YAML front-matter (lines between a leading ``---``
# and the matching closing ``---``) before scanning. Deferred to a later
# slice; the first slice does not yet skip front-matter content.


def extract_references(text: str, *, source_path: str | None = None) -> list[Reference]:
    """Extract repo-local references from *text*.

    Parameters:
        text: Full file contents as a single string. Line endings may be
            ``\\n`` or ``\\r\\n`` (CRLF is normalised to LF internally).
        source_path: Optional repo-relative path of the file being scanned.
            Currently unused by extraction itself but accepted so the caller
            can pass through the same value used for fenced-block resolution
            in future slices without an API change.

    Returns:
        List of ``Reference`` records, in document order. Duplicates (same
        path appearing multiple times in the file) are preserved — the
        caller decides whether to deduplicate.
    """
    del source_path  # accepted for forward-compatibility; not used yet
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    refs: list[Reference] = []

    in_fence = False
    fence_marker: str | None = None
    for line_no, line in enumerate(lines, start=1):
        # Fenced code blocks are skipped wholesale. Both ``` and ~~~ are
        # recognised; the closing fence must match the opening marker's
        # leading char so that a different fence inside doesn't terminate
        # the block prematurely.
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group("fence")
            if not in_fence:
                in_fence = True
                fence_marker = marker[0]
                continue
            # Closing fence: same character family (``` closes ```; ~~~
            # closes ~~~). Only end the block if it matches.
            if fence_marker is not None and marker[0] == fence_marker:
                in_fence = False
                fence_marker = None
                continue
            # Otherwise leave the fence open and skip the line.
            continue
        if in_fence:
            continue

        # Markdown reference-style link definitions are line-anchored, so
        # extract them before the inline patterns chew up the same text.
        ref_def = _MD_REF_DEF_RE.match(line)
        if ref_def:
            url = ref_def.group("url")
            if _is_repo_local_target(url):
                cleaned = _clean_target(url)
                if cleaned:
                    column = ref_def.start("url") + 1
                    refs.append(
                        Reference(
                            path=cleaned,
                            line=line_no,
                            column=column,
                            shape="md_ref_def",
                            raw=url,
                        )
                    )
            # Reference-style definitions don't contain anything else worth
            # scanning on the same line.
            continue

        # Markdown inline links / images.
        for m in _MD_LINK_RE.finditer(line):
            url = m.group("url")
            if not _is_repo_local_target(url):
                continue
            cleaned = _clean_target(url)
            if not cleaned:
                continue
            shape = "md_image" if m.group("bang") == "!" else "md_link"
            column = m.start("url") + 1
            refs.append(
                Reference(
                    path=cleaned,
                    line=line_no,
                    column=column,
                    shape=shape,
                    raw=url,
                )
            )

        # Inline code spans — only those whose body matches a tracked path
        # shape (a known top-level prefix OR a known file extension AND a
        # path separator).
        for m in _INLINE_CODE_RE.finditer(line):
            body = m.group("body").strip()
            if not _looks_like_inline_code_path(body):
                continue
            cleaned = _clean_target(body)
            if not cleaned:
                continue
            column = m.start("body") + 1
            refs.append(
                Reference(
                    path=cleaned,
                    line=line_no,
                    column=column,
                    shape="inline_code",
                    raw=body,
                )
            )

        # Bare paths — only matched when they begin with a known top-level
        # directory. We strip out any overlap with already-captured Markdown
        # link targets to avoid double-counting (Markdown link URLs are not
        # inside backticks here so this is a simple substring check).
        for m in _BARE_PATH_RE.finditer(line):
            candidate = m.group("path")
            # Skip if this span sits inside a Markdown link target we
            # already captured.
            if _span_overlaps_existing(line_no, m.start(), m.end(), refs):
                continue
            # Skip if it sits inside an inline code span: those were already
            # processed above.
            if _span_inside_code(line, m.start(), m.end()):
                continue
            cleaned = _clean_target(candidate)
            if not cleaned:
                continue
            refs.append(
                Reference(
                    path=cleaned,
                    line=line_no,
                    column=m.start() + 1,
                    shape="bare_path",
                    raw=candidate,
                )
            )

    return refs


def _is_repo_local_target(url: str) -> bool:
    """Return True when *url* looks like a repo-local target."""
    if not url:
        return False
    # Strip any in-page anchor and query string for the scheme check.
    base = url.split("#", 1)[0].split("?", 1)[0]
    if not base:
        return False
    # URL scheme: ``http:``, ``https:``, ``mailto:``, ``ftp:``, ``//host``…
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", base):
        return False
    if base.startswith("//"):
        return False
    # Absolute filesystem paths are not repo-local.
    if base.startswith("/"):
        return False
    return True


def _clean_target(url: str) -> str:
    """Strip in-page anchor and query string; normalise separators."""
    base = url.split("#", 1)[0].split("?", 1)[0].strip()
    # Normalise backslashes to forward slashes for cross-platform docs.
    return base.replace("\\", "/")


def _looks_like_inline_code_path(body: str) -> bool:
    """Return True when an inline-code body looks like a repo-local path.

    Rules:
    - Must contain at least one ``/``.
    - Either starts with a known top-level directory, OR ends with a known
      file extension.
    - No whitespace in the body.
    """
    if not body or " " in body or "\t" in body:
        return False
    if "/" not in body:
        return False
    first_seg = body.split("/", 1)[0]
    if first_seg in KNOWN_TOP_LEVEL_DIRS:
        return True
    lowered = body.lower()
    return any(lowered.endswith(ext) for ext in KNOWN_FILE_EXTENSIONS)


def _span_overlaps_existing(line_no: int, start: int, end: int, refs: list[Reference]) -> bool:
    """Return True if [start, end) on *line_no* sits inside an already-captured ref."""
    for r in refs:
        if r.line != line_no:
            continue
        r_start = r.column - 1
        r_end = r_start + len(r.raw)
        if r_start <= start and end <= r_end:
            return True
    return False


def _span_inside_code(line: str, start: int, end: int) -> bool:
    """Return True if the span [start, end) sits inside a single-backtick code span."""
    # Walk the line and track whether the index is inside a backtick span.
    in_code = False
    open_at = -1
    for i, ch in enumerate(line):
        if ch == "`":
            if not in_code:
                in_code = True
                open_at = i
            else:
                in_code = False
                close_at = i
                if open_at < start and end <= close_at:
                    return True
                open_at = -1
    return False


# Broad-shape heuristics (deterministic).
#
# A reference is considered ``suspicious_broad`` if any of:
# - it ends with ``/`` (a bare-directory reference);
# - it contains a glob character (``*`` or ``?``);
# - it has no extension AND no further path segment after the top-level dir
#   (e.g. ``docs`` or ``src``) — i.e. a bare top-level directory reference
#   with no anchor.
_GLOB_CHARS = ("*", "?")


def is_suspicious_broad(reference: Reference) -> bool:
    """Return True if *reference*'s shape is intentionally broad/ambiguous."""
    path = reference.path
    if not path:
        return False
    if path.endswith("/"):
        return True
    if any(c in path for c in _GLOB_CHARS):
        return True
    # Bare top-level directory with no further segment, e.g. ``docs``.
    if path in KNOWN_TOP_LEVEL_DIRS:
        return True
    return False


__all__ = [
    "KNOWN_FILE_EXTENSIONS",
    "KNOWN_TOP_LEVEL_DIRS",
    "Reference",
    "extract_references",
    "is_suspicious_broad",
]
