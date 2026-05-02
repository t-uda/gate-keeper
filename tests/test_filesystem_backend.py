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
