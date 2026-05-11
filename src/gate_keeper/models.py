"""Rule IR and diagnostic schema for gate-keeper.

This module is the single source of truth for the JSON contract between
`gate-keeper compile` and `gate-keeper validate`. The persisted shape is
documented in docs/rule-ir.md and exemplified by tests/fixtures/ir/.
"""

from __future__ import annotations

import enum
import math
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
    MARKDOWN_EVIDENCE_BLOCK = "markdown_evidence_block"
    GITHUB_PR_OPEN = "github_pr_open"
    GITHUB_NOT_DRAFT = "github_not_draft"
    GITHUB_LABELS_ABSENT = "github_labels_absent"
    GITHUB_TASKS_COMPLETE = "github_tasks_complete"
    GITHUB_CHECKS_SUCCESS = "github_checks_success"
    GITHUB_THREADS_RESOLVED = "github_threads_resolved"
    GITHUB_NON_AUTHOR_APPROVAL = "github_non_author_approval"
    GITHUB_CHANGED_FILES_ABSENT = "github_changed_files_absent"
    CHANGED_FILE_POLICY = "changed_file_policy"
    SEMANTIC_RUBRIC = "semantic_rubric"
    EXTERNAL_CHECK = "external_check"


class TargetKind(str, enum.Enum):
    """Artifact kind a rule applies to (#169).

    Optional rule annotation that lets the LLM rubric backend recognise when
    a rule's premise does not match the artifact under evaluation (e.g. a
    PR-description rule run against a commit message). When absent, defaults
    to ``UNSPECIFIED`` and the prompt template behaves as before.
    """

    UNSPECIFIED = "unspecified"
    PR_DESCRIPTION = "pr_description"
    COMMIT_MESSAGE = "commit_message"
    ISSUE_BODY = "issue_body"
    DOCUMENTATION = "documentation"
    CODE_CHANGE = "code_change"


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
    target_kind: TargetKind = TargetKind.UNSPECIFIED
    # Non-binding suggestion fields populated by the classifier when a
    # textlint-suitable pattern is detected (issue #215).  They are advisory
    # output only — they do NOT change effective routing (``backend_hint`` /
    # ``kind`` remain the source of truth for the validator).  Authors adopt
    # the suggestion by setting ``backend_hint="external"`` and
    # ``kind="external_check"`` in the rule IR explicitly; see
    # ``docs/backend-external.md``.
    suggested_backend: str | None = None
    suggestion_confidence: float | None = None
    suggestion_rationale: str | None = None

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
        optional = {
            "target_kind",
            "suggested_backend",
            "suggestion_confidence",
            "suggestion_rationale",
        }
        _require_keys(data, required, optional, "Rule")
        target_kind_raw = data.get("target_kind")
        if target_kind_raw is None:
            target_kind = TargetKind.UNSPECIFIED
        else:
            target_kind = _coerce_enum(TargetKind, target_kind_raw, "Rule.target_kind")

        # ``suggestion_confidence`` is an optional float in [0.0, 1.0].
        # Reject non-finite values (NaN/Inf) and out-of-range numbers up
        # front so hand-authored or transformed IR cannot smuggle
        # unnormalised confidence scores past the loader (codex review
        # feedback on #227).
        suggestion_confidence_raw = data.get("suggestion_confidence")
        if suggestion_confidence_raw is None:
            suggestion_confidence: float | None = None
        else:
            if isinstance(suggestion_confidence_raw, bool) or not isinstance(
                suggestion_confidence_raw, (int, float)
            ):
                raise ValueError(
                    f"Rule.suggestion_confidence: expected float, "
                    f"got {type(suggestion_confidence_raw).__name__}"
                )
            suggestion_confidence = float(suggestion_confidence_raw)
            if not math.isfinite(suggestion_confidence):
                raise ValueError(
                    f"Rule.suggestion_confidence: expected finite float in [0.0, 1.0], "
                    f"got {suggestion_confidence_raw!r}"
                )
            if not (0.0 <= suggestion_confidence <= 1.0):
                raise ValueError(
                    f"Rule.suggestion_confidence: expected float in [0.0, 1.0], "
                    f"got {suggestion_confidence_raw!r}"
                )

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
            target_kind=target_kind,
            suggested_backend=_expect_optional_str(data.get("suggested_backend"), "Rule.suggested_backend"),
            suggestion_confidence=suggestion_confidence,
            suggestion_rationale=_expect_optional_str(
                data.get("suggestion_rationale"), "Rule.suggestion_rationale"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
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
        # Omit unspecified target_kind from the persisted output so existing
        # IR fixtures remain byte-identical and the field stays opt-in.
        if self.target_kind is not TargetKind.UNSPECIFIED:
            result["target_kind"] = self.target_kind.value
        # Suggestion fields are advisory output and omitted when absent so
        # existing IR fixtures remain byte-identical.
        if self.suggested_backend is not None:
            result["suggested_backend"] = self.suggested_backend
        if self.suggestion_confidence is not None:
            result["suggestion_confidence"] = self.suggestion_confidence
        if self.suggestion_rationale is not None:
            result["suggestion_rationale"] = self.suggestion_rationale
        return result


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
    "TargetKind",
    "SourceLocation",
    "Rule",
    "Evidence",
    "Diagnostic",
    "RuleSet",
    "DiagnosticReport",
]
