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


__all__ = [
    "TASK_CHECKED_RE",
    "TASK_UNCHECKED_RE",
    "FENCE_START_RE",
    "strip_fenced_blocks",
]
