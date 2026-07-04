"""Tests for ``scripts/dependency_gates/check_manifest_coverage`` (issue #280).

Two layers:

- Unit scenarios stub ``compute_changed_files`` and exercise the in-process
  ``_run`` function directly. No git, no subprocess.
- One ``@pytest.mark.integration`` test runs the validator end-to-end through
  the real ``CommandAdapter`` (mirrors ``tests/test_command_adapter.py``
  ``TestRealSubprocess`` style).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

_SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from dependency_gates import check_manifest_coverage as validator  # noqa: E402

VALIDATOR_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "dependency_gates" / "check_manifest_coverage.py"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_repo(
    root: Path,
    *,
    manifest_text: str | None = None,
    exemptions_text: str | None = None,
) -> None:
    """Materialise a minimal repo layout under *root*."""
    gk = root / ".gate-keeper"
    gk.mkdir(parents=True, exist_ok=True)

    if manifest_text is None:
        manifest_text = dedent(
            """
            nodes:
              - id: src-models
                path: src/gate_keeper/models.py
                kind: code
              - id: docs-rule-ir
                path: docs/rule-ir.md
                kind: reference
            edges:
              - from: src-models
                to: docs-rule-ir
                relation: documents
            """
        ).lstrip()
    (gk / "dependency-manifest.yml").write_text(manifest_text, encoding="utf-8")

    if exemptions_text is not None:
        (gk / "ref-exemptions.yml").write_text(exemptions_text, encoding="utf-8")

    # Materialise the nodes that the manifest references so the manifest
    # loader's referential validation passes.
    (root / "src" / "gate_keeper").mkdir(parents=True, exist_ok=True)
    (root / "src" / "gate_keeper" / "models.py").write_text("# models\n", encoding="utf-8")
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "rule-ir.md").write_text("# IR\n", encoding="utf-8")


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


def _evidence_kinds(outcome: validator.Outcome) -> list[str]:
    return [e["kind"] for e in outcome.evidence]


def _run(
    root: Path,
    target: str,
    *,
    exemptions_path: Path | None = None,
) -> validator.Outcome:
    manifest_path = root / ".gate-keeper" / "dependency-manifest.yml"
    if exemptions_path is None:
        exemptions_path = root / ".gate-keeper" / "ref-exemptions.yml"
    return validator._run(
        repo_root=root,
        manifest_path=manifest_path,
        exemptions_path=exemptions_path,
        target=target,
    )


# ---------------------------------------------------------------------------
# Acceptance criterion 1: uncovered new file → FAIL uncovered_file
# ---------------------------------------------------------------------------


def test_uncovered_changed_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A changed file with no manifest edge and no exemption → FAIL."""
    _seed_repo(tmp_path)
    (tmp_path / "new_script.py").write_text("# new\n", encoding="utf-8")
    _patch_changed(monkeypatch, {"new_script.py"})
    out = _run(tmp_path, "new_script.py")
    assert out.status == "fail"
    assert "uncovered_file" in _evidence_kinds(out)
    ev = next(e for e in out.evidence if e["kind"] == "uncovered_file")
    assert ev["data"]["target"] == "new_script.py"
    assert "dependency-manifest.yml" in ev["data"]["manifest_path"]


# ---------------------------------------------------------------------------
# Acceptance criterion 2: covered or exempted file → PASS
# ---------------------------------------------------------------------------


def test_manifest_covered_file_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A changed file with a manifest edge → PASS covering_edge."""
    _seed_repo(tmp_path)
    _patch_changed(monkeypatch, {"src/gate_keeper/models.py"})
    out = _run(tmp_path, "src/gate_keeper/models.py")
    assert out.status == "pass"
    assert "covering_edge" in _evidence_kinds(out)
    ev = next(e for e in out.evidence if e["kind"] == "covering_edge")
    assert ev["data"]["target"] == "src/gate_keeper/models.py"
    assert len(ev["data"]["edges"]) >= 1


def test_manifest_covered_target_node_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A changed file that is the `to` node of an edge → PASS covering_edge."""
    _seed_repo(tmp_path)
    _patch_changed(monkeypatch, {"docs/rule-ir.md"})
    out = _run(tmp_path, "docs/rule-ir.md")
    assert out.status == "pass"
    assert "covering_edge" in _evidence_kinds(out)


