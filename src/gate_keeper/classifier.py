from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Iterable

from gate_keeper.models import Backend, Confidence, Rule, RuleKind, RuleSet, Severity
from gate_keeper.parser import ParsedDocument, ParsedRuleCandidate, extract_candidates

_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)?)"
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_QUOTED_RE = re.compile(r'(?<!\\)(?:"([^\"]+)"|\'([^\']+)\')')

_GITHUB_HINT_WORDS = (
    "pull request",
    "pr ",
    "draft",
    "review",
    "thread",
    "status check",
    "checks",
    "label",
    "labels",
    "approval",
    "approved",
    "github",
)

_FILESYSTEM_HINT_WORDS = (
    "file",
    "path",
    "directory",
    "folder",
    "exists",
    "exist",
    "absent",
    "present",
    "required",
    "forbidden",
    "contain",
    "include",
    "mention",
    "match",
    "name",
    "named",
    "text",
    "task",
    "checklist",
)


def _clean_title(text: str, limit: int = 72) -> str:
    collapsed = re.sub(r"\s+", " ", text.strip())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _extract_quoted_snippet(text: str) -> str | None:
    for match in _BACKTICK_RE.findall(text):
        if match.strip() and "/" not in match and "." not in match:
            return match.strip()
    quoted = _QUOTED_RE.search(text)
    if quoted:
        value = (quoted.group(1) or quoted.group(2) or "").strip()
        if value and "/" not in value and "." not in value:
            return value
    return None


def _extract_path_like(text: str) -> str | None:
    for candidate in _BACKTICK_RE.findall(text):
        if "/" in candidate or "." in candidate:
            return candidate.strip()
    for quoted in _QUOTED_RE.findall(text):
        value = next((item for item in quoted if item), "").strip()
        if "/" in value or "." in value:
            return value
    for match in _PATH_RE.finditer(text):
        path = match.group("path")
        if any(sep in path for sep in ("/", ".")):
            return path
    return None


def _extract_pattern(text: str) -> str | None:
    snippet = _extract_quoted_snippet(text)
    if snippet:
        return snippet
    path = _extract_path_like(text)
    if path and not path.endswith((".md", ".txt", ".rst", ".py", ".json", ".yml", ".yaml")):
        return path
    lowered = re.sub(r"^\s*[-*+]\s+", "", text).strip()
    lowered = re.sub(
        r"^(must|must not|should|should not|never|required|forbidden|fail|block|ensure|require)\b[:\s-]*",
        "",
        lowered,
        flags=re.IGNORECASE,
    )
    return lowered or None


