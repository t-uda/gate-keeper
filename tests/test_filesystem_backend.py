"""Tests for gate_keeper.backends.filesystem."""

from __future__ import annotations

from pathlib import Path

from gate_keeper.backends.filesystem import check
from gate_keeper.models import (
    Backend,
    Confidence,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)
from gate_keeper.targets import TargetSpec, resolve_targets

_FIXTURES = Path(__file__).parent / "fixtures" / "filesystem"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule(kind: RuleKind, params: dict | None = None) -> Rule:
    return Rule(
        id="test-rule",
        title="Test rule",
        source=SourceLocation(path="test.md", line=1),
        text="test rule text",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=Backend.FILESYSTEM,
        confidence=Confidence.HIGH,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# file_exists
# ---------------------------------------------------------------------------


class TestFileExists:
    def test_pass_when_file_present(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.FILE_EXISTS), target)
        assert diag.status is Status.PASS
        assert diag.backend is Backend.FILESYSTEM

    def test_fail_when_file_missing(self, tmp_path):
        target = tmp_path / "nonexistent.txt"
        diag = check(_rule(RuleKind.FILE_EXISTS), target)
        assert diag.status is Status.FAIL

    def test_evidence_contains_path(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.FILE_EXISTS), target)
        assert any(e.kind == "file_stat" for e in diag.evidence)
        assert any(e.data.get("exists") is True for e in diag.evidence)

    def test_unavailable_not_returned_for_missing(self, tmp_path):
        # file_exists should return fail (not unavailable) for a missing path
        diag = check(_rule(RuleKind.FILE_EXISTS), tmp_path / "ghost.txt")
        assert diag.status is Status.FAIL


# ---------------------------------------------------------------------------
# file_absent
# ---------------------------------------------------------------------------


class TestFileAbsent:
    def test_pass_when_file_missing(self, tmp_path):
        target = tmp_path / "absent.txt"
        diag = check(_rule(RuleKind.FILE_ABSENT), target)
        assert diag.status is Status.PASS

    def test_fail_when_file_present(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.FILE_ABSENT), target)
        assert diag.status is Status.FAIL

    def test_evidence_contains_exists_true(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.FILE_ABSENT), target)
        assert any(e.data.get("exists") is True for e in diag.evidence)


# ---------------------------------------------------------------------------
# path_matches
# ---------------------------------------------------------------------------


class TestPathMatches:
    def test_pass_name_glob(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.PATH_MATCHES, {"pattern": "*.txt"}), target)
        assert diag.status is Status.PASS

    def test_fail_name_glob_mismatch(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.PATH_MATCHES, {"pattern": "*.md"}), target)
        assert diag.status is Status.FAIL

    def test_pass_full_path_glob(self):
        target = _FIXTURES / "nested" / "deep.txt"
        path_str = str(target)
        diag = check(_rule(RuleKind.PATH_MATCHES, {"pattern": path_str}), target)
        assert diag.status is Status.PASS

    def test_unavailable_when_pattern_missing(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.PATH_MATCHES, {}), target)
        assert diag.status is Status.UNAVAILABLE

    def test_evidence_contains_pattern(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.PATH_MATCHES, {"pattern": "*.txt"}), target)
        assert any(e.kind == "path_match" for e in diag.evidence)
        assert any("pattern" in e.data for e in diag.evidence)


# ---------------------------------------------------------------------------
# text_required
# ---------------------------------------------------------------------------


class TestTextRequired:
    def test_pass_substring_found(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), target)
        assert diag.status is Status.PASS

    def test_fail_substring_not_found(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "MISSING_TOKEN_XYZ"}), target)
        assert diag.status is Status.FAIL

    def test_unavailable_when_pattern_missing(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {}), target)
        assert diag.status is Status.UNAVAILABLE

    def test_unavailable_when_file_missing(self, tmp_path):
        target = tmp_path / "no_such_file.txt"
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), target)
        assert diag.status is Status.UNAVAILABLE

    def test_regex_mode_pass(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": r"gate.keeper", "regex": True}), target)
        assert diag.status is Status.PASS

    def test_regex_mode_fail(self):
        target = _FIXTURES / "content.txt"
        diag = check(
            _rule(RuleKind.TEXT_REQUIRED, {"pattern": r"NEVER_MATCHES_\d{99}", "regex": True}), target
        )
        assert diag.status is Status.FAIL

    def test_evidence_match_count(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), target)
        ev = next(e for e in diag.evidence if e.kind == "text_match")
        assert ev.data["match_count"] >= 1

    def test_unavailable_on_directory_target(self, tmp_path):
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), tmp_path)
        assert diag.status is Status.UNAVAILABLE


