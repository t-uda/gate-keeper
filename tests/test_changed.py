"""Tests for gate_keeper.changed and gate_keeper.targets.resolve_changed_targets.

Covers:
- resolve_base_ref: env var priority, default fallback
- compute_changed_files: happy path, bad repo root, bad base ref, git not found
- find_repo_root: walks up to .git, fails without .git
- resolve_changed_targets: text-readable filter, deleted-file filter, cap, dedup
- CLI --target-changed / --base-ref integration: AC1–AC8
  - AC1: changed text files survive the filter; evidence records base_ref + resolved_count
  - AC2: intersection with explicit --target
  - AC3: empty changed set → changed_set_empty evidence, exit 1
  - AC4: subdirectory invocation (path normalisation)
  - AC5: 200-file cap → usage error
  - AC6: non-git directory or bad base-ref → usage error
  - AC8: no --target-changed → existing test suite unaffected
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gate_keeper.changed import (
    DEFAULT_BASE_REF,
    DEFAULT_BASE_REF_ENV,
    ChangedFilesError,
    compute_changed_files,
    find_repo_root,
    resolve_base_ref,
)
from gate_keeper.cli import main
from gate_keeper.diagnostics import EXIT_FAIL, EXIT_OK, EXIT_USAGE
from gate_keeper.targets import (
    TargetExpansionError,
    resolve_changed_targets,
)

REPO_ROOT = Path(__file__).parent.parent
SIMPLE_RULES = Path(__file__).parent / "fixtures" / "validate" / "rules-file-exists.md"


# ---------------------------------------------------------------------------
# resolve_base_ref
# ---------------------------------------------------------------------------


class TestResolveBaseRef:
    def test_returns_default_when_env_unset(self):
        assert resolve_base_ref({}) == DEFAULT_BASE_REF

    def test_returns_env_var_when_set(self):
        assert resolve_base_ref({DEFAULT_BASE_REF_ENV: "my-branch"}) == "my-branch"

    def test_empty_string_falls_back_to_default(self):
        # An empty string is falsy; should fall back to default.
        assert resolve_base_ref({DEFAULT_BASE_REF_ENV: ""}) == DEFAULT_BASE_REF

    def test_reads_os_environ_by_default(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_BASE_REF_ENV, "env-branch")
        assert resolve_base_ref() == "env-branch"


# ---------------------------------------------------------------------------
# find_repo_root
# ---------------------------------------------------------------------------


class TestFindRepoRoot:
    def test_finds_git_in_cwd(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert find_repo_root(tmp_path) == tmp_path

    def test_finds_git_in_parent(self, tmp_path):
        (tmp_path / ".git").mkdir()
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert find_repo_root(sub) == tmp_path

    def test_raises_when_no_git(self, tmp_path):
        with pytest.raises(ChangedFilesError, match="not a git repository"):
            find_repo_root(tmp_path)

    def test_real_repo(self):
        # Gate-keeper's own .git is present — verify it resolves.
        root = find_repo_root(REPO_ROOT)
        assert (root / ".git").exists()


# ---------------------------------------------------------------------------
# compute_changed_files
# ---------------------------------------------------------------------------


class TestComputeChangedFiles:
    def test_bad_repo_root_raises(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        with pytest.raises(ChangedFilesError, match="repo root does not exist"):
            compute_changed_files(nonexistent, "origin/main")

    def test_bad_base_ref_raises(self, tmp_path):
        # Create a minimal git repo with one commit so the command runs.
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )
        with pytest.raises(ChangedFilesError, match="git diff exited"):
            compute_changed_files(tmp_path, "this-ref-does-not-exist")

    def test_git_not_found_raises(self, tmp_path):
        (tmp_path / ".git").mkdir()
        with patch("gate_keeper.changed.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(ChangedFilesError, match="git executable not found"):
                compute_changed_files(tmp_path, "origin/main")

    def test_returns_frozenset_of_posix_paths(self):
        # Smoke test against the real repo: result is a frozenset of strings.
        changed = compute_changed_files(REPO_ROOT, "HEAD~1")
        assert isinstance(changed, frozenset)
        assert all(isinstance(p, str) for p in changed)


# ---------------------------------------------------------------------------
# resolve_changed_targets
# ---------------------------------------------------------------------------


def _write(p: Path, content: str = "hello\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class TestResolveChangedTargets:
    def test_text_files_included(self, tmp_path):
        f = _write(tmp_path / "a.md")
        spec = resolve_changed_targets(frozenset(["a.md"]), tmp_path)
        assert len(spec.paths) == 1
        assert spec.paths[0].resolve() == f.resolve()

    def test_binary_files_excluded(self, tmp_path):
        b = tmp_path / "img.png"
        b.write_bytes(b"\x89PNG\r\n\x1a\n\x00")
        spec = resolve_changed_targets(frozenset(["img.png"]), tmp_path)
        assert spec.paths == []

    def test_deleted_files_excluded(self, tmp_path):
        # File listed in changed set but doesn't exist on disk.
        spec = resolve_changed_targets(frozenset(["deleted.md"]), tmp_path)
        assert spec.paths == []

    def test_cap_enforced(self, tmp_path):
        for i in range(5):
            _write(tmp_path / f"file_{i}.txt")
        paths = frozenset(f"file_{i}.txt" for i in range(5))
        with pytest.raises(TargetExpansionError, match="exceeds limit"):
            resolve_changed_targets(paths, tmp_path, file_limit=3)

    def test_deduplication(self, tmp_path):
        _write(tmp_path / "a.md")
        # Duplicate posix paths are already deduplicated by frozenset.
        spec = resolve_changed_targets(frozenset(["a.md"]), tmp_path)
        assert len(spec.paths) == 1

    def test_is_multi_always_true(self, tmp_path):
        _write(tmp_path / "sole.md")
        spec = resolve_changed_targets(frozenset(["sole.md"]), tmp_path)
        assert spec.is_multi is True

    def test_empty_changed_set_returns_empty_spec(self, tmp_path):
        spec = resolve_changed_targets(frozenset(), tmp_path)
        assert spec.paths == []
        assert spec.is_multi is True

    def test_paths_are_absolute(self, tmp_path):
        _write(tmp_path / "sub" / "doc.md")
        spec = resolve_changed_targets(frozenset(["sub/doc.md"]), tmp_path)
        assert len(spec.paths) == 1
        assert spec.paths[0].is_absolute()

    def test_paths_sorted_lexicographically(self, tmp_path):
        for name in ["z.md", "a.md", "m.md"]:
            _write(tmp_path / name)
        spec = resolve_changed_targets(frozenset(["z.md", "a.md", "m.md"]), tmp_path)
        import os

        assert spec.paths == sorted(spec.paths, key=os.fspath)


# ---------------------------------------------------------------------------
# CLI integration: --target-changed
# ---------------------------------------------------------------------------

# A trivial rules document for filesystem integration tests.
_RULES_MD = """# Test rules

