"""Tests for gate-keeper validate command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper.cli import main
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, EXIT_USAGE

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_DOC = REPO_ROOT / "docs" / "example-rules.md"
LOCAL_FIXTURES = Path(__file__).parent / "fixtures" / "local"
PASS_DIR = LOCAL_FIXTURES / "pass"
FAIL_DIR = LOCAL_FIXTURES / "fail"
PASS_README = PASS_DIR / "README.md"
# A minimal rules doc that produces exactly one file_exists rule.
SIMPLE_RULES = Path(__file__).parent / "fixtures" / "validate" / "rules-file-exists.md"


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------


class TestBasicInvocation:
    def test_validate_existing_file_exits_zero(self, capsys):
        """Simple file-exists rule against an existing file exits 0."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--format",
                "text",
            ]
        )
        assert rc == EXIT_OK

    def test_validate_missing_document_exits_2(self, capsys):
        rc = main(
            [
                "validate",
                "/nonexistent/rules.md",
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
            ]
        )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "error:" in captured.err

    def test_validate_non_utf8_document_exits_2(self, tmp_path, capsys):
        bad = tmp_path / "binary.md"
        bad.write_bytes(b"\xff\xfe not utf-8 \x80\x81")
        rc = main(
            [
                "validate",
                str(bad),
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
            ]
        )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "UTF-8" in captured.err

    def test_validate_text_format_produces_output(self, capsys):
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--format",
                "text",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_OK
        # text format includes backend/status in square brackets
        assert "[filesystem/" in captured.out

    def test_validate_json_format_produces_valid_json(self, capsys):
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "diagnostics" in data
        assert isinstance(data["diagnostics"], list)
        assert len(data["diagnostics"]) > 0


# ---------------------------------------------------------------------------
# Backend choices
# ---------------------------------------------------------------------------


class TestBackendChoices:
    def test_auto_backend_accepted(self, capsys):
        """auto backend routes the file_exists rule to filesystem → PASS."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "auto",
            ]
        )
        assert rc == EXIT_OK

    def test_filesystem_backend_accepted(self, capsys):
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        assert rc == EXIT_OK

    def test_github_backend_accepted_but_unavailable(self, capsys):
        """github backend is registered; file_exists → UNSUPPORTED → exit 1."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "github",
            ]
        )
        # github stub returns UNAVAILABLE for all rules → exit 1
        assert rc == EXIT_FAIL

    def test_llm_rubric_backend_accepted_but_unavailable(self, capsys):
        """llm-rubric backend is registered; all rules → UNAVAILABLE → exit 1."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "llm-rubric",
            ]
        )
        assert rc == EXIT_FAIL

    def test_unknown_backend_rejected_by_argparse(self, capsys):
        """argparse rejects unknown backend names before main() runs."""
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "validate",
                    str(SIMPLE_RULES),
                    "--target",
                    str(PASS_README),
                    "--backend",
                    "totally-fake",
                ]
            )
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_file_exists_pass_exits_0(self, capsys):
        """File exists → PASS → exit 0."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        assert rc == EXIT_OK

    def test_file_exists_missing_exits_1(self, capsys):
        """File missing → FAIL → exit 1."""
        missing = FAIL_DIR / "README.md"
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(missing),
                "--backend",
                "filesystem",
            ]
        )
        assert rc == EXIT_FAIL


# ---------------------------------------------------------------------------
# Example rules document smoke test (acceptance criterion AC-6)
# ---------------------------------------------------------------------------


class TestExampleRulesSmoke:
    def test_example_rules_auto_pass_fixture_does_not_raise(self, capsys):
        """AC-6: runs without exception; emits diagnostics; returns a numeric exit code."""
        rc = main(
            [
                "validate",
                str(EXAMPLE_DOC),
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
            ]
        )
        # filesystem rules pass; github/llm-rubric stubs are UNAVAILABLE → exit 1
        assert rc in (EXIT_OK, EXIT_FAIL)
        captured = capsys.readouterr()
        # Should produce diagnostic output on stdout (at least one line)
        assert captured.out.strip()

    def test_example_rules_json_format_valid_report(self, capsys):
        rc = main(
            [
                "validate",
                str(EXAMPLE_DOC),
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
                "--format",
                "json",
            ]
        )
        assert rc in (EXIT_OK, EXIT_FAIL)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "diagnostics" in data
        # Every diagnostic must carry required fields
        for diag in data["diagnostics"]:
            assert "rule_id" in diag
            assert "status" in diag
            assert "backend" in diag
            assert "severity" in diag
            assert "message" in diag
            assert "evidence" in diag

    def test_example_rules_has_github_unavailable_diagnostics(self, capsys):
        """GitHub rules in example-rules.md produce UNAVAILABLE (fail-closed)."""
        main(
            [
                "validate",
                str(EXAMPLE_DOC),
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        statuses = {d["status"] for d in data["diagnostics"]}
        # github stubs return unavailable → fail-closed
        assert "unavailable" in statuses


# ---------------------------------------------------------------------------
# Diagnostic field contract
# ---------------------------------------------------------------------------


class TestDiagnosticFields:
    def test_all_diagnostics_have_required_fields_in_json(self, capsys):
        main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        for diag in data["diagnostics"]:
            assert "rule_id" in diag
            assert "source" in diag
            assert "backend" in diag
            assert "status" in diag
            assert "severity" in diag
            assert "message" in diag
            assert "evidence" in diag

    def test_text_format_includes_backend_and_status(self, capsys):
        main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--format",
                "text",
            ]
        )
        captured = capsys.readouterr()
        # text format: [backend/status] in every line
        assert "[filesystem/" in captured.out
