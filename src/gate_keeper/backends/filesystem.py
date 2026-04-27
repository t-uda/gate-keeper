from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
import re

from gate_keeper.models import Backend, Diagnostic, DiagnosticReport, Evidence, Rule, RuleKind, Severity, SourceLocation, Status

_TASK_RE = re.compile(r"^\s*[-*+]\s+\[(?P<state>[ xX])\]\s+(?P<body>.+?)\s*$")


@dataclass(frozen=True)
class BackendContext:
    root: Path


def _make_diag(rule: Rule, status: Status, message: str, evidence: list[Evidence], *, backend: Backend | None = None) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=backend or rule.backend_hint,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _resolve_subject(root: Path, rule: Rule) -> Path:
    path = rule.params.get("path")
    if path:
        subject = Path(str(path))
        if subject.is_absolute():
            return subject
        return root / subject
    return root


def _iter_paths(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


def _taskbox_state(text: str) -> tuple[int, int]:
    complete = 0
    total = 0
    for line in text.splitlines():
        match = _TASK_RE.match(line)
        if match:
            total += 1
            if match.group("state").lower() == "x":
                complete += 1
    return complete, total


def _evaluate_file_exists(rule: Rule, root: Path) -> Diagnostic:
    subject = _resolve_subject(root, rule)
    if subject.exists():
        return _make_diag(
            rule,
            Status.PASS,
            f"found required path {subject}",
            [Evidence(kind="filesystem.path", data={"path": str(subject), "exists": True})],
        )
    return _make_diag(
        rule,
        Status.FAIL,
        f"required path missing: {subject}",
        [Evidence(kind="filesystem.path", data={"path": str(subject), "exists": False})],
    )


def _evaluate_file_absent(rule: Rule, root: Path) -> Diagnostic:
    subject = _resolve_subject(root, rule)
    if subject.exists():
        return _make_diag(
            rule,
            Status.FAIL,
            f"forbidden path exists: {subject}",
            [Evidence(kind="filesystem.path", data={"path": str(subject), "exists": True})],
        )
    return _make_diag(
        rule,
        Status.PASS,
        f"forbidden path absent as required: {subject}",
        [Evidence(kind="filesystem.path", data={"path": str(subject), "exists": False})],
    )


def _evaluate_path_matches(rule: Rule, root: Path) -> Diagnostic:
    pattern = str(rule.params.get("pattern") or rule.params.get("path") or "").strip()
    if not pattern:
        return _make_diag(
            rule,
            Status.UNSUPPORTED,
            "path pattern rule is missing a pattern",
            [Evidence(kind="filesystem.error", data={"reason": "missing pattern"})],
        )
    matches = []
    for path in _iter_paths(root):
        rel = path.relative_to(root) if root.exists() and root.is_dir() and path.is_relative_to(root) else path
        if fnmatch(str(rel), pattern) or fnmatch(path.name, pattern):
            matches.append(str(rel))
    if matches:
        return _make_diag(
            rule,
            Status.PASS,
            f"found path(s) matching {pattern}",
            [Evidence(kind="filesystem.path_match", data={"pattern": pattern, "matches": matches})],
        )
    return _make_diag(
        rule,
        Status.FAIL,
        f"no path matched {pattern}",
        [Evidence(kind="filesystem.path_match", data={"pattern": pattern, "matches": []})],
    )


def _search_text(rule: Rule, root: Path, *, forbidden: bool) -> Diagnostic:
    subject = _resolve_subject(root, rule)
    pattern = str(rule.params.get("pattern") or "").strip()
    if not pattern:
        return _make_diag(
            rule,
            Status.UNSUPPORTED,
            "text rule is missing a pattern",
            [Evidence(kind="filesystem.error", data={"reason": "missing pattern"})],
        )
    if not subject.exists():
        return _make_diag(
            rule,
            Status.UNAVAILABLE,
            f"required evidence unavailable: {subject} does not exist",
            [Evidence(kind="filesystem.path", data={"path": str(subject), "exists": False})],
        )

    paths = [subject] if subject.is_file() else [p for p in subject.rglob("*") if p.is_file()]
    matches: list[dict[str, object]] = []
    scanned: list[str] = []
    for path in paths:
        scanned.append(str(path))
        try:
            text = _read_text(path)
        except OSError as exc:  # pragma: no cover - filesystem-specific
            return _make_diag(
                rule,
                Status.UNAVAILABLE,
                f"required evidence unavailable: unable to read {path}",
                [Evidence(kind="filesystem.error", data={"path": str(path), "error": str(exc)})],
            )
        found = pattern in text
        if forbidden and found:
            matches.append({"path": str(path), "found": True})
        elif not forbidden and found:
            matches.append({"path": str(path), "found": True})

    if forbidden:
        if matches:
            return _make_diag(
                rule,
                Status.FAIL,
                f"forbidden text found: {pattern}",
                [Evidence(kind="filesystem.text_match", data={"pattern": pattern, "matches": matches, "scanned": scanned})],
            )
        return _make_diag(
            rule,
            Status.PASS,
            f"forbidden text absent: {pattern}",
            [Evidence(kind="filesystem.text_match", data={"pattern": pattern, "matches": [], "scanned": scanned})],
        )

    if matches:
        return _make_diag(
            rule,
            Status.PASS,
            f"required text found: {pattern}",
            [Evidence(kind="filesystem.text_match", data={"pattern": pattern, "matches": matches, "scanned": scanned})],
        )
    return _make_diag(
        rule,
        Status.FAIL,
        f"required text missing: {pattern}",
        [Evidence(kind="filesystem.text_match", data={"pattern": pattern, "matches": [], "scanned": scanned})],
    )


def _evaluate_tasks_complete(rule: Rule, root: Path) -> Diagnostic:
    subject = _resolve_subject(root, rule)
    if not subject.exists():
        return _make_diag(
            rule,
            Status.UNAVAILABLE,
            f"required evidence unavailable: {subject} does not exist",
            [Evidence(kind="filesystem.path", data={"path": str(subject), "exists": False})],
        )
    if subject.is_dir():
        files = [p for p in subject.rglob("*.md") if p.is_file()]
        if not files:
            files = [p for p in subject.rglob("*") if p.is_file()]
        if not files:
            return _make_diag(
                rule,
                Status.UNSUPPORTED,
                f"no markdown files found under {subject}",
                [Evidence(kind="filesystem.error", data={"reason": "no markdown files found", "path": str(subject)})],
            )
        target = files[0]
    else:
        target = subject
    try:
        text = _read_text(target)
    except OSError as exc:  # pragma: no cover - filesystem-specific
        return _make_diag(
            rule,
            Status.UNAVAILABLE,
            f"required evidence unavailable: unable to read {target}",
            [Evidence(kind="filesystem.error", data={"path": str(target), "error": str(exc)})],
        )
    complete, total = _taskbox_state(text)
    if total == 0:
        return _make_diag(
            rule,
            Status.UNSUPPORTED,
            f"no markdown task boxes found in {target}",
            [Evidence(kind="filesystem.taskboxes", data={"path": str(target), "complete": 0, "total": 0})],
        )
    if complete != total:
        return _make_diag(
            rule,
            Status.FAIL,
            f"markdown tasks incomplete in {target}: {complete}/{total} complete",
            [Evidence(kind="filesystem.taskboxes", data={"path": str(target), "complete": complete, "total": total})],
        )
    return _make_diag(
        rule,
        Status.PASS,
        f"all markdown tasks complete in {target}: {complete}/{total}",
        [Evidence(kind="filesystem.taskboxes", data={"path": str(target), "complete": complete, "total": total})],
    )


def evaluate_rule(rule: Rule, target: str | Path) -> Diagnostic:
    root = Path(target)
    if rule.kind == RuleKind.FILE_EXISTS:
        return _evaluate_file_exists(rule, root)
    if rule.kind == RuleKind.FILE_ABSENT:
        return _evaluate_file_absent(rule, root)
    if rule.kind == RuleKind.PATH_MATCHES:
        return _evaluate_path_matches(rule, root)
    if rule.kind == RuleKind.TEXT_REQUIRED:
        return _search_text(rule, root, forbidden=False)
    if rule.kind == RuleKind.TEXT_FORBIDDEN:
        return _search_text(rule, root, forbidden=True)
    if rule.kind == RuleKind.MARKDOWN_TASKS_COMPLETE:
        return _evaluate_tasks_complete(rule, root)
    return _make_diag(
        rule,
        Status.UNSUPPORTED,
        f"backend does not support {rule.kind.value}",
        [Evidence(kind="filesystem.error", data={"reason": "unsupported kind", "kind": rule.kind.value})],
        backend=Backend.FILESYSTEM,
    )


def evaluate_ruleset(ruleset, target: str | Path) -> DiagnosticReport:
    return DiagnosticReport(diagnostics=[evaluate_rule(rule, target) for rule in ruleset.rules])


__all__ = ["evaluate_rule", "evaluate_ruleset"]
