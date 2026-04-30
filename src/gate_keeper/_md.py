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
# Fenced-block detection
# ---------------------------------------------------------------------------

FENCE_START_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def strip_fenced_blocks(text: str) -> str:
    """Return *text* with fenced code block contents removed (fence lines included).

    Opening fence: a line whose stripped form starts with three or more backticks
    or tildes.  The closing fence must use the same fence character and be at least
    as long as the opening fence.  Nested or mismatched fences are left as-is.
    An unterminated fence consumes the rest of the document.
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text.splitlines(keepends=True):
        m = FENCE_START_RE.match(line)
        if m:
            marker = m.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
                fence_char = ""
                fence_len = 0
        elif not in_fence:
            out.append(line)
    return "".join(out)


__all__ = [
    "TASK_CHECKED_RE",
    "TASK_UNCHECKED_RE",
    "FENCE_START_RE",
    "strip_fenced_blocks",
]