# ---------------------------------------------------------------------------
# text_forbidden
# ---------------------------------------------------------------------------


class TestTextForbidden:
    def test_pass_when_pattern_absent(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {"pattern": "FORBIDDEN_XYZ"}), target)
        assert diag.status is Status.PASS

    def test_fail_when_pattern_present(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {"pattern": "uv"}), target)
        assert diag.status is Status.FAIL

    def test_unavailable_when_pattern_missing(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {}), target)
        assert diag.status is Status.UNAVAILABLE

    def test_unavailable_when_file_missing(self, tmp_path):
        target = tmp_path / "no_such_file.txt"
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {"pattern": "anything"}), target)
        assert diag.status is Status.UNAVAILABLE

    def test_regex_mode_pass(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {"pattern": r"NEVER_\d{99}", "regex": True}), target)
        assert diag.status is Status.PASS

    def test_regex_mode_fail(self):
        target = _FIXTURES / "content.txt"
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {"pattern": r"gate.keeper", "regex": True}), target)
        assert diag.status is Status.FAIL


# ---------------------------------------------------------------------------
# markdown_tasks_complete
# ---------------------------------------------------------------------------


class TestMarkdownTasksComplete:
    def test_pass_all_checked(self):
        target = _FIXTURES / "tasks_all_checked.md"
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), target)
        assert diag.status is Status.PASS

    def test_fail_has_unchecked(self):
        target = _FIXTURES / "tasks_with_unchecked.md"
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), target)
        assert diag.status is Status.FAIL

    def test_pass_no_task_checkboxes(self):
        target = _FIXTURES / "tasks_none.md"
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), target)
        assert diag.status is Status.PASS

    def test_unavailable_when_file_missing(self, tmp_path):
        target = tmp_path / "no_such_file.md"
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), target)
        assert diag.status is Status.UNAVAILABLE

    def test_evidence_counts(self):
        target = _FIXTURES / "tasks_with_unchecked.md"
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), target)
        ev = next(e for e in diag.evidence if e.kind == "markdown_tasks")
        assert ev.data["unchecked"] == 2
        assert ev.data["checked"] == 1
        assert ev.data["total"] == 3

    def test_evidence_counts_all_checked(self):
        target = _FIXTURES / "tasks_all_checked.md"
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), target)
        ev = next(e for e in diag.evidence if e.kind == "markdown_tasks")
        assert ev.data["unchecked"] == 0
        assert ev.data["checked"] == 3
        assert ev.data["total"] == 3

    def test_tmp_path_all_checked(self, tmp_path):
        f = tmp_path / "checklist.md"
        f.write_text("- [x] done\n- [x] also done\n")
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), f)
        assert diag.status is Status.PASS

    def test_tmp_path_unchecked(self, tmp_path):
        f = tmp_path / "checklist.md"
        f.write_text("- [x] done\n- [ ] not done\n")
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), f)
        assert diag.status is Status.FAIL

    def test_checkboxes_inside_code_block_ignored(self, tmp_path):
        f = tmp_path / "doc.md"
        f.write_text("Real tasks:\n- [x] done\n\n```\n- [ ] inside code block\n```\n")
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), f)
        assert diag.status is Status.PASS
        ev = next(e for e in diag.evidence if e.kind == "markdown_tasks")
        assert ev.data["unchecked"] == 0
        assert ev.data["checked"] == 1

    def test_unavailable_on_non_utf8_file(self, tmp_path):
        f = tmp_path / "latin1.md"
        f.write_bytes(b"- [x] done \xff\n")
        diag = check(_rule(RuleKind.MARKDOWN_TASKS_COMPLETE), f)
        assert diag.status is Status.UNAVAILABLE


# ---------------------------------------------------------------------------
# markdown_evidence_block
# ---------------------------------------------------------------------------


_EVIDENCE_DEFAULT_PARAMS = {
    "heading": "Policy evidence",
    "format": "yaml",
    "required_keys": ["policy_bundle", "required_reads", "unresolved_decisions"],
    "allowed_sentinel_values": [
        "not_applicable",
        "not_defined_yet",
        "missing_blocker",
        "unavailable",
    ],
}


