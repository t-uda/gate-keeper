"""Tests for ``gate_keeper.targets``: ``TargetSpec`` and ``resolve_targets``.

Covers issue #146 — first slice of the multi-target design
(``docs/design/multi-target.md``). Scope is filesystem-only: literal files,
directories, repeated files, globs, the empty-glob fail-closed case, and the
file-count cap.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gate_keeper.targets import (
    DEFAULT_FILE_LIMIT,
    TargetExpansionError,
    TargetSpec,
    looks_like_glob,
    resolve_targets,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(p: Path, content: str = "x\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Single-target paths preserve current behaviour
# ---------------------------------------------------------------------------


class TestSingleTarget:
    def test_single_literal_file_is_not_multi(self, tmp_path):
        f = _write(tmp_path / "a.txt")
        spec = resolve_targets([str(f)])
        assert spec.is_multi is False
        assert spec.paths == [f]

    def test_single_missing_path_passed_through(self, tmp_path):
        # Non-existent literal: backend will surface UNAVAILABLE — but
        # resolve_targets must not silently drop it.
        ghost = tmp_path / "ghost.txt"
        spec = resolve_targets([str(ghost)])
        assert spec.is_multi is False
        assert spec.paths == [ghost]

    def test_empty_arglist_raises(self):
        with pytest.raises(ValueError, match="at least one target"):
            resolve_targets([])


# ---------------------------------------------------------------------------
# Repeated --target flags
# ---------------------------------------------------------------------------


class TestRepeatedFlags:
    def test_two_files_combined_and_sorted(self, tmp_path):
        a = _write(tmp_path / "a.txt")
        b = _write(tmp_path / "b.txt")
        spec = resolve_targets([str(b), str(a)])
        assert spec.is_multi is True
        assert spec.paths == sorted([a, b], key=os.fspath)

    def test_duplicates_deduplicated(self, tmp_path):
        a = _write(tmp_path / "a.txt")
        spec = resolve_targets([str(a), str(a), str(a)])
        # Duplicates collapse but is_multi stays True (caller passed >1 arg).
        assert spec.is_multi is True
        assert spec.paths == [a]

    def test_raw_targets_preserved_in_input_order(self, tmp_path):
        a = _write(tmp_path / "a.txt")
        b = _write(tmp_path / "b.txt")
        spec = resolve_targets([str(b), str(a)])
        assert spec.raw_targets == [str(b), str(a)]


# ---------------------------------------------------------------------------
# Directory expansion
# ---------------------------------------------------------------------------


class TestDirectoryExpansion:
    def test_directory_expands_text_files(self, tmp_path):
        _write(tmp_path / "a.txt")
        _write(tmp_path / "sub" / "b.md")
        _write(tmp_path / "sub" / "c.py", "print('hi')\n")
        spec = resolve_targets([str(tmp_path)])
        assert spec.is_multi is True
        assert {p.name for p in spec.paths} == {"a.txt", "b.md", "c.py"}

    def test_directory_skips_binary_files(self, tmp_path):
        _write(tmp_path / "a.txt")
        binary = tmp_path / "blob.bin"
        binary.write_bytes(b"\x00\xff binary data \xfe\x80")
        spec = resolve_targets([str(tmp_path)])
        names = {p.name for p in spec.paths}
        assert "a.txt" in names
        assert "blob.bin" not in names

    def test_empty_directory_resolves_to_empty(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        spec = resolve_targets([str(empty)])
        assert spec.is_multi is True
        assert spec.paths == []

    def test_directory_walk_is_deterministic(self, tmp_path):
        _write(tmp_path / "z.txt")
        _write(tmp_path / "a.txt")
        _write(tmp_path / "m" / "b.txt")
        spec = resolve_targets([str(tmp_path)])
        # Sort key is os.fspath, so output must equal the lexicographic sort.
        assert spec.paths == sorted(spec.paths, key=os.fspath)


# ---------------------------------------------------------------------------
# Glob expansion
# ---------------------------------------------------------------------------


class TestGlobs:
    def test_simple_glob_expands(self, tmp_path):
        _write(tmp_path / "a.txt")
        _write(tmp_path / "b.txt")
        _write(tmp_path / "c.md")
        spec = resolve_targets([str(tmp_path / "*.txt")])
        assert spec.is_multi is True
        assert {p.name for p in spec.paths} == {"a.txt", "b.txt"}

    def test_recursive_double_star_glob(self, tmp_path):
        _write(tmp_path / "a.md")
        _write(tmp_path / "sub" / "b.md")
        _write(tmp_path / "sub" / "deep" / "c.md")
        spec = resolve_targets([str(tmp_path / "**" / "*.md")])
        assert spec.is_multi is True
        assert {p.name for p in spec.paths} == {"a.md", "b.md", "c.md"}

    def test_empty_glob_resolves_to_empty(self, tmp_path):
        # Glob that matches nothing: spec is multi but paths is empty.
        # Backend translates this to UNAVAILABLE (fail-closed) — see
        # test_filesystem_backend.TestMultiTarget.
        spec = resolve_targets([str(tmp_path / "no_match_*.zzz")])
        assert spec.is_multi is True
        assert spec.paths == []

    def test_glob_is_deterministic_across_calls(self, tmp_path):
        for n in ("z", "a", "m", "b"):
            _write(tmp_path / f"{n}.txt")
        s1 = resolve_targets([str(tmp_path / "*.txt")])
        s2 = resolve_targets([str(tmp_path / "*.txt")])
        assert s1.paths == s2.paths

    def test_single_glob_token_is_multi(self, tmp_path):
        # Even when the glob expands to one file, the spec is multi because
        # the caller asked for set-style semantics.
        _write(tmp_path / "only.txt")
        spec = resolve_targets([str(tmp_path / "*.txt")])
        assert spec.is_multi is True


# ---------------------------------------------------------------------------
# File-count cap
# ---------------------------------------------------------------------------


class TestFileCountCap:
    def test_default_limit_is_200(self):
        assert DEFAULT_FILE_LIMIT == 200

    def test_over_cap_raises(self, tmp_path):
        # Create more files than the limit allows.
        cap = 5
        for i in range(cap + 2):
            _write(tmp_path / f"f{i:03d}.txt")
        with pytest.raises(TargetExpansionError, match="exceeds limit of 5"):
            resolve_targets([str(tmp_path)], file_limit=cap)

    def test_exact_cap_succeeds(self, tmp_path):
        cap = 3
        for i in range(cap):
            _write(tmp_path / f"f{i:03d}.txt")
        spec = resolve_targets([str(tmp_path)], file_limit=cap)
        assert len(spec.paths) == cap


# ---------------------------------------------------------------------------
# Glob detection helper
# ---------------------------------------------------------------------------


class TestLooksLikeGlob:
    @pytest.mark.parametrize("token", ["*.md", "src/**/*.py", "x?.txt", "[ab].md"])
    def test_glob_metacharacters_detected(self, token):
        assert looks_like_glob(token) is True

    @pytest.mark.parametrize("token", ["README.md", "src/file.py", "owner/repo#1"])
    def test_plain_strings_not_globs(self, token):
        assert looks_like_glob(token) is False


# ---------------------------------------------------------------------------
# TargetSpec invariants
# ---------------------------------------------------------------------------


class TestTargetSpecInvariants:
    def test_paths_must_be_sorted(self, tmp_path):
        # Direct construction with unsorted paths should raise.
        a = _write(tmp_path / "a.txt")
        b = _write(tmp_path / "b.txt")
        with pytest.raises(ValueError, match="sorted lexicographically"):
            TargetSpec(paths=[b, a], raw_targets=[], is_multi=True)
