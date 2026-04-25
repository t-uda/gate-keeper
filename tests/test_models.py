from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    DiagnosticReport,
    Evidence,
    Rule,
    RuleKind,
    RuleSet,
    Severity,
    SourceLocation,
    Status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ir"


def test_backend_members():
    assert {m.value for m in Backend} == {"filesystem", "github", "llm-rubric"}


def test_status_members():
    assert {m.value for m in Status} == {
        "pass",
        "fail",
        "unavailable",
        "unsupported",
        "error",
    }


def test_severity_members():
    assert {m.value for m in Severity} == {"error", "warning", "advisory"}


def test_confidence_members():
    assert {m.value for m in Confidence} == {"high", "medium", "low"}


def test_rule_kind_members():
    assert {m.value for m in RuleKind} == {
        "file_exists",
        "file_absent",
        "path_matches",
        "text_required",
        "text_forbidden",
        "markdown_tasks_complete",
        "github_pr_open",
        "github_not_draft",
        "github_labels_absent",
        "github_tasks_complete",
        "github_checks_success",
        "github_threads_resolved",
        "github_non_author_approval",
        "semantic_rubric",
    }


def _ruleset_payload() -> dict:
    return {
        "rules": [
            {
                "id": "r1",
                "title": "t1",
                "source": {"path": "doc.md", "line": 3, "heading": "H"},
                "text": "must do x",
                "kind": "text_required",
                "severity": "error",
                "backend_hint": "filesystem",
                "confidence": "high",
                "params": {"path": "AGENTS.md", "pattern": "uv"},
            }
        ]
    }


def _diagnostic_payload() -> dict:
    return {
        "diagnostics": [
            {
                "rule_id": "r1",
                "source": {"path": "doc.md", "line": 3},
                "backend": "filesystem",
                "status": "fail",
                "severity": "error",
                "message": "missing pattern",
                "evidence": [
                    {"kind": "text_match", "data": {"match_count": 0}}
                ],
                "remediation": "add the pattern",
            }
        ]
    }


def test_ruleset_round_trip():
    payload = _ruleset_payload()
    rs = RuleSet.from_dict(payload)
    assert rs.rules[0].kind is RuleKind.TEXT_REQUIRED
    assert rs.to_dict() == payload


def test_diagnostic_round_trip():
    payload = _diagnostic_payload()
    report = DiagnosticReport.from_dict(payload)
    assert report.diagnostics[0].status is Status.FAIL
    assert report.to_dict() == payload


def test_source_location_omits_null_heading():
    loc = SourceLocation(path="x.md", line=1)
    assert loc.to_dict() == {"path": "x.md", "line": 1}


def test_diagnostic_omits_null_remediation():
    diag = Diagnostic(
        rule_id="r1",
        source=SourceLocation(path="x.md", line=1),
        backend=Backend.FILESYSTEM,
        status=Status.PASS,
        severity=Severity.ERROR,
        message="ok",
        evidence=[Evidence(kind="text_match", data={"match_count": 1})],
    )
    assert "remediation" not in diag.to_dict()


@pytest.mark.parametrize(
    "fixture_name, model_cls",
    [
        ("rule-filesystem-text-required.json", RuleSet),
        ("rule-github-pr-open.json", RuleSet),
        ("diagnostic-mixed.json", DiagnosticReport),
    ],
)
def test_fixtures_round_trip(fixture_name, model_cls):
    payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    instance = model_cls.from_dict(payload)
    assert instance.to_dict() == payload


def test_rule_rejects_unknown_field():
    payload = _ruleset_payload()
    payload["rules"][0]["extra"] = "nope"
    with pytest.raises(ValueError, match="unknown fields"):
        RuleSet.from_dict(payload)


def test_rule_rejects_missing_field():
    payload = _ruleset_payload()
    del payload["rules"][0]["params"]
    with pytest.raises(ValueError, match="missing required fields"):
        RuleSet.from_dict(payload)


def test_rule_rejects_invalid_enum():
    payload = _ruleset_payload()
    payload["rules"][0]["kind"] = "not_a_kind"
    with pytest.raises(ValueError, match="not a valid RuleKind"):
        RuleSet.from_dict(payload)


def test_rule_rejects_wrong_type():
    payload = _ruleset_payload()
    payload["rules"][0]["params"] = "should be dict"
    with pytest.raises(ValueError, match="expected mapping"):
        RuleSet.from_dict(payload)


def test_diagnostic_rejects_invalid_status():
    payload = _diagnostic_payload()
    payload["diagnostics"][0]["status"] = "ok"
    with pytest.raises(ValueError, match="not a valid Status"):
        DiagnosticReport.from_dict(payload)


def test_source_location_rejects_bool_for_line():
    payload = copy.deepcopy(_ruleset_payload())
    payload["rules"][0]["source"]["line"] = True
    with pytest.raises(ValueError, match="expected int"):
        RuleSet.from_dict(payload)


@pytest.mark.parametrize("line", [0, -1])
def test_source_location_rejects_non_positive_line(line):
    payload = copy.deepcopy(_ruleset_payload())
    payload["rules"][0]["source"]["line"] = line
    with pytest.raises(ValueError, match="1-based line number"):
        RuleSet.from_dict(payload)


def test_ruleset_rejects_duplicate_rule_ids():
    payload = _ruleset_payload()
    duplicate = copy.deepcopy(payload["rules"][0])
    payload["rules"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate rule ids"):
        RuleSet.from_dict(payload)


def test_unavailable_status_distinct_from_pass_fail():
    payload = _diagnostic_payload()
    payload["diagnostics"][0]["status"] = "unavailable"
    payload["diagnostics"][0].pop("remediation")
    report = DiagnosticReport.from_dict(payload)
    assert report.diagnostics[0].status is Status.UNAVAILABLE
    assert report.diagnostics[0].status is not Status.PASS
    assert report.diagnostics[0].status is not Status.FAIL
