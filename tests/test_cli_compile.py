"""Tests for gate-keeper compile command."""
from __future__ import annotations

import json
from pathlib import Path

from gate_keeper.cli import main

REPO_ROOT = Path(__file__).parent.parent
EXAMPLE_DOC = REPO_ROOT / "docs" / "example-rules.md"


def test_compile_json_smoke(capsys):
    """Compile the example document: exit 0 and valid RuleSet JSON."""
    rc = main(["compile", str(EXAMPLE_DOC), "--format", "json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "rules" in data
    assert isinstance(data["rules"], list)
    assert len(data["rules"]) > 0


def test_compile_missing_file_exits_2(capsys):
    """Missing input exits 2 with a compiler-style error on stderr."""
    rc = main(["compile", "/nonexistent/does-not-exist.md", "--format", "json"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err


def test_compile_rules_have_required_fields(capsys):
    """Each compiled rule must carry kind, backend_hint, confidence, and source location."""
    rc = main(["compile", str(EXAMPLE_DOC), "--format", "json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    for rule in data["rules"]:
        assert "kind" in rule
        assert "backend_hint" in rule
        assert "confidence" in rule
        assert "source" in rule
        assert "line" in rule["source"]
        assert rule["source"]["line"] >= 1


def test_compile_output_has_mixed_backends(capsys):
    """Example document must produce both filesystem and github backend rules."""
    rc = main(["compile", str(EXAMPLE_DOC), "--format", "json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    backends = {rule["backend_hint"] for rule in data["rules"]}
    assert "filesystem" in backends
    assert "github" in backends
