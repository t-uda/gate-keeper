"""Tests for the ``changed_file_policy`` rule kind (issue #230).

This rule kind evaluates *only changed files* — from a GitHub PR or from a
local git working tree — against a YAML policy manifest.  The manifest schema
mirrors a strict subset of
``spread-applicant-ai/policy-manifests/path-rules.yaml``.

Coverage areas (per #230 done criteria):

- GitHub PR mode: pass / fail on forbidden changed paths.
- Local git modes: ``staged``, ``unstaged``, ``staged_and_unstaged``,
  ``untracked``, ``all``.
- Manifest exceptions: a ``known_allowed_exception`` entry must exempt a
  matching file from a corresponding forbidden pattern.
- Malformed manifest fails closed.
- Fail-closed on unknown manifest fields or kinds.
- Diagnostics identify the exact manifest entry that caused each finding.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from gate_keeper.backends import _gh, _target
from gate_keeper.backends import github as gh_backend
from gate_keeper.models import (
    Backend,
    Confidence,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_RESOLVE_OK = json.dumps({"number": 42, "url": "https://github.com/owner/repo/pull/42"})


def _ok(stdout: str) -> _gh.GhResult:
    return _gh.GhResult(ok=True, stdout=stdout, stderr="", returncode=0, cmd=("gh",))


def _make_run_gh_sequence(results):
    queue = list(results)

    def _fake(args, **kwargs):
        if queue:
            return queue.pop(0)
        raise AssertionError(f"run_gh called more times than expected; args={args!r}")

    return _fake


def _patch_with_pages(monkeypatch, resolve_result, pages):
    monkeypatch.setattr(_target, "run_gh", _make_run_gh_sequence([resolve_result]))
    monkeypatch.setattr(gh_backend, "run_gh", _make_run_gh_sequence(pages))


def _files_response(paths, *, has_next_page=False, end_cursor=None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "files": {
                            "nodes": [{"path": p} for p in paths],
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                        }
                    }
                }
            }
        }
    )


def _make_rule(
    *,
    manifest_path: str,
    changed_files_source: str = "github_pr",
    local_git_mode: str | None = None,
    repo_root: str | None = None,
    omit_manifest_path: bool = False,
    omit_source: bool = False,
) -> Rule:
    params: dict = {}
    if not omit_manifest_path:
        params["manifest_path"] = manifest_path
    if not omit_source:
        params["changed_files_source"] = changed_files_source
    if local_git_mode is not None:
        params["local_git_mode"] = local_git_mode
    if repo_root is not None:
        params["repo_root"] = repo_root
    return Rule(
        id="rule-cf-policy",
        title="Real-data intake policy",
        source=SourceLocation(path="rules.md", line=1),
        text="PRs must not change forbidden manifest paths.",
        kind=RuleKind.CHANGED_FILE_POLICY,
        severity=Severity.ERROR,
        backend_hint=Backend.GITHUB,
        confidence=Confidence.HIGH,
        params=params,
    )


# Modeled on the spread-applicant-ai/policy-manifests/path-rules.yaml schema
# but kept minimal and self-contained for hermetic tests.
_SAMPLE_MANIFEST = dedent(
    """\
    version: 1
    source_documents:
      - docs/policy.md
    allowed_kinds:
      - forbidden_path_pattern
      - known_allowed_exception
      - generated_output_extension
      - authored_text_extension
    allowed_stop_conditions:
      - fail_closed_never_commit
      - refuse_and_surface
    entries:
      - kind: forbidden_path_pattern
        pattern: "source/official/**/*.pdf"
        stop_condition: fail_closed_never_commit
        note: Generated submission PDFs must not be tracked publicly.
        source: docs/policy.md#3
      - kind: forbidden_path_pattern
        pattern: "outputs/**/*.xlsx"
        stop_condition: fail_closed_never_commit
        source: docs/policy.md#3
      - kind: forbidden_path_pattern
        pattern: "context/researcher/raw/**"
        stop_condition: fail_closed_never_commit
        note: Raw researcher partition never tracked publicly.
        source: docs/policy.md#5
      - kind: forbidden_path_pattern
        pattern: "**/.env"
        stop_condition: fail_closed_never_commit
        source: docs/policy.md#2.5
      - kind: generated_output_extension
        extension: .xlsx
        source: docs/policy.md
      - kind: authored_text_extension
        extension: .md
        source: docs/policy.md
    known_allowed_exceptions_note: >-
      No cross-layer exceptions are approved at issuance.
    default_unknown_extension:
      treatment: fail_closed_private
      source: docs/policy.md#6
    default_unknown_path:
      stop_condition: fail_closed_never_commit
      source: docs/policy.md#6
    """
)


@pytest.fixture
def manifest_file(tmp_path: Path) -> Path:
    """Write the sample manifest to a temp file and return its path."""
    p = tmp_path / "path-rules.yaml"
    p.write_text(_SAMPLE_MANIFEST, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Params validation
# ---------------------------------------------------------------------------


class TestParamsValidation:
    def test_missing_manifest_path_unavailable(self, monkeypatch):
        rule = _make_rule(manifest_path="x", omit_manifest_path=True)
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data["missing"] == "manifest_path"

    def test_missing_source_unavailable(self, monkeypatch):
        rule = _make_rule(manifest_path="x", omit_source=True)
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data["missing"] == "changed_files_source"

    def test_invalid_source_unavailable(self, monkeypatch, manifest_file):
        rule = _make_rule(
            manifest_path=str(manifest_file),
            changed_files_source="invalid",
        )
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data["field"] == "changed_files_source"

    def test_invalid_local_git_mode_unavailable(self, tmp_path):
        rule = _make_rule(
            manifest_path="x",
            changed_files_source="local_git",
            local_git_mode="bogus",
            repo_root=str(tmp_path),
        )
        diag = gh_backend.check(rule, str(tmp_path))
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "params_error"
        assert diag.evidence[0].data["field"] == "local_git_mode"


# ---------------------------------------------------------------------------
# Manifest parser — fail-closed on malformed or unknown content
# ---------------------------------------------------------------------------


class TestManifestParser:
    def test_missing_manifest_file_unavailable(self, monkeypatch, tmp_path):
        bogus = tmp_path / "does-not-exist.yaml"
        rule = _make_rule(manifest_path=str(bogus))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "manifest_error"
        assert "not found" in diag.evidence[0].data["reason"]

    def test_malformed_yaml_unavailable(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("entries: [unterminated\n", encoding="utf-8")
        rule = _make_rule(manifest_path=str(bad))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "manifest_error"
        assert "YAML parse error" in diag.evidence[0].data["reason"]

    def test_unsupported_version_unavailable(self, monkeypatch, tmp_path):
        p = tmp_path / "wrong-version.yaml"
        p.write_text("version: 2\nentries: []\n", encoding="utf-8")
        rule = _make_rule(manifest_path=str(p))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "manifest_error"
        assert diag.evidence[0].data["version"] == 2

    def test_missing_entries_unavailable(self, monkeypatch, tmp_path):
        p = tmp_path / "no-entries.yaml"
        p.write_text("version: 1\n", encoding="utf-8")
        rule = _make_rule(manifest_path=str(p))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "manifest_error"
        assert "entries" in diag.evidence[0].data["reason"]

    def test_unknown_top_level_key_unavailable(self, monkeypatch, tmp_path):
        p = tmp_path / "unknown-top.yaml"
        p.write_text(
            "version: 1\nentries: []\nmysterious_key: 7\n",
            encoding="utf-8",
        )
        rule = _make_rule(manifest_path=str(p))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "manifest_error"
        assert "mysterious_key" in diag.evidence[0].data["unknown_keys"]

    def test_unknown_entry_kind_unavailable(self, monkeypatch, tmp_path):
        p = tmp_path / "bad-kind.yaml"
        p.write_text(
            dedent(
                """\
                version: 1
                entries:
                  - kind: invented_kind
                    pattern: "x/**"
                """
            ),
            encoding="utf-8",
        )
        rule = _make_rule(manifest_path=str(p))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "manifest_error"
        assert diag.evidence[0].data["kind"] == "invented_kind"

    def test_entry_missing_required_field_unavailable(self, monkeypatch, tmp_path):
        p = tmp_path / "missing-fields.yaml"
        p.write_text(
            dedent(
                """\
                version: 1
                entries:
                  - kind: forbidden_path_pattern
                    pattern: "outputs/**"
                """
            ),
            encoding="utf-8",
        )
        rule = _make_rule(manifest_path=str(p))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "manifest_error"
        # stop_condition and source are required for forbidden_path_pattern.
        missing = ev.data["missing_fields"]
        assert "stop_condition" in missing
        assert "source" in missing

    def test_entry_unknown_field_unavailable(self, monkeypatch, tmp_path):
        p = tmp_path / "extra-field.yaml"
        p.write_text(
            dedent(
                """\
                version: 1
                entries:
                  - kind: forbidden_path_pattern
                    pattern: "outputs/**"
                    stop_condition: fail_closed_never_commit
                    source: docs/policy.md
                    bonus_field: 7
                """
            ),
            encoding="utf-8",
        )
        rule = _make_rule(manifest_path=str(p))
        _patch_with_pages(monkeypatch, _ok(_RESOLVE_OK), [])
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.UNAVAILABLE
        ev = diag.evidence[0]
        assert ev.kind == "manifest_error"
        assert "bonus_field" in ev.data["unknown_fields"]


# ---------------------------------------------------------------------------
# GitHub PR mode — pass and fail paths via the existing PR file-list machinery
# ---------------------------------------------------------------------------


class TestGitHubPrMode:
    def test_pass_when_no_changed_files_match(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["source/official/source-index.yaml", "README.md"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.PASS
        ev = diag.evidence[0]
        assert ev.kind == "changed_file_policy"
        assert ev.data["total_changed_files"] == 2
        assert ev.data["violations"] == []
        assert ev.data["source"].startswith("github_pr:")

    def test_fail_on_forbidden_pdf(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["source/official/call.pdf", "README.md"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        ev = diag.evidence[0]
        violations = ev.data["violations"]
        assert len(violations) == 1
        v = violations[0]
        assert v["path"] == "source/official/call.pdf"
        assert v["matched_pattern"] == "source/official/**/*.pdf"
        assert v["stop_condition"] == "fail_closed_never_commit"
        assert v["manifest_source"] == "docs/policy.md#3"
        # Diagnostic carries remediation text identifying the file.
        assert diag.remediation is not None
        assert "source/official/call.pdf" in diag.remediation

    def test_fail_on_forbidden_xlsx(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["outputs/foo.xlsx"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        v = diag.evidence[0].data["violations"][0]
        assert v["path"] == "outputs/foo.xlsx"
        assert v["matched_pattern"] == "outputs/**/*.xlsx"

    def test_fail_on_researcher_raw(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["context/researcher/raw/interview.md"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        v = diag.evidence[0].data["violations"][0]
        assert v["path"] == "context/researcher/raw/interview.md"
        assert v["matched_pattern"] == "context/researcher/raw/**"

    def test_fail_on_dotenv(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response([".env"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        v = diag.evidence[0].data["violations"][0]
        assert v["path"] == ".env"
        assert v["matched_pattern"] == "**/.env"

    def test_multiple_violations_collected(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [
                _ok(
                    _files_response(
                        [
                            "source/official/call.pdf",
                            "outputs/foo.xlsx",
                            "README.md",
                        ]
                    )
                )
            ],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert paths == {"source/official/call.pdf", "outputs/foo.xlsx"}


# ---------------------------------------------------------------------------
# Manifest exceptions — known_allowed_exception entries exempt matching files
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_exception_exempts_matching_file(self, monkeypatch, tmp_path):
        manifest = tmp_path / "with-exception.yaml"
        manifest.write_text(
            dedent(
                """\
                version: 1
                entries:
                  - kind: forbidden_path_pattern
                    pattern: "outputs/**/*.xlsx"
                    stop_condition: fail_closed_never_commit
                    source: docs/policy.md
                  - kind: known_allowed_exception
                    pattern: "outputs/sample.xlsx"
                    exempts_pattern: "outputs/**/*.xlsx"
                    authorising_issue: "#42"
                    source: docs/policy.md
                """
            ),
            encoding="utf-8",
        )
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["outputs/sample.xlsx", "outputs/other.xlsx"]))],
        )
        rule = _make_rule(manifest_path=str(manifest))
        diag = gh_backend.check(rule, "owner/repo#42")
        assert diag.status is Status.FAIL
        # ``outputs/sample.xlsx`` must NOT appear in violations (exempted).
        # ``outputs/other.xlsx`` must.
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert paths == {"outputs/other.xlsx"}


# ---------------------------------------------------------------------------
# Local git mode — staged, unstaged, untracked, all
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    """Initialise a hermetic local git repo at *repo* with a baseline commit."""
    subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "commit.gpgsign", "false"],
        check=True,
        capture_output=True,
    )
    # Baseline file so subsequent diffs are well-defined.
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )


class TestLocalGitMode:
    def test_local_staged_fail_on_forbidden(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        # Stage a forbidden output.
        (repo / "outputs").mkdir()
        forbidden = repo / "outputs" / "foo.xlsx"
        forbidden.write_text("garbage", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "outputs/foo.xlsx"], check=True, capture_output=True)

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="staged",
            repo_root=str(repo),
        )
        diag = gh_backend.check(rule, str(repo))
        assert diag.status is Status.FAIL
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert "outputs/foo.xlsx" in paths
        assert diag.evidence[0].data["source"] == "local_git:staged"

    def test_local_staged_pass_on_clean(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        # Stage an allowed file.
        allowed = repo / "source"
        allowed.mkdir()
        (allowed / "official").mkdir()
        (allowed / "official" / "source-index.yaml").write_text("x: 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "source/official/source-index.yaml"],
            check=True,
            capture_output=True,
        )

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="staged",
            repo_root=str(repo),
        )
        diag = gh_backend.check(rule, str(repo))
        assert diag.status is Status.PASS
        assert diag.evidence[0].data["violations"] == []

    def test_local_untracked_fail_on_forbidden(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        # Create an untracked .env file (not staged).
        (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="untracked",
            repo_root=str(repo),
        )
        diag = gh_backend.check(rule, str(repo))
        assert diag.status is Status.FAIL
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert ".env" in paths

    def test_local_staged_and_unstaged_picks_up_both(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        # Staged: outputs/a.xlsx ; Unstaged: edit README.md
        (repo / "outputs").mkdir()
        (repo / "outputs" / "a.xlsx").write_text("g", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "outputs/a.xlsx"], check=True, capture_output=True)
        # Modify README (unstaged).
        (repo / "README.md").write_text("changed\n", encoding="utf-8")

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="staged_and_unstaged",
            repo_root=str(repo),
        )
        diag = gh_backend.check(rule, str(repo))
        assert diag.status is Status.FAIL
        # README.md unmodified by policy; outputs/a.xlsx is forbidden.
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert paths == {"outputs/a.xlsx"}
        assert diag.evidence[0].data["total_changed_files"] == 2

    def test_local_all_includes_untracked(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        # Untracked forbidden file.
        (repo / "context").mkdir()
        (repo / "context" / "researcher").mkdir()
        (repo / "context" / "researcher" / "raw").mkdir()
        (repo / "context" / "researcher" / "raw" / "interview.md").write_text("notes", encoding="utf-8")

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="all",
            repo_root=str(repo),
        )
        diag = gh_backend.check(rule, str(repo))
        assert diag.status is Status.FAIL
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert "context/researcher/raw/interview.md" in paths

    def test_local_git_uses_target_as_repo_root_when_param_absent(self, tmp_path: Path):
        """When ``repo_root`` param is absent the target string is used."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "outputs").mkdir()
        (repo / "outputs" / "b.xlsx").write_text("g", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "outputs/b.xlsx"], check=True, capture_output=True)

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="staged",
        )
        diag = gh_backend.check(rule, str(repo))
        assert diag.status is Status.FAIL
        paths = {v["path"] for v in diag.evidence[0].data["violations"]}
        assert "outputs/b.xlsx" in paths

    def test_local_git_failure_unavailable(self, tmp_path: Path):
        """A non-git directory must surface as UNAVAILABLE, not crash."""
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()

        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(_SAMPLE_MANIFEST, encoding="utf-8")

        rule = _make_rule(
            manifest_path=str(manifest),
            changed_files_source="local_git",
            local_git_mode="staged",
            repo_root=str(not_a_repo),
        )
        diag = gh_backend.check(rule, str(not_a_repo))
        assert diag.status is Status.UNAVAILABLE
        assert diag.evidence[0].kind == "local_git_error"


