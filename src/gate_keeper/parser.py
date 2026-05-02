"""Line-oriented Markdown rule document extractor.

Parses ATX headings, bullet/ordered lists, task checkboxes, and normative
paragraphs into candidate Rule IR entries.  Classification (kind /
backend_hint) is deferred to issue #3; all extracted rules carry the neutral
defaults (semantic_rubric / llm-rubric / low confidence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from gate_keeper.models import (
    Backend,
    Confidence,
    Rule,
    RuleKind,
    RuleSet,
    Severity,
    SourceLocation,
)

# Multi-word phrases must appear before their single-word prefixes.
_NORMATIVE_RE = re.compile(
    r"\b(?:must\s+not|should\s+not|must|should|never|required|forbidden"
    r"|fail|block|ensure|require)\b",
    re.IGNORECASE,
)

_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_TASK_BOX_RE = re.compile(r"^[ \t]*[-*+]\s+\[[ xX]\]\s+(.*)")
_BULLET_RE = re.compile(r"^[ \t]*[-*+]\s+(.*)")
_ORDERED_RE = re.compile(r"^[ \t]*\d+[.)]\s+(.*)")
_CODE_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})")


def _has_normative(text: str) -> bool:
    return bool(_NORMATIVE_RE.search(text))


def _make_id(path: str, line: int) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", Path(path).stem.lower()).strip("-")
    return f"rule-{stem}-L{line}"


def _heading_chain(stack: list[tuple[int, str]]) -> str | None:
    if not stack:
        return None
    return " > ".join(title for _, title in stack)


@dataclass
class _Candidate:
    text: str
    line_start: int
    heading: str | None


def _is_block_start(line: str) -> bool:
    """Return True if *line* begins a new block-level element."""
    return bool(
        not line.strip()
        or _ATX_HEADING_RE.match(line)
        or _CODE_FENCE_RE.match(line)
        or _TASK_BOX_RE.match(line)
        or _BULLET_RE.match(line)
        or _ORDERED_RE.match(line)
    )


def parse(path: str, content: str) -> RuleSet:
    """Extract candidate rules from Markdown *content* at *path*."""
    lines = content.splitlines()
    heading_stack: list[tuple[int, str]] = []
    candidates: list[_Candidate] = []
    in_code_fence = False
    fence_char = ""
    fence_len = 0

    i = 0
    while i < len(lines):
        raw = lines[i]
        line_no = i + 1  # 1-based

        # Code-fence toggle — skip everything inside a fenced block.
        fence_m = _CODE_FENCE_RE.match(raw)
        if fence_m:
            marker = fence_m.group(2)
            if not in_code_fence:
                in_code_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_code_fence = False
                fence_char = ""
                fence_len = 0
            i += 1
            continue

        if in_code_fence:
            i += 1
            continue

        # ATX heading — update the heading stack; not a rule itself.
        heading_m = _ATX_HEADING_RE.match(raw)
        if heading_m:
            level = len(heading_m.group(1))
            title = heading_m.group(2)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            i += 1
            continue

        heading = _heading_chain(heading_stack)

        # Task checkbox — always a candidate regardless of normative keywords.
        task_m = _TASK_BOX_RE.match(raw)
        if task_m:
            candidates.append(
                _Candidate(
                    text=task_m.group(1).strip(),
                    line_start=line_no,
                    heading=heading,
                )
            )
            i += 1
            continue

        # Bullet list item — candidate only when it contains normative wording.
        bullet_m = _BULLET_RE.match(raw)
        if bullet_m:
            text = bullet_m.group(1).strip()
            if _has_normative(text):
                candidates.append(_Candidate(text=text, line_start=line_no, heading=heading))
            i += 1
            continue

        # Ordered list item — same policy as bullets.
        ordered_m = _ORDERED_RE.match(raw)
        if ordered_m:
            text = ordered_m.group(1).strip()
            if _has_normative(text):
                candidates.append(_Candidate(text=text, line_start=line_no, heading=heading))
            i += 1
            continue

        # Paragraph — collect continuation lines, then filter on normative keywords.
        stripped = raw.strip()
        if stripped:
            para_start = line_no
            parts = [stripped]
            i += 1
            while i < len(lines) and not _is_block_start(lines[i]):
                nxt = lines[i].strip()
                if nxt:
                    parts.append(nxt)
                i += 1
            para_text = " ".join(parts)
            if _has_normative(para_text):
                candidates.append(
                    _Candidate(
                        text=para_text,
                        line_start=para_start,
                        heading=heading,
                    )
                )
            continue

        i += 1

    rules = [
        Rule(
            id=_make_id(path, c.line_start),
            title=c.text[:80],
            source=SourceLocation(path=path, line=c.line_start, heading=c.heading),
            text=c.text,
            kind=RuleKind.SEMANTIC_RUBRIC,
            severity=Severity.WARNING,
            backend_hint=Backend.LLM_RUBRIC,
            confidence=Confidence.LOW,
            params={},
        )
        for c in candidates
    ]
    return RuleSet(rules=rules)


def parse_file(path: str | Path) -> RuleSet:
    """Parse a Markdown file and return the extracted rule set."""
    p = Path(path)
    return parse(str(p), p.read_text(encoding="utf-8"))
