"""Filesystem and text backend for gate-keeper.

Evaluates a single compiled Rule against a local target path.
Never raises — all exceptions are translated into diagnostics.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from gate_keeper.models import (
    Backend,
    Diagnostic,
    Evidence,
    Rule,
    RuleKind,
    Status,
)

_FILESYSTEM_KINDS = frozenset(
    {
        RuleKind.FILE_EXISTS,
        RuleKind.FILE_ABSENT,
        RuleKind.PATH_MATCHES,
        RuleKind.TEXT_REQUIRED,
        RuleKind.TEXT_FORBIDDEN,
        RuleKind.MARKDOWN_TASKS_COMPLETE,
    }
)

_TASK_CHECKED_RE = re.compile(r"^[ \t]*[-*+]\s+\[[xX]\]", re.MULTILINE)
_TASK_UNCHECKED_RE = re.compile(r"^[ \t]*[-*+]\s+\[ \]", re.MULTILINE)
_FENCE_START_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _diag(rule: Rule, status: Status, message: str, evidence: list[Evidence]) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.FILESYSTEM,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Evaluate *rule* against *target*. Returns a Diagnostic; never raises."""
    try:
        return _dispatch(rule, Path(target))
    except Exception as exc:  # noqa: BLE001
        return _diag(
            rule,
            Status.ERROR,
            f"internal error: {exc}",
            [Evidence(kind="exception", data={"type": type(exc).__name__, "message": str(exc)})],
        )


def _dispatch(rule: Rule, target: Path) -> Diagnostic:
    kind = rule.kind

    if kind not in _FILESYSTEM_KINDS:
        return _diag(
            rule,
            Status.UNSUPPORTED,
            f"rule kind {kind.value!r} is not supported by the filesystem backend",
            [Evidence(kind="backend_capability", data={"backend": "filesystem", "kind": kind.value})],
        )

    if kind is RuleKind.FILE_EXISTS:
        return _file_exists(rule, target)
    if kind is RuleKind.FILE_ABSENT:
        return _file_absent(rule, target)
    if kind is RuleKind.PATH_MATCHES:
        return _path_matches(rule, target)
    if kind is RuleKind.TEXT_REQUIRED:
        return _text_required(rule, target)
    if kind is RuleKind.TEXT_FORBIDDEN:
        return _text_forbidden(rule, target)
    # RuleKind.MARKDOWN_TASKS_COMPLETE
    return _markdown_tasks_complete(rule, target)


def _file_exists(rule: Rule, target: Path) -> Diagnostic:
    path_str = str(target)
    exists = target.exists()
    evidence = [Evidence(kind="file_stat", data={"path": path_str, "exists": exists})]
    if exists:
        return _diag(rule, Status.PASS, f"{path_str} exists.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} does not exist.", evidence)


def _file_absent(rule: Rule, target: Path) -> Diagnostic:
    path_str = str(target)
    exists = target.exists()
    evidence = [Evidence(kind="file_stat", data={"path": path_str, "exists": exists})]
    if not exists:
        return _diag(rule, Status.PASS, f"{path_str} is absent.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} exists but must be absent.", evidence)


def _path_matches(rule: Rule, target: Path) -> Diagnostic:
    pattern = rule.params.get("pattern")
    if not pattern:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.pattern is required for path_matches but was not provided",
            [Evidence(kind="params_error", data={"missing": "pattern"})],
        )
    path_str = str(target)
    matched = fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(target.name, pattern)
    evidence = [Evidence(kind="path_match", data={"path": path_str, "pattern": pattern, "matched": matched})]
    if matched:
        return _diag(rule, Status.PASS, f"{path_str} matches pattern {pattern!r}.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} does not match pattern {pattern!r}.", evidence)


def _read_file(rule: Rule, target: Path) -> tuple[str | None, Diagnostic | None]:
    """Return (content, None) on success or (None, unavailable_diagnostic) on failure."""
    path_str = str(target)
    if not target.exists():
        return None, _diag(
            rule,
            Status.UNAVAILABLE,
            f"{path_str} does not exist; cannot read for text check.",
            [Evidence(kind="file_stat", data={"path": path_str, "exists": False})],
        )
    if not target.is_file():
        return None, _diag(
            rule,
            Status.UNAVAILABLE,
            f"{path_str} is not a regular file.",
            [Evidence(kind="file_stat", data={"path": path_str, "is_file": False})],
        )
    try:
        return target.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, _diag(
            rule,
            Status.UNAVAILABLE,
            f"cannot read {path_str}: {exc}",
            [Evidence(kind="io_error", data={"path": path_str, "error": str(exc)})],
        )


def _text_required(rule: Rule, target: Path) -> Diagnostic:
    pattern = rule.params.get("pattern")
    if not pattern:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.pattern is required for text_required but was not provided",
            [Evidence(kind="params_error", data={"missing": "pattern"})],
        )
    content, err = _read_file(rule, target)
    if err is not None:
        return err
    path_str = str(target)
    use_regex = bool(rule.params.get("regex", False))
    if use_regex:
        match_count = len(re.findall(pattern, content))
    else:
        match_count = content.count(pattern)
    evidence = [Evidence(kind="text_match", data={"path": path_str, "pattern": pattern, "match_count": match_count})]
    if match_count > 0:
        return _diag(rule, Status.PASS, f"{path_str} contains {pattern!r}.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} does not contain {pattern!r}.", evidence)


def _text_forbidden(rule: Rule, target: Path) -> Diagnostic:
    pattern = rule.params.get("pattern")
    if not pattern:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.pattern is required for text_forbidden but was not provided",
            [Evidence(kind="params_error", data={"missing": "pattern"})],
        )
    content, err = _read_file(rule, target)
    if err is not None:
        return err
    path_str = str(target)
    use_regex = bool(rule.params.get("regex", False))
    if use_regex:
        match_count = len(re.findall(pattern, content))
    else:
        match_count = content.count(pattern)
    evidence = [Evidence(kind="text_match", data={"path": path_str, "pattern": pattern, "match_count": match_count})]
    if match_count == 0:
        return _diag(rule, Status.PASS, f"{path_str} does not contain forbidden pattern {pattern!r}.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} contains forbidden pattern {pattern!r}.", evidence)


def _strip_fenced_blocks(text: str) -> str:
    """Return *text* with fenced code block contents removed (fence lines included)."""
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text.splitlines(keepends=True):
        m = _FENCE_START_RE.match(line)
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


def _markdown_tasks_complete(rule: Rule, target: Path) -> Diagnostic:
    content, err = _read_file(rule, target)
    if err is not None:
        return err
    path_str = str(target)
    scannable = _strip_fenced_blocks(content)
    checked = len(_TASK_CHECKED_RE.findall(scannable))
    unchecked = len(_TASK_UNCHECKED_RE.findall(scannable))
    total = checked + unchecked
    evidence = [Evidence(kind="markdown_tasks", data={"path": path_str, "checked": checked, "unchecked": unchecked, "total": total})]
    if unchecked:
        return _diag(rule, Status.FAIL, f"{path_str} has {unchecked} unchecked task(s) of {total}.", evidence)
    return _diag(rule, Status.PASS, f"{path_str} has all {total} task(s) checked.", evidence)
