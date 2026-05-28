"""Tests for the deterministic repo-local reference scanner (issue #269, slice 1).

Two layers:

- Unit tests of ``scripts/dependency_gates/_repo_ref_scan.extract_references``
  exercise the syntactic extractor on small inline snippets.
- Validator tests stub ``compute_changed_files`` and exercise
  ``scripts/dependency_gates/check_repo_refs._run`` directly. No git, no
  subprocess.

Mirrors the layered style of ``tests/test_dependency_validator.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from gate_keeper.models import Diagnostic, DiagnosticReport, SourceLocation

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from dependency_gates import _repo_ref_scan as scanner  # noqa: E402
from dependency_gates import check_repo_refs as validator  # noqa: E402

# --------------------------------------------------------------------------
# Extractor unit tests
# --------------------------------------------------------------------------


def test_extracts_markdown_inline_link():
    text = "See [the CLI reference](docs/cli-reference.md) for details.\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.path == "docs/cli-reference.md"
    assert ref.shape == "md_link"
    assert ref.line == 1


def test_extracts_markdown_image():
    text = "![Architecture diagram](docs/design/diagram.png)\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1
    assert refs[0].shape == "md_image"
    assert refs[0].path == "docs/design/diagram.png"


def test_extracts_markdown_reference_definition():
    text = "[ref]: docs/cli-reference.md\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1
    assert refs[0].shape == "md_ref_def"
    assert refs[0].path == "docs/cli-reference.md"


def test_ignores_url_targets():
    text = "[ext](https://example.com/path) and [mail](mailto:a@b.c)\n"
    refs = scanner.extract_references(text)
    assert refs == []


def test_ignores_in_page_anchors():
    text = "[anchor](#section-2)\n"
    refs = scanner.extract_references(text)
    assert refs == []


def test_bare_path_lookbehind_excludes_markdown_link_text():
    """Regression for sonnet review on #273 / #269.

    Without ``[`` in ``_BARE_PATH_RE``'s lookbehind, the path-shaped fragment
    inside Markdown link *text* (``[docs/old.md](docs/new.md)``) would be
    re-extracted as a ``bare_path`` reference in addition to the correct
    ``md_link``. When the text-side path no longer existed on disk (e.g.
    after a rename) the validator emitted a spurious ``broken_reference``
    → ``Status.FAIL`` on perfectly valid Markdown.
    """
    text = "[docs/old.md](docs/new.md)\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1, f"expected exactly one ref, got {refs}"
    assert refs[0].shape == "md_link"
    assert refs[0].path == "docs/new.md"


def test_bare_path_lookbehind_excludes_external_link_text():
    """Sibling regression: link text must not leak even when the URL is external.

    The link target is an external URL (correctly ignored by the extractor),
    but the link text contains a repo-shape path. The bare-path matcher must
    still be blocked by the ``[`` lookbehind so the text fragment is not
    surfaced as a ``broken_reference``.
    """
    text = "[docs/something](https://external.example.com)\n"
    refs = scanner.extract_references(text)
    assert refs == [], f"expected no refs (URL external, link text inert), got {refs}"


def test_extracts_inline_code_with_known_prefix():
    text = "Edit `src/gate_keeper/cli.py` to add the flag.\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1
    assert refs[0].shape == "inline_code"
    assert refs[0].path == "src/gate_keeper/cli.py"


def test_extracts_inline_code_with_known_extension():
    text = "Run `pyproject.toml` linter.\n"
    refs = scanner.extract_references(text)
    # pyproject.toml has no '/', so it should NOT be picked up by inline-code.
    assert refs == []


def test_extracts_inline_code_known_extension_with_slash():
    text = "See `tooling/pyproject.toml` for details.\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1
    assert refs[0].shape == "inline_code"
    assert refs[0].path == "tooling/pyproject.toml"


def test_skips_fenced_code_blocks():
    text = dedent(
        """
        Before fence: [pre](docs/before.md)
        ```python
        # docs/inside-fence.md should not be extracted
        path = "src/inside.py"
        ```
        After fence: [post](docs/after.md)
        """
    ).lstrip()
    refs = scanner.extract_references(text)
    paths = [r.path for r in refs]
    assert "docs/before.md" in paths
    assert "docs/after.md" in paths
    assert "docs/inside-fence.md" not in paths
    assert "src/inside.py" not in paths


def test_strips_anchor_and_query():
    text = "[anchor](docs/cli-reference.md#validate)\n"
    refs = scanner.extract_references(text)
    assert len(refs) == 1
    assert refs[0].path == "docs/cli-reference.md"


def test_does_not_match_unknown_top_level_dir_bare_path():
    text = "vendored/foo/bar.py is the legacy path.\n"
    refs = scanner.extract_references(text)
    assert refs == []


def test_suspicious_broad_glob():
    ref = scanner.Reference(path="docs/*.md", line=1, column=1, shape="bare_path", raw="docs/*.md")
    assert scanner.is_suspicious_broad(ref) is True


def test_suspicious_broad_trailing_slash():
    ref = scanner.Reference(path="scripts/", line=1, column=1, shape="bare_path", raw="scripts/")
    assert scanner.is_suspicious_broad(ref) is True


def test_suspicious_broad_bare_top_level():
    ref = scanner.Reference(path="docs", line=1, column=1, shape="md_link", raw="docs")
    assert scanner.is_suspicious_broad(ref) is True


def test_concrete_path_is_not_suspicious_broad():
    ref = scanner.Reference(
        path="docs/cli-reference.md",
        line=1,
        column=1,
        shape="md_link",
        raw="docs/cli-reference.md",
    )
    assert scanner.is_suspicious_broad(ref) is False


# --------------------------------------------------------------------------
# Validator integration tests (stubbed changed-file resolver)
# --------------------------------------------------------------------------


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


def _patch_changed_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(repo_root, base_ref):  # type: ignore[no-untyped-def]
        raise validator.ChangedFilesError("git unavailable for test")

    monkeypatch.setattr(validator, "compute_changed_files", _raise)
    monkeypatch.setattr(
        validator,
        "resolve_base_ref",
        lambda env=None: "origin/main",
    )


def _seed_target(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _seed_manifest(root: Path) -> None:
    manifest_dir = root / ".gate-keeper"
    manifest_dir.mkdir(exist_ok=True)
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


def _run(repo_root: Path, target: str) -> validator.Outcome:
    return validator._run(
        repo_root=repo_root,
        manifest_path=repo_root / ".gate-keeper" / "dependency-manifest.yml",
        target=target,
    )


def test_validator_link_to_existing_path_no_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_target(tmp_path, "docs/cli-reference.md", "# CLI reference\n")
    _seed_target(tmp_path, "src/gate_keeper/cli.py", "x = 1\n")
    _seed_target(
        tmp_path,
        "README.md",
        "See [the CLI reference](docs/cli-reference.md) for details.\n",
    )
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, set())

    out = _run(tmp_path, "README.md")
    assert out.status == "pass"
    # No broken/uncovered/broad findings — only the clean-evidence entry.
    kinds = {e["kind"] for e in out.evidence}
    assert "broken_reference" not in kinds
    assert "uncovered_reference" not in kinds
    assert "suspicious_broad" not in kinds


def test_validator_link_to_nonexistent_path_emits_broken_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_target(tmp_path, "docs/cli-reference.md", "# CLI reference\n")
    _seed_target(tmp_path, "src/gate_keeper/cli.py", "x = 1\n")
    _seed_target(
        tmp_path,
        "README.md",
        "See [missing doc](docs/no-such-file.md) for details.\n",
    )
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, set())

    out = _run(tmp_path, "README.md")
    assert out.status == "fail"
    broken = [e for e in out.evidence if e["kind"] == "broken_reference"]
    assert len(broken) == 1
    paths = [r["path"] for r in broken[0]["data"]["references"]]
    assert "docs/no-such-file.md" in paths
    assert out.remediation is not None


def test_validator_newly_introduced_reference_in_changed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_target(tmp_path, "docs/cli-reference.md", "# CLI reference\n")
    _seed_target(tmp_path, "docs/getting-started.md", "# Getting started\n")
    _seed_target(tmp_path, "src/gate_keeper/cli.py", "x = 1\n")
    # README references a NEW doc (getting-started) that is NOT in the
    # changed-set and NOT in any manifest edge. The README itself IS in the
    # changed-set, so the reference is "newly introduced".
    _seed_target(
        tmp_path,
        "README.md",
        "See [getting started](docs/getting-started.md) and the [CLI](docs/cli-reference.md).\n",
    )
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, {"README.md"})

    out = _run(tmp_path, "README.md")
    assert out.status == "pass"  # informational — does not fail in slice 1
    uncovered = [e for e in out.evidence if e["kind"] == "uncovered_reference"]
    assert len(uncovered) == 1
    paths = [r["path"] for r in uncovered[0]["data"]["references"]]
    assert "docs/getting-started.md" in paths
    # cli-reference.md is in a manifest edge → must NOT be reported as new.
    assert "docs/cli-reference.md" not in paths
    assert uncovered[0]["data"]["subkind"] == "newly_introduced"


def test_validator_suspicious_broad_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_target(tmp_path, "docs/cli-reference.md", "# CLI reference\n")
    # Glob shape in a markdown link.
    _seed_target(
        tmp_path,
        "README.md",
        "All [docs](docs/*.md) live here.\n",
    )
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, set())

    out = _run(tmp_path, "README.md")
    # The glob path itself does not exist, but is_suspicious_broad
    # short-circuits before the broken-reference check, so we expect the
    # broad evidence kind and NOT broken_reference for this path.
    assert out.status == "pass"
    broad = [e for e in out.evidence if e["kind"] == "suspicious_broad"]
    assert len(broad) == 1
    paths = [r["path"] for r in broad[0]["data"]["references"]]
    assert "docs/*.md" in paths
    broken = [e for e in out.evidence if e["kind"] == "broken_reference"]
    assert broken == []


def test_validator_non_text_target_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_target(tmp_path, "src/gate_keeper/cli.py", "x = 1\n")
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, set())

    out = _run(tmp_path, "src/gate_keeper/cli.py")
    assert out.status == "pass"
    assert any(e["kind"] == "scanner_not_applicable" for e in out.evidence)


def test_validator_missing_target_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, set())

    out = _run(tmp_path, "docs/nonexistent.md")
    assert out.status == "unavailable"
    assert any(e["kind"] == "target_unreadable" for e in out.evidence)


def test_validator_changed_file_set_unresolvable_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _seed_target(tmp_path, "docs/cli-reference.md", "# CLI reference\n")
    _seed_target(
        tmp_path,
        "README.md",
        "See [missing](docs/no-such-file.md).\n",
    )
    _seed_manifest(tmp_path)
    _patch_changed_unavailable(monkeypatch)

    out = _run(tmp_path, "README.md")
    # Broken-reference detection is independent of the changed-file set
    # → it should still fail. The unresolved evidence is appended.
    assert out.status == "fail"
    assert any(e["kind"] == "broken_reference" for e in out.evidence)
    assert any(e["kind"] == "changed_file_source_unresolved" for e in out.evidence)


def test_validator_empty_target_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "")
    assert out.status == "unavailable"
    assert any(e["kind"] == "params_error" for e in out.evidence)


# --------------------------------------------------------------------------
# IR-shape round-trip
# --------------------------------------------------------------------------


def test_emitted_diagnostic_round_trips_through_ir_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The emitted JSON, wrapped into a Diagnostic envelope, must load via
    ``Diagnostic.from_dict`` without schema break.

    The command-adapter contract has the validator emit only ``status``,
    ``message``, ``evidence`` (and optionally ``remediation``); the adapter
    wraps that into a full Diagnostic with ``rule_id``, ``source``,
    ``backend``, ``severity``. We replicate the wrapping here and confirm
    the round-trip.
    """
    _seed_target(tmp_path, "docs/cli-reference.md", "# CLI reference\n")
    _seed_target(
        tmp_path,
        "README.md",
        "See [missing](docs/no-such-file.md).\n",
    )
    _seed_manifest(tmp_path)
    _patch_changed(monkeypatch, set())

    out = _run(tmp_path, "README.md")
    # Replicate the adapter envelope.
    diagnostic_dict = {
        "rule_id": "repo-local-reference-scanner",
        "source": {"path": "docs/dependency-gate-rules.json", "line": 1},
        "backend": "external",
        "status": out.status,
        "severity": "warning",
        "message": out.message,
        "evidence": [{"kind": e["kind"], "data": e["data"]} for e in out.evidence],
    }
    if out.remediation is not None:
        diagnostic_dict["remediation"] = out.remediation

    diag = Diagnostic.from_dict(diagnostic_dict)
    assert diag.rule_id == "repo-local-reference-scanner"
    assert diag.status.value == out.status
    # Round-trip back to dict — confirms no fields lost or added.
    assert diag.to_dict()["evidence"] == diagnostic_dict["evidence"]

    # Loader at report level too.
    report = DiagnosticReport.from_dict({"diagnostics": [diagnostic_dict]})
    assert len(report.diagnostics) == 1
    # Source location is well-formed.
    assert isinstance(report.diagnostics[0].source, SourceLocation)


def test_emit_writes_valid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The ``_emit`` helper must write a JSON-parseable object on stdout."""
    out = validator.Outcome(
        status="pass",
        message="ok",
        evidence=[{"kind": "repo_refs_clean", "data": {"target": "x", "references_scanned": 0}}],
    )
    written: list[str] = []
    monkeypatch.setattr(validator.sys.stdout, "write", lambda s: written.append(s) or len(s))
    validator._emit(out)
    parsed = json.loads("".join(written))
    assert parsed["status"] == "pass"
    assert parsed["evidence"][0]["kind"] == "repo_refs_clean"
