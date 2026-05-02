"""Loader for `tests/fixtures/semantic/` benchmark entries.

Schema and contribution rules live in `tests/fixtures/semantic/README.md`.
This module is intentionally test-only; it is not exposed from the package.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "semantic"
ENTRIES_DIR = FIXTURES_ROOT / "entries"
TARGETS_DIR = FIXTURES_ROOT / "targets"


class Category(str, enum.Enum):
    CLARITY = "clarity"
    COMPLETENESS = "completeness"
    JUSTIFICATION = "justification"
    NAMING = "naming"
    CONSISTENCY = "consistency"


class IntendedBackend(str, enum.Enum):
    LLM_RUBRIC = "llm-rubric"
    EXTERNAL_TEXTLINT = "external+textlint"
    FILESYSTEM = "filesystem"
    GITHUB = "github"


class Judgment(str, enum.Enum):
    PASS = "pass"
    FAIL = "fail"


class TargetKind(str, enum.Enum):
    PATH = "path"
    INLINE = "inline"


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    value: str

    def resolve(self, root: Path = TARGETS_DIR) -> str:
        """Return the literal text the rule should be evaluated against."""
        if self.kind is TargetKind.INLINE:
            return self.value
        path = root / self.value
        if not path.is_file():
            raise FileNotFoundError(f"target path does not resolve to a file: {path}")
        return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class FixtureEntry:
    id: str
    rule_text: str
    target: Target
    expected_judgment: Judgment
    expected_rationale_keywords: tuple[str, ...]
    category: Category
    intended_backend: IntendedBackend
    notes: str | None
    source_path: Path


_REQUIRED = {
    "rule_text",
    "target",
    "expected_judgment",
    "expected_rationale_keywords",
    "category",
    "intended_backend",
}
_OPTIONAL = {"notes"}
_TARGET_REQUIRED = {"kind", "value"}


def _expect_str(value: Any, ctx: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{ctx}: expected str, got {type(value).__name__}")
    return value


def _expect_dict(value: Any, ctx: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{ctx}: expected mapping, got {type(value).__name__}")
    return value


def _expect_list_of_str(value: Any, ctx: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{ctx}: expected list, got {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        out.append(_expect_str(item, f"{ctx}[{i}]"))
    return out


def _coerce_enum(enum_cls: type[enum.Enum], value: Any, ctx: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = sorted(member.value for member in enum_cls)
        raise ValueError(f"{ctx}: {value!r} not in {valid}") from exc


def _parse_target(data: Any, ctx: str) -> Target:
    obj = _expect_dict(data, ctx)
    keys = set(obj)
    missing = _TARGET_REQUIRED - keys
    if missing:
        raise ValueError(f"{ctx}: missing fields: {sorted(missing)}")
    unknown = keys - _TARGET_REQUIRED
    if unknown:
        raise ValueError(f"{ctx}: unknown fields: {sorted(unknown)}")
    return Target(
        kind=_coerce_enum(TargetKind, obj["kind"], f"{ctx}.kind"),
        value=_expect_str(obj["value"], f"{ctx}.value"),
    )


def parse_entry(data: Any, *, source_path: Path) -> FixtureEntry:
    """Parse a single fixture entry; raise ValueError on any schema violation."""
    obj = _expect_dict(data, f"FixtureEntry({source_path.name})")
    keys = set(obj)
    missing = _REQUIRED - keys
    if missing:
        raise ValueError(f"FixtureEntry({source_path.name}): missing required fields: {sorted(missing)}")
    unknown = keys - _REQUIRED - _OPTIONAL
    if unknown:
        raise ValueError(f"FixtureEntry({source_path.name}): unknown fields: {sorted(unknown)}")
    notes_value = obj.get("notes")
    if notes_value is not None and not isinstance(notes_value, str):
        raise ValueError(
            f"FixtureEntry({source_path.name}).notes: expected str or absent, "
            f"got {type(notes_value).__name__}"
        )
    return FixtureEntry(
        id=source_path.stem,
        rule_text=_expect_str(obj["rule_text"], "rule_text"),
        target=_parse_target(obj["target"], "target"),
        expected_judgment=_coerce_enum(Judgment, obj["expected_judgment"], "expected_judgment"),
        expected_rationale_keywords=tuple(
            _expect_list_of_str(obj["expected_rationale_keywords"], "expected_rationale_keywords")
        ),
        category=_coerce_enum(Category, obj["category"], "category"),
        intended_backend=_coerce_enum(IntendedBackend, obj["intended_backend"], "intended_backend"),
        notes=notes_value,
        source_path=source_path,
    )


def load_entry(path: Path) -> FixtureEntry:
    """Load a single fixture entry from a JSON file."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_entry(data, source_path=path)


def iter_entries(entries_dir: Path = ENTRIES_DIR) -> Iterator[FixtureEntry]:
    """Yield all fixture entries from `entries_dir`, sorted by filename."""
    for path in sorted(entries_dir.glob("*.json")):
        yield load_entry(path)


def load_all(entries_dir: Path = ENTRIES_DIR) -> list[FixtureEntry]:
    return list(iter_entries(entries_dir))


__all__ = [
    "Category",
    "FixtureEntry",
    "IntendedBackend",
    "Judgment",
    "Target",
    "TargetKind",
    "ENTRIES_DIR",
    "FIXTURES_ROOT",
    "TARGETS_DIR",
    "iter_entries",
    "load_all",
    "load_entry",
    "parse_entry",
]
