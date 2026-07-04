"""Engine-level tests for per-rule target scope (issue #279 — S3).

Exercises the validator dispatch loop (both sequential and concurrent) with
rules carrying ``params.target_scope``: each rule dispatches against its own
effective set, empty / invalid / over-cap scopes short-circuit to the ratified
evidence vocabulary (``docs/design/multi-target.md`` §9), unscoped rules are
byte-identical, and the #178 target-kind precheck is untouched (§9.7).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import gate_keeper.targets as targets_mod
from gate_keeper import backends as registry
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    Rule,
    RuleKind,
    RuleSet,
    Severity,
    SourceLocation,
    Status,
    TargetKind,
)
from gate_keeper.targets import TargetSpec
from gate_keeper.validator import validate


def _write(p: Path, content: str = "x\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _rule(
    rule_id: str,
    *,
    params: dict | None = None,
    backend_hint: Backend = Backend.FILESYSTEM,
    kind: RuleKind = RuleKind.FILE_EXISTS,
    target_kind: TargetKind = TargetKind.UNSPECIFIED,
) -> Rule:
    return Rule(
        id=rule_id,
        title=f"rule {rule_id}",
        source=SourceLocation(path="rules.md", line=1),
        text="text",
        kind=kind,
        severity=Severity.ERROR,
        backend_hint=backend_hint,
        confidence=Confidence.HIGH,
        params=params or {},
        target_kind=target_kind,
    )


def _tree(root: Path) -> dict[str, Path]:
    return {
        "docs/a.md": _write(root / "docs" / "a.md"),
        "src/x.py": _write(root / "src" / "x.py"),
        "src/y.py": _write(root / "src" / "y.py"),
    }


def _candidate_spec(files: dict[str, Path]) -> TargetSpec:
    paths = sorted(files.values(), key=os.fspath)
    return TargetSpec(paths=paths, raw_targets=["."], is_multi=True)


class _Recorder:
    """Registry stub that records the target each rule was dispatched against."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def __call__(self, rule: Rule, target: object) -> Diagnostic:
        self.calls.append((rule.id, target))
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.FILESYSTEM,
            status=Status.PASS,
            severity=rule.severity,
            message="stub pass",
            evidence=[],
        )


def _install_recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setitem(registry._REGISTRY, "filesystem", rec)
    return rec


def _evidence_of(diag: Diagnostic, kind: str):
    return next((e for e in diag.evidence if e.kind == kind), None)


# ---------------------------------------------------------------------------
# AC1 — two scopes evaluate disjoint subsets; effective set recorded
# ---------------------------------------------------------------------------


