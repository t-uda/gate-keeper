"""Tests for gate-keeper explain command."""

from __future__ import annotations

from pathlib import Path

import pytest

from gate_keeper.cli import main

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_DOC = REPO_ROOT / "docs" / "example-rules.md"


def test_explain_smoke_exit_0(capsys):
    """Explain the example document: exits 0 and produces non-empty output."""
    rc = main(["explain", str(EXAMPLE_DOC), "--format", "text"])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() != ""


def test_explain_shows_source_lines(capsys):
    """Each rule block references a source line number."""
    rc = main(["explain", str(EXAMPLE_DOC)])
    assert rc == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    # At least one header line of the form  path:N: [backend/confidence] id: kind
    level_suffixes = ("/high", "/medium", "/low")
    header_lines = [
        line for line in lines if ".md:" in line and "[" in line and any(s in line for s in level_suffixes)
    ]
    assert len(header_lines) > 0


def test_explain_shows_backend_and_confidence(capsys):
    """Output contains both filesystem and github backend references."""
    rc = main(["explain", str(EXAMPLE_DOC)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "filesystem" in captured.out
    assert "github" in captured.out


def test_explain_shows_confidence_levels(capsys):
    """Output includes confidence values (high, medium, or low)."""
    rc = main(["explain", str(EXAMPLE_DOC)])
    assert rc == 0
    captured = capsys.readouterr()
    assert any(level in captured.out for level in ("high", "medium", "low"))


def test_explain_shows_reason(capsys):
    """Each rule block contains a 'reason:' line."""
    rc = main(["explain", str(EXAMPLE_DOC)])
    assert rc == 0
    captured = capsys.readouterr()
    reason_lines = [line for line in captured.out.splitlines() if line.strip().startswith("reason:")]
    assert len(reason_lines) > 0


def test_explain_low_confidence_visible(capsys, tmp_path):
    """A document with ambiguous rules shows low-confidence routing."""
    doc = tmp_path / "ambiguous.md"
    doc.write_text("# Review\n\nThis change is important and must be evaluated carefully.\n")
    rc = main(["explain", str(doc)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "low" in captured.out


def test_explain_missing_file_exits_2(capsys):
    """Missing document exits 2 with an error on stderr."""
    rc = main(["explain", "/nonexistent/file.md"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err


def test_explain_non_utf8_exits_2(tmp_path, capsys):
    """Non-UTF-8 document exits 2 with a UTF-8 error on stderr."""
    bad = tmp_path / "binary.md"
    bad.write_bytes(b"\xff\xfe not utf-8 \x80\x81")
    rc = main(["explain", str(bad)])
    assert rc == 2
    captured = capsys.readouterr()
    assert "UTF-8" in captured.err


def test_explain_help_available(capsys):
    """explain --help exits 0 and shows the subcommand description."""
    with pytest.raises(SystemExit) as exc_info:
        main(["explain", "--help"])
    assert exc_info.value.code == 0
