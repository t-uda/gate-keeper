"""End-to-end CLI tests for per-rule target scope (issue #279 — S3).

Drives ``gate-keeper validate`` with an IR ruleset whose rules declare
``params.target_scope``, exercising the two binding acceptance-criteria flows:
AC1 (two scopes against ``--target .`` evaluate disjoint subsets) and AC2 (scope
intersected with the ``--target-changed`` set from S1).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gate_keeper.cli import main
from gate_keeper.diagnostics import EXIT_OK


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    for cmd in (
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        ["git", "-C", str(root), "config", "user.name", "T"],
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(cmd, check=True, capture_output=True)
    return root


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _scoped_rules_ir() -> dict:
    def _rule(rule_id: str, scope: list[str]) -> dict:
        return {
            "id": rule_id,
            "title": rule_id,
            "source": {"path": "rules.md", "line": 1},
            "text": "must contain gatekeeper",
            "kind": "text_required",
            "severity": "error",
            "backend_hint": "filesystem",
            "confidence": "high",
            "params": {"pattern": "gatekeeper", "target_scope": scope},
        }

    return {
        "rules": [
            _rule("docs-rule", ["docs/**/*.md"]),
            _rule("src-rule", ["src/**/*.py"]),
        ]
    }


def _populate(repo: Path) -> None:
    _write(repo / "docs" / "a.md", "gatekeeper docs a\n")
    _write(repo / "docs" / "sub" / "b.md", "gatekeeper docs b\n")
    _write(repo / "src" / "x.py", "# gatekeeper src x\n")
    _write(repo / "src" / "pkg" / "y.py", "# gatekeeper src y\n")


def _effective_paths(diag: dict) -> list[str]:
    for ev in diag["evidence"]:
        if ev["kind"] == "scope_effective_set":
            return ev["data"]["effective_paths"]
    raise AssertionError(f"no scope_effective_set evidence in {diag['rule_id']}")


def test_ac1_two_scopes_disjoint_subsets(tmp_path, monkeypatch, capsys):
    repo = _make_git_repo(tmp_path / "repo")
    _populate(repo)
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(_scoped_rules_ir()), encoding="utf-8")

    monkeypatch.chdir(repo)
    rc = main(
        [
            "validate",
            str(rules_file),
            "--rules-format",
            "ir",
            "--target",
            ".",
            "--backend",
            "filesystem",
            "--format",
            "json",
        ]
    )
    assert rc == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    by_id = {d["rule_id"]: d for d in report["diagnostics"]}

    assert _effective_paths(by_id["docs-rule"]) == ["docs/a.md", "docs/sub/b.md"]
    assert _effective_paths(by_id["src-rule"]) == ["src/pkg/y.py", "src/x.py"]
    # Both rules pass — every scoped file contains the required pattern.
    assert by_id["docs-rule"]["status"] == "pass"
    assert by_id["src-rule"]["status"] == "pass"


def test_ac2_scope_intersects_changed_set(tmp_path, monkeypatch, capsys):
    repo = _make_git_repo(tmp_path / "repo")
    # A pre-existing, committed docs file that is NOT part of the changed set.
    _write(repo / "docs" / "old.md", "gatekeeper old\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True
    )

    # Second commit introduces the changed files the audit should narrow to.
    _write(repo / "docs" / "a.md", "gatekeeper docs a\n")
    _write(repo / "src" / "x.py", "# gatekeeper src x\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "change"], check=True, capture_output=True
    )

    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(_scoped_rules_ir()), encoding="utf-8")

    monkeypatch.chdir(repo)
    rc = main(
        [
            "validate",
            str(rules_file),
            "--rules-format",
            "ir",
            "--target-changed",
            "--base-ref",
            "HEAD~1",
            "--backend",
            "filesystem",
            "--format",
            "json",
        ]
    )
    assert rc == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    by_id = {d["rule_id"]: d for d in report["diagnostics"]}

    # docs scope ∩ changed set = only the newly-changed docs file; the committed
    # docs/old.md is outside the changed set and must not be audited.
    assert _effective_paths(by_id["docs-rule"]) == ["docs/a.md"]
    assert _effective_paths(by_id["src-rule"]) == ["src/x.py"]
