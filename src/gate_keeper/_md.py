"""Shared Markdown helpers for gate-keeper backends.

Provides regex patterns and utilities for detecting Markdown task checkboxes
and stripping fenced code blocks. Both the filesystem and github backends
import from here to avoid duplication.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Task-box regexes
# ---------------------------------------------------------------------------

TASK_CHECKED_RE = re.compile(r"^[ \t]*[-*+]\s+\[[xX]\]", re.MULTILINE)
TASK_UNCHECKED_RE = re.compile(r"^[ \t]*[-*+]\s+\[ \]", re.MULTILINE)

# ---------------------------------------------------------------------------
# Fenced-block detection (CommonMark §4.5)
# ---------------------------------------------------------------------------
#
# An opening fence is a line indented at most 3 spaces consisting of 3+ matching
# fence characters (` or ~), followed by an optional info string and trailing
# whitespace. Backtick fences MUST NOT contain backticks in their info string;
# tilde fences may contain anything. The closing fence must use the same
# character, be at least as long as the opening, indented at most 3 spaces, and
# be followed only by whitespace.

FENCE_START_RE = re.compile(
    r"^ {0,3}"  # at most 3 leading spaces
    r"(?P<marker>`{3,}|~{3,})"  # the fence run
    r"(?P<info>[^`\n]*)?"  # info string (no backticks for backtick fences;
    # tilde fences are slightly more permissive but
    # this is good enough for MVP)
    r"\s*$"  # only whitespace allowed after info string
)


def strip_fenced_blocks(text: str) -> str:
    """Return *text* with fenced code block contents removed (fence lines included).

    Follows CommonMark §4.5 closely enough for MVP usage:
      * opening fence indented ≤3 spaces, 3+ matching ``\\`/~`` characters;
      * info string allowed but, for backtick fences, must not contain backticks;
      * closing fence: same character, at least as long, ≤3-space indent, only
        whitespace after the marker;
      * an unterminated fence consumes the rest of the document.

    Lines that look like fences but violate any of these rules (e.g. ``    \\`\\`\\`py``
    indented with 4+ spaces, or `\\`\\`\\`\\` followed by free-form trailing text on a
    backtick fence) are left in place and treated as ordinary content.
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text.splitlines(keepends=True):
        # Strip a single trailing newline before matching so the regex's trailing
        # ``\s*$`` anchor works regardless of EOL.
        stripped = line.rstrip("\r\n")
        m = FENCE_START_RE.match(stripped)
        if m:
            marker = m.group("marker")
            info = m.group("info") or ""
            if not in_fence:
                # Opening fence: backtick fences disallow backticks in info string.
                if marker[0] == "`" and "`" in info:
                    out.append(line)
                    continue
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len and not info.strip():
                # Closing fence: same char, ≥ length, no info string.
                in_fence = False
                fence_char = ""
                fence_len = 0
            # else: mismatched marker inside an open fence — drop it as content
        elif not in_fence:
            out.append(line)
    return "".join(out)


_ATX_HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")


def heading_present(text: str, heading: str) -> bool:
    """Return True if *text* contains an ATX heading whose trimmed title is *heading*.

    Matching is exact (case-sensitive, no Unicode normalisation) and ignores
    heading level. Helper for backends that want to distinguish "missing
    heading" from "heading present but no fenced block follows".
    """
    for line in text.splitlines():
        m = _ATX_HEADING_RE.match(line)
        if m is not None and m.group("title").strip() == heading:
            return True
    return False


def find_first_fenced_block_after_heading(text: str, heading: str) -> tuple[str, int, str | None] | None:
    """Find the first fenced code block after the first ATX heading equal to *heading*.

    *heading* matches verbatim against the trimmed heading title (the text after
    the leading ``#`` characters and the required separating whitespace, with
    any trailing ``#`` characters and surrounding whitespace removed). Matching
    is exact: case-sensitive, no Unicode normalisation. Heading level (``#`` vs
    ``###``) is not constrained.

    Returns ``(block_content, fence_line_1based, info_string)`` for the first
    fenced code block that starts after the matched heading and before the next
    heading at the same or shallower level. ``info_string`` is the trimmed info
    string on the opening fence (e.g. ``"yaml"``); empty info strings yield
    ``None``.

    Returns ``None`` when:
      - no heading with title equal to *heading* exists, or
      - no fenced block opens between the heading and the next sibling/parent
        heading (or end of document).

    Unterminated fences (no closing fence before EOF) consume to end of
    document; the partial body is returned. Mirrors
    :func:`strip_fenced_blocks`'s fence-detection rules so behaviour is
    consistent across helpers.
    """
    lines = text.splitlines()
    heading_idx: int | None = None
    heading_level: int | None = None
    for i, line in enumerate(lines):
        m = _ATX_HEADING_RE.match(line)
        if not m:
            continue
        title = m.group("title").strip()
        if title == heading:
            heading_idx = i
            heading_level = len(m.group("hashes"))
            break
    if heading_idx is None or heading_level is None:
        return None

    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_start_line: int | None = None
    fence_info: str | None = None
    body: list[str] = []
    for j in range(heading_idx + 1, len(lines)):
        raw = lines[j]
        if not in_fence:
            # Stop at the next heading at the same or shallower level.
            mh = _ATX_HEADING_RE.match(raw)
            if mh and len(mh.group("hashes")) <= heading_level:
                return None
            stripped = raw.rstrip("\r\n")
            mf = FENCE_START_RE.match(stripped)
            if mf:
                marker = mf.group("marker")
                info_raw = mf.group("info") or ""
                # Backtick fences disallow backticks in their info string.
                if marker[0] == "`" and "`" in info_raw:
                    continue
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
                fence_start_line = j + 1  # 1-based
                info_trimmed = info_raw.strip()
                fence_info = info_trimmed or None
                body = []
            continue
        # in_fence: try to close, otherwise accumulate body
        stripped = raw.rstrip("\r\n")
        mf = FENCE_START_RE.match(stripped)
        if mf:
            marker = mf.group("marker")
            info_raw = mf.group("info") or ""
            if marker[0] == fence_char and len(marker) >= fence_len and not info_raw.strip():
                # Closing fence
                assert fence_start_line is not None
                return ("\n".join(body), fence_start_line, fence_info)
        body.append(raw)
    if in_fence:
        # Unterminated fence: return what we have.
        assert fence_start_line is not None
        return ("\n".join(body), fence_start_line, fence_info)
    return None


__all__ = [
    "TASK_CHECKED_RE",
    "TASK_UNCHECKED_RE",
    "FENCE_START_RE",
    "strip_fenced_blocks",
    "find_first_fenced_block_after_heading",
    "heading_present",
]