class TestMarkdownEvidenceBlock:
    """Six required fixtures: pass, missing heading, missing block, malformed
    YAML, missing required keys, sentinel-value violation."""

    _EBF = _FIXTURES / "evidence_block"

    def test_pass(self):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)), self._EBF / "pass.md"
        )
        assert diag.status is Status.PASS, diag.message
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["heading"] == "Policy evidence"
        assert ev.data["format"] == "yaml"

    def test_fail_missing_heading(self):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            self._EBF / "missing_heading.md",
        )
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["failure"] == "heading_missing"

    def test_fail_missing_block(self):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            self._EBF / "missing_block.md",
        )
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["failure"] == "block_missing"

    def test_fail_malformed_yaml(self):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            self._EBF / "malformed_yaml.md",
        )
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["failure"] == "malformed"
        assert "fence_line" in ev.data
        assert "error" in ev.data

    def test_fail_missing_required_keys(self):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            self._EBF / "missing_keys.md",
        )
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["failure"] == "key_or_sentinel"
        assert set(ev.data["missing_keys"]) >= {"required_reads", "unresolved_decisions"}

    def test_fail_invalid_sentinel(self):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            self._EBF / "bad_sentinel.md",
        )
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["failure"] == "key_or_sentinel"
        invalid = ev.data["invalid_sentinels"]
        assert any(item["key"] == "unresolved_decisions" and item["value"] == "tbd" for item in invalid)

    def test_unavailable_when_heading_param_missing(self, tmp_path):
        target = tmp_path / "x.md"
        target.write_text("# nothing\n")
        params = dict(_EVIDENCE_DEFAULT_PARAMS)
        del params["heading"]
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.UNAVAILABLE

    def test_unavailable_when_format_param_missing(self, tmp_path):
        target = tmp_path / "x.md"
        target.write_text("# nothing\n")
        params = dict(_EVIDENCE_DEFAULT_PARAMS)
        del params["format"]
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.UNAVAILABLE

    def test_unsupported_format(self, tmp_path):
        target = tmp_path / "x.md"
        target.write_text("# nothing\n")
        params = dict(_EVIDENCE_DEFAULT_PARAMS)
        params["format"] = "json"
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.UNSUPPORTED

    def test_unavailable_when_required_keys_empty(self, tmp_path):
        target = tmp_path / "x.md"
        target.write_text("# nothing\n")
        params = dict(_EVIDENCE_DEFAULT_PARAMS)
        params["required_keys"] = []
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.UNAVAILABLE

    def test_unavailable_when_target_missing(self, tmp_path):
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            tmp_path / "nope.md",
        )
        assert diag.status is Status.UNAVAILABLE

    def test_dotted_key_pass(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text(
            "## Policy evidence\n\n"
            "```yaml\n"
            "policy:\n"
            "  bundle: spread-applicant-ai/v3\n"
            "  reviewer: jdoe\n"
            "```\n"
        )
        params = {
            "heading": "Policy evidence",
            "format": "yaml",
            "required_keys": ["policy.bundle", "policy.reviewer"],
        }
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.PASS

    def test_dotted_key_missing(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text("## Policy evidence\n\n```yaml\npolicy:\n  bundle: spread-applicant-ai/v3\n```\n")
        params = {
            "heading": "Policy evidence",
            "format": "yaml",
            "required_keys": ["policy.bundle", "policy.reviewer"],
        }
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert "policy.reviewer" in ev.data["missing_keys"]

    def test_sentinel_check_skips_non_sentinel_strings(self, tmp_path):
        # "spread-applicant-ai/v3" contains hyphens and a slash → not sentinel-shaped.
        target = tmp_path / "doc.md"
        target.write_text(
            "## Policy evidence\n\n"
            "```yaml\n"
            "policy_bundle: spread-applicant-ai/v3\n"
            "required_reads:\n"
            "  - docs/x.md\n"
            "unresolved_decisions: not_applicable\n"
            "```\n"
        )
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            target,
        )
        assert diag.status is Status.PASS

    def test_sentinel_check_skipped_when_allowlist_empty(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text(
            "## Policy evidence\n\n"
            "```yaml\n"
            "policy_bundle: spread-applicant-ai/v3\n"
            "required_reads:\n"
            "  - docs/x.md\n"
            "unresolved_decisions: tbd\n"
            "```\n"
        )
        params = {
            "heading": "Policy evidence",
            "format": "yaml",
            "required_keys": ["policy_bundle", "required_reads", "unresolved_decisions"],
        }
        diag = check(_rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, params), target)
        assert diag.status is Status.PASS

    def test_block_must_be_a_mapping(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text("## Policy evidence\n\n```yaml\n- foo\n- bar\n```\n")
        diag = check(
            _rule(RuleKind.MARKDOWN_EVIDENCE_BLOCK, dict(_EVIDENCE_DEFAULT_PARAMS)),
            target,
        )
        assert diag.status is Status.FAIL
        ev = next(e for e in diag.evidence if e.kind == "evidence_block")
        assert ev.data["failure"] == "not_a_mapping"


# ---------------------------------------------------------------------------
# UnicodeDecodeError → unavailable (P2 fix)
# ---------------------------------------------------------------------------


class TestUnicodeDecodeError:
    def test_text_required_non_utf8_is_unavailable(self, tmp_path):
        f = tmp_path / "latin1.txt"
        f.write_bytes(b"hello \xff world")
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "hello"}), f)
        assert diag.status is Status.UNAVAILABLE

    def test_text_forbidden_non_utf8_is_unavailable(self, tmp_path):
        f = tmp_path / "latin1.txt"
        f.write_bytes(b"hello \xff world")
        diag = check(_rule(RuleKind.TEXT_FORBIDDEN, {"pattern": "hello"}), f)
        assert diag.status is Status.UNAVAILABLE


