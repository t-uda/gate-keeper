"""Tests for ``scripts/dependency_gates/check_cli_reference`` (umbrella #159).

Two layers:

- Unit scenarios stub ``compute_changed_files`` and exercise the in-process
  ``_run`` function directly. No git, no subprocess.
- One ``@pytest.mark.integration`` test runs the validator end-to-end through
  the real ``CommandAdapter`` (mirrors ``tests/test_command_adapter.py``
  ``TestRealSubprocess`` style).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from dependency_gates import _changed_files as cf_mod  # noqa: E402
from dependency_gates import check_cli_reference as validator  # noqa: E402

VALIDATOR_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "dependency_gates" / "check_cli_reference.py"
)


def _seed_repo(root: Path, source_text: str = "src body\n") -> tuple[Path, Path]:
    """Materialise the cli/docs pair on disk under *root*."""
    src = root / "src" / "gate_keeper" / "cli.py"
    src.parent.mkdir(parents=True)
    src.write_text(source_text, encoding="utf-8")

    doc = root / "docs" / "cli-reference.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# CLI reference\n", encoding="utf-8")

    manifest_dir = root / ".gate-keeper"
    manifest_dir.mkdir()
    (manifest_dir / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: cli-implementation
                path: src/gate_keeper/cli.py
                kind: code
              - id: cli-reference
                path: docs/cli-reference.md
                kind: reference
            edges:
              - from: cli-implementation
                to: cli-reference
                relation: documents
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return src, doc


def _patch_changed(monkeypatch: pytest.MonkeyPatch, paths: set[str]) -> None:
    monkeypatch.setattr(
        validator,
        "compute_changed_files",
        lambda repo_root, base_ref: frozenset(paths),
    )
    monkeypatch.setattr(
        validator,
        "resolve_base_ref",
        lambda env=None: "origin/main",
    )


def _run(repo_root: Path, target: str) -> validator.Outcome:
    return validator._run(
        repo_root=repo_root,
        manifest_path=repo_root / ".gate-keeper" / "dependency-manifest.yml",
        target=target,
    )


def test_clean_tree_emits_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "pass"
    assert out.evidence_kind == "dependent_artifact_unaffected"


def test_co_change_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    _patch_changed(
        monkeypatch,
        {"src/gate_keeper/cli.py", "docs/cli-reference.md"},
    )
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "pass"
    assert out.evidence_kind == "dependent_artifact_co_changed"


def test_unack_change_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    _patch_changed(monkeypatch, {"src/gate_keeper/cli.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "dependent_artifact_changed_without_target_update"
    assert "ack_path" in out.evidence_data
    assert out.remediation is not None
    assert ".gate-keeper/acks/" in out.remediation


def test_matching_ack_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src, _doc = _seed_repo(tmp_path, source_text="changed body\n")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    acks_dir = tmp_path / ".gate-keeper" / "acks"
    acks_dir.mkdir()
    (acks_dir / "cli-implementation-documents-cli-reference.yml").write_text(
        dedent(
            f"""
            edge_id: cli-implementation-documents-cli-reference
            source_sha: {sha}
            ack_by: reviewer
            ack_at: 2026-05-10T00:00:00Z
            reason: reviewed; doc still accurate
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, {"src/gate_keeper/cli.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "pass"
    assert out.evidence_kind == "dependent_artifact_acked"
    assert out.evidence_data["source_sha"] == sha
    assert out.evidence_data["ack_by"] == "reviewer"


def test_ack_with_wrong_edge_id_does_not_satisfy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    src, _doc = _seed_repo(tmp_path)
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    acks_dir = tmp_path / ".gate-keeper" / "acks"
    acks_dir.mkdir()
    # Filename matches the edge id, but the body's edge_id field does not.
    (acks_dir / "cli-implementation-documents-cli-reference.yml").write_text(
        dedent(
            f"""
            edge_id: some-other-edge
            source_sha: {sha}
            ack_by: reviewer
            ack_at: 2026-05-10T00:00:00Z
            reason: copy-pasted ack body from elsewhere
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, {"src/gate_keeper/cli.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "dependent_artifact_changed_without_target_update"


def test_ack_yaml_parse_error_surfaced_as_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    acks_dir = tmp_path / ".gate-keeper" / "acks"
    acks_dir.mkdir()
    (acks_dir / "cli-implementation-documents-cli-reference.yml").write_text(
        ":\n  - this is not a valid mapping\n: also not\n",
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, {"src/gate_keeper/cli.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "ack_invalid"


def test_ack_non_mapping_body_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    acks_dir = tmp_path / ".gate-keeper" / "acks"
    acks_dir.mkdir()
    (acks_dir / "cli-implementation-documents-cli-reference.yml").write_text(
        "- just\n- a\n- list\n",
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, {"src/gate_keeper/cli.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "ack_invalid"


def _seed_stamped_repo(
    root: Path,
    *,
    source_text: str = "src body\n",
    stamp_value: str | None = None,
    stamp_source_id: str = "src",
    extra_frontmatter: dict[str, str] | None = None,
    target_has_frontmatter: bool = True,
    extra_target_body: str = "doc\n",
) -> tuple[Path, Path]:
    """Materialise a stamped-mode pair under *root*.

    By default the target frontmatter contains ``tracks:
    <stamp_source_id>@sha256:<digest>`` matching the seeded source bytes.
    Pass ``stamp_value`` to inject a literal frontmatter ``tracks:`` value
    (used by the malformed/stale tests).
    """
    src = root / "src" / "gate_keeper" / "cli.py"
    src.parent.mkdir(parents=True)
    src.write_text(source_text, encoding="utf-8")

    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    if stamp_value is None:
        stamp_value = f"{stamp_source_id}@sha256:{sha}"

    doc = root / "docs" / "cli-reference.md"
    doc.parent.mkdir(parents=True)
    if target_has_frontmatter:
        front = {"tracks": stamp_value}
        if extra_frontmatter:
            front.update(extra_frontmatter)
        body = "---\n"
        for k, v in front.items():
            body += f"{k}: {v}\n"
        body += "---\n" + extra_target_body
    else:
        body = extra_target_body
    doc.write_text(body, encoding="utf-8")

    manifest_dir = root / ".gate-keeper"
    manifest_dir.mkdir()
    (manifest_dir / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: src
                path: src/gate_keeper/cli.py
              - id: tgt
                path: docs/cli-reference.md
            edges:
              - from: src
                to: tgt
                relation: documents
                mode: stamped
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return src, doc


def test_stamped_matching_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_stamped_repo(tmp_path)
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "pass"
    assert out.evidence_kind == "dependent_artifact_unaffected"
    assert out.evidence_data["mode"] == "stamped"


def test_stamped_stale_digest_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Stamp digest does not match source's current sha256 → fail."""
    _seed_stamped_repo(
        tmp_path,
        source_text="updated body\n",
        stamp_value="src@sha256:" + ("0" * 64),
    )
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "dependent_artifact_stale_by_hash"
    assert out.evidence_data["stamp_digest"] == "0" * 64
    expected_sha = hashlib.sha256(b"updated body\n").hexdigest()
    assert out.evidence_data["source_sha"] == expected_sha
    assert out.remediation is not None
    assert "tracks:" in out.remediation


def test_stamped_missing_stamp_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Target has no ``tracks:`` field in frontmatter → fail."""
    _seed_stamped_repo(tmp_path, target_has_frontmatter=False)
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_missing"
    assert "expected_stamp" in out.evidence_data


def test_stamped_frontmatter_without_tracks_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Target has frontmatter but no ``tracks:`` key → fail (missing)."""
    src = tmp_path / "src" / "gate_keeper" / "cli.py"
    src.parent.mkdir(parents=True)
    src.write_text("body\n", encoding="utf-8")
    doc = tmp_path / "docs" / "cli-reference.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\ntitle: doc\n---\nbody\n", encoding="utf-8")
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: src
                path: src/gate_keeper/cli.py
              - id: tgt
                path: docs/cli-reference.md
            edges:
              - from: src
                to: tgt
                relation: documents
                mode: stamped
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_missing"


def test_stamped_malformed_stamp_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Stamp present but not in canonical ``<id>@sha256:<hex>`` form."""
    _seed_stamped_repo(tmp_path, stamp_value="not-a-valid-stamp")
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_malformed"


def test_stamped_wrong_algorithm_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Algorithm other than sha256 is rejected as malformed in slice 1."""
    _seed_stamped_repo(tmp_path, stamp_value="src@md5:" + ("a" * 32))
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_malformed"


def test_stamped_short_digest_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A digest that isn't 64 lowercase hex chars is malformed."""
    _seed_stamped_repo(tmp_path, stamp_value="src@sha256:abcd")
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_malformed"


def test_stamped_non_utf8_target_reports_malformed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Target file with non-UTF-8 bytes must surface as ``target_stamp_malformed``.

    Regression for codex P2 on PR #196: the previous ``read_target_stamp``
    only caught ``OSError``, so a target with mojibake / binary bytes
    propagated ``UnicodeDecodeError`` past ``_evaluate_stamped_edge`` and
    crashed the validator instead of producing a structured diagnostic.
    """
    _seed_stamped_repo(tmp_path)
    # Overwrite the stamped target with bytes that are not valid UTF-8.
    doc = tmp_path / "docs" / "cli-reference.md"
    doc.write_bytes(b"---\ntracks: src@sha256:" + (b"a" * 64) + b"\n---\nbody \xff\n")
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_malformed"
    assert "decode" in out.evidence_data["error"]


def test_stamped_wrong_source_id_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Stamp source-id does not match the manifest 'from' node id."""
    _seed_stamped_repo(tmp_path, stamp_source_id="some-other-source")
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "target_stamp_malformed"
    assert out.evidence_data["stamp_source_id"] == "some-other-source"
    assert out.evidence_data["expected_source_id"] == "src"


def test_stamped_missing_source_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Source path declared in manifest does not exist on disk."""
    doc = tmp_path / "docs" / "cli-reference.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\ntracks: src@sha256:" + ("0" * 64) + "\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: src
                path: src/gate_keeper/cli.py
              - id: tgt
                path: docs/cli-reference.md
            edges:
              - from: src
                to: tgt
                relation: documents
                mode: stamped
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "unavailable"
    assert out.evidence_kind == "dependent_artifact_source_missing"


def test_stamped_missing_target_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Target path declared in manifest does not exist on disk.

    The validator is invoked with the target path string, so the path passes
    the per-edge dispatch lookup; only the on-disk read surfaces the absence.
    """
    src = tmp_path / "src" / "gate_keeper" / "cli.py"
    src.parent.mkdir(parents=True)
    src.write_text("body\n", encoding="utf-8")
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: src
                path: src/gate_keeper/cli.py
              - id: tgt
                path: docs/cli-reference.md
            edges:
              - from: src
                to: tgt
                relation: documents
                mode: stamped
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "unavailable"
    assert out.evidence_kind == "dependent_artifact_target_missing"


def test_stamped_does_not_invoke_changed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A stamped-only manifest does not consult git diff (Mode B is target-side only)."""
    _seed_stamped_repo(tmp_path)

    def _boom(*_a: Any, **_kw: Any) -> Any:  # pragma: no cover - assertion path
        raise AssertionError("compute_changed_files must not be called for stamped-only edges")

    monkeypatch.setattr(validator, "compute_changed_files", _boom)
    monkeypatch.setattr(validator, "resolve_base_ref", lambda env=None: "origin/main")
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "pass"
    assert out.evidence_kind == "dependent_artifact_unaffected"


def test_fixture_manifest_loads_with_mixed_modes():
    """The shipped fixture manifest covers both Mode A and Mode B edges."""
    from dependency_gates.manifest import load_manifest

    fixture = Path(__file__).resolve().parent / "fixtures" / "dependency_gates" / "stamped_manifest.yml"
    manifest = load_manifest(fixture)
    modes_by_to = {edge.to_id: edge.mode for edge in manifest.edges}
    assert modes_by_to["appendix-explanation"] == "stamped"
    assert modes_by_to["cli-reference"] == "affected_set"


def test_stamped_pairs_desugar_propagates_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``pairs:`` sugar with ``mode: stamped`` desugars into stamped edges."""
    src = tmp_path / "src" / "gate_keeper" / "cli.py"
    src.parent.mkdir(parents=True)
    src.write_text("body\n", encoding="utf-8")
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    doc = tmp_path / "docs" / "cli-reference.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        f"---\ntracks: src@sha256:{sha}\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: src
                path: src/gate_keeper/cli.py
              - id: tgt
                path: docs/cli-reference.md
            pairs:
              - id: cli-pair
                relation: documents
                source: src
                target: tgt
                mode: stamped
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    # pairs desugar to two directed edges; the src->tgt direction passes
    # because the target stamp matches. The tgt->src direction would also
    # be evaluated but the validator is invoked with target=docs/cli-reference.md
    # so only edges with to_id = tgt apply.
    assert out.status == "pass"
    assert out.evidence_kind == "dependent_artifact_unaffected"


def test_multiple_edges_to_same_target_aggregated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two edges both pointing at the same target are both evaluated."""
    src1 = tmp_path / "src" / "a.py"
    src1.parent.mkdir(parents=True)
    src1.write_text("a\n", encoding="utf-8")
    src2 = tmp_path / "src" / "b.py"
    src2.write_text("b\n", encoding="utf-8")
    doc = tmp_path / "docs" / "cli-reference.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("doc\n", encoding="utf-8")
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: a
                path: src/a.py
              - id: b
                path: src/b.py
              - id: tgt
                path: docs/cli-reference.md
            edges:
              - from: a
                to: tgt
                relation: documents
              - from: b
                to: tgt
                relation: documents
            """
        ).lstrip(),
        encoding="utf-8",
    )
    # Only `b.py` changed; `a` edge passes (unaffected) but `b` edge fails.
    _patch_changed(monkeypatch, {"src/b.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "dependent_artifact_changed_without_target_update"
    assert out.evidence_data["source_path"] == "src/b.py"


def test_invalid_nodes_top_level_type_rejected():
    """Manifest with `nodes: {}` (a mapping, not a list) is rejected explicitly."""
    from dependency_gates.manifest import ManifestError, parse_manifest

    with pytest.raises(ManifestError, match="must be a list"):
        parse_manifest("nodes: {}\n")


def test_stale_ack_does_not_satisfy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    acks_dir = tmp_path / ".gate-keeper" / "acks"
    acks_dir.mkdir()
    (acks_dir / "cli-implementation-documents-cli-reference.yml").write_text(
        dedent(
            """
            edge_id: cli-implementation-documents-cli-reference
            source_sha: deadbeef
            ack_by: reviewer
            ack_at: 2026-05-01T00:00:00Z
            reason: stale ack
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _patch_changed(monkeypatch, {"src/gate_keeper/cli.py"})
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "dependent_artifact_changed_without_target_update"


def test_target_outside_edges_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "README.md")
    assert out.status == "pass"
    assert out.evidence_kind == "edge_not_applicable"


def test_manifest_invalid_fails(tmp_path: Path):
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        "nodes:\n  - id: a\n",  # missing 'path'
        encoding="utf-8",
    )
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "manifest_invalid"


def test_manifest_target_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Manifest references files that don't exist on disk.
    (tmp_path / ".gate-keeper").mkdir()
    (tmp_path / ".gate-keeper" / "dependency-manifest.yml").write_text(
        dedent(
            """
            nodes:
              - id: a
                path: src/missing.py
              - id: b
                path: docs/cli-reference.md
            edges:
              - from: a
                to: b
                relation: documents
            """
        ).lstrip(),
        encoding="utf-8",
    )
    # Ensure 'b' exists so target resolution finds an applicable edge.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "cli-reference.md").write_text("doc\n", encoding="utf-8")
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "fail"
    assert out.evidence_kind == "manifest_target_missing"
    assert "src/missing.py" in out.evidence_data["missing_paths"]


def test_changed_file_source_unresolved_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_repo(tmp_path)

    def _raise(*_a: Any, **_kw: Any) -> Any:
        raise cf_mod.ChangedFilesError("git diff exited 128: bad ref")

    monkeypatch.setattr(validator, "compute_changed_files", _raise)
    monkeypatch.setattr(validator, "resolve_base_ref", lambda env=None: "origin/main")
    out = _run(tmp_path, "docs/cli-reference.md")
    assert out.status == "unavailable"
    assert out.evidence_kind == "changed_file_source_unresolved"


def test_resolve_base_ref_env_override():
    # Direct unit on the helper.
    assert cf_mod.resolve_base_ref({}) == cf_mod.DEFAULT_BASE_REF
    assert cf_mod.resolve_base_ref({"GATE_KEEPER_BASE_REF": "main"}) == "main"


def test_validator_emit_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _seed_repo(tmp_path)
    _patch_changed(monkeypatch, set())

    payload = json.dumps(
        {
            "rule": {"params": {"manifest": ".gate-keeper/dependency-manifest.yml"}},
            "target": "docs/cli-reference.md",
        }
    )
    monkeypatch.setattr(sys, "stdin", _StringIO(payload))
    monkeypatch.chdir(tmp_path)
    rc = validator.main()
    assert rc == 0
    captured = capsys.readouterr().out
    diag = json.loads(captured)
    assert diag["status"] == "pass"
    assert diag["evidence"][0]["kind"] == "dependent_artifact_unaffected"


class _StringIO:
    """Tiny stdin stub that supports only ``read()``."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ---------------------------------------------------------------------------
# Integration: real subprocess through the command adapter.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_end_to_end_through_command_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the validator script via ``CommandAdapter`` against a fake repo."""
    from gate_keeper.adapters.command import CommandAdapter, set_enabled
    from gate_keeper.models import (
        Backend,
        Confidence,
        Rule,
        RuleKind,
        Severity,
        SourceLocation,
        Status,
    )

    _seed_repo(tmp_path)

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)

    # The temp repo has no `origin`; point the validator at HEAD so the
    # diff (HEAD...HEAD) is empty — i.e. simulate a clean PR base.
    monkeypatch.setenv("GATE_KEEPER_BASE_REF", "HEAD")
    monkeypatch.chdir(tmp_path)

    rule = Rule(
        id="cli-reference-tracks-cli-implementation",
        title="CLI reference tracks CLI implementation",
        source=SourceLocation(path="rules.json", line=1),
        text="cli-reference must track cli-implementation",
        kind=RuleKind.EXTERNAL_CHECK,
        severity=Severity.WARNING,
        backend_hint=Backend.EXTERNAL,
        confidence=Confidence.HIGH,
        params={
            "tool": "command",
            "argv": [sys.executable, str(VALIDATOR_SCRIPT)],
            "timeout_seconds": 30,
        },
    )

    set_enabled(True)
    try:
        diag = CommandAdapter().check(rule, "docs/cli-reference.md")
    finally:
        set_enabled(False)

    assert diag.status is Status.PASS
    assert diag.evidence[0].kind == "dependent_artifact_unaffected"
    assert diag.backend is Backend.EXTERNAL
    assert diag.rule_id == "cli-reference-tracks-cli-implementation"