class TestDisjointSubsets:
    @pytest.mark.parametrize("concurrency", [1, 3])
    def test_two_scopes_evaluate_only_their_subset(self, tmp_path, monkeypatch, concurrency):
        files = _tree(tmp_path)
        rec = _install_recorder(monkeypatch)
        docs_rule = _rule("docs-rule", params={"target_scope": ["docs/**/*.md"]})
        src_rule = _rule("src-rule", params={"target_scope": ["src/**/*.py"]})
        report = validate(
            RuleSet(rules=[docs_rule, src_rule]),
            _candidate_spec(files),
            repo_root=tmp_path,
            concurrency=concurrency,
        )

        dispatched = {rid: t for rid, t in rec.calls}
        # docs-rule saw only the docs file (single-file spec unwrapped by caller
        # is still a TargetSpec here since the stub receives it directly).
        docs_target = dispatched["docs-rule"]
        assert isinstance(docs_target, TargetSpec)
        assert [p.name for p in docs_target.paths] == ["a.md"]
        src_target = dispatched["src-rule"]
        assert isinstance(src_target, TargetSpec)
        assert sorted(p.name for p in src_target.paths) == ["x.py", "y.py"]

        by_id = {d.rule_id: d for d in report.diagnostics}
        docs_ev = _evidence_of(by_id["docs-rule"], "scope_effective_set")
        assert docs_ev is not None
        assert docs_ev.data["effective_paths"] == ["docs/a.md"]
        src_ev = _evidence_of(by_id["src-rule"], "scope_effective_set")
        assert src_ev is not None
        assert src_ev.data["effective_paths"] == ["src/x.py", "src/y.py"]

    def test_report_order_preserved(self, tmp_path, monkeypatch):
        files = _tree(tmp_path)
        _install_recorder(monkeypatch)
        rules = [
            _rule("r1", params={"target_scope": ["src/**/*.py"]}),
            _rule("r2", params={"target_scope": ["docs/**/*.md"]}),
            _rule("r3"),
        ]
        report = validate(RuleSet(rules=rules), _candidate_spec(files), repo_root=tmp_path, concurrency=2)
        assert [d.rule_id for d in report.diagnostics] == ["r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# AC3 — empty effective set → scope_empty PASS (not a run abort)
# ---------------------------------------------------------------------------


class TestScopeEmpty:
    @pytest.mark.parametrize("concurrency", [1, 2])
    def test_empty_intersection_passes_with_evidence(self, tmp_path, monkeypatch, concurrency):
        files = _tree(tmp_path)
        rec = _install_recorder(monkeypatch)
        # Candidate pool has no docs, so a docs scope intersects nothing.
        candidate = TargetSpec(
            paths=sorted([files["src/x.py"], files["src/y.py"]], key=os.fspath),
            raw_targets=["src"],
            is_multi=True,
        )
        rule = _rule("docs-rule", params={"target_scope": ["docs/**/*.md"]})
        report = validate(RuleSet(rules=[rule]), candidate, repo_root=tmp_path, concurrency=concurrency)
        diag = report.diagnostics[0]
        assert diag.status is Status.PASS
        ev = _evidence_of(diag, "scope_empty")
        assert ev is not None
        assert ev.data["effective_size"] == 0
        assert ev.data["scope_size"] == 1
        # No backend call was made for a scope_empty short-circuit.
        assert rec.calls == []


# ---------------------------------------------------------------------------
# scope_invalid — malformed / zero-match scope → UNAVAILABLE
# ---------------------------------------------------------------------------


class TestScopeInvalid:
    def test_malformed_scope_unavailable(self, tmp_path, monkeypatch):
        files = _tree(tmp_path)
        rec = _install_recorder(monkeypatch)
        rule = _rule("bad", params={"target_scope": []})
        report = validate(RuleSet(rules=[rule]), _candidate_spec(files), repo_root=tmp_path)
        diag = report.diagnostics[0]
        assert diag.status is Status.UNAVAILABLE
        assert _evidence_of(diag, "scope_invalid") is not None
        assert rec.calls == []

    def test_zero_match_scope_unavailable(self, tmp_path, monkeypatch):
        files = _tree(tmp_path)
        _install_recorder(monkeypatch)
        rule = _rule("bad", params={"target_scope": ["nope/**/*.rb"]})
        report = validate(RuleSet(rules=[rule]), _candidate_spec(files), repo_root=tmp_path)
        assert report.diagnostics[0].status is Status.UNAVAILABLE
        assert _evidence_of(report.diagnostics[0], "scope_invalid") is not None


# ---------------------------------------------------------------------------
# AC4 — per-rule file-limit breach is per-rule, not a run abort
# ---------------------------------------------------------------------------


class TestPerRuleFileLimit:
    @pytest.mark.parametrize("concurrency", [1, 2])
    def test_over_broad_rule_isolated(self, tmp_path, monkeypatch, concurrency):
        files = _tree(tmp_path)
        rec = _install_recorder(monkeypatch)
        monkeypatch.setattr(targets_mod, "DEFAULT_FILE_LIMIT", 1)
        broad = _rule("broad", params={"target_scope": ["src/**/*.py"]})  # 2 files > cap 1
        narrow = _rule("narrow", params={"target_scope": ["docs/**/*.md"]})  # 1 file == cap
        report = validate(
            RuleSet(rules=[broad, narrow]),
            _candidate_spec(files),
            repo_root=tmp_path,
            concurrency=concurrency,
        )
        by_id = {d.rule_id: d for d in report.diagnostics}
        assert by_id["broad"].status is Status.UNAVAILABLE
        ev = _evidence_of(by_id["broad"], "scope_file_limit_exceeded")
        assert ev is not None
        assert ev.data["effective_size"] == 2
        assert ev.data["file_limit"] == 1
        # The narrow rule still evaluated normally — the over-broad rule did not
        # sink the whole run.
        assert by_id["narrow"].status is Status.PASS
        assert {rid for rid, _ in rec.calls} == {"narrow"}


# ---------------------------------------------------------------------------
# AC5 — unscoped rules dispatch byte-for-byte against the run-level target
# ---------------------------------------------------------------------------


class TestUnscopedByteIdentical:
    def test_unscoped_rule_gets_run_level_target(self, tmp_path, monkeypatch):
        files = _tree(tmp_path)
        rec = _install_recorder(monkeypatch)
        candidate = _candidate_spec(files)
        rule = _rule("plain")  # no target_scope
        report = validate(RuleSet(rules=[rule]), candidate, repo_root=tmp_path)
        # Dispatched against the exact run-level TargetSpec, no scope evidence.
        assert rec.calls == [("plain", candidate)]
        assert _evidence_of(report.diagnostics[0], "scope_effective_set") is None

    def test_unscoped_rule_with_string_target(self, tmp_path, monkeypatch):
        rec = _install_recorder(monkeypatch)
        f = _write(tmp_path / "one.md")
        rule = _rule("plain")
        validate(RuleSet(rules=[rule]), str(f), repo_root=tmp_path)
        assert rec.calls == [("plain", str(f))]


# ---------------------------------------------------------------------------
# §9.7 / §9.10 — precheck untouched; llm-rubric multi-file stays unsupported
# ---------------------------------------------------------------------------


class TestInteractions:
    def test_target_kind_precheck_wins_over_scope(self, tmp_path):
        files = _tree(tmp_path)
        rule = _rule(
            "mismatch",
            backend_hint=Backend.LLM_RUBRIC,
            kind=RuleKind.SEMANTIC_RUBRIC,
            target_kind=TargetKind.COMMIT_MESSAGE,
            params={"target_scope": ["docs/**/*.md"]},
        )
        report = validate(
            RuleSet(rules=[rule]),
            _candidate_spec(files),
            repo_root=tmp_path,
            artifact_kind=TargetKind.PR_DESCRIPTION,
        )
        diag = report.diagnostics[0]
        # §9.7: the run-level target-kind precheck short-circuits before scope.
        assert diag.status is Status.UNSUPPORTED
        assert _evidence_of(diag, "target_kind_mismatch") is not None
        assert _evidence_of(diag, "scope_effective_set") is None

    def test_llm_rubric_multi_file_effective_set_stays_unsupported(self, tmp_path):
        files = _tree(tmp_path)
        rule = _rule(
            "semantic",
            backend_hint=Backend.LLM_RUBRIC,
            kind=RuleKind.SEMANTIC_RUBRIC,
            params={"target_scope": ["src/**/*.py"]},  # 2 files → multi
        )
        report = validate(RuleSet(rules=[rule]), _candidate_spec(files), repo_root=tmp_path)
        diag = report.diagnostics[0]
        # §9.10: a multi-file effective set routed to llm-rubric is still
        # multi_target_unsupported until S5 (#281).
        assert diag.status is Status.UNSUPPORTED
        assert _evidence_of(diag, "multi_target_unsupported") is not None
        # The scope_effective_set evidence still rides along for auditability.
        assert _evidence_of(diag, "scope_effective_set") is not None

    def test_pr_reference_target_dispatches_scoped_rule_verbatim(self, tmp_path, monkeypatch):
        # A non-filesystem run target (candidate pool is None): a scoped rule
        # cannot intersect and is dispatched verbatim rather than silently passed.
        rec = _Recorder()
        monkeypatch.setitem(registry._REGISTRY, "github", rec)
        rule = _rule(
            "gh",
            backend_hint=Backend.GITHUB,
            kind=RuleKind.GITHUB_PR_OPEN,
            params={"target_scope": ["docs/**/*.md"]},
        )
        validate(RuleSet(rules=[rule]), "owner/repo#1", repo_root=tmp_path)
        assert rec.calls == [("gh", "owner/repo#1")]


class TestScopeEvidenceOnError:
    @pytest.mark.parametrize("concurrency", [1, 3])
    def test_scope_evidence_survives_backend_exception(self, tmp_path, monkeypatch, concurrency):
        # A scoped rule whose backend raises must still carry scope_effective_set
        # evidence on the resulting ERROR diagnostic, in both the sequential and
        # concurrent paths (codex P2 review on #289).
        files = _tree(tmp_path)

        def _boom(rule, target):
            raise RuntimeError("backend crash")

        monkeypatch.setitem(registry._REGISTRY, "filesystem", _boom)
        rule = _rule("scoped", params={"target_scope": ["src/**/*.py"]})
        report = validate(
            RuleSet(rules=[rule]),
            _candidate_spec(files),
            repo_root=tmp_path,
            concurrency=concurrency,
        )
        diag = report.diagnostics[0]
        assert diag.status is Status.ERROR
        ev = _evidence_of(diag, "scope_effective_set")
        assert ev is not None
        assert ev.data["effective_paths"] == ["src/x.py", "src/y.py"]


class TestIrRoundTrip:
    def test_target_scope_survives_ir_round_trip(self):
        # target_scope rides inside params (an opaque passthrough dict), so the
        # IR schema is unchanged and --rules-format ir preserves it (§9.9, AC5).
        rule = _rule("scoped", params={"target_scope": ["docs/**/*.md"], "pattern": "x"})
        restored = Rule.from_dict(rule.to_dict())
        assert restored.params["target_scope"] == ["docs/**/*.md"]
        assert restored.params == rule.params