def _has_any(text: str, terms: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _determine_kind(candidate: ParsedRuleCandidate) -> tuple[RuleKind, Backend, Confidence, str]:
    text = candidate.text.lower()
    heading = (candidate.source.heading or "").lower()
    context = f"{heading} {text}".strip()

    if candidate.structure == "task":
        return (
            RuleKind.MARKDOWN_TASKS_COMPLETE,
            Backend.FILESYSTEM,
            Confidence.HIGH,
            "Task checkbox extracted from Markdown checklist.",
        )

    if _has_any(context, _GITHUB_HINT_WORDS):
        if "draft" in context:
            return (RuleKind.GITHUB_NOT_DRAFT, Backend.GITHUB, Confidence.HIGH, "GitHub draft-state rule.")
        if "status" in context or "check" in context or "checks" in context:
            return (RuleKind.GITHUB_CHECKS_SUCCESS, Backend.GITHUB, Confidence.HIGH, "GitHub status-check rule.")
        if "thread" in context:
            return (RuleKind.GITHUB_THREADS_RESOLVED, Backend.GITHUB, Confidence.HIGH, "GitHub review-thread rule.")
        if "approval" in context or "approved" in context:
            return (RuleKind.GITHUB_NON_AUTHOR_APPROVAL, Backend.GITHUB, Confidence.MEDIUM, "GitHub approval rule.")
        if "label" in context:
            return (RuleKind.GITHUB_LABELS_ABSENT, Backend.GITHUB, Confidence.HIGH, "GitHub label gate.")
        if "task" in context or "todo" in context:
            return (RuleKind.GITHUB_TASKS_COMPLETE, Backend.GITHUB, Confidence.MEDIUM, "GitHub task-list rule.")
        return (RuleKind.GITHUB_PR_OPEN, Backend.GITHUB, Confidence.MEDIUM, "GitHub PR-state rule.")

    if _has_any(context, _FILESYSTEM_HINT_WORDS):
        path = _extract_path_like(candidate.text)
        if "absent" in context or "forbidden" in context or "must not" in context or "never" in context:
            if "path" in context or "name" in context or "match" in context:
                return (RuleKind.PATH_MATCHES, Backend.FILESYSTEM, Confidence.MEDIUM, "Filesystem path-pattern rule.")
            return (RuleKind.FILE_ABSENT, Backend.FILESYSTEM, Confidence.HIGH, "Filesystem absence rule.")
        if "match" in context or "regex" in context or "glob" in context or "named" in context:
            return (RuleKind.PATH_MATCHES, Backend.FILESYSTEM, Confidence.HIGH, "Filesystem path-pattern rule.")
        if "task" in context or "checklist" in context:
            return (RuleKind.MARKDOWN_TASKS_COMPLETE, Backend.FILESYSTEM, Confidence.HIGH, "Filesystem Markdown task rule.")
        if path and ("file" in context or "exist" in context or "present" in context or "must include" in context or "must have" in context) and "mention" not in context and "text" not in context:
            return (RuleKind.FILE_EXISTS, Backend.FILESYSTEM, Confidence.HIGH, "Filesystem existence rule.")
        if "mention" in context or "contain" in context or "include" in context or "text" in context:
            return (RuleKind.TEXT_REQUIRED, Backend.FILESYSTEM, Confidence.HIGH, "Filesystem text-required rule.")
        if "exist" in context or "present" in context or "require" in context or "must" in context:
            return (RuleKind.FILE_EXISTS, Backend.FILESYSTEM, Confidence.HIGH, "Filesystem existence rule.")

    return (
        RuleKind.SEMANTIC_RUBRIC,
        Backend.LLM_RUBRIC,
        Confidence.LOW,
        "Ambiguous rule routed to the semantic rubric backend.",
    )


def _severity_for_text(text: str) -> Severity:
    lowered = text.lower()
    if "should not" in lowered or "should" in lowered:
        return Severity.WARNING
    return Severity.ERROR


def _build_params(candidate: ParsedRuleCandidate, kind: RuleKind) -> dict[str, object]:
    text = candidate.text
    params: dict[str, object] = {}
    path = _extract_path_like(text)
    pattern = _extract_pattern(text)

    if kind in {RuleKind.FILE_EXISTS, RuleKind.FILE_ABSENT, RuleKind.PATH_MATCHES, RuleKind.TEXT_REQUIRED, RuleKind.TEXT_FORBIDDEN, RuleKind.MARKDOWN_TASKS_COMPLETE}:
        if path:
            params["path"] = path
        if kind in {RuleKind.TEXT_REQUIRED, RuleKind.TEXT_FORBIDDEN, RuleKind.PATH_MATCHES} and pattern:
            params["pattern"] = pattern
        if kind == RuleKind.MARKDOWN_TASKS_COMPLETE:
            params.setdefault("path", path or candidate.source.path)

    if kind == RuleKind.GITHUB_PR_OPEN:
        params["target"] = pattern or text
    elif kind == RuleKind.GITHUB_NOT_DRAFT:
        params["target"] = pattern or text
    elif kind == RuleKind.GITHUB_LABELS_ABSENT:
        if path:
            params["label"] = path
    elif kind in {RuleKind.GITHUB_TASKS_COMPLETE, RuleKind.GITHUB_CHECKS_SUCCESS, RuleKind.GITHUB_THREADS_RESOLVED, RuleKind.GITHUB_NON_AUTHOR_APPROVAL}:
        params["target"] = pattern or text

    return params


def classify_candidate(candidate: ParsedRuleCandidate, *, document_stem: str, index: int) -> tuple[Rule, str]:
    kind, backend_hint, confidence, explanation = _determine_kind(candidate)
    title = _clean_title(candidate.text)
    rule = Rule(
        id=f"{document_stem}-{index}",
        title=title,
        source=candidate.source,
        text=candidate.text,
        kind=kind,
        severity=_severity_for_text(candidate.text),
        backend_hint=backend_hint,
        confidence=confidence,
        params=_build_params(candidate, kind),
    )
    return rule, explanation


def classify_candidates(document: ParsedDocument) -> tuple[RuleSet, list[str]]:
    rules: list[Rule] = []
    explanations: list[str] = []
    stem = Path(document.path).stem or "rule"
    for index, candidate in enumerate(document.candidates, start=1):
        rule, explanation = classify_candidate(candidate, document_stem=stem, index=index)
        rules.append(rule)
        explanations.append(explanation)
    return RuleSet(rules=rules), explanations


def compile_document(document_path: str | Path, content: str) -> tuple[RuleSet, list[str]]:
    parsed = extract_candidates(document_path, content)
    return classify_candidates(parsed)


__all__ = [
    "classify_candidate",
    "classify_candidates",
    "compile_document",
]
