"""Tests for the --include glob feature on ``compile`` and ``validate`` (issue #145).

Covers:
- Single-document positional form continues to work unchanged.
- One ``--include`` glob composes a single Markdown document into one RuleSet.
- Multiple ``--include`` flags merge into one RuleSet across documents.
- Unmatched glob exits ``2`` and reports the unmatched pattern.
- Duplicate rule ids exit ``2`` and report both source path/line.
- Include expansion order is deterministic (lexicographic path order).
- Mixing positional and ``--include`` is rejected as a usage error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper.cli import main
from gate_keeper.diagnostics import EXIT_OK, EXIT_USAGE

REPO_ROOT = Path(__file__).parent.parent
LOCAL_FIXTURES = Path(__file__).parent / "fixtures" / "local"
PASS_DIR = LOCAL_FIXTURES / "pass"
PASS_README = PASS_DIR / "README.md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_rules_doc(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _basic_doc(name: str, line_text: str) -> str:
    """Return a minimal rule document body that produces exactly one rule.

    The bullet form ``- <text>`` plus a normative keyword (``must``) is enough
    for ``parser.parse`` to emit a single Rule whose source location points at
    that bullet line.
    """
    return f"# {name}\n\nIntro paragraph (non-normative).\n\n## Rules\n\n- {line_text}\n"


# ---------------------------------------------------------------------------
# Compile: single-document form still works (regression guard)
# ---------------------------------------------------------------------------


class TestCompileSingleDocument:
    def test_compile_positional_single_document(self, tmp_path, capsys):
        doc = tmp_path / "rules.md"
        _write_rules_doc(doc, _basic_doc("R", "`README.md` must exist."))
        rc = main(["compile", str(doc)])
        captured = capsys.readouterr()
        assert rc == EXIT_OK
        data = json.loads(captured.out)
        assert len(data["rules"]) == 1
        assert data["rules"][0]["source"]["path"] == str(doc)


# ---------------------------------------------------------------------------
# Compile: --include
# ---------------------------------------------------------------------------


class TestCompileInclude:
    def test_compile_one_glob_composes_ruleset(self, tmp_path, capsys, monkeypatch):
        rules_dir = tmp_path / "rules"
        _write_rules_doc(rules_dir / "a.md", _basic_doc("A", "`a.md` must exist."))
        _write_rules_doc(rules_dir / "b.md", _basic_doc("B", "`b.md` must exist."))
        monkeypatch.chdir(tmp_path)
        rc = main(["compile", "--include", "rules/*.md"])
        captured = capsys.readouterr()
        assert rc == EXIT_OK
        data = json.loads(captured.out)
        assert len(data["rules"]) == 2
        # Sources point back to each individual document.
        sources = sorted(r["source"]["path"] for r in data["rules"])
        assert sources == ["rules/a.md", "rules/b.md"]

    def test_compile_multiple_globs_merge(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "rulesA").mkdir()
        (tmp_path / "rulesB").mkdir()
        _write_rules_doc(tmp_path / "rulesA" / "x.md", _basic_doc("X", "`x.md` must exist."))
        _write_rules_doc(tmp_path / "rulesB" / "y.md", _basic_doc("Y", "`y.md` must exist."))
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "compile",
                "--include",
                "rulesA/*.md",
                "--include",
                "rulesB/*.md",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_OK
        data = json.loads(captured.out)
        assert len(data["rules"]) == 2

    def test_compile_unmatched_glob_exits_2(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = main(["compile", "--include", "no-such-dir/*.md"])
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "no-such-dir/*.md" in captured.err
        assert "no files matched" in captured.err

    def test_compile_duplicate_rule_id_exits_2(self, tmp_path, capsys, monkeypatch):
        # Two files with the same stem at the same bullet line collide on the
        # default rule-id scheme ``rule-<stem>-L<line>``. Different parent
        # directories — same stem, same line — is the canonical collision case
        # the issue #145 spec calls out.
        (tmp_path / "rules" / "sub1").mkdir(parents=True)
        (tmp_path / "rules" / "sub2").mkdir(parents=True)
        body = _basic_doc("Same", "`README.md` must exist.")
        _write_rules_doc(tmp_path / "rules" / "sub1" / "shared.md", body)
        _write_rules_doc(tmp_path / "rules" / "sub2" / "shared.md", body)
        monkeypatch.chdir(tmp_path)
        rc = main(["compile", "--include", "rules/*/shared.md"])
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "duplicate rule id" in captured.err
        # Both source paths must appear in the error so the user can locate
        # and rename either rule.
        assert "rules/sub1/shared.md" in captured.err
        assert "rules/sub2/shared.md" in captured.err

    def test_compile_include_order_is_lexicographic(self, tmp_path, capsys, monkeypatch):
        # Lexicographic ordering on the matched path strings is the documented
        # determinism contract. Files named to defeat insertion order:
        # ``zeta.md`` is created first but must come *after* ``alpha.md`` in
        # the merged rule list.
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        _write_rules_doc(rules_dir / "zeta.md", _basic_doc("Z", "`z.md` must exist."))
        _write_rules_doc(rules_dir / "alpha.md", _basic_doc("A", "`a.md` must exist."))
        monkeypatch.chdir(tmp_path)
        rc = main(["compile", "--include", "rules/*.md"])
        captured = capsys.readouterr()
        assert rc == EXIT_OK
        data = json.loads(captured.out)
        paths = [r["source"]["path"] for r in data["rules"]]
        # alpha.md must precede zeta.md.
        assert paths == sorted(paths)
        assert paths[0].endswith("alpha.md")
        assert paths[-1].endswith("zeta.md")

    def test_compile_rejects_positional_and_include_together(self, tmp_path, capsys, monkeypatch):
        doc = tmp_path / "rules.md"
        _write_rules_doc(doc, _basic_doc("R", "`README.md` must exist."))
        monkeypatch.chdir(tmp_path)
        rc = main(["compile", "rules.md", "--include", "rules.md"])
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "either" in captured.err

    def test_compile_requires_some_input(self, capsys):
        rc = main(["compile"])
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "either" in captured.err


# ---------------------------------------------------------------------------
# Validate: --include
# ---------------------------------------------------------------------------


class TestValidateInclude:
    def test_validate_with_include_runs(self, tmp_path, capsys, monkeypatch):
        # Two rule docs, each producing one filesystem rule. Validation
        # against ``PASS_README`` (which exists) should exit OK.
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        _write_rules_doc(rules_dir / "a.md", _basic_doc("A", "`README.md` must exist."))
        _write_rules_doc(rules_dir / "b.md", _basic_doc("B", "`README.md` must exist."))
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "validate",
                "--include",
                "rules/*.md",
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
                "--format",
                "json",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_OK
        data = json.loads(captured.out)
        # Two diagnostics, each pointing at one of the two source documents.
        assert len(data["diagnostics"]) == 2
        sources = sorted(d["source"]["path"] for d in data["diagnostics"])
        assert sources == ["rules/a.md", "rules/b.md"]

    def test_validate_unmatched_include_exits_2(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "validate",
                "--include",
                "no-such-dir/*.md",
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "no files matched" in captured.err

    def test_validate_duplicate_rule_id_exits_2(self, tmp_path, capsys, monkeypatch):
        (tmp_path / "rules" / "sub1").mkdir(parents=True)
        (tmp_path / "rules" / "sub2").mkdir(parents=True)
        body = _basic_doc("Same", "`README.md` must exist.")
        _write_rules_doc(tmp_path / "rules" / "sub1" / "shared.md", body)
        _write_rules_doc(tmp_path / "rules" / "sub2" / "shared.md", body)
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "validate",
                "--include",
                "rules/*/shared.md",
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "duplicate rule id" in captured.err
        assert "rules/sub1/shared.md" in captured.err
        assert "rules/sub2/shared.md" in captured.err

    def test_validate_rejects_positional_and_include_together(self, tmp_path, capsys, monkeypatch):
        doc = tmp_path / "rules.md"
        _write_rules_doc(doc, _basic_doc("R", "`README.md` must exist."))
        monkeypatch.chdir(tmp_path)
        rc = main(
            [
                "validate",
                "rules.md",
                "--include",
                "rules.md",
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "either" in captured.err

    def test_validate_requires_some_input(self, capsys):
        rc = main(
            [
                "validate",
                "--target",
                str(PASS_README),
                "--backend",
                "filesystem",
            ]
        )
        captured = capsys.readouterr()
        assert rc == EXIT_USAGE
        assert "either" in captured.err


# ---------------------------------------------------------------------------
# Determinism on the helper directly
# ---------------------------------------------------------------------------


class TestExpandIncludeGlobs:
    """Direct unit tests on the helper to lock the lexicographic contract."""

    def test_helper_returns_sorted_paths(self, tmp_path, monkeypatch):
        from gate_keeper.cli import _expand_include_globs

        for name in ["c.md", "a.md", "b.md"]:
            (tmp_path / name).write_text("# x\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        out = _expand_include_globs(["*.md"])
        assert [p.name for p in out] == ["a.md", "b.md", "c.md"]

    def test_helper_dedupes_overlapping_globs(self, tmp_path, monkeypatch):
        from gate_keeper.cli import _expand_include_globs

        (tmp_path / "a.md").write_text("# x\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        # Two globs that both match a.md → only one path in output.
        out = _expand_include_globs(["*.md", "a.md"])
        assert [p.name for p in out] == ["a.md"]

    def test_helper_raises_on_unmatched(self, tmp_path, monkeypatch):
        from gate_keeper.cli import _expand_include_globs, _IncludeError

        monkeypatch.chdir(tmp_path)
        with pytest.raises(_IncludeError) as excinfo:
            _expand_include_globs(["nope/*.md"])
        assert "nope/*.md" in str(excinfo.value)