def test_exempted_file_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A changed file with an exemption entry → PASS exemption_applied."""
    exemptions = dedent(
        """
        exemptions:
          - path: scripts/helper.py
            category: manual
            reason: "bootstrap exemption"
        """
    ).lstrip()
    _seed_repo(tmp_path, exemptions_text=exemptions)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.py").write_text("# helper\n", encoding="utf-8")
    _patch_changed(monkeypatch, {"scripts/helper.py"})
    out = _run(tmp_path, "scripts/helper.py")
    assert out.status == "pass"
    assert "exemption_applied" in _evidence_kinds(out)
    ev = next(e for e in out.evidence if e["kind"] == "exemption_applied")
    assert ev["data"]["category"] == "manual"
    assert ev["data"]["reason"] == "bootstrap exemption"


def test_exemption_reason_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An exemption entry without a reason field → PASS."""
    exemptions = dedent(
        """
        exemptions:
          - path: scripts/helper.py
            category: manual
        """
    ).lstrip()
    _seed_repo(tmp_path, exemptions_text=exemptions)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.py").write_text("# helper\n", encoding="utf-8")
    _patch_changed(monkeypatch, {"scripts/helper.py"})
    out = _run(tmp_path, "scripts/helper.py")
    assert out.status == "pass"
    assert "exemption_applied" in _evidence_kinds(out)


# ---------------------------------------------------------------------------
# Acceptance criterion 3: fail-closed on unreadable manifest
# ---------------------------------------------------------------------------


def test_missing_manifest_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A missing manifest → unavailable manifest_invalid."""
    _patch_changed(monkeypatch, {"some/file.py"})
    out = validator._run(
        repo_root=tmp_path,
        manifest_path=tmp_path / ".gate-keeper" / "dependency-manifest.yml",
        exemptions_path=tmp_path / ".gate-keeper" / "ref-exemptions.yml",
        target="some/file.py",
    )
    assert out.status == "unavailable"
    assert "manifest_invalid" in _evidence_kinds(out)


def test_malformed_manifest_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A malformed manifest → unavailable manifest_invalid."""
    gk = tmp_path / ".gate-keeper"
    gk.mkdir()
    (gk / "dependency-manifest.yml").write_text("nodes: [[[bad", encoding="utf-8")
    _patch_changed(monkeypatch, {"some/file.py"})
    out = _run(tmp_path, "some/file.py")
    assert out.status == "unavailable"
    assert "manifest_invalid" in _evidence_kinds(out)


def test_changed_file_source_unresolved_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Unavailable git diff → unavailable changed_file_source_unresolved."""
    # ChangedFilesError is defined in the validator module (inlined helpers).
    ChangedFilesError = validator.ChangedFilesError

    _seed_repo(tmp_path)
    monkeypatch.setattr(
        validator,
        "compute_changed_files",
        lambda repo_root, base_ref: (_ for _ in ()).throw(  # type: ignore[misc]
            ChangedFilesError("git not found")
        ),
    )
    monkeypatch.setattr(validator, "resolve_base_ref", lambda env=None: "origin/main")
    out = _run(tmp_path, "src/gate_keeper/models.py")
    assert out.status == "unavailable"
    assert "changed_file_source_unresolved" in _evidence_kinds(out)


# ---------------------------------------------------------------------------
# Acceptance criterion 4: unreadable exemption file → manifest_invalid
# ---------------------------------------------------------------------------


def test_malformed_exemption_file_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A malformed exemption file → unavailable manifest_invalid."""
    exemptions = "exemptions: [[[bad yaml"
    _seed_repo(tmp_path, exemptions_text=exemptions)
    _patch_changed(monkeypatch, {"scripts/helper.py"})
    out = _run(tmp_path, "scripts/helper.py")
    assert out.status == "unavailable"
    assert "manifest_invalid" in _evidence_kinds(out)


