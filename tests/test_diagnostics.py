"""Tests for gate_keeper.diagnostics."""

from __future__ import annotations

import json

import pytest

from gate_keeper.diagnostics import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_USAGE,
    compute_exit_code,
    render_json,
    render_text,
    usage_error,
)
from gate_keeper.models import (
    Backend,
    Diagnostic,
    Evidence,
    Severity,
    SourceLocation,
    Status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _loc(path: str = "rules.md", line: int = 5, heading: str | None = None) -> SourceLocation:
    return SourceLocation(path=path, line=line, heading=heading)


def _diag(
    rule_id: str = "r1",
    status: Status = Status.PASS,
    severity: Severity = Severity.ERROR,
    backend: Backend = Backend.FILESYSTEM,
    message: str = "ok",
    evidence: list[Evidence] | None = None,
    path: str = "rules.md",
    line: int = 5,
) -> Diagnostic:
    return Diagnostic(
        rule_id=rule_id,
        source=_loc(path=path, line=line),
        backend=backend,
        status=status,
        severity=severity,
        message=message,
        evidence=evidence or [],
    )


def _ev(kind: str = "test_evidence", **data) -> Evidence:
    return Evidence(kind=kind, data=dict(data))


# ---------------------------------------------------------------------------
# Exit-code policy
# ---------------------------------------------------------------------------

def test_all_pass_exits_0():
    diags = [_diag(status=Status.PASS), _diag(rule_id="r2", status=Status.PASS)]
    assert compute_exit_code(diags) == EXIT_OK


def test_empty_exits_0():
    assert compute_exit_code([]) == EXIT_OK


def test_one_fail_exits_1():
    diags = [_diag(status=Status.PASS), _diag(rule_id="r2", status=Status.FAIL)]
    assert compute_exit_code(diags) == EXIT_FAIL


def test_unavailable_exits_1():
    assert compute_exit_code([_diag(status=Status.UNAVAILABLE)]) == EXIT_FAIL


def test_unsupported_exits_1():
    assert compute_exit_code([_diag(status=Status.UNSUPPORTED)]) == EXIT_FAIL


def test_error_status_exits_1():
    assert compute_exit_code([_diag(status=Status.ERROR)]) == EXIT_FAIL


def test_exit_usage_constant_is_2():
    assert EXIT_USAGE == 2


# ---------------------------------------------------------------------------
# Exit-2 helper
# ---------------------------------------------------------------------------

def test_usage_error_returns_exit_2():
    msg, code = usage_error("cannot read input.md")
    assert code == EXIT_USAGE
    assert "cannot read input.md" in msg


def test_usage_error_prefixes_error():
    msg, _ = usage_error("bad flag")
    assert msg.startswith("error:")


# ---------------------------------------------------------------------------
# Text renderer — all-pass
# ---------------------------------------------------------------------------

def test_text_all_pass_contains_required_fields():
    diags = [_diag(status=Status.PASS, message="all good", path="doc.md", line=12)]
    text = render_text(diags)
    assert "doc.md:12:" in text
    assert "r1" in text
    assert "filesystem" in text
    assert "pass" in text
    assert "error" in text
    assert "all good" in text


def test_text_stable_across_calls():
    diags = [_diag(status=Status.PASS)]
    assert render_text(diags) == render_text(diags)


def test_text_one_diagnostic_per_line():
    diags = [_diag(rule_id="r1"), _diag(rule_id="r2")]
    lines = render_text(diags).splitlines()
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# Text renderer — fail
# ---------------------------------------------------------------------------

def test_text_one_fail_surfaced():
    diags = [
        _diag(rule_id="r1", status=Status.PASS, message="fine"),
        _diag(rule_id="r2", status=Status.FAIL, message="broke"),
    ]
    text = render_text(diags)
    assert "r2" in text
    assert "fail" in text
    assert "broke" in text


# ---------------------------------------------------------------------------
# Text renderer — evidence
# ---------------------------------------------------------------------------

def test_text_evidence_included():
    ev = _ev("text_match", path="AGENTS.md", match_count=3)
    diags = [_diag(evidence=[ev])]
    text = render_text(diags)
    assert "text_match" in text
    assert "AGENTS.md" in text
    assert "3" in text


def test_text_no_evidence_no_brackets():
    diags = [_diag(evidence=[])]
    text = render_text(diags)
    assert "[" not in text.split("]", 1)[-1]  # no trailing evidence bracket


def test_text_evidence_no_data_renders_kind_only():
    ev = Evidence(kind="flag_set", data={})
    diags = [_diag(evidence=[ev])]
    text = render_text(diags)
    assert "flag_set" in text


# ---------------------------------------------------------------------------
# JSON renderer — all-pass
# ---------------------------------------------------------------------------

def test_json_all_pass_status_preserved():
    diags = [_diag(status=Status.PASS)]
    output = json.loads(render_json(diags))
    assert output["diagnostics"][0]["status"] == "pass"


def test_json_all_pass_exit_0():
    diags = [_diag(status=Status.PASS)]
    assert compute_exit_code(diags) == EXIT_OK


def test_json_snapshot_stable():
    ev = _ev("text_match", path="AGENTS.md", match_count=0)
    diag = Diagnostic(
        rule_id="check-uv",
        source=SourceLocation(path="rules.md", line=10, heading="Commands"),
        backend=Backend.FILESYSTEM,
        status=Status.FAIL,
        severity=Severity.ERROR,
        message="pattern not found",
        evidence=[ev],
        remediation="add uv to AGENTS.md",
    )
    out1 = render_json([diag])
    out2 = render_json([diag])
    assert out1 == out2
    parsed = json.loads(out1)
    d = parsed["diagnostics"][0]
    assert d["rule_id"] == "check-uv"
    assert d["status"] == "fail"
    assert d["evidence"][0]["kind"] == "text_match"


# ---------------------------------------------------------------------------
# JSON renderer — fail
# ---------------------------------------------------------------------------

def test_json_one_fail_surfaced():
    diags = [_diag(rule_id="r1", status=Status.FAIL, message="broke")]
    output = json.loads(render_json(diags))
    assert output["diagnostics"][0]["status"] == "fail"
    assert output["diagnostics"][0]["message"] == "broke"


# ---------------------------------------------------------------------------
# JSON renderer — unavailable distinguishable from fail
# ---------------------------------------------------------------------------

def test_json_unavailable_distinguishable_from_fail():
    diags = [
        _diag(rule_id="r-fail", status=Status.FAIL),
        _diag(rule_id="r-unavail", status=Status.UNAVAILABLE),
    ]
    output = json.loads(render_json(diags))
    by_id = {d["rule_id"]: d["status"] for d in output["diagnostics"]}
    assert by_id["r-fail"] == "fail"
    assert by_id["r-unavail"] == "unavailable"
    assert by_id["r-fail"] != by_id["r-unavail"]


def test_json_unavailable_exits_1():
    diags = [_diag(status=Status.UNAVAILABLE)]
    assert compute_exit_code(diags) == EXIT_FAIL


# ---------------------------------------------------------------------------
# JSON renderer — determinism
# ---------------------------------------------------------------------------

def test_json_deterministic_ordering():
    diags = [_diag(rule_id="r1", status=Status.PASS), _diag(rule_id="r2", status=Status.FAIL)]
    assert render_json(diags) == render_json(diags)


def test_json_keys_sorted():
    diags = [_diag()]
    raw = render_json(diags)
    parsed = json.loads(raw)
    diag_keys = list(parsed["diagnostics"][0].keys())
    assert diag_keys == sorted(diag_keys)
