from __future__ import annotations

import json
from pathlib import Path

from gate_keeper.cli import main


def test_help_exits_successfully(capsys):
    assert main([]) == 0
    captured = capsys.readouterr()
    assert "Compile natural-language rules" in captured.out


def test_compile_command_emits_json(capsys):
    assert main(["compile", "docs/example-rules.md", "--format", "json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload["rules"]) == 3
    assert payload["rules"][0]["source"]["path"] == "docs/example-rules.md"


def test_validate_command_uses_local_filesystem_backend(capsys):
    target = Path("tests/fixtures/local/pass")
    assert main([
        "validate",
        "docs/example-rules.md",
        "--target",
        str(target),
        "--backend",
        "auto",
        "--format",
        "json",
    ]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert all(diag["status"] == "pass" for diag in payload["diagnostics"])


def test_validate_command_reports_failures(capsys):
    target = Path("tests/fixtures/local/fail")
    assert main([
        "validate",
        "docs/example-rules.md",
        "--target",
        str(target),
        "--backend",
        "auto",
        "--format",
        "json",
    ]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert any(diag["status"] != "pass" for diag in payload["diagnostics"])