# ---------------------------------------------------------------------------
# Diagnostic shape — round-trip, evidence keys
# ---------------------------------------------------------------------------


class TestDiagnosticShape:
    def test_evidence_carries_manifest_metadata(self, monkeypatch, manifest_file):
        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["source/official/call.pdf"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        ev = diag.evidence[0]
        assert ev.kind == "changed_file_policy"
        assert ev.data["manifest_path"] == str(manifest_file)
        assert ev.data["forbidden_pattern_count"] >= 1
        assert "violations" in ev.data
        v = ev.data["violations"][0]
        # The diagnostic must identify the exact manifest entry.
        assert {"path", "matched_pattern", "stop_condition", "manifest_source", "note"} <= set(v.keys())

    def test_diagnostic_round_trip(self, monkeypatch, manifest_file):
        from gate_keeper.models import Diagnostic

        _patch_with_pages(
            monkeypatch,
            _ok(_RESOLVE_OK),
            [_ok(_files_response(["outputs/foo.xlsx"]))],
        )
        rule = _make_rule(manifest_path=str(manifest_file))
        diag = gh_backend.check(rule, "owner/repo#42")
        rt = Diagnostic.from_dict(diag.to_dict())
        assert rt.status is Status.FAIL
        assert rt.evidence[0].kind == "changed_file_policy"
        assert rt.evidence[0].data["violations"][0]["path"] == "outputs/foo.xlsx"
