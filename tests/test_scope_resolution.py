"""Unit tests for ``gate_keeper.targets.resolve_rule_scope`` (issue #279 — S3).

Covers the per-rule target-scope contract ratified in
``docs/design/multi-target.md`` §9: grammar validation, scope ∩ candidate
intersection, the four outcomes (dispatch / empty / invalid / file-limit), the
shared-expansion memo, and repo-relative normalization.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import gate_keeper.targets as targets_mod
from gate_keeper.targets import resolve_rule_scope


def _write(p: Path, content: str = "x\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _tree(root: Path) -> dict[str, Path]:
    """Create a small repo tree and return the created files by rel path."""
    return {
        "docs/a.md": _write(root / "docs" / "a.md"),
        "docs/sub/b.md": _write(root / "docs" / "sub" / "b.md"),
        "src/x.py": _write(root / "src" / "x.py"),
        "src/pkg/y.py": _write(root / "src" / "pkg" / "y.py"),
        "README.md": _write(root / "README.md"),
    }


# ---------------------------------------------------------------------------
# Dispatch — valid scope with a non-empty intersection
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_scope_intersects_candidate_subset(self, tmp_path):
        files = _tree(tmp_path)
        candidate = list(files.values())
        res = resolve_rule_scope(["docs/**/*.md"], candidate, tmp_path)
        assert res.status == "dispatch"
        assert res.effective_relpaths == ["docs/a.md", "docs/sub/b.md"]
        assert res.spec is not None
        assert res.spec.is_multi is True
        assert {p.name for p in res.spec.paths} == {"a.md", "b.md"}
        # scope_size counts the repo-wide expansion, candidate_size the pool.
        assert res.scope_size == 2
        assert res.candidate_size == len(candidate)
        assert res.effective_size == 2

    def test_single_file_effective_set_is_not_multi(self, tmp_path):
        files = _tree(tmp_path)
        res = resolve_rule_scope(["README.md"], list(files.values()), tmp_path)
        assert res.status == "dispatch"
        assert res.effective_relpaths == ["README.md"]
        assert res.spec is not None
        assert res.spec.is_multi is False

    def test_multiple_globs_union(self, tmp_path):
        files = _tree(tmp_path)
        res = resolve_rule_scope(["docs/**/*.md", "src/**/*.py"], list(files.values()), tmp_path)
        assert res.status == "dispatch"
        assert res.effective_relpaths == [
            "docs/a.md",
            "docs/sub/b.md",
            "src/pkg/y.py",
            "src/x.py",
        ]

    def test_effective_set_is_intersection_not_whole_scope(self, tmp_path):
        files = _tree(tmp_path)
        # Candidate pool is only the two docs files; a src scope must not pull in
        # files outside the candidate set.
        candidate = [files["docs/a.md"], files["docs/sub/b.md"]]
        res = resolve_rule_scope(["src/**/*.py", "docs/**/*.md"], candidate, tmp_path)
        assert res.status == "dispatch"
        assert res.effective_relpaths == ["docs/a.md", "docs/sub/b.md"]
        # scope expands repo-wide (4 files) but effective is the intersection (2).
        assert res.scope_size == 4
        assert res.effective_size == 2

    def test_spec_paths_preserve_candidate_path_objects(self, tmp_path):
        files = _tree(tmp_path)
        candidate = list(files.values())
        res = resolve_rule_scope(["src/x.py"], candidate, tmp_path)
        assert res.spec is not None
        # The dispatched path is the exact object from the candidate pool.
        assert res.spec.paths[0] is files["src/x.py"]


# ---------------------------------------------------------------------------
# scope_empty — valid scope, empty intersection → PASS territory
# ---------------------------------------------------------------------------


class TestScopeEmpty:
    def test_valid_scope_no_intersection(self, tmp_path):
        files = _tree(tmp_path)
        # Candidate pool has no docs; a docs scope intersects to nothing.
        candidate = [files["src/x.py"], files["src/pkg/y.py"]]
        res = resolve_rule_scope(["docs/**/*.md"], candidate, tmp_path)
        assert res.status == "empty"
        assert res.spec is None
        assert res.scope_size == 2  # docs expands repo-wide
        assert res.effective_size == 0

    def test_empty_candidate_pool(self, tmp_path):
        _tree(tmp_path)
        res = resolve_rule_scope(["docs/**/*.md"], [], tmp_path)
        assert res.status == "empty"
        assert res.candidate_size == 0


# ---------------------------------------------------------------------------
# scope_invalid — malformed grammar or zero repo-wide matches
# ---------------------------------------------------------------------------


class TestScopeInvalid:
    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "docs/**/*.md",  # bare string, not a list
            [],  # empty list
            ["docs/**/*.md", 5],  # non-str element
            {"glob": "docs"},  # mapping
        ],
    )
    def test_malformed_grammar(self, tmp_path, bad):
        files = _tree(tmp_path)
        res = resolve_rule_scope(bad, list(files.values()), tmp_path)
        assert res.status == "invalid"
        assert res.spec is None
        assert res.detail

    def test_scope_matches_nothing_repo_wide(self, tmp_path):
        files = _tree(tmp_path)
        res = resolve_rule_scope(["nonexistent/**/*.txt"], list(files.values()), tmp_path)
        assert res.status == "invalid"
        assert "zero files" in (res.detail or "")

    def test_scope_matches_only_binary_is_invalid(self, tmp_path):
        # A scope that resolves only to a binary (non-text) file matches nothing
        # in the text vocabulary → invalid (fail-closed).
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "logo.png").write_bytes(b"\x00\x01\x02")
        res = resolve_rule_scope(["assets/*.png"], [], tmp_path)
        assert res.status == "invalid"


# ---------------------------------------------------------------------------
# scope_file_limit_exceeded — per-rule cap on the effective set
# ---------------------------------------------------------------------------


class TestFileLimit:
    def test_effective_over_cap(self, tmp_path, monkeypatch):
        files = _tree(tmp_path)
        candidate = list(files.values())
        # Force a low per-rule cap; the src scope intersects 2 files > cap 1.
        monkeypatch.setattr(targets_mod, "DEFAULT_FILE_LIMIT", 1)
        res = resolve_rule_scope(["src/**/*.py"], candidate, tmp_path)
        assert res.status == "file_limit_exceeded"
        assert res.spec is None
        assert res.effective_size == 2
        assert res.file_limit == 1

    def test_explicit_file_limit_arg(self, tmp_path):
        files = _tree(tmp_path)
        res = resolve_rule_scope(["src/**/*.py"], list(files.values()), tmp_path, file_limit=1)
        assert res.status == "file_limit_exceeded"
        assert res.file_limit == 1

    def test_at_cap_dispatches(self, tmp_path):
        files = _tree(tmp_path)
        res = resolve_rule_scope(["src/**/*.py"], list(files.values()), tmp_path, file_limit=2)
        assert res.status == "dispatch"


# ---------------------------------------------------------------------------
# Memoization (§9.6) and normalization edges
# ---------------------------------------------------------------------------


class TestMemoAndNormalization:
    def test_memo_reused_across_calls(self, tmp_path):
        files = _tree(tmp_path)
        memo: dict = {}
        candidate = list(files.values())
        resolve_rule_scope(["docs/**/*.md"], candidate, tmp_path, memo=memo)
        # One distinct pattern expanded once, keyed by (pattern, repo_root).
        assert len(memo) == 1
        key = ("docs/**/*.md", str(tmp_path.resolve()))
        assert key in memo
        # A second call with the same pattern reuses the cached frozenset.
        cached = memo[key]
        resolve_rule_scope(["docs/**/*.md"], candidate, tmp_path, memo=memo)
        assert memo[key] is cached

    def test_candidate_outside_repo_dropped(self, tmp_path):
        files = _tree(tmp_path)
        outside = _write(tmp_path.parent / "outside.md")
        candidate = [*files.values(), outside]
        res = resolve_rule_scope(["**/*.md"], candidate, tmp_path)
        assert res.status == "dispatch"
        # outside.md cannot be repo-relative to tmp_path, so it is excluded.
        assert all("outside" not in rel for rel in res.effective_relpaths)

    def test_directory_scope_pattern_walks(self, tmp_path):
        files = _tree(tmp_path)
        # A literal directory scope walks into its text files.
        res = resolve_rule_scope(["docs"], list(files.values()), tmp_path)
        assert res.status == "dispatch"
        assert res.effective_relpaths == ["docs/a.md", "docs/sub/b.md"]