# ---------------------------------------------------------------------------
# Unsupported rule kind
# ---------------------------------------------------------------------------


class TestUnsupported:
    def test_github_kind_returns_unsupported(self):
        diag = check(_rule(RuleKind.GITHUB_PR_OPEN), _FIXTURES / "content.txt")
        assert diag.status is Status.UNSUPPORTED
        assert diag.backend is Backend.FILESYSTEM

    def test_semantic_rubric_returns_unsupported(self):
        diag = check(_rule(RuleKind.SEMANTIC_RUBRIC), _FIXTURES / "content.txt")
        assert diag.status is Status.UNSUPPORTED

    def test_unsupported_evidence_contains_kind(self):
        diag = check(_rule(RuleKind.GITHUB_CHECKS_SUCCESS), _FIXTURES / "content.txt")
        assert any("kind" in e.data for e in diag.evidence)


# ---------------------------------------------------------------------------
# Diagnostic contract — all results conform to the model
# ---------------------------------------------------------------------------


class TestDiagnosticContract:
    """Every returned Diagnostic round-trips through to_dict / from_dict."""

    def _assert_valid(self, diag):
        from gate_keeper.models import Diagnostic

        d = Diagnostic.from_dict(diag.to_dict())
        assert d.rule_id == diag.rule_id
        assert d.status == diag.status

    def test_file_exists_pass(self):
        self._assert_valid(check(_rule(RuleKind.FILE_EXISTS), _FIXTURES / "content.txt"))

    def test_file_exists_fail(self, tmp_path):
        self._assert_valid(check(_rule(RuleKind.FILE_EXISTS), tmp_path / "x.txt"))

    def test_text_required_unavailable(self, tmp_path):
        self._assert_valid(check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "x"}), tmp_path / "x.txt"))

    def test_unsupported(self):
        self._assert_valid(check(_rule(RuleKind.GITHUB_PR_OPEN), _FIXTURES / "content.txt"))


# ---------------------------------------------------------------------------
# Multi-target aggregation (issue #146)
# ---------------------------------------------------------------------------


