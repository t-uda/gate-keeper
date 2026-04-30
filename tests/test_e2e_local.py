"""End-to-end tests: parse → classify → filesystem backend → diagnostics → exit code.

Drives the full local pipeline without GitHub auth or network access.
Each test covers a specific filesystem rule kind against purpose-built fixtures
under tests/fixtures/local/{pass,fail}/.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from gate_keeper import classifier, parser
from gate_keeper.backends.filesystem import check
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, compute_exit_code
from gate_keeper.models import Backend, RuleKind, Status

FIXTURES = Path(__file__).parent / "fixtures" / "local"
RULES_DOC = FIXTURES / "rules-local.md"
PASS_DIR = FIXTURES / "pass"
FAIL_DIR = FIXTURES / "fail"


def _ruleset():
    rs = parser.parse_file(RULES_DOC)
    return classifier.classify(rs)


def _rules(kind: RuleKind) -> list:
    return [
        r
        for r in _ruleset().rules
        if r.kind is kind and r.backend_hint is Backend.FILESYSTEM
    ]


# ---------------------------------------------------------------------------
# Passing fixture — every rule kind passes against pass/
# ---------------------------------------------------------------------------


class TestPassingFixture:
    """All filesystem rule kinds produce PASS diagnostics against pass/ fixtures."""

    def test_file_exists_readme_present(self):
        rules = _rules(RuleKind.FILE_EXISTS)
        assert rules, "rules-local.md must produce a FILE_EXISTS rule"
        diags = [check(r, PASS_DIR / "README.md") for r in rules]
        assert all(d.status is Status.PASS for d in diags)
        assert compute_exit_code(diags) == EXIT_OK

    def test_file_absent_banned_txt_missing(self):
        rules = _rules(RuleKind.FILE_ABSENT)
        assert rules, "rules-local.md must produce a FILE_ABSENT rule"
        # pass/ has no BANNED.txt → absent → passes
        diags = [check(r, PASS_DIR / "BANNED.txt") for r in rules]
        assert all(d.status is Status.PASS for d in diags)
        assert compute_exit_code(diags) == EXIT_OK

    def test_text_required_manifest_contains_version(self):
        rules = _rules(RuleKind.TEXT_REQUIRED)
        assert rules, "rules-local.md must produce a TEXT_REQUIRED rule"
        rules = [replace(r, params={**r.params, "pattern": "version: 1"}) for r in rules]
        diags = [check(r, PASS_DIR / "manifest.txt") for r in rules]
        assert all(d.status is Status.PASS for d in diags)
        assert compute_exit_code(diags) == EXIT_OK

    def test_text_forbidden_clean_readme(self):
        rules = _rules(RuleKind.TEXT_FORBIDDEN)
        assert rules, "rules-local.md must produce a TEXT_FORBIDDEN rule"
        rules = [replace(r, params={**r.params, "pattern": "DO NOT MERGE"}) for r in rules]
        diags = [check(r, PASS_DIR / "README.md") for r in rules]
        assert all(d.status is Status.PASS for d in diags)
        assert compute_exit_code(diags) == EXIT_OK

    def test_path_matches_md_extension(self):
        rules = _rules(RuleKind.PATH_MATCHES)
        assert rules, "rules-local.md must produce a PATH_MATCHES rule"
        rules = [replace(r, params={**r.params, "pattern": "*.md"}) for r in rules]
        diags = [check(r, PASS_DIR / "README.md") for r in rules]
        assert all(d.status is Status.PASS for d in diags)
        assert compute_exit_code(diags) == EXIT_OK

    def test_markdown_tasks_complete_all_checked(self):
        rules = _rules(RuleKind.MARKDOWN_TASKS_COMPLETE)
        assert rules, "rules-local.md must produce a MARKDOWN_TASKS_COMPLETE rule"
        diags = [check(r, PASS_DIR / "tasks.md") for r in rules]
        assert all(d.status is Status.PASS for d in diags)
        assert compute_exit_code(diags) == EXIT_OK


# ---------------------------------------------------------------------------
# Failing fixture — every rule kind produces a non-pass status and exit code 1
# ---------------------------------------------------------------------------


class TestFailingFixture:
    """At least one diagnostic fails or is unavailable → exit code 1."""

    def test_file_exists_readme_absent_fails(self):
        rules = _rules(RuleKind.FILE_EXISTS)
        # fail/ has no README.md
        diags = [check(r, FAIL_DIR / "README.md") for r in rules]
        assert any(d.status is Status.FAIL for d in diags)
        assert compute_exit_code(diags) == EXIT_FAIL

    def test_file_absent_banned_txt_present_fails(self):
        rules = _rules(RuleKind.FILE_ABSENT)
        # fail/ has BANNED.txt → present → fails
        diags = [check(r, FAIL_DIR / "BANNED.txt") for r in rules]
        assert any(d.status is Status.FAIL for d in diags)
        assert compute_exit_code(diags) == EXIT_FAIL

    def test_text_required_missing_file_is_unavailable(self):
        rules = _rules(RuleKind.TEXT_REQUIRED)
        rules = [replace(r, params={**r.params, "pattern": "version: 1"}) for r in rules]
        # fail/ has no manifest.txt → UNAVAILABLE → non-zero exit
        diags = [check(r, FAIL_DIR / "manifest.txt") for r in rules]
        assert all(d.status is Status.UNAVAILABLE for d in diags)
        assert compute_exit_code(diags) == EXIT_FAIL

    def test_text_forbidden_dirty_file_fails(self):
        rules = _rules(RuleKind.TEXT_FORBIDDEN)
        rules = [replace(r, params={**r.params, "pattern": "DO NOT MERGE"}) for r in rules]
        diags = [check(r, FAIL_DIR / "dirty.txt") for r in rules]
        assert any(d.status is Status.FAIL for d in diags)
        assert compute_exit_code(diags) == EXIT_FAIL

    def test_path_matches_wrong_extension_fails(self):
        rules = _rules(RuleKind.PATH_MATCHES)
        rules = [replace(r, params={**r.params, "pattern": "*.md"}) for r in rules]
        # dirty.txt doesn't match *.md
        diags = [check(r, FAIL_DIR / "dirty.txt") for r in rules]
        assert any(d.status is Status.FAIL for d in diags)
        assert compute_exit_code(diags) == EXIT_FAIL

    def test_markdown_tasks_incomplete_fails(self):
        rules = _rules(RuleKind.MARKDOWN_TASKS_COMPLETE)
        diags = [check(r, FAIL_DIR / "tasks.md") for r in rules]
        assert any(d.status is Status.FAIL for d in diags)
        assert compute_exit_code(diags) == EXIT_FAIL


# ---------------------------------------------------------------------------
# Diagnostic contract — output fields and JSON round-trip
# ---------------------------------------------------------------------------


class TestDiagnosticContract:
    """Every diagnostic carries the required fields and survives JSON round-trip."""

    def test_all_required_fields_present(self):
        rules = _rules(RuleKind.FILE_EXISTS)
        for rule in rules:
            diag = check(rule, PASS_DIR / "README.md")
            assert diag.rule_id
            assert diag.source
            assert diag.backend is Backend.FILESYSTEM
            assert diag.status
            assert diag.severity
            assert diag.message
            assert isinstance(diag.evidence, list)

    def test_diagnostic_round_trips_json(self):
        from gate_keeper.models import Diagnostic

        rules = _rules(RuleKind.FILE_EXISTS)
        for rule in rules:
            diag = check(rule, PASS_DIR / "README.md")
            rebuilt = Diagnostic.from_dict(diag.to_dict())
            assert rebuilt.rule_id == diag.rule_id
            assert rebuilt.status == diag.status
            assert rebuilt.backend == diag.backend

    def test_exit_code_is_zero_when_all_pass(self):
        rules = _rules(RuleKind.FILE_EXISTS)
        diags = [check(r, PASS_DIR / "README.md") for r in rules]
        assert compute_exit_code(diags) == EXIT_OK

    def test_exit_code_is_one_when_any_fail(self):
        rules = _rules(RuleKind.FILE_EXISTS)
        diags = [check(r, FAIL_DIR / "README.md") for r in rules]
        assert compute_exit_code(diags) == EXIT_FAIL


# ---------------------------------------------------------------------------
# Smoke test: compile docs/example-rules.md still emits valid JSON
# ---------------------------------------------------------------------------


class TestCompileSmoke:
    """compile emits valid RuleSet JSON with the correct shape."""

    def test_example_rules_compile_exits_zero(self, capsys):
        import json

        from gate_keeper.cli import main

        repo_root = Path(__file__).parent.parent
        rc = main(["compile", str(repo_root / "docs" / "example-rules.md"), "--format", "json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "rules" in data
        assert len(data["rules"]) > 0

    def test_example_rules_has_filesystem_and_github_rules(self, capsys):
        import json

        from gate_keeper.cli import main

        repo_root = Path(__file__).parent.parent
        main(["compile", str(repo_root / "docs" / "example-rules.md"), "--format", "json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        backends = {r["backend_hint"] for r in data["rules"]}
        assert "filesystem" in backends
        assert "github" in backends
