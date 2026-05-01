"""End-to-end CLI smoke tests for gate-keeper.

Verifies the full compile → classify → validate → exit-code pipeline through
the CLI entry point without any network access. GitHub backend tests in this
file monkeypatch run_gh to avoid live calls.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gate_keeper.backends import _gh
from gate_keeper.cli import main
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK

REPO_ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "local"
PASS_DIR = FIXTURES / "pass"
FAIL_DIR = FIXTURES / "fail"
PASS_README = PASS_DIR / "README.md"
FAIL_README = FAIL_DIR / "README.md"  # does not exist
PASS_TASKS = PASS_DIR / "tasks.md"
FAIL_TASKS = FAIL_DIR / "tasks.md"
SMOKE_RULES = FIXTURES / "rules-smoke.md"
EXAMPLE_RULES = REPO_ROOT / "docs" / "example-rules.md"


# ---------------------------------------------------------------------------
# Local passing fixture → exit 0
# ---------------------------------------------------------------------------


class TestLocalPassFixture:
    """validate with filesystem-only rules + passing target exits 0."""

    def test_file_exists_pass_exits_zero(self, capsys):
        """README.md must exist — pass/README.md exists → exit 0."""
        rc = main([
            "validate", str(SMOKE_RULES),
            "--target", str(PASS_README),
            "--backend", "filesystem",
            "--format", "text",
        ])
        assert rc == EXIT_OK

    def test_file_exists_pass_emits_pass_diagnostic(self, capsys):
        main([
            "validate", str(SMOKE_RULES),
            "--target", str(PASS_README),
            "--backend", "filesystem",
        ])
        captured = capsys.readouterr()
        assert "[filesystem/pass]" in captured.out

    def test_validate_pass_json_format_exit_zero(self, capsys):
        rc = main([
            "validate", str(SMOKE_RULES),
            "--target", str(PASS_README),
            "--backend", "filesystem",
            "--format", "json",
        ])
        assert rc == EXIT_OK
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        statuses = {d["status"] for d in data["diagnostics"]}
        assert statuses == {"pass"}


# ---------------------------------------------------------------------------
# Local failing fixture → exit 1 with diagnostics
# ---------------------------------------------------------------------------


class TestLocalFailFixture:
    """validate with filesystem-only rules + failing target exits 1 with output."""

    def test_file_exists_missing_exits_one(self, capsys):
        """README.md must exist — fail/README.md is absent → exit 1."""
        rc = main([
            "validate", str(SMOKE_RULES),
            "--target", str(FAIL_README),
            "--backend", "filesystem",
            "--format", "text",
        ])
        assert rc == EXIT_FAIL

    def test_file_exists_missing_emits_fail_diagnostic(self, capsys):
        main([
            "validate", str(SMOKE_RULES),
            "--target", str(FAIL_README),
            "--backend", "filesystem",
        ])
        captured = capsys.readouterr()
        assert "[filesystem/fail]" in captured.out

    def test_file_exists_missing_emits_useful_message(self, capsys):
        """Failing diagnostic includes the path and a useful description."""
        main([
            "validate", str(SMOKE_RULES),
            "--target", str(FAIL_README),
            "--backend", "filesystem",
        ])
        captured = capsys.readouterr()
        assert str(FAIL_README) in captured.out
        assert "not exist" in captured.out or "absent" in captured.out or "fail" in captured.out

    def test_json_format_fail_has_evidence(self, capsys):
        main([
            "validate", str(SMOKE_RULES),
            "--target", str(FAIL_README),
            "--backend", "filesystem",
            "--format", "json",
        ])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["diagnostics"]
        for diag in data["diagnostics"]:
            assert diag["status"] == "fail"
            assert diag["evidence"]


# ---------------------------------------------------------------------------
# compile smoke test
# ---------------------------------------------------------------------------


class TestCompileSmokeE2E:
    """compile emits valid JSON IR from example-rules.md."""

    def test_compile_example_rules_exit_zero(self, capsys):
        rc = main(["compile", str(EXAMPLE_RULES), "--format", "json"])
        assert rc == EXIT_OK

    def test_compile_example_rules_valid_ruleset(self, capsys):
        main(["compile", str(EXAMPLE_RULES), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "rules" in data
        assert len(data["rules"]) > 0
        for rule in data["rules"]:
            assert "id" in rule
            assert "kind" in rule
            assert "backend_hint" in rule
            assert "confidence" in rule
            assert "source" in rule

    def test_compile_example_rules_mixed_backends(self, capsys):
        main(["compile", str(EXAMPLE_RULES), "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        backends = {r["backend_hint"] for r in data["rules"]}
        assert "filesystem" in backends
        assert "github" in backends


# ---------------------------------------------------------------------------
# Example rules + local target (verify command from issue #18)
# ---------------------------------------------------------------------------


class TestExampleRulesLocalTarget:
    """Running example-rules.md against a local target doesn't raise or hang."""

    def test_example_rules_local_target_auto_returns_exit_code(self, capsys):
        """Runs without exception; filesystem rules may pass, github rules unavailable."""
        rc = main([
            "validate", str(EXAMPLE_RULES),
            "--target", str(PASS_DIR),
            "--backend", "auto",
        ])
        assert rc in (EXIT_OK, EXIT_FAIL)

    def test_example_rules_local_target_produces_diagnostics(self, capsys):
        main([
            "validate", str(EXAMPLE_RULES),
            "--target", str(PASS_DIR),
            "--backend", "auto",
            "--format", "json",
        ])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "diagnostics" in data
        assert len(data["diagnostics"]) > 0