class TestMultiTarget:
    """Filesystem backend aggregates per-file results into one Diagnostic."""

    def test_single_file_targetspec_preserves_behaviour(self, tmp_path):
        # is_multi=False with a single resolved path → identical to legacy
        # single-target call.
        f = tmp_path / "a.txt"
        f.write_text("uv\n")
        spec = TargetSpec(paths=[f], raw_targets=[str(f)], is_multi=False)
        diag_multi = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        diag_single = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), f)
        assert diag_multi.status is Status.PASS
        assert diag_single.status is Status.PASS
        # Single-path TargetSpec must round-trip through the legacy path —
        # diagnostics should be byte-identical (same evidence schema).
        assert [e.kind for e in diag_multi.evidence] == [e.kind for e in diag_single.evidence]

    def test_pass_when_all_files_pass(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("uv\n")
        b = tmp_path / "b.txt"
        b.write_text("uv elsewhere\n")
        spec = resolve_targets([str(a), str(b)])
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        assert diag.status is Status.PASS
        summary = next(e for e in diag.evidence if e.kind == "multi_target_summary")
        assert summary.data["file_count"] == 2
        assert summary.data["pass_count"] == 2
        assert summary.data["fail_count"] == 0

    def test_fail_when_any_file_fails(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("uv\n")
        b = tmp_path / "b.txt"
        b.write_text("nothing here\n")
        spec = resolve_targets([str(a), str(b)])
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        assert diag.status is Status.FAIL
        summary = next(e for e in diag.evidence if e.kind == "multi_target_summary")
        assert summary.data["fail_count"] == 1
        assert summary.data["pass_count"] == 1
        # Per-file evidence records present.
        file_results = [e for e in diag.evidence if e.kind == "file_result"]
        statuses = {e.data["status"] for e in file_results}
        assert statuses == {"pass", "fail"}
        # Failing path appears in the diagnostic message for quick scanning.
        assert "b.txt" in diag.message

    def test_unavailable_when_any_file_unavailable(self, tmp_path):
        # text_required on a binary file → UNAVAILABLE per-file → aggregated
        # UNAVAILABLE (fail-closed for evaluator-state failures).
        good = tmp_path / "a.txt"
        good.write_text("uv\n")
        bad = tmp_path / "b.txt"
        bad.write_bytes(b"\xff\xfe binary \x00")
        # ``resolve_targets`` filters binaries during directory expansion;
        # construct the spec explicitly so the backend has to handle a binary
        # entry that snuck in via a literal --target.
        spec_explicit = TargetSpec(
            paths=sorted([good, bad], key=str),
            raw_targets=[str(good), str(bad)],
            is_multi=True,
        )
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec_explicit)
        assert diag.status is Status.UNAVAILABLE
        summary = next(e for e in diag.evidence if e.kind == "multi_target_summary")
        assert summary.data["unavailable_count"] >= 1

    def test_unavailable_when_file_set_empty(self, tmp_path):
        # Empty multi-target spec → UNAVAILABLE (fail-closed).
        empty = tmp_path / "empty"
        empty.mkdir()
        spec = resolve_targets([str(empty)])
        assert spec.paths == []
        diag = check(_rule(RuleKind.FILE_EXISTS), spec)
        assert diag.status is Status.UNAVAILABLE
        # Summary still emitted (file_count=0).
        summary = next(e for e in diag.evidence if e.kind == "multi_target_summary")
        assert summary.data["file_count"] == 0

    def test_unsupported_kind_short_circuits(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("hi\n")
        b = tmp_path / "b.txt"
        b.write_text("hi\n")
        spec = resolve_targets([str(a), str(b)])
        diag = check(_rule(RuleKind.GITHUB_PR_OPEN), spec)
        assert diag.status is Status.UNSUPPORTED

    def test_directory_target_aggregates(self, tmp_path):
        (tmp_path / "a.md").write_text("uv\n")
        (tmp_path / "b.md").write_text("uv\n")
        spec = resolve_targets([str(tmp_path)])
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        assert diag.status is Status.PASS

    def test_glob_target_aggregates(self, tmp_path):
        (tmp_path / "a.md").write_text("uv\n")
        (tmp_path / "b.md").write_text("uv\n")
        (tmp_path / "skip.txt").write_text("nothing\n")
        spec = resolve_targets([str(tmp_path / "*.md")])
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        assert diag.status is Status.PASS
        summary = next(e for e in diag.evidence if e.kind == "multi_target_summary")
        assert summary.data["file_count"] == 2

    def test_diagnostic_round_trips(self, tmp_path):
        from gate_keeper.models import Diagnostic

        a = tmp_path / "a.txt"
        a.write_text("uv\n")
        b = tmp_path / "b.txt"
        b.write_text("nothing\n")
        spec = resolve_targets([str(a), str(b)])
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        rebuilt = Diagnostic.from_dict(diag.to_dict())
        assert rebuilt.status is Status.FAIL
        assert any(e.kind == "multi_target_summary" for e in rebuilt.evidence)

    def test_per_file_evidence_truncates_when_over_limit(self, tmp_path):
        # Force more files than the per-file evidence cap so the summary
        # carries an evidence_truncated counter.
        from gate_keeper.backends import filesystem as fsmod

        for i in range(fsmod._PER_FILE_EVIDENCE_LIMIT + 5):
            (tmp_path / f"f{i:03d}.txt").write_text("uv\n")
        spec = resolve_targets([str(tmp_path)], file_limit=fsmod._PER_FILE_EVIDENCE_LIMIT + 10)
        diag = check(_rule(RuleKind.TEXT_REQUIRED, {"pattern": "uv"}), spec)
        assert diag.status is Status.PASS
        summary = next(e for e in diag.evidence if e.kind == "multi_target_summary")
        assert summary.data["evidence_truncated"] == 5