def test_exemption_entry_missing_path_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exemption entry without a path field → unavailable manifest_invalid."""
    exemptions = dedent(
        """
        exemptions:
          - category: manual
            reason: "missing path"
        """
    ).lstrip()
    _seed_repo(tmp_path, exemptions_text=exemptions)
    _patch_changed(monkeypatch, {"scripts/helper.py"})
    out = _run(tmp_path, "scripts/helper.py")
    assert out.status == "unavailable"
    assert "manifest_invalid" in _evidence_kinds(out)


def test_unknown_exemption_category_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exemption entry with unknown category → unavailable manifest_invalid."""
    exemptions = dedent(
        """
        exemptions:
          - path: scripts/helper.py
            category: unknown_future_category
        """
    ).lstrip()
    _seed_repo(tmp_path, exemptions_text=exemptions)
    _patch_changed(monkeypatch, {"scripts/helper.py"})
    out = _run(tmp_path, "scripts/helper.py")
    assert out.status == "unavailable"
    assert "manifest_invalid" in _evidence_kinds(out)


# ---------------------------------------------------------------------------
# Unchanged files are silently passed
# ---------------------------------------------------------------------------


def test_unchanged_file_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A file not in the changed-file set → PASS dependent_artifact_unaffected."""
    _seed_repo(tmp_path)
    # changed set is empty — nothing changed
    _patch_changed(monkeypatch, set())
    out = _run(tmp_path, "src/gate_keeper/models.py")
    assert out.status == "pass"
    assert "dependent_artifact_unaffected" in _evidence_kinds(out)


# ---------------------------------------------------------------------------
# Absent exemption file is not an error
# ---------------------------------------------------------------------------


def test_absent_exemption_file_is_not_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When the exemption file is absent, the validator still runs."""
    _seed_repo(tmp_path)  # no exemptions_text → file not created
    _patch_changed(monkeypatch, {"src/gate_keeper/models.py"})
    out = _run(tmp_path, "src/gate_keeper/models.py")
    # Covered by manifest edge → pass
    assert out.status == "pass"
    assert "covering_edge" in _evidence_kinds(out)


def test_absent_exemption_file_uncovered_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Absent exemption file + uncovered changed file → FAIL."""
    _seed_repo(tmp_path)  # no exemptions_text
    (tmp_path / "extra.py").write_text("# extra\n", encoding="utf-8")
    _patch_changed(monkeypatch, {"extra.py"})
    out = _run(tmp_path, "extra.py")
    assert out.status == "fail"
    assert "uncovered_file" in _evidence_kinds(out)


# ---------------------------------------------------------------------------
# Exemption coverage — manifest covered takes priority over exemption
# ---------------------------------------------------------------------------


def test_manifest_coverage_takes_priority_over_exemption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When a file has both a manifest edge and an exemption, covering_edge wins."""
    exemptions = dedent(
        """
        exemptions:
          - path: src/gate_keeper/models.py
            category: manual
            reason: "redundant exemption"
        """
    ).lstrip()
    _seed_repo(tmp_path, exemptions_text=exemptions)
    _patch_changed(monkeypatch, {"src/gate_keeper/models.py"})
    out = _run(tmp_path, "src/gate_keeper/models.py")
    assert out.status == "pass"
    # covering_edge wins; exemption_applied is not emitted
    assert "covering_edge" in _evidence_kinds(out)
    assert "exemption_applied" not in _evidence_kinds(out)


# ---------------------------------------------------------------------------
# Diagnostic round-trip
# ---------------------------------------------------------------------------