# ---------------------------------------------------------------------------
# GitHub backend command-construction tests (no live network)
# ---------------------------------------------------------------------------


def _ok_result(stdout: str) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh",))


def _unavailable_result() -> _gh.GhResult:
    return _gh.GhResult(
        ok=False,
        stdout="",
        stderr="could not resolve to a Repository",
        returncode=1,
        cmd=("gh",),
    )


_RESOLVE_PAYLOAD = json.dumps({
    "number": 99,
    "url": "https://github.com/owner/repo/pull/99",
})

_PR_VIEW_PAYLOAD = json.dumps({
    "state": "OPEN",
    "isDraft": False,
    "labels": [],
    "body": "no tasks here",
    "statusCheckRollup": [],
    "reviews": [],
    "headRefName": "feat/branch",
    "baseRefName": "main",
    "author": {"login": "alice"},
})


class TestGithubCommandConstruction:
    """Verify that the GitHub backend constructs the expected gh CLI commands
    without making live network calls. Monkeypatches run_gh so no auth needed."""

    def _capture_gh_calls(self, monkeypatch) -> list[tuple[str, ...]]:
        """Return a list of every args tuple passed to run_gh."""
        calls: list[tuple[str, ...]] = []
        call_index = [0]
        payloads = [_RESOLVE_PAYLOAD, _PR_VIEW_PAYLOAD]

        def fake_run_gh(args: tuple[str, ...]) -> _gh.GhResult:
            calls.append(tuple(args))
            idx = min(call_index[0], len(payloads) - 1)
            call_index[0] += 1
            return _ok_result(payloads[idx])

        monkeypatch.setattr("gate_keeper.backends._target.run_gh", fake_run_gh)
        monkeypatch.setattr("gate_keeper.backends.github.run_gh", fake_run_gh)
        return calls

    def test_pr_view_called_for_github_rule(self, monkeypatch, capsys):
        """GitHub backend issues a gh pr view command for GitHub rule kinds."""
        calls = self._capture_gh_calls(monkeypatch)
        main([
            "validate", str(EXAMPLE_RULES),
            "--target", "https://github.com/owner/repo/pull/99",
            "--backend", "github",
        ])
        gh_commands = [c for c in calls if "pr" in c and "view" in c]
        assert gh_commands, "expected at least one 'gh pr view' call"

    def test_pr_view_includes_json_flag(self, monkeypatch, capsys):
        """gh pr view commands use --json for structured output."""
        calls = self._capture_gh_calls(monkeypatch)
        main([
            "validate", str(EXAMPLE_RULES),
            "--target", "https://github.com/owner/repo/pull/99",
            "--backend", "github",
        ])
        pr_view_calls = [c for c in calls if "pr" in c and "view" in c]
        for call in pr_view_calls:
            assert "--json" in call, f"gh pr view call missing --json: {call}"

    def test_gh_not_called_for_filesystem_backend(self, monkeypatch, capsys):
        """Filesystem backend rules never invoke gh CLI."""
        gh_calls: list[Any] = []

        def spy_run_gh(args: tuple[str, ...]) -> _gh.GhResult:
            gh_calls.append(args)
            return _ok_result("{}")

        monkeypatch.setattr("gate_keeper.backends._target.run_gh", spy_run_gh)
        monkeypatch.setattr("gate_keeper.backends.github.run_gh", spy_run_gh)

        main([
            "validate", str(SMOKE_RULES),
            "--target", str(PASS_README),
            "--backend", "filesystem",
        ])
        assert gh_calls == [], "filesystem backend must not call gh"

    def test_missing_gh_binary_produces_unavailable_not_crash(self, monkeypatch, capsys):
        """When gh binary is missing, GitHub rules emit UNAVAILABLE (fail-closed)."""
        def fake_missing(args: tuple[str, ...]) -> _gh.GhResult:
            return _gh.GhResult(
                ok=False,
                stdout="",
                stderr="gh: command not found",
                returncode=127,
                cmd=("gh",),
                binary_missing=True,
            )

        monkeypatch.setattr("gate_keeper.backends._target.run_gh", fake_missing)
        monkeypatch.setattr("gate_keeper.backends.github.run_gh", fake_missing)

        rc = main([
            "validate", str(EXAMPLE_RULES),
            "--target", "https://github.com/owner/repo/pull/1",
            "--backend", "github",
            "--format", "json",
        ])
        assert rc == EXIT_FAIL
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        statuses = {d["status"] for d in data["diagnostics"]}
        assert "unavailable" in statuses, "missing gh binary must produce unavailable, not error"
