"""Tests for the semantic-rubric benchmark fixture loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from semantic_fixtures import (
    ENTRIES_DIR,
    TARGETS_DIR,
    Category,
    FixtureEntry,
    IntendedBackend,
    Judgment,
    Target,
    TargetKind,
    iter_entries,
    load_all,
    load_entry,
    parse_entry,
)


def _valid_entry_dict() -> dict[str, Any]:
    return {
        "rule_text": "The README quickstart contains an install step.",
        "target": {"kind": "inline", "value": "Install with `uv sync`.\n"},
        "expected_judgment": "pass",
        "expected_rationale_keywords": ["install", "uv sync"],
        "category": "completeness",
        "intended_backend": "llm-rubric",
        "notes": "trivial smoke entry",
    }


def test_parse_entry_valid(tmp_path: Path):
    data = _valid_entry_dict()
    entry = parse_entry(data, source_path=tmp_path / "smoke.json")
    assert entry == FixtureEntry(
        id="smoke",
        rule_text=data["rule_text"],
        target=Target(kind=TargetKind.INLINE, value="Install with `uv sync`.\n"),
        expected_judgment=Judgment.PASS,
        expected_rationale_keywords=("install", "uv sync"),
        category=Category.COMPLETENESS,
        intended_backend=IntendedBackend.LLM_RUBRIC,
        notes="trivial smoke entry",
        source_path=tmp_path / "smoke.json",
    )


def test_parse_entry_optional_notes_omitted(tmp_path: Path):
    data = _valid_entry_dict()
    del data["notes"]
    entry = parse_entry(data, source_path=tmp_path / "no-notes.json")
    assert entry.notes is None


def test_parse_entry_unknown_field_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["extra"] = "nope"
    with pytest.raises(ValueError, match=r"unknown fields: \['extra'\]"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_missing_required_field_fails(tmp_path: Path):
    data = _valid_entry_dict()
    del data["rule_text"]
    with pytest.raises(ValueError, match=r"missing required fields: \['rule_text'\]"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_invalid_judgment_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["expected_judgment"] = "maybe"
    with pytest.raises(ValueError, match=r"expected_judgment: 'maybe' not in"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_invalid_category_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["category"] = "vibes"
    with pytest.raises(ValueError, match=r"category: 'vibes' not in"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_invalid_intended_backend_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["intended_backend"] = "telepathy"
    with pytest.raises(ValueError, match=r"intended_backend: 'telepathy' not in"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_invalid_target_kind_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["target"] = {"kind": "telepath", "value": "x"}
    with pytest.raises(ValueError, match=r"target.kind: 'telepath' not in"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_target_missing_value_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["target"] = {"kind": "inline"}
    with pytest.raises(ValueError, match=r"target: missing fields: \['value'\]"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_keywords_must_be_strings(tmp_path: Path):
    data = _valid_entry_dict()
    data["expected_rationale_keywords"] = ["ok", 42]
    with pytest.raises(ValueError, match=r"expected_rationale_keywords\[1\]: expected str"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_parse_entry_notes_wrong_type_fails(tmp_path: Path):
    data = _valid_entry_dict()
    data["notes"] = 7
    with pytest.raises(ValueError, match=r"notes: expected str or absent"):
        parse_entry(data, source_path=tmp_path / "bad.json")


def test_load_entry_round_trip(tmp_path: Path):
    path = tmp_path / "round-trip.json"
    path.write_text(json.dumps(_valid_entry_dict()), encoding="utf-8")
    entry = load_entry(path)
    assert entry.id == "round-trip"
    assert entry.target.kind is TargetKind.INLINE


def test_target_resolve_inline(tmp_path: Path):
    target = Target(kind=TargetKind.INLINE, value="hello\n")
    assert target.resolve(tmp_path) == "hello\n"


def test_target_resolve_path(tmp_path: Path):
    f = tmp_path / "snippet.md"
    f.write_text("# heading\n", encoding="utf-8")
    target = Target(kind=TargetKind.PATH, value="snippet.md")
    assert target.resolve(tmp_path) == "# heading\n"


def test_target_resolve_path_missing_raises(tmp_path: Path):
    target = Target(kind=TargetKind.PATH, value="nope.md")
    with pytest.raises(FileNotFoundError):
        target.resolve(tmp_path)


# --- bundled fixture set assertions -----------------------------------------


def test_bundled_entries_load_cleanly():
    entries = load_all()
    assert len(entries) >= 20, "issue #65 requires at least 20 fixture entries"


def test_bundled_entries_cover_at_least_three_categories():
    categories = {entry.category for entry in iter_entries()}
    assert len(categories) >= 3, f"expected ≥3 categories, got {sorted(c.value for c in categories)}"


def test_bundled_entries_have_unique_ids():
    ids = [entry.id for entry in iter_entries()]
    assert len(ids) == len(set(ids)), "fixture entry ids (filename stems) must be unique"


def test_bundled_entries_path_targets_resolve():
    for entry in iter_entries():
        if entry.target.kind is TargetKind.PATH:
            text = entry.target.resolve(TARGETS_DIR)
            assert text, f"target file for {entry.id} resolved to empty content"


def test_bundled_entries_initial_intended_backend_is_llm_rubric():
    """Per issue #65: initial landing keeps every entry on llm-rubric."""
    for entry in iter_entries():
        assert entry.intended_backend is IntendedBackend.LLM_RUBRIC, (
            f"entry {entry.id}: initial fixture set must use intended_backend=llm-rubric "
            f"(got {entry.intended_backend.value}); other backends arrive with #94"
        )


def test_bundled_entries_have_keywords():
    for entry in iter_entries():
        assert entry.expected_rationale_keywords, (
            f"entry {entry.id}: expected_rationale_keywords must be non-empty"
        )


def test_entries_directory_only_contains_json():
    stray = [p.name for p in ENTRIES_DIR.iterdir() if p.suffix != ".json" and p.is_file()]
    assert not stray, f"non-JSON files in entries/: {stray}"