## Checks

- Every file must be readable.
"""


def _make_rules(tmp_path: Path) -> Path:
    rules = tmp_path / "rules.md"
    rules.write_text(_RULES_MD, encoding="utf-8")
    return rules


def _make_git_repo(tmp_path: Path) -> Path:
    """Initialise a minimal git repo in tmp_path with one initial commit."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    for cmd in [
        ["git", "-C", str(tmp_path), "config", "user.email", "t@example.com"],
        ["git", "-C", str(tmp_path), "config", "user.name", "T"],
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    return tmp_path


class TestCliTargetChanged:
    def test_no_target_no_target_changed_is_usage_error(self, tmp_path, capsys):
        """AC8: --target is now optional when --target-changed is given; omitting both is an error."""
        rules = _make_rules(tmp_path)
        rc = main(["validate", str(rules), "--backend", "filesystem"])
        assert rc == EXIT_USAGE

    def test_target_changed_requires_git_repo(self, tmp_path, capsys):
        """AC6: non-git directory → usage error, no traceback."""
        rules = _make_rules(tmp_path)
        # Patch find_repo_root to simulate a non-git working directory.
        with patch(
            "gate_keeper.changed.find_repo_root",
            side_effect=ChangedFilesError("not a git repository"),
        ):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "HEAD~1",
                    "--backend",
                    "filesystem",
                ]
            )
        assert rc == EXIT_USAGE
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "traceback" not in captured.err.lower()

    def test_bad_base_ref_is_usage_error(self, tmp_path, capsys):
        """AC6: bad --base-ref → structured usage error."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        # Patch find_repo_root to return the tmp repo, and compute_changed_files
        # to simulate a bad base ref error (as git diff would produce).
        with (
            patch("gate_keeper.changed.find_repo_root", return_value=repo),
            patch(
                "gate_keeper.changed.compute_changed_files",
                side_effect=ChangedFilesError("git diff exited 128: bad ref"),
            ),
        ):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "this-ref-absolutely-does-not-exist-xyzzy",
                    "--backend",
                    "filesystem",
                ]
            )
        assert rc == EXIT_USAGE
        err = capsys.readouterr().err
        assert "error:" in err

    def test_empty_changed_set_emits_evidence_and_fails(self, tmp_path, capsys):
        """AC3: empty changed set → changed_set_empty evidence, exit 1."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        # HEAD...HEAD is always an empty diff.
        with patch("gate_keeper.changed.find_repo_root", return_value=repo):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "HEAD",
                    "--backend",
                    "filesystem",
                ]
            )
        assert rc == EXIT_FAIL
        out = capsys.readouterr().out
        assert "changed_set_empty" in out

    def test_empty_changed_set_json_output(self, tmp_path, capsys):
        """AC3: empty changed set with --format json emits valid JSON with changed_set_empty."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        with patch("gate_keeper.changed.find_repo_root", return_value=repo):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "HEAD",
                    "--format",
                    "json",
                ]
            )
        assert rc == EXIT_FAIL
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data["diagnostics"]) == 1
        diag = data["diagnostics"][0]
        assert diag["rule_id"] == "changed_set_empty"
        assert diag["status"] == "fail"
        ev = diag["evidence"][0]
        assert ev["kind"] == "changed_set_empty"
        assert ev["data"]["base_ref"] == "HEAD"
        assert ev["data"]["resolved_count"] == 0

    def test_changed_file_is_audited(self, tmp_path, capsys):
        """AC1: a text file in the changed set is included in the run."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        (repo / "new_doc.md").write_text("# New\n", encoding="utf-8")

        with (
            patch("gate_keeper.changed.find_repo_root", return_value=repo),
            patch(
                "gate_keeper.changed.compute_changed_files",
                return_value=frozenset(["new_doc.md"]),
            ),
        ):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "HEAD",
                    "--backend",
                    "filesystem",
                ]
            )
        # Should not be a usage error; the file was found and evaluated.
        assert rc != EXIT_USAGE

    def test_intersection_with_explicit_target(self, tmp_path, capsys):
        """AC2: --target-changed + --target produces the intersection."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        a = _write(repo / "a.md")
        _write(repo / "b.txt")

        # changed set = {a.md, b.txt}; explicit --target = 'a.md'
        # intersection = {a.md}
        with (
            patch("gate_keeper.changed.find_repo_root", return_value=repo),
            patch(
                "gate_keeper.changed.compute_changed_files",
                return_value=frozenset(["a.md", "b.txt"]),
            ),
        ):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "HEAD",
                    "--target",
                    str(a),
                    "--backend",
                    "filesystem",
                    "--format",
                    "json",
                ]
            )
        assert rc != EXIT_USAGE
        out = capsys.readouterr().out
        data = json.loads(out)
        # All diagnostics should be for a.md only, not b.txt.
        sources = {d["source"]["path"] for d in data["diagnostics"]}
        assert not any("b.txt" in s for s in sources)

    def test_subdirectory_invocation(self, tmp_path, capsys):
        """AC4: invoking from a subdirectory still resolves paths correctly (R4)."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        sub = repo / "docs"
        sub.mkdir()
        _write(sub / "readme.md")

        import os

        original_cwd = Path.cwd()
        os.chdir(sub)
        try:
            # The changed set uses repo-root-relative POSIX paths; find_repo_root
            # returns the repo even when invoked from a subdirectory.
            with (
                patch("gate_keeper.changed.find_repo_root", return_value=repo),
                patch(
                    "gate_keeper.changed.compute_changed_files",
                    return_value=frozenset(["docs/readme.md"]),
                ),
            ):
                rc = main(
                    [
                        "validate",
                        str(rules),
                        "--target-changed",
                        "--base-ref",
                        "HEAD",
                        "--backend",
                        "filesystem",
                    ]
                )
        finally:
            os.chdir(original_cwd)

        assert rc != EXIT_USAGE

    def test_cap_exceeded_is_usage_error(self, tmp_path, capsys):
        """AC5: 200-file cap on the changed set → usage error."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        for i in range(5):
            _write(repo / f"doc_{i}.md")

        big_set = frozenset(f"doc_{i}.md" for i in range(5))
        with (
            patch("gate_keeper.changed.find_repo_root", return_value=repo),
            patch("gate_keeper.changed.compute_changed_files", return_value=big_set),
            patch("gate_keeper.targets.DEFAULT_FILE_LIMIT", 3),
        ):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--base-ref",
                    "HEAD",
                    "--backend",
                    "filesystem",
                ]
            )
        assert rc == EXIT_USAGE

    def test_base_ref_env_fallback(self, tmp_path, monkeypatch, capsys):
        """AC1: $GATE_KEEPER_BASE_REF is picked up when --base-ref is omitted."""
        repo = _make_git_repo(tmp_path)
        rules = _make_rules(repo)
        monkeypatch.setenv("GATE_KEEPER_BASE_REF", "HEAD")

        with patch("gate_keeper.changed.find_repo_root", return_value=repo):
            rc = main(
                [
                    "validate",
                    str(rules),
                    "--target-changed",
                    "--backend",
                    "filesystem",
                ]
            )
        # HEAD...HEAD → empty diff → exit 1 with changed_set_empty
        assert rc == EXIT_FAIL
        out = capsys.readouterr().out
        assert "changed_set_empty" in out

    def test_existing_target_only_invocation_unchanged(self, capsys):
        """AC8: behaviour when --target-changed is omitted is unchanged."""
        fixtures = Path(__file__).parent / "fixtures"
        pass_readme = fixtures / "local" / "pass" / "README.md"
        rc = main(
            [
                "validate",
                str(SIMPLE_RULES),
                "--target",
                str(pass_readme),
                "--backend",
                "filesystem",
            ]
        )
        assert rc == EXIT_OK
