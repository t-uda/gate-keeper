"""Tests for gate-keeper validate command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper.cli import main
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, EXIT_USAGE

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_DOC = REPO_ROOT / "docs" / "example-rules.md"
DOGFOODING_RULES_DOC = REPO_ROOT / "docs" / "dogfooding-rules.md"
LOCAL_FIXTURES = Path(__file__).parent / "fixtures" / "local"
PASS_DIR = LOCAL_FIXTURES / "pass"
FAIL_DIR = LOCAL_FIXTURES / "fail"
PASS_README = PASS_DIR / "README.md"
# A minimal rules doc that produces exactly one file_exists rule.
SIMPLE_RULES = Path(__file__).parent / "fixtures" / "validate" / "rules-file-exists.md"
IR_FIXTURES = Path(__file__).parent / "fixtures" / "ir"
IR_FILESYSTEM_RULES = IR_FIXTURES / "rule-filesystem-text-required.json"
IR_TEXTLINT_RULES = IR_FIXTURES / "rule-external-textlint.json"


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
        """GitHub rules in example-rules.md produce a fail-closed status.

        With ``--target <dir>`` the CLI builds a multi-target ``TargetSpec``;
        the github backend rejects multi-target with ``unsupported`` (issue
        #146). Either ``unavailable`` (legacy single-target rejection) or
        ``unsupported`` (new multi-target rejection) is fail-closed and
        acceptable.
        """
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
        assert {"unavailable", "unsupported"} & statuses


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


# ---------------------------------------------------------------------------
# --verbose flag (issue #70)
# ---------------------------------------------------------------------------


class TestVerboseFlag:
    """CLI-level tests for gate-keeper validate --verbose."""

    def test_verbose_flag_wires_through_to_output(self, capsys, monkeypatch):
        """validate --verbose must produce expanded llm-rubric block in stdout.

        Mocks validator.validate to inject a diagnostic with llm_judgment
        evidence so the test is deterministic and network-free.
        """
        from unittest.mock import MagicMock

        from gate_keeper.models import (
            Backend,
            Diagnostic,
            DiagnosticReport,
            Evidence,
            Severity,
            SourceLocation,
            Status,
        )

        llm_ev = Evidence(
            kind="llm_judgment",
            data={
                "judgment": "fail",
                "primary_reason": "Missing usage section.",
                "supporting_evidence_quotes": ["no ## Usage heading found"],
                "suggested_action": "Add a ## Usage section.",
                "model": "claude-haiku-4-5",
                "prompt_version": "v1",
            },
        )
        fake_diag = Diagnostic(
            rule_id="doc-has-usage",
            source=SourceLocation(path="rules.md", line=1),
            backend=Backend.LLM_RUBRIC,
            status=Status.FAIL,
            severity=Severity.ERROR,
            message="rule failed",
            evidence=[llm_ev],
        )
        fake_report = DiagnosticReport(diagnostics=[fake_diag])

        monkeypatch.setattr(
            "gate_keeper.validator.validate",
            MagicMock(return_value=fake_report),
        )

        main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--verbose",
            ]
        )
        captured = capsys.readouterr()

        # --verbose must expand the llm-rubric block (multiple lines)
        lines = captured.out.strip().splitlines()
        assert len(lines) > 1, "verbose output must span multiple lines"
        # The expanded block must contain a dedicated judgment field line
        assert any("judgment  : fail" in ln for ln in lines[1:]), (
            "verbose block must include a `judgment` field"
        )
        # The primary_reason must appear under its own field
        assert any("Missing usage section." in ln for ln in lines[1:]), (
            "verbose block must include the primary_reason"
        )

    def test_verbose_flag_absent_gives_single_line(self, capsys, monkeypatch):
        """Without --verbose, each diagnostic is a single line even with llm_judgment evidence."""
        from unittest.mock import MagicMock

        from gate_keeper.models import (
            Backend,
            Diagnostic,
            DiagnosticReport,
            Evidence,
            Severity,
            SourceLocation,
            Status,
        )

        llm_ev = Evidence(
            kind="llm_judgment",
            data={
                "judgment": "fail",
                "primary_reason": "Missing section.",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
                "model": "claude-haiku-4-5",
                "prompt_version": "v1",
            },
        )
        fake_diag = Diagnostic(
            rule_id="doc-check",
            source=SourceLocation(path="rules.md", line=1),
            backend=Backend.LLM_RUBRIC,
            status=Status.FAIL,
            severity=Severity.ERROR,
            message="rule failed",
            evidence=[llm_ev],
        )
        fake_report = DiagnosticReport(diagnostics=[fake_diag])

        monkeypatch.setattr(
            "gate_keeper.validator.validate",
            MagicMock(return_value=fake_report),
        )

        main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
            ]
        )
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert len(lines) == 1, "non-verbose output must be one line per diagnostic"

    def test_short_verbose_alias_v_wires_through(self, capsys, monkeypatch):
        """-v is an alias for --verbose and must produce the same expanded output."""
        from unittest.mock import MagicMock

        from gate_keeper.models import (
            Backend,
            Diagnostic,
            DiagnosticReport,
            Evidence,
            Severity,
            SourceLocation,
            Status,
        )

        llm_ev = Evidence(
            kind="llm_judgment",
            data={
                "judgment": "fail",
                "primary_reason": "Missing section.",
                "supporting_evidence_quotes": [],
                "suggested_action": None,
                "model": "claude-haiku-4-5",
                "prompt_version": "v1",
            },
        )
        fake_diag = Diagnostic(
            rule_id="doc-check",
            source=SourceLocation(path="rules.md", line=1),
            backend=Backend.LLM_RUBRIC,
            status=Status.FAIL,
            severity=Severity.ERROR,
            message="rule failed",
            evidence=[llm_ev],
        )
        fake_report = DiagnosticReport(diagnostics=[fake_diag])

        monkeypatch.setattr(
            "gate_keeper.validator.validate",
            MagicMock(return_value=fake_report),
        )

        main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "-v",
            ]
        )
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        assert len(lines) > 1, "-v must expand llm-rubric output just like --verbose"


# ---------------------------------------------------------------------------
# --reproducibility flag (#68)
# ---------------------------------------------------------------------------


class TestReproducibilityFlag:
    """CLI plumbing for ``--reproducibility N`` (#68)."""

    def test_default_reproducibility_is_one(self, capsys):
        """Without --reproducibility the default is 1 (existing behaviour)."""
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

    def test_reproducibility_zero_rejected(self, capsys):
        """N < 1 is rejected as a usage error."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--reproducibility",
                "0",
            ]
        )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "--reproducibility" in captured.err

    def test_reproducibility_negative_rejected(self, capsys):
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--reproducibility",
                "-3",
            ]
        )
        assert rc == EXIT_USAGE

    def test_non_llm_backend_ignores_reproducibility(self, capsys):
        """File-system backend ignores --reproducibility (no-op, no extra evidence)."""
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--reproducibility",
                "5",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        # No reproducibility_score evidence on file-system rules.
        for diag in report["diagnostics"]:
            for ev in diag["evidence"]:
                assert ev["kind"] != "reproducibility_score"

    def test_llm_backend_reproducibility_records_score(self, monkeypatch, tmp_path, capsys):
        """LLM-rubric backend with N>1 records reproducibility_score evidence."""
        from gate_keeper.backends import llm_rubric as llm_backend

        # Configure provider via patched env, mock provider call.
        env = {
            "GATE_KEEPER_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env)
        monkeypatch.setattr(
            llm_backend,
            "_call_anthropic",
            lambda *_a, **_k: (
                json.dumps(
                    {
                        "judgment": "pass",
                        "primary_reason": "looks good",
                        "supporting_evidence_quotes": [],
                        "suggested_action": None,
                    }
                ),
                {"latency_ms": 11, "tokens_in": 22, "tokens_out": 33},
            ),
        )

        # Build a minimal semantic rule document.
        rules_doc = tmp_path / "semantic-rules.md"
        rules_doc.write_text(
            "# Rules\n\n## R1\n\nThe documentation should be clear and comprehensive.\n",
            encoding="utf-8",
        )

        rc = main(
            [
                "validate",
                str(rules_doc),
                "--target",
                str(PASS_README),
                "--backend",
                "auto",
                "--reproducibility",
                "3",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        # At least one diagnostic must carry the reproducibility_score evidence.
        repro_count = 0
        for diag in report["diagnostics"]:
            for ev in diag["evidence"]:
                if ev["kind"] == "reproducibility_score":
                    repro_count += 1
                    assert ev["data"]["n"] == 3
                    assert 0.0 <= ev["data"]["score"] <= 1.0
        assert repro_count >= 1, "expected at least one reproducibility_score evidence in JSON output"
        assert rc == EXIT_OK


# ---------------------------------------------------------------------------
# --rules-format flag (issue #144)
# ---------------------------------------------------------------------------


class TestRulesFormatMarkdownDefault:
    """Without ``--rules-format`` the existing Markdown path is preserved."""

    def test_default_format_is_markdown(self, capsys):
        """Omitting --rules-format runs the Markdown loader (regression check)."""
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
        report = json.loads(captured.out)
        assert report["diagnostics"], "markdown path should still produce diagnostics"

    def test_explicit_markdown_format_matches_default(self, capsys):
        """--rules-format markdown is accepted and equivalent to the default."""
        rc = main(
            [
                "validate",
                "--rules-format",
                "markdown",
                str(SIMPLE_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        assert rc == EXIT_OK


class TestRulesFormatIR:
    """``--rules-format ir`` reads precompiled RuleSet JSON without re-classifying."""

    def test_ir_filesystem_rule_validates_against_target(self, tmp_path, capsys):
        """An IR file with a text_required filesystem rule validates end-to-end."""
        # The IR fixture is a text_required rule with pattern "uv"; the
        # filesystem backend expects --target to point at the file under
        # test, so build a matching file under tmp_path.
        agents = tmp_path / "AGENTS.md"
        agents.write_text("Run `uv sync` to install.\n", encoding="utf-8")

        rc = main(
            [
                "validate",
                "--rules-format",
                "ir",
                str(IR_FILESYSTEM_RULES),
                "--target",
                str(agents),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        diagnostics = report["diagnostics"]
        assert len(diagnostics) == 1
        diag = diagnostics[0]
        # IR-supplied rule_id must survive untouched (no re-classification).
        assert diag["rule_id"] == "agents-md-must-mention-uv"
        assert diag["backend"] == "filesystem"
        assert diag["status"] == "pass"

    def test_ir_loading_preserves_kind_backend_hint_and_params(self, monkeypatch):
        """Regression: IR loading must not re-classify rules.

        Hand-authored ``kind``, ``backend_hint``, and ``params`` must reach
        the validator unchanged. We capture the RuleSet that
        ``validator.validate`` is called with and assert each field matches
        the on-disk IR fixture exactly.
        """
        from unittest.mock import MagicMock

        from gate_keeper.cli import _load_ir_ruleset
        from gate_keeper.models import (
            Backend,
            DiagnosticReport,
            RuleKind,
            RuleSet,
        )

        captured_rulesets: list[RuleSet] = []

        def fake_validate(ruleset, target, *, backend, reproducibility):
            captured_rulesets.append(ruleset)
            return DiagnosticReport(diagnostics=[])

        monkeypatch.setattr("gate_keeper.validator.validate", MagicMock(side_effect=fake_validate))

        rc = main(
            [
                "validate",
                "--rules-format",
                "ir",
                str(IR_TEXTLINT_RULES),
                "--target",
                str(PASS_README),
                "--backend",
                "auto",
            ]
        )
        # With no diagnostics the exit code is OK regardless of backend.
        assert rc == EXIT_OK
        assert len(captured_rulesets) == 1
        ruleset = captured_rulesets[0]
        assert len(ruleset.rules) == 1
        rule = ruleset.rules[0]
        # Confirm the exact fields the IR file declares — proves the
        # classifier was bypassed.
        assert rule.id == "prose-textlint"
        assert rule.kind == RuleKind.EXTERNAL_CHECK
        assert rule.backend_hint == Backend.EXTERNAL
        assert rule.params == {"tool": "textlint"}

        # Loader-level smoke check: the helper returns an equivalent
        # RuleSet directly (and never raises through the classifier).
        loaded = _load_ir_ruleset(IR_TEXTLINT_RULES)
        assert isinstance(loaded, RuleSet)
        assert loaded.rules[0].kind == RuleKind.EXTERNAL_CHECK
        assert loaded.rules[0].backend_hint == Backend.EXTERNAL
        assert loaded.rules[0].params == {"tool": "textlint"}

    def test_ir_missing_file_exits_2(self, capsys):
        rc = main(
            [
                "validate",
                "--rules-format",
                "ir",
                "/nonexistent/rules.json",
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
            ]
        )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "/nonexistent/rules.json" in captured.err

    def test_ir_invalid_json_exits_2_with_path_and_reason(self, tmp_path, capsys):
        bad = tmp_path / "rules.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        rc = main(
            [
                "validate",
                "--rules-format",
                "ir",
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
        assert str(bad) in captured.err
        assert "invalid JSON" in captured.err

    def test_ir_invalid_shape_exits_2_with_path_and_reason(self, tmp_path, capsys):
        """JSON that fails strict RuleSet parsing exits 2 with a clear error."""
        bad = tmp_path / "rules.json"
        # Missing required "rules" key.
        bad.write_text(json.dumps({"oops": []}), encoding="utf-8")
        rc = main(
            [
                "validate",
                "--rules-format",
                "ir",
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
        assert str(bad) in captured.err
        assert "invalid rule IR" in captured.err

    def test_ir_unknown_field_exits_2(self, tmp_path, capsys):
        """Strict parser rejects unknown fields — must surface as exit 2."""
        bad = tmp_path / "rules.json"
        bad.write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "r1",
                            "title": "t",
                            "source": {"path": "x.md", "line": 1},
                            "text": "t",
                            "kind": "file_exists",
                            "severity": "warning",
                            "backend_hint": "filesystem",
                            "confidence": "high",
                            "params": {},
                            "extra_unknown_field": "not allowed",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        rc = main(
            [
                "validate",
                "--rules-format",
                "ir",
                str(bad),
                "--target",
                str(PASS_DIR),
                "--backend",
                "auto",
            ]
        )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "invalid rule IR" in captured.err

    def test_ir_unknown_format_value_rejected_by_argparse(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "validate",
                    "--rules-format",
                    "yaml",
                    str(SIMPLE_RULES),
                    "--target",
                    str(PASS_README),
                ]
            )
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Multi-target evaluation (issue #146)
# ---------------------------------------------------------------------------


class TestMultiTarget:
    """``--target`` accepts multiple paths, directories, and globs."""

    def _write_rules(self, tmp_path):
        """Return a minimal rules doc that produces one ``file_exists`` rule.

        ``file_exists`` is the simplest classifier-friendly kind that needs no
        params; aggregated multi-target dispatch can therefore be exercised
        purely from the CLI without hand-built IR.
        """
        rules = tmp_path / "rules.md"
        rules.write_text(
            "# Rules\n\n## Required Files\n\n- `README.md` must exist.\n",
            encoding="utf-8",
        )
        return rules

    def test_repeated_target_flags_aggregate(self, tmp_path, capsys):
        # Use a ``file_exists`` rule because it requires no params and
        # exercises per-file dispatch directly (each target path is the
        # ``file_exists`` argument).
        rules = self._write_rules(tmp_path)
        a = tmp_path / "a.txt"
        a.write_text("hello\n")
        b = tmp_path / "b.txt"
        b.write_text("hello\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(a),
                "--target",
                str(b),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Exactly one diagnostic per rule, regardless of file count.
        assert len(data["diagnostics"]) == 1
        diag = data["diagnostics"][0]
        assert diag["status"] == "pass"
        summary = next(e for e in diag["evidence"] if e["kind"] == "multi_target_summary")
        assert summary["data"]["file_count"] == 2

    def test_directory_target_aggregates(self, tmp_path, capsys):
        rules = self._write_rules(tmp_path)
        target_dir = tmp_path / "files"
        target_dir.mkdir()
        (target_dir / "a.md").write_text("hello\n")
        (target_dir / "b.md").write_text("hello\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(target_dir),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "pass"

    def test_glob_target_expands(self, tmp_path, capsys):
        rules = self._write_rules(tmp_path)
        target_dir = tmp_path / "files"
        target_dir.mkdir()
        (target_dir / "a.md").write_text("hello\n")
        (target_dir / "b.md").write_text("hello\n")
        (target_dir / "skip.txt").write_text("skip\n")
        glob_pattern = str(target_dir / "*.md")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                glob_pattern,
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        summary = next(e for e in diag["evidence"] if e["kind"] == "multi_target_summary")
        assert summary["data"]["file_count"] == 2

    def test_repeated_target_with_missing_path_fails(self, tmp_path, capsys):
        # Mixed pass/fail: one existing target + one nonexistent target
        # → aggregated FAIL (any per-file FAIL → overall FAIL).
        rules = self._write_rules(tmp_path)
        a = tmp_path / "a.txt"
        a.write_text("hello\n")
        ghost = tmp_path / "ghost.txt"
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(a),
                "--target",
                str(ghost),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "fail"
        summary = next(e for e in diag["evidence"] if e["kind"] == "multi_target_summary")
        assert summary["data"]["fail_count"] == 1
        assert summary["data"]["pass_count"] == 1

    def test_empty_glob_fails_closed(self, tmp_path, capsys):
        rules = self._write_rules(tmp_path)
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(tmp_path / "no_match_*.zzz"),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        # Empty file set → UNAVAILABLE → fail-closed exit code.
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "unavailable"

    def test_over_cap_emits_usage_error(self, tmp_path, capsys, monkeypatch):
        rules = self._write_rules(tmp_path)
        # Lower the cap via monkeypatch on the module-level constant; the CLI
        # reads the constant on every call so this is sufficient.
        monkeypatch.setattr("gate_keeper.targets.DEFAULT_FILE_LIMIT", 2)
        for i in range(5):
            (tmp_path / f"f{i}.md").write_text("hello\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(tmp_path),
                "--backend",
                "filesystem",
            ]
        )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "exceeds limit" in captured.err

    def test_repeated_targets_dispatched_to_non_filesystem_backend_fails_closed(self, tmp_path, capsys):
        # Force --backend external. Two targets → multi-target → external
        # dispatcher must reject (UNSUPPORTED), not silently use one path.
        rules = self._write_rules(tmp_path)
        a = tmp_path / "a.txt"
        a.write_text("uv\n")
        b = tmp_path / "b.txt"
        b.write_text("uv\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(a),
                "--target",
                str(b),
                "--backend",
                "external",
                "--format",
                "json",
            ]
        )
        # Exit code is FAIL because UNSUPPORTED is non-pass.
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "unsupported"
        ev_kinds = {e["kind"] for e in diag["evidence"]}
        assert "multi_target_unsupported" in ev_kinds

    def test_single_file_target_unchanged(self, tmp_path, capsys):
        # The sole-target compatibility path: passes the raw string straight
        # through. We sanity-check by inspecting that no multi_target_summary
        # evidence is emitted (legacy single-file evidence kind preserved).
        rules = self._write_rules(tmp_path)
        f = tmp_path / "single.md"
        f.write_text("hello\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(f),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        ev_kinds = {e["kind"] for e in diag["evidence"]}
        assert "multi_target_summary" not in ev_kinds
        # Legacy single-file evidence kind preserved (file_exists rule).
        assert "file_stat" in ev_kinds

    def test_determinism_across_path_orderings(self, tmp_path, capsys):
        rules = self._write_rules(tmp_path)
        a = tmp_path / "a.txt"
        a.write_text("hello\n")
        b = tmp_path / "b.txt"
        b.write_text("hello\n")
        c = tmp_path / "c.txt"
        c.write_text("hello\n")

        def _run(order):
            argv = ["validate", str(rules)]
            for p in order:
                argv.extend(["--target", str(p)])
            argv.extend(["--backend", "filesystem", "--format", "json"])
            main(argv)
            captured = capsys.readouterr()
            return json.loads(captured.out)

        def _strip_volatile(report):
            # Drop ``raw_targets`` from each summary because it intentionally
            # preserves caller-supplied order; compare everything else.
            for diag in report["diagnostics"]:
                for ev in diag["evidence"]:
                    if ev["kind"] == "multi_target_summary":
                        ev["data"].pop("raw_targets", None)
            return report

        first = _strip_volatile(_run([a, b, c]))
        second = _strip_volatile(_run([c, b, a]))
        third = _strip_volatile(_run([b, a, c]))
        # Aggregate diagnostic content (per-file evidence + statuses) is
        # order-independent: paths are sorted lexicographically before
        # evaluation.
        assert first == second == third

    def test_repeated_target_with_github_backend_unsupported(self, tmp_path, capsys):
        # github backend cannot accept multi-target — fail closed.
        rules = self._write_rules(tmp_path)
        a = tmp_path / "a.txt"
        a.write_text("uv\n")
        b = tmp_path / "b.txt"
        b.write_text("uv\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(a),
                "--target",
                str(b),
                "--backend",
                "github",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "unsupported"
        assert any(e["kind"] == "multi_target_unsupported" for e in diag["evidence"])

    def test_existing_file_with_glob_chars_passes_through(self, tmp_path, capsys):
        # Codex/Copilot follow-up: a literal filename containing ``[`` is
        # not a glob — preserve single-target compatibility when the file
        # actually exists on disk.
        rules = self._write_rules(tmp_path)
        f = tmp_path / "a[b].txt"
        f.write_text("hello\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(f),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        # No multi-target aggregation; the literal file went through the
        # legacy single-path code path.
        ev_kinds = {e["kind"] for e in diag["evidence"]}
        assert "multi_target_summary" not in ev_kinds

    def test_github_pr_url_with_query_routed_to_github(self, tmp_path, capsys):
        # Codex P1: a PR URL with a ``?`` query string contains a glob
        # metacharacter but must still reach the github backend as a raw
        # string. Without the fix, the CLI converted it to an empty
        # TargetSpec and the github backend returned ``unsupported``.
        rules = self._write_rules(tmp_path)
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                "https://github.com/t-uda/gate-keeper/pull/1?diff=split",
                "--backend",
                "github",
                "--format",
                "json",
            ]
        )
        # Will be UNAVAILABLE (real gh call against an unrelated repo) or
        # PASS — the contract under test is "URL was not silently dropped".
        del rc
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        ev_kinds = {e["kind"] for e in diag["evidence"]}
        # multi_target_unsupported would indicate the URL was wrongly
        # routed through TargetSpec.
        assert "multi_target_unsupported" not in ev_kinds

    def test_repeated_target_with_llm_rubric_backend_unsupported(self, tmp_path, capsys):
        # llm-rubric backend rejects multi-target in this slice.
        rules = self._write_rules(tmp_path)
        a = tmp_path / "a.txt"
        a.write_text("uv\n")
        b = tmp_path / "b.txt"
        b.write_text("uv\n")
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(a),
                "--target",
                str(b),
                "--backend",
                "llm-rubric",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "unsupported"
        assert any(e["kind"] == "multi_target_unsupported" for e in diag["evidence"])


# ---------------------------------------------------------------------------
# ENAMETOOLONG defence in --target disambiguation (issue #166)
# ---------------------------------------------------------------------------


class TestLongLiteralTargetDoesNotCrash:
    """Issue #166: a literal ``--target`` value with a ``/``-segment longer
    than NAME_MAX (255 bytes on Linux) used to propagate an uncaught
    ``OSError(ENAMETOOLONG)`` from ``Path(sole).is_dir()`` inside the
    path-vs-literal disambiguation block. The CLI must instead treat the
    token as literal text and emit a clean Diagnostic.
    """

    def test_long_literal_target_emits_clean_diagnostic(self, capsys, monkeypatch):
        # Force the llm-rubric backend to its unconfigured-provider path so
        # the test exercises the CLI surface without depending on a real
        # provider key. ``_load_env_file`` is the only knob that reliably
        # masks any developer dotenv (see test_dogfooding_rules.py for the
        # rationale on why patching DOTENV_PATH is insufficient).
        from gate_keeper.backends import llm_rubric

        monkeypatch.setattr(llm_rubric, "_load_env_file", lambda *a, **kw: {})

        # 600-byte literal blob with two ``/``-segments, each well above
        # NAME_MAX (255 bytes). No glob metachars so the disambiguation
        # block reaches the ``Path(sole).is_dir()`` call.
        long_literal = ("a" * 300) + "/" + ("b" * 300)
        assert "/" in long_literal
        assert max(len(seg) for seg in long_literal.split("/")) > 255
        assert not any(c in long_literal for c in "*?[")

        rc = main(
            [
                "validate",
                str(DOGFOODING_RULES_DOC),
                "--target",
                long_literal,
                "--backend",
                "llm-rubric",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        # Must produce well-formed JSON (no Python traceback on stderr).
        data = json.loads(captured.out)
        assert "diagnostics" in data
        assert isinstance(data["diagnostics"], list)
        assert data["diagnostics"], "expected at least one diagnostic"

        # Each rule routes to llm-rubric; with the provider stubbed
        # unconfigured, every diagnostic is UNAVAILABLE with
        # ``provider_unconfigured`` evidence — the contract documented in
        # docs/llm-rubric.md.
        for diag in data["diagnostics"]:
            assert diag["status"] == "unavailable"
            kinds = {ev["kind"] for ev in diag["evidence"]}
            assert "provider_unconfigured" in kinds, f"expected provider_unconfigured evidence, got {kinds}"

        # Exit code is fail-closed (FAIL) for unavailable diagnostics.
        assert rc == EXIT_FAIL

    def test_long_literal_target_no_traceback_on_stderr(self, capsys, monkeypatch):
        # Companion check: the previous bug surfaced as a Python traceback
        # on stderr. Pin the no-traceback contract explicitly.
        from gate_keeper.backends import llm_rubric

        monkeypatch.setattr(llm_rubric, "_load_env_file", lambda *a, **kw: {})

        long_literal = "x" * 400  # single segment > NAME_MAX, no slash
        main(
            [
                "validate",
                str(DOGFOODING_RULES_DOC),
                "--target",
                long_literal,
                "--backend",
                "llm-rubric",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        assert "Traceback" not in captured.err
        assert "ENAMETOOLONG" not in captured.err
        assert "File name too long" not in captured.err


# ---------------------------------------------------------------------------
# Glob-metachar literal text on non-filesystem rules (issue #165)
# ---------------------------------------------------------------------------


class TestGlobMetacharLiteralFallthrough:
    """Issue #165: a single ``--target`` value that contains glob metachars
    (``*``/``?``/``[``) but expands to zero filesystem matches must be
    forwarded as literal text when the ruleset has no filesystem rules.

    Before this fix, ``resolve_targets`` produced ``TargetSpec(is_multi=True,
    paths=[])`` and the llm-rubric backend rejected it with
    ``multi_target_unsupported`` — the literal author-supplied prose never
    reached the model. This dropped 5/5 PR-body samples in tick 1 of the
    dogfood loop (umbrella #164) because every realistic markdown body
    contains ``**bold**``, ``[link](x)`` brackets, or ``- [x] item``
    checkbox syntax.
    """

    def test_markdown_body_with_metachars_reaches_backend_as_literal(self, capsys, monkeypatch):
        # Mask the developer dotenv via the established stub pattern so the
        # llm-rubric backend takes its provider-unconfigured path. See
        # tests/test_dogfooding_rules.py for the rationale on why patching
        # ``_load_env_file`` (rather than ``DOTENV_PATH``) is required.
        from gate_keeper.backends import llm_rubric

        monkeypatch.setattr(llm_rubric, "_load_env_file", lambda *a, **kw: {})

        # Realistic PR body fragment containing every glob metachar that
        # tick 1 sampled: ``*`` (italics/bold), ``[`` (link), ``?`` (URL
        # query). No filesystem path on disk would ever match it.
        body = "**bold** with [link](x?diff=split) and *italics*"
        assert any(c in body for c in "*?[")

        rc = main(
            [
                "validate",
                str(DOGFOODING_RULES_DOC),
                "--target",
                body,
                "--backend",
                "llm-rubric",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["diagnostics"], "expected at least one diagnostic"

        # Contract: the backend must see the literal text.  With the
        # provider stubbed unconfigured every diagnostic is UNAVAILABLE
        # with ``provider_unconfigured`` evidence — proving the rubric
        # entered ``check`` rather than fast-failing on the multi-target
        # rejection branch.  Crucially, no diagnostic carries
        # ``multi_target_unsupported`` evidence (the bug signature).
        for diag in data["diagnostics"]:
            assert diag["status"] == "unavailable", (
                f"expected unavailable, got {diag['status']}: {diag['evidence']}"
            )
            ev_kinds = {ev["kind"] for ev in diag["evidence"]}
            assert "provider_unconfigured" in ev_kinds, (
                f"backend did not reach configuration check: {ev_kinds}"
            )
            assert "multi_target_unsupported" not in ev_kinds, (
                "literal --target was mis-routed through multi-target glob resolver"
            )

        # Exit code is fail-closed (FAIL) for unavailable diagnostics.
        assert rc == EXIT_FAIL

    def test_github_checkbox_syntax_reaches_backend_as_literal(self, capsys, monkeypatch):
        # Tick 2 (PR #167 body) hit this case: a markdown task list of the
        # form ``- [x] item`` contains the ``[`` glob metachar and no
        # filesystem matches exist for the bracket-prefixed token.
        from gate_keeper.backends import llm_rubric

        monkeypatch.setattr(llm_rubric, "_load_env_file", lambda *a, **kw: {})

        body = "- [x] item one\n- [ ] item two\n- [x] item three"
        assert "[" in body

        rc = main(
            [
                "validate",
                str(DOGFOODING_RULES_DOC),
                "--target",
                body,
                "--backend",
                "llm-rubric",
                "--format",
                "json",
            ]
        )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["diagnostics"]

        for diag in data["diagnostics"]:
            assert diag["status"] == "unavailable"
            ev_kinds = {ev["kind"] for ev in diag["evidence"]}
            assert "provider_unconfigured" in ev_kinds
            assert "multi_target_unsupported" not in ev_kinds

        assert rc == EXIT_FAIL

    def test_filesystem_rule_with_empty_glob_still_fails_closed(self, tmp_path, capsys):
        # Negative regression: when the ruleset *does* contain a filesystem
        # rule, the legacy "empty glob → UNAVAILABLE" contract must survive.
        # Without this guard the #165 fall-through would silently rewrite
        # ``--target docs/*.zzz`` to a literal string and mask a real
        # missing-file diagnostic.
        rules = tmp_path / "rules.md"
        rules.write_text(
            "# Rules\n\n## Required Files\n\n- `README.md` must exist.\n",
            encoding="utf-8",
        )
        rc = main(
            [
                "validate",
                str(rules),
                "--target",
                str(tmp_path / "no_match_*.zzz"),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        diag = data["diagnostics"][0]
        assert diag["status"] == "unavailable"
