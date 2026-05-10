"""Stamp parsing for Mode B (target-stamped freshness) dependency gates.

A stamped target records the source revision it tracks via a YAML frontmatter
``tracks:`` field, e.g.::

    ---
    tracks: cli-implementation@sha256:abc123...
    ---
    # CLI reference

    ...

Slice 1 (issue #181): whole-file SHA-256 of the source artifact bytes; one
source per target; frontmatter required (no inline tracking line outside
frontmatter). See docs/design/dependency-gates.md §3.3 (Stamp syntax).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Canonical stamp syntax: ``<source-node-id>@sha256:<64 lowercase hex chars>``.
# The node id allows the conventional manifest character set (alnum, dash,
# underscore, slash, colon, dot). Whitespace is rejected.
_STAMP_RE = re.compile(r"^(?P<source_id>\S+)@sha256:(?P<digest>[0-9a-f]{64})$")

# Frontmatter must open with ``---`` on the very first line and close with
# ``---`` on its own line. An empty frontmatter (``---\n---\n``) is valid and
# parses to ``{}``.
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(.*?)^---\r?\n",
    re.DOTALL | re.MULTILINE,
)


class StampError(ValueError):
    """Raised when a target's frontmatter cannot be parsed or the stamp is malformed."""


@dataclass(frozen=True)
class Stamp:
    """Parsed ``tracks:`` value pointing at a specific source digest."""

    source_id: str
    algorithm: str  # always "sha256" in slice 1
    digest: str

    def matches(self, *, source_id: str, digest: str) -> bool:
        """Return True iff stamp source_id and digest both match the supplied values."""
        return self.source_id == source_id and self.digest == digest


def extract_frontmatter(text: str) -> dict[str, Any] | None:
    """Return the parsed YAML frontmatter of *text*, or ``None`` if absent.

    A frontmatter block must open with ``---`` on the very first line and
    close with ``---`` on its own line. Inline tracking lines outside
    frontmatter are deliberately ignored in slice 1.

    Raises ``StampError`` if frontmatter is present but cannot be parsed as a
    YAML mapping.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    body = match.group(1)
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise StampError(f"frontmatter YAML parse error: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise StampError(f"frontmatter must be a mapping, got {type(data).__name__}")
    return data


def parse_stamp(value: Any) -> Stamp:
    """Parse a ``tracks:`` value into a :class:`Stamp`.

    Slice 1 accepts only the form ``<source-id>@sha256:<64 hex chars>``.
    Lists, mappings, and other algorithms are rejected with ``StampError``.
    """
    if not isinstance(value, str):
        raise StampError(
            f"'tracks' must be a string of the form '<source-id>@sha256:<digest>', got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        raise StampError("'tracks' value is empty")
    match = _STAMP_RE.match(stripped)
    if match is None:
        raise StampError(
            f"'tracks' value {value!r} is not in canonical form '<source-id>@sha256:<64-hex-digest>'"
        )
    return Stamp(
        source_id=match.group("source_id"),
        algorithm="sha256",
        digest=match.group("digest"),
    )


def read_target_stamp(path: Path) -> Stamp | None:
    """Read *path* and return its parsed stamp, or ``None`` when absent.

    Returns ``None`` only when no frontmatter exists or frontmatter has no
    ``tracks`` key. All other failure modes raise ``StampError``, including:

    - ``OSError`` (file missing, permission denied, etc.) — surfaced as
      ``cannot read target ...``.
    - ``UnicodeDecodeError`` (target contains non-UTF-8 / binary bytes) —
      surfaced as ``cannot decode target ...``. Frontmatter is defined over
      UTF-8 text, so decode failure means the target cannot carry a stamp;
      the validator maps this to ``target_stamp_malformed`` rather than
      crashing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StampError(f"cannot read target {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise StampError(f"cannot decode target {path} as UTF-8: {exc}") from exc
    frontmatter = extract_frontmatter(text)
    if frontmatter is None:
        return None
    if "tracks" not in frontmatter:
        return None
    return parse_stamp(frontmatter["tracks"])
