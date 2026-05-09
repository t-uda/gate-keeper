"""Unit tests for ``scripts/dependency_gates/manifest`` (umbrella #159, slice 1).

Pin schema validation, ``pairs:`` desugaring, and ``mode: stamped`` parsing.
The loader is consumed by validators that run through the ``command`` adapter,
so its error surface needs to be deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from dependency_gates.manifest import (  # noqa: E402
    Edge,
    Manifest,
    ManifestError,
    Node,
    load_manifest,
    parse_manifest,
)


def test_minimal_graph_parses():
    text = dedent(
        """
        nodes:
          - id: a
            path: a.txt
          - id: b
            path: b.txt
        edges:
          - from: a
            to: b
            relation: documents
        """
    )
    manifest = parse_manifest(text)
    assert isinstance(manifest, Manifest)
    assert len(manifest.nodes) == 2
    assert {n.id for n in manifest.nodes} == {"a", "b"}
    assert len(manifest.edges) == 1
    edge = manifest.edges[0]
    assert (edge.from_id, edge.to_id, edge.relation) == ("a", "b", "documents")
    assert edge.mode == "affected_set"  # default
    assert edge.pair_id is None


def test_node_anchor_and_kind_round_trip():
    text = dedent(
        """
        nodes:
          - id: a
            path: a.tex
            anchor: "thm:stability"
            kind: paper
        """
    )
    manifest = parse_manifest(text)
    node = manifest.node("a")
    assert node.anchor == "thm:stability"
    assert node.kind == "paper"


def test_pairs_sugar_desugars_to_two_directed_edges():
    text = dedent(
        """
        nodes:
          - id: ja
            path: docs/ja/foo.md
          - id: en
            path: docs/en/foo.md
        pairs:
          - id: docs-foo-ja-en
            relation: translation
            source: ja
            target: en
        """
    )
    manifest = parse_manifest(text)
    assert len(manifest.edges) == 2
    pair_ids = {e.pair_id for e in manifest.edges}
    assert pair_ids == {"docs-foo-ja-en"}
    directions = {(e.from_id, e.to_id) for e in manifest.edges}
    assert directions == {("ja", "en"), ("en", "ja")}
    relations = {e.relation for e in manifest.edges}
    assert relations == {"translation"}


def test_mode_stamped_parses():
    text = dedent(
        """
        nodes:
          - id: src
            path: paper.tex
          - id: tgt
            path: lean/Stability.lean
        edges:
          - from: src
            to: tgt
            relation: formalization
            mode: stamped
        """
    )
    manifest = parse_manifest(text)
    assert manifest.edges[0].mode == "stamped"


def test_invalid_mode_rejected():
    text = dedent(
        """
        nodes:
          - id: a
            path: a.txt
          - id: b
            path: b.txt
        edges:
          - from: a
            to: b
            relation: r
            mode: unknown_mode
        """
    )
    with pytest.raises(ManifestError, match="mode"):
        parse_manifest(text)


def test_duplicate_node_id_rejected():
    text = dedent(
        """
        nodes:
          - id: a
            path: one.txt
          - id: a
            path: two.txt
        """
    )
    with pytest.raises(ManifestError, match="duplicate node id"):
        parse_manifest(text)


def test_edge_references_unknown_node_rejected():
    text = dedent(
        """
        nodes:
          - id: a
            path: a.txt
        edges:
          - from: a
            to: ghost
            relation: r
        """
    )
    with pytest.raises(ManifestError, match="unknown node id 'ghost'"):
        parse_manifest(text)


def test_pairs_references_unknown_node_rejected():
    text = dedent(
        """
        nodes:
          - id: a
            path: a.txt
        pairs:
          - id: p
            relation: r
            source: a
            target: ghost
        """
    )
    with pytest.raises(ManifestError, match="unknown node id 'ghost'"):
        parse_manifest(text)


def test_missing_required_node_field_rejected():
    text = dedent(
        """
        nodes:
          - id: a
        """
    )
    with pytest.raises(ManifestError, match="missing required fields"):
        parse_manifest(text)


def test_unknown_top_level_field_rejected():
    text = dedent(
        """
        nodes: []
        edges: []
        bogus: 1
        """
    )
    with pytest.raises(ManifestError, match="unknown fields"):
        parse_manifest(text)


def test_empty_manifest_is_valid():
    manifest = parse_manifest("")
    assert manifest.nodes == ()
    assert manifest.edges == ()


def test_pairs_and_explicit_edges_coexist():
    text = dedent(
        """
        nodes:
          - id: a
            path: a.txt
          - id: b
            path: b.txt
          - id: c
            path: c.txt
        edges:
          - from: a
            to: c
            relation: documents
        pairs:
          - id: ab-pair
            relation: translation
            source: a
            target: b
        """
    )
    manifest = parse_manifest(text)
    # 1 explicit + 2 desugared from the pair.
    assert len(manifest.edges) == 3
    explicit_edges = [e for e in manifest.edges if e.pair_id is None]
    pair_edges = [e for e in manifest.edges if e.pair_id == "ab-pair"]
    assert len(explicit_edges) == 1
    assert len(pair_edges) == 2


def test_load_manifest_reads_file(tmp_path: Path):
    p = tmp_path / "manifest.yml"
    p.write_text(
        dedent(
            """
            nodes:
              - id: a
                path: a.txt
            """
        ).lstrip(),
        encoding="utf-8",
    )
    manifest = load_manifest(p)
    assert manifest.source_path == p
    assert manifest.nodes_by_id["a"] == Node(id="a", path="a.txt")


def test_load_manifest_missing_file(tmp_path: Path):
    missing = tmp_path / "nope.yml"
    with pytest.raises(ManifestError, match="cannot read"):
        load_manifest(missing)


def test_yaml_parse_error_surfaced():
    with pytest.raises(ManifestError, match="YAML parse error"):
        parse_manifest("nodes: [\n  - id: a\n   broken")


def test_edge_dataclass_is_frozen():
    edge = Edge(from_id="a", to_id="b", relation="r")
    with pytest.raises(Exception):  # FrozenInstanceError subclasses TypeError
        edge.relation = "x"  # type: ignore[misc]
