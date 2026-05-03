"""Rule IR and diagnostic schema for gate-keeper.

This module is the single source of truth for the JSON contract between
`gate-keeper compile` and `gate-keeper validate`. The persisted shape is
documented in docs/rule-ir.md and exemplified by tests/fixtures/ir/.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class Backend(str, enum.Enum):
    FILESYSTEM = "filesystem"
    GITHUB = "github"
    LLM_RUBRIC = "llm-rubric"
    EXTERNAL = "external"


class Status(str, enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class Severity(str, enum.Enum):
    ERROR = "error"
    WARNING = "warning"
    ADVISORY = "advisory"


class Confidence(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RuleKind(str, enum.Enum):
    FILE_EXISTS = "file_exists"
    FILE_ABSENT = "file_absent"
    PATH_MATCHES = "path_matches"
    TEXT_REQUIRED = "text_required"
    TEXT_FORBIDDEN = "text_forbidden"
    MARKDOWN_TASKS_COMPLETE = "markdown_tasks_complete"
    GITHUB_PR_OPEN = "github_pr_open"
    GITHUB_NOT_DRAFT = "github_not_draft"
    GITHUB_LABELS_ABSENT = "github_labels_absent"
    GITHUB_TASKS_COMPLETE = "github_tasks_complete"
    GITHUB_CHECKS_SUCCESS = "github_checks_success"
    GITHUB_THREADS_RESOLVED = "github_threads_resolved"
    GITHUB_NON_AUTHOR_APPROVAL = "github_non_author_approval"
    SEMANTIC_RUBRIC = "semantic_rubric"
    EXTERNAL_CHECK = "external_check"


def _require_keys(
    data: Any,
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{context}: expected mapping, got {type(data).__name__}")
    keys = set(data)
    missing = required - keys
    if missing:
        raise ValueError(f"{context}: missing required fields: {sorted(missing)}")
    unknown = keys - required - optional
    if unknown:
        raise ValueError(f"{context}: unknown fields: {sorted(unknown)}")


def _coerce_enum(enum_cls: type[enum.Enum], value: Any, context: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = sorted(member.value for member in enum_cls)
        raise ValueError(
            f"{context}: {value!r} is not a valid {enum_cls.__name__}; expected one of {valid}"
        ) from exc


def _expect_str(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{context}: expected str, got {type(value).__name__}")
    return value


def _expect_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context}: expected int, got {type(value).__name__}")
    return value


def _expect_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected mapping, got {type(value).__name__}")
    return value


def _expect_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context}: expected list, got {type(value).__name__}")
    return value


def _expect_optional_str(value: Any, context: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, context)


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line: int
    heading: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> SourceLocation:
        _require_keys(data, {"path", "line"}, {"heading"}, "SourceLocation")
        line = _expect_int(data["line"], "SourceLocation.line")
        if line < 1:
            raise ValueError(f"SourceLocation.line: expected 1-based line number, got {line}")
        return cls(
            path=_expect_str(data["path"], "SourceLocation.path"),
            line=line,
            heading=_expect_optional_str(data.get("heading"), "SourceLocation.heading"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"path": self.path, "line": self.line}
        if self.heading is not None:
            result["heading"] = self.heading
        return result


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    source: SourceLocation
    text: str
    kind: RuleKind
    severity: Severity
    backend_hint: Backend
    confidence: Confidence
    params: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Any) -> Rule:
        required = {
            "id",
            "title",
            "source",
            "text",
            "kind",
            "severity",
            "backend_hint",
            "confidence",
            "params",
        }
        _require_keys(data, required, set(), "Rule")
        return cls(
            id=_expect_str(data["id"], "Rule.id"),
            title=_expect_str(data["title"], "Rule.title"),
            source=SourceLocation.from_dict(data["source"]),
            text=_expect_str(data["text"], "Rule.text"),
            kind=_coerce_enum(RuleKind, data["kind"], "Rule.kind"),
            severity=_coerce_enum(Severity, data["severity"], "Rule.severity"),
            backend_hint=_coerce_enum(Backend, data["backend_hint"], "Rule.backend_hint"),
            confidence=_coerce_enum(Confidence, data["confidence"], "Rule.confidence"),
            params=dict(_expect_dict(data["params"], "Rule.params")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source.to_dict(),
            "text": self.text,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "backend_hint": self.backend_hint.value,
            "confidence": self.confidence.value,
            "params": dict(self.params),
        }


@dataclass(frozen=True)
class Evidence:
    kind: str
    data: dict[str, Any]

    @classmethod
    def from_dict(cls, data: Any) -> Evidence:
        _require_keys(data, {"kind", "data"}, set(), "Evidence")
        return cls(
            kind=_expect_str(data["kind"], "Evidence.kind"),
            data=dict(_expect_dict(data["data"], "Evidence.data")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "data": dict(self.data)}


@dataclass(frozen=True)
class Diagnostic:
    rule_id: str
    source: SourceLocation
    backend: Backend
    status: Status
    severity: Severity
    message: str
    evidence: list[Evidence]
    remediation: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Diagnostic:
        required = {
            "rule_id",
            "source",
            "backend",
            "status",
            "severity",
            "message",
            "evidence",
        }
        _require_keys(data, required, {"remediation"}, "Diagnostic")
        evidence_items = _expect_list(data["evidence"], "Diagnostic.evidence")
        return cls(
            rule_id=_expect_str(data["rule_id"], "Diagnostic.rule_id"),
            source=SourceLocation.from_dict(data["source"]),
            backend=_coerce_enum(Backend, data["backend"], "Diagnostic.backend"),
            status=_coerce_enum(Status, data["status"], "Diagnostic.status"),
            severity=_coerce_enum(Severity, data["severity"], "Diagnostic.severity"),
            message=_expect_str(data["message"], "Diagnostic.message"),
            evidence=[Evidence.from_dict(item) for item in evidence_items],
            remediation=_expect_optional_str(data.get("remediation"), "Diagnostic.remediation"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rule_id": self.rule_id,
            "source": self.source.to_dict(),
            "backend": self.backend.value,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "evidence": [item.to_dict() for item in self.evidence],
        }
        if self.remediation is not None:
            result["remediation"] = self.remediation
        return result


@dataclass(frozen=True)
class RuleSet:
    rules: list[Rule]

    @classmethod
    def from_dict(cls, data: Any) -> RuleSet:
        _require_keys(data, {"rules"}, set(), "RuleSet")
        items = _expect_list(data["rules"], "RuleSet.rules")
        rules = [Rule.from_dict(item) for item in items]
        seen: set[str] = set()
        duplicates: set[str] = set()
        for rule in rules:
            if rule.id in seen:
                duplicates.add(rule.id)
            seen.add(rule.id)
        if duplicates:
            raise ValueError(f"RuleSet.rules: duplicate rule ids: {sorted(duplicates)}")
        return cls(rules=rules)

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [rule.to_dict() for rule in self.rules]}


@dataclass(frozen=True)
class DiagnosticReport:
    diagnostics: list[Diagnostic]

    @classmethod
    def from_dict(cls, data: Any) -> DiagnosticReport:
        _require_keys(data, {"diagnostics"}, set(), "DiagnosticReport")
        items = _expect_list(data["diagnostics"], "DiagnosticReport.diagnostics")
        return cls(diagnostics=[Diagnostic.from_dict(item) for item in items])

    def to_dict(self) -> dict[str, Any]:
        return {"diagnostics": [diag.to_dict() for diag in self.diagnostics]}


__all__ = [
    "Backend",
    "Status",
    "Severity",
    "Confidence",
    "RuleKind",
    "SourceLocation",
    "Rule",
    "Evidence",
    "Diagnostic",
    "RuleSet",
    "DiagnosticReport",
]
