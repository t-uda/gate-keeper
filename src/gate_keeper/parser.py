from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path

from gate_keeper.models import SourceLocation

_NORMATIVE_KEYWORDS = (
    "must",
    "must not",
    "should",
    "should not",
    "never",
    "required",
    "forbidden",
    "fail",
    "block",
    "ensure",
    "require",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")
_TASK_RE = re.compile(r"^\s*[-*+]\s+\[(?P<state>[ xX])\]\s+(?P<body>.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<body>.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class ParsedRuleCandidate:
    """Line-oriented rule candidate extracted from a Markdown document."""

    text: str
    source: SourceLocation
    start_line: int
    end_line: int
    structure: str
    raw: str


@dataclass(frozen=True)
class ParsedDocument:
    path: str
    candidates: list[ParsedRuleCandidate]


def _heading_context(stack: list[str]) -> str | None:
    if not stack:
        return None
    return " > ".join(stack)


def _looks_normative_paragraph(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _NORMATIVE_KEYWORDS)


def _clean_inline(text: str) -> str:
    text = text.strip()
    if text.startswith("- ") or text.startswith("* ") or text.startswith("+ "):
        return text[2:].strip()
    return text


def extract_candidates(document_path: str | Path, content: str) -> ParsedDocument:
    """Extract deterministic rule candidates from a Markdown document.

    The MVP parser is intentionally line-oriented: it captures headings, list
    items, task checkboxes, and normative paragraphs. Code blocks are skipped.
    """

    path = str(document_path)
    heading_stack: list[str] = []
    candidates: list[ParsedRuleCandidate] = []
    in_code_block = False

    for line_no, raw_line in enumerate(content.splitlines(), start=1):
        stripped = raw_line.rstrip("\n")

        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group("title").strip()
            heading_stack = heading_stack[: max(level - 1, 0)]
            heading_stack.append(title)
            continue

        body = None
        structure = None
        task_match = _TASK_RE.match(stripped)
        if task_match:
            body = task_match.group("body").strip()
            structure = "task"
        else:
            list_match = _LIST_RE.match(stripped)
            if list_match:
                body = list_match.group("body").strip()
                structure = "list"
            else:
                paragraph = stripped.strip()
                if paragraph and _looks_normative_paragraph(paragraph):
                    body = paragraph
                    structure = "paragraph"

        if not body or not structure:
            continue

        candidate = ParsedRuleCandidate(
            text=_clean_inline(body),
            source=SourceLocation(path=path, line=line_no, heading=_heading_context(heading_stack)),
            start_line=line_no,
            end_line=line_no,
            structure=structure,
            raw=stripped,
        )
        candidates.append(candidate)

    return ParsedDocument(path=path, candidates=candidates)


__all__ = ["ParsedRuleCandidate", "ParsedDocument", "extract_candidates"]
