"""Unit tests for ``scripts/dependency_gates/_stamp`` (issue #181).

Covers stamp parsing in isolation from the validator dispatch, mirroring the
``test_dependency_manifest`` style for ``manifest.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from dependency_gates._stamp import (  # noqa: E402
    Stamp,
    StampError,
    extract_frontmatter,
    parse_stamp,
    read_target_stamp,
)

GOOD_DIGEST = "a" * 64


class TestParseStamp:
    def test_canonical_form_parses(self):
        stamp = parse_stamp(f"src@sha256:{GOOD_DIGEST}")
        assert stamp == Stamp(source_id="src", algorithm="sha256", digest=GOOD_DIGEST)

    def test_node_id_with_dash_underscore_dot(self):
        stamp = parse_stamp(f"my.node_id-1@sha256:{GOOD_DIGEST}")
        assert stamp.source_id == "my.node_id-1"

    def test_leading_trailing_whitespace_stripped(self):
        stamp = parse_stamp(f"  src@sha256:{GOOD_DIGEST}  ")
        assert stamp.source_id == "src"

    def test_empty_string_rejected(self):
        with pytest.raises(StampError, match="empty"):
            parse_stamp("")

    def test_non_string_rejected(self):
        with pytest.raises(StampError, match="must be a string"):
            parse_stamp(["src", "sha256", GOOD_DIGEST])

    def test_missing_algorithm_rejected(self):
        with pytest.raises(StampError, match="canonical form"):
            parse_stamp(f"src:{GOOD_DIGEST}")

    def test_uppercase_digest_rejected(self):
        # Stamps are case-sensitive on the digest to match git's lowercase
        # convention; explicit failure is better than silent normalisation.
        with pytest.raises(StampError, match="canonical form"):
            parse_stamp("src@sha256:" + ("A" * 64))

    def test_short_digest_rejected(self):
        with pytest.raises(StampError, match="canonical form"):
            parse_stamp("src@sha256:abcd")

    def test_unsupported_algorithm_rejected(self):
        with pytest.raises(StampError, match="canonical form"):
            parse_stamp(f"src@md5:{'a' * 32}")


class TestExtractFrontmatter:
    def test_basic_frontmatter(self):
        text = "---\ntitle: Hello\n---\nbody\n"
        assert extract_frontmatter(text) == {"title": "Hello"}

    def test_no_frontmatter(self):
        assert extract_frontmatter("just body\n") is None

    def test_empty_frontmatter(self):
        assert extract_frontmatter("---\n---\nbody\n") == {}

    def test_crlf_line_endings(self):
        text = "---\r\ntitle: x\r\n---\r\nbody\r\n"
        assert extract_frontmatter(text) == {"title": "x"}

    def test_non_mapping_rejected(self):
        with pytest.raises(StampError, match="must be a mapping"):
            extract_frontmatter("---\n- a\n- b\n---\nbody\n")

    def test_yaml_parse_error_rejected(self):
        with pytest.raises(StampError, match="parse error"):
            extract_frontmatter("---\n: : :\n  - bad\n---\nbody\n")

    def test_frontmatter_must_be_at_start(self):
        # A doc that has --- markers later in the file but not at start has
        # no frontmatter.
        text = "intro paragraph\n---\ntitle: x\n---\nbody\n"
        assert extract_frontmatter(text) is None


class TestReadTargetStamp:
    def test_no_frontmatter_returns_none(self, tmp_path: Path):
        p = tmp_path / "doc.md"
        p.write_text("just text\n", encoding="utf-8")
        assert read_target_stamp(p) is None

    def test_no_tracks_key_returns_none(self, tmp_path: Path):
        p = tmp_path / "doc.md"
        p.write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")
        assert read_target_stamp(p) is None

    def test_valid_stamp(self, tmp_path: Path):
        p = tmp_path / "doc.md"
        p.write_text(
            f"---\ntracks: src@sha256:{GOOD_DIGEST}\n---\nbody\n",
            encoding="utf-8",
        )
        stamp = read_target_stamp(p)
        assert stamp is not None
        assert stamp.source_id == "src"
        assert stamp.digest == GOOD_DIGEST

    def test_malformed_stamp_raises(self, tmp_path: Path):
        p = tmp_path / "doc.md"
        p.write_text(
            "---\ntracks: not-a-stamp\n---\nbody\n",
            encoding="utf-8",
        )
        with pytest.raises(StampError):
            read_target_stamp(p)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(StampError, match="cannot read"):
            read_target_stamp(tmp_path / "no-such.md")
