"""Line-oriented Markdown rule document extractor.

Parses ATX headings, bullet/ordered lists, task checkboxes, and normative
paragraphs into candidate Rule IR entries.  Classification (kind /
backend_hint) is deferred to issue #3; all extracted rules carry the neutral
defaults (semantic_rubric / llm-rubric / low confidence).

Inline rule annotations (#169)
------------------------------

A trailing ``[target_kind: <value>]`` token at the end of a normative bullet
or paragraph is recognised as an artifact-kind annotation. The token is
stripped from the rule text and the parsed ``target_kind`` is attached to
the resulting :class:`Rule`. Unknown values are tolerated: they are stripped
from the text and a parse-time warning record is recorded on the rule
(``params['target_kind_parse_warning']``); the rule's ``target_kind`` falls
back to :data:`TargetKind.UNSPECIFIED` rather than failing the parse. This
keeps rule-doc authoring forgiving while still surfacing typos.
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
    TargetKind,
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

# Trailing ``[target_kind: <value>]`` annotation (#169). The value capture
# is intentionally permissive (``[^\]]+``) so common typos — hyphens
# (``commit-message``), case variants, trailing whitespace — are recognised
# as annotation attempts and routed through the fallback warning path
# rather than silently left in the rule text. The value is then validated
# in Python against the :class:`TargetKind` enum.
_TARGET_KIND_RE = re.compile(r"\s*\[\s*target_kind\s*:\s*([^\]]+?)\s*\]\s*$")


def _extract_target_kind(text: str) -> tuple[str, TargetKind, str | None]:
    """Strip a trailing ``[target_kind: ...]`` token from *text*.

    Returns ``(stripped_text, target_kind, warning)``:

    - ``stripped_text``: *text* with the annotation token removed (if any).
    - ``target_kind``: the parsed :class:`TargetKind`, or ``UNSPECIFIED``
      when no annotation is present **or** the value is unknown.
    - ``warning``: ``None`` on a clean parse, otherwise a short string
      describing why the value was rejected (typo, unknown enum value).

    The annotation is recognised whenever the trailing bracketed token
    starts with ``target_kind:``; the value capture is permissive
    (anything up to the closing bracket) so common typos —
    ``commit-message`` (hyphen vs underscore), case variants, etc. — are
    reported via the warning channel rather than silently left in the
    rule text.
    """
    m = _TARGET_KIND_RE.search(text)
    if not m:
        return text, TargetKind.UNSPECIFIED, None
    raw = m.group(1).strip()
    stripped = text[: m.start()].rstrip()
    try:
        kind = TargetKind(raw)
    except ValueError:
        return (
            stripped,
            TargetKind.UNSPECIFIED,
            f"unknown target_kind value {raw!r}; expected one of "
            f"{sorted(member.value for member in TargetKind)}",
        )
    return stripped, kind, None


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
    target_kind: TargetKind = TargetKind.UNSPECIFIED
    target_kind_warning: str | None = None


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
            text = task_m.group(1).strip()
            stripped, kind, warning = _extract_target_kind(text)
            candidates.append(
                _Candidate(
                    text=stripped,
                    line_start=line_no,
                    heading=heading,
                    target_kind=kind,
                    target_kind_warning=warning,
                )
            )
            i += 1
            continue

        # Bullet list item — candidate only when it contains normative wording.
        bullet_m = _BULLET_RE.match(raw)
        if bullet_m:
            text = bullet_m.group(1).strip()
            if _has_normative(text):
                stripped, kind, warning = _extract_target_kind(text)
                candidates.append(
                    _Candidate(
                        text=stripped,
                        line_start=line_no,
                        heading=heading,
                        target_kind=kind,
                        target_kind_warning=warning,
                    )
                )
            i += 1
            continue

        # Ordered list item — same policy as bullets.
        ordered_m = _ORDERED_RE.match(raw)
        if ordered_m:
            text = ordered_m.group(1).strip()
            if _has_normative(text):
                stripped, kind, warning = _extract_target_kind(text)
                candidates.append(
                    _Candidate(
                        text=stripped,
                        line_start=line_no,
                        heading=heading,
                        target_kind=kind,
                        target_kind_warning=warning,
                    )
                )
            i += 1
            continue

        # Paragraph — collect continuation lines, then filter on normative keywords.
        stripped_raw = raw.strip()
        if stripped_raw:
            para_start = line_no
            parts = [stripped_raw]
            i += 1
            while i < len(lines) and not _is_block_start(lines[i]):
                nxt = lines[i].strip()
                if nxt:
                    parts.append(nxt)
                i += 1
            para_text = " ".join(parts)
            if _has_normative(para_text):
                stripped, kind, warning = _extract_target_kind(para_text)
                candidates.append(
                    _Candidate(
                        text=stripped,
                        line_start=para_start,
                        heading=heading,
                        target_kind=kind,
                        target_kind_warning=warning,
                    )
                )
            continue

        i += 1

    rules: list[Rule] = []
    for c in candidates:
        params: dict[str, object] = {}
        if c.target_kind_warning is not None:
            params["target_kind_parse_warning"] = c.target_kind_warning
        rules.append(
            Rule(
                id=_make_id(path, c.line_start),
                title=c.text[:80],
                source=SourceLocation(path=path, line=c.line_start, heading=c.heading),
                text=c.text,
                kind=RuleKind.SEMANTIC_RUBRIC,
                severity=Severity.WARNING,
                backend_hint=Backend.LLM_RUBRIC,
                confidence=Confidence.LOW,
                params=params,
                target_kind=c.target_kind,
            )
        )
    return RuleSet(rules=rules)


def parse_file(path: str | Path) -> RuleSet:
    """Parse a Markdown file and return the extracted rule set."""
    p = Path(path)
    return parse(str(p), p.read_text(encoding="utf-8"))