def test_diagnostic_json_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Validator output is valid JSON and round-trips through json.loads."""
    _seed_repo(tmp_path)
    (tmp_path / "uncovered.py").write_text("# uncovered\n", encoding="utf-8")
    _patch_changed(monkeypatch, {"uncovered.py"})
    out = _run(tmp_path, "uncovered.py")

    import io

    buf = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = buf
    try:
        validator._emit(out)
    finally:
        sys.stdout = original_stdout

    raw = buf.getvalue()
    parsed = json.loads(raw)
    assert parsed["status"] == "fail"
    assert isinstance(parsed["evidence"], list)
    assert len(parsed["evidence"]) >= 1
    assert parsed["evidence"][0]["kind"] == "uncovered_file"


# ---------------------------------------------------------------------------
# Integration test — real subprocess via CommandAdapter
# ---------------------------------------------------------------------------


def _git_init_and_commit(root: Path, env: dict) -> str:
    """Init a bare git repo, commit all existing files, return HEAD sha."""
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)  # noqa: S603,S607
    subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True, check=True)  # noqa: S603,S607
    subprocess.run(  # noqa: S603,S607
        ["git", "commit", "--allow-empty", "-m", "initial"],
        cwd=str(root),
        env=env,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(  # noqa: S603,S607
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.integration
def test_integration_covered_file(tmp_path: Path):
    """Validator returns pass (unaffected) when the target is not in the diff."""
    import os

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    _seed_repo(tmp_path)
    _git_init_and_commit(tmp_path, git_env)

    # GATE_KEEPER_BASE_REF=HEAD → git diff HEAD...HEAD is empty, nothing changed.
    env = {**git_env, "GATE_KEEPER_BASE_REF": "HEAD"}

    payload = json.dumps(
        {
            "rule": {
                "id": "manifest-coverage",
                "kind": "external_check",
                "params": {
                    "tool": "command",
                    "manifest": str(tmp_path / ".gate-keeper" / "dependency-manifest.yml"),
                    "exemptions": str(tmp_path / ".gate-keeper" / "ref-exemptions.yml"),
                },
            },
            "target": "src/gate_keeper/models.py",
        }
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(VALIDATOR_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    # HEAD diff is empty → unaffected → pass
    assert out["status"] == "pass"
    kinds = [e["kind"] for e in out["evidence"]]
    assert "dependent_artifact_unaffected" in kinds


@pytest.mark.integration
def test_integration_uncovered_changed_file(tmp_path: Path):
    """Validator returns fail for an uncovered file that appears in the diff."""
    import os

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }

    _seed_repo(tmp_path)
    base_sha = _git_init_and_commit(tmp_path, git_env)

    # Add an uncovered file and commit it so it shows in the diff.
    uncovered = tmp_path / "extra_uncovered.py"
    uncovered.write_text("# uncovered\n", encoding="utf-8")
    subprocess.run(["git", "add", str(uncovered)], cwd=str(tmp_path), capture_output=True, check=True)  # noqa: S603,S607
    subprocess.run(  # noqa: S603,S607
        ["git", "commit", "-m", "add uncovered file"],
        cwd=str(tmp_path),
        env=git_env,
        capture_output=True,
        check=True,
    )

    # base_sha...HEAD now includes extra_uncovered.py.
    env = {**git_env, "GATE_KEEPER_BASE_REF": base_sha}

    payload = json.dumps(
        {
            "rule": {
                "id": "manifest-coverage",
                "kind": "external_check",
                "params": {
                    "tool": "command",
                    "manifest": str(tmp_path / ".gate-keeper" / "dependency-manifest.yml"),
                    "exemptions": str(tmp_path / ".gate-keeper" / "ref-exemptions.yml"),
                },
            },
            "target": "extra_uncovered.py",
        }
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(VALIDATOR_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert out["status"] == "fail"
    kinds = [e["kind"] for e in out["evidence"]]
    assert "uncovered_file" in kinds
