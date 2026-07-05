"""Tests for narrowed affected-context assembly on the llm-rubric backend (#281, S5).

Covers the six acceptance criteria from issue #281:

1. Scoped llm-rubric rule + ``--target-changed``-style candidate set over 3
   files → one provider call with per-file headers; ``affected_context``
   evidence lists the assembled set + token estimate.
2. Over-budget → deterministic lexicographic truncation; ``truncation_warning``
   names the omitted files.
3. Zero survivors after truncation → ``UNAVAILABLE`` with explanatory evidence
   (fail-closed), no provider call.
4. S2-B cache interplay: the assembled-content hash is the target content hash.
5. Mixed ``target_kind`` semantics (§9.12.3): the dynamic path assembles every
   in-scope file regardless of per-file kind; the run-level #178 precheck is the
   only kind gate.
6. A fixture demonstrating whole-closure vs narrowed token estimate for the same
   rule.

All provider calls are stubbed — no live API calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from gate_keeper.backends import eval_cache as cache_mod
from gate_keeper.backends import llm_rubric as llm_backend
from gate_keeper.models import (
    Backend,
    Confidence,
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

_ANTHROPIC_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "anthropic",
    "ANTHROPIC_API_KEY": "sk-ant-test",
}

#: A token every fixture file contains, so a stubbed pass quote grounds against
#: the assembled body regardless of which files survive truncation.
_SHARED_QUOTE = "SHARED_MARKER"


def _rule(
    *,
    rule_id: str = "r1",
    text: str = "All files agree on the shared marker.",
    params: dict[str, Any] | None = None,
    target_kind: TargetKind = TargetKind.UNSPECIFIED,
) -> Rule:
    return Rule(
        id=rule_id,
        title="Test rule",
        source=SourceLocation(path="rules.md", line=1),
        text=text,
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.WARNING,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params=params or {},
        target_kind=target_kind,
    )


class _RecordingProvider:
    """Stub provider that records every prompt and returns a grounded pass."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, api_key, system, user, model, **_kw):
        self.calls.append(user)
        payload = json.dumps(
            {
                "judgment": "pass",
                "primary_reason": "Marker present.",
                "supporting_evidence_quotes": [_SHARED_QUOTE],
                "suggested_action": None,
            }
        )
        return payload, {"latency_ms": 5, "tokens_in": 10, "tokens_out": 5}


def _patch_provider(monkeypatch) -> _RecordingProvider:
    prov = _RecordingProvider()
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: dict(_ANTHROPIC_ENV))
    monkeypatch.setattr(llm_backend, "_call_anthropic", prov)
    return prov


def _write_files(root: Path, names_to_body: dict[str, str]) -> list[Path]:
    paths = []
    for name, body in names_to_body.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        paths.append(p)
    return sorted(paths, key=os.fspath)


def _multi_spec(paths: list[Path]) -> TargetSpec:
    return TargetSpec(
        paths=sorted(paths, key=os.fspath),
        raw_targets=[str(p) for p in paths],
        is_multi=True,
    )


def _evidence(diag, kind):
    for ev in diag.evidence:
        if ev.kind == kind:
            return ev
    return None


def _require(diag, kind):
    ev = _evidence(diag, kind)
    assert ev is not None, f"expected {kind!r} evidence, got kinds {[e.kind for e in diag.evidence]}"
    return ev


# ---------------------------------------------------------------------------
# Criterion 1 — one provider call, per-file headers, assembled-set evidence
# ---------------------------------------------------------------------------


class TestCriterion1SingleCallPerFileHeaders:
    def test_three_files_one_call_with_headers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        paths = _write_files(
            tmp_path,
            {
                "src/a.py": f"# a\n{_SHARED_QUOTE}\n",
                "src/b.py": f"# b\n{_SHARED_QUOTE}\n",
                "src/c.py": f"# c\n{_SHARED_QUOTE}\n",
            },
        )
        report = validate(_make_ruleset(_rule()), _multi_spec(paths), backend="llm-rubric")
        diag = report.diagnostics[0]

        assert diag.status is Status.PASS
        # Exactly one provider call (single strategy), not one-per-file.
        assert len(prov.calls) == 1
        prompt = prov.calls[0]
        # Per-file headers for every assembled file.
        for label in ("src/a.py", "src/b.py", "src/c.py"):
            assert f"--- {label} ---" in prompt

        ev = _evidence(diag, "affected_context")
        assert ev is not None
        assert ev.data["included_files"] == ["src/a.py", "src/b.py", "src/c.py"]
        assert ev.data["included_count"] == 3
        assert ev.data["truncated"] is False
        assert ev.data["tokens_estimated"] > 0
        assert _evidence(diag, "truncation_warning") is None

    def test_via_scope_and_changed_candidate_set(self, tmp_path, monkeypatch):
        """Faithful to the criterion wording: a scoped rule over a changed
        candidate set of 3 files assembles them in a single call."""
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        paths = _write_files(
            tmp_path,
            {
                "src/a.py": f"{_SHARED_QUOTE}\n",
                "src/b.py": f"{_SHARED_QUOTE}\n",
                "src/c.py": f"{_SHARED_QUOTE}\n",
                "docs/readme.md": "unrelated\n",  # out of scope, excluded
            },
        )
        candidate = _multi_spec(paths)
        rule = _rule(params={"target_scope": ["src/**/*.py"]})
        report = validate(RuleSet(rules=[rule]), candidate, repo_root=tmp_path)
        diag = report.diagnostics[0]

        assert len(prov.calls) == 1
        ev = _evidence(diag, "affected_context")
        assert ev is not None
        # docs/readme.md is narrowed out by scope; only the 3 src files assemble.
        assert ev.data["included_files"] == ["src/a.py", "src/b.py", "src/c.py"]
        assert _evidence(diag, "scope_effective_set") is not None
        assert "docs/readme.md" not in prov.calls[0]


# ---------------------------------------------------------------------------
# Criterion 2 — deterministic lexicographic truncation with evidence
# ---------------------------------------------------------------------------


class TestCriterion2Truncation:
    def test_over_budget_truncates_lexicographic_prefix(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        # Each file body is ~80 chars → ~25 tokens plus header. A budget that
        # fits the first file (a.py) but not the second forces truncation.
        body = _SHARED_QUOTE + " " + ("x" * 60) + "\n"
        paths = _write_files(
            tmp_path,
            {"a.py": body, "b.py": body, "c.py": body},
        )
        # Budget large enough for exactly one file's block.
        one_block = llm_backend._render_affected_block("a.py", body)
        budget = llm_backend._estimate_tokens(len(one_block))
        rule = _rule(params={"token_budget": budget})
        report = validate(_make_ruleset(rule), _multi_spec(paths), backend="llm-rubric")
        diag = report.diagnostics[0]

        assert diag.status is Status.PASS
        ev = _require(diag, "affected_context")
        assert ev.data["included_files"] == ["a.py"]  # lexicographic prefix
        assert ev.data["omitted_files"] == ["b.py", "c.py"]
        assert ev.data["truncated"] is True

        warn = _evidence(diag, "truncation_warning")
        assert warn is not None
        assert warn.data["omitted_files"] == ["b.py", "c.py"]
        assert warn.data["omitted_count"] == 2
        assert warn.data["included_count"] == 1
        # Only the surviving file appears in the prompt.
        assert "--- a.py ---" in prov.calls[0]
        assert "--- b.py ---" not in prov.calls[0]

    def test_truncation_is_deterministic(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _patch_provider(monkeypatch)
        body = _SHARED_QUOTE + "\n" + ("y" * 40) + "\n"
        paths = _write_files(tmp_path, {"m.py": body, "n.py": body, "z.py": body})
        one_block = llm_backend._render_affected_block("m.py", body)
        budget = llm_backend._estimate_tokens(len(one_block) * 2)  # fits two blocks
        rule = _rule(params={"token_budget": budget})
        spec = _multi_spec(paths)
        first = validate(_make_ruleset(rule), spec, backend="llm-rubric").diagnostics[0]
        second = validate(_make_ruleset(rule), spec, backend="llm-rubric").diagnostics[0]
        assert (
            _require(first, "affected_context").data["included_files"]
            == _require(second, "affected_context").data["included_files"]
        )


# ---------------------------------------------------------------------------
# Criterion 3 — zero survivors → UNAVAILABLE, fail-closed, no provider call
# ---------------------------------------------------------------------------


class TestCriterion3ZeroSurvivors:
    def test_lead_file_over_budget_yields_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        paths = _write_files(
            tmp_path,
            {"a.py": f"{_SHARED_QUOTE} lots of content here\n" * 5, "b.py": "more\n"},
        )
        rule = _rule(params={"token_budget": 1})  # even the lead file is over budget
        report = validate(_make_ruleset(rule), _multi_spec(paths), backend="llm-rubric")
        diag = report.diagnostics[0]

        assert diag.status is Status.UNAVAILABLE
        ev = _evidence(diag, "affected_context_empty")
        assert ev is not None
        assert ev.data["candidate_count"] == 2
        assert ev.data["token_budget"] == 1
        # Fail-closed: no provider call was made for a set that could not assemble.
        assert prov.calls == []

    def test_all_unreadable_yields_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        # Paths that do not exist on disk → unreadable → excluded → zero survivors.
        spec = TargetSpec(
            paths=sorted([tmp_path / "gone-a.py", tmp_path / "gone-b.py"], key=os.fspath),
            raw_targets=["gone-a.py", "gone-b.py"],
            is_multi=True,
        )
        report = validate(_make_ruleset(_rule()), spec, backend="llm-rubric")
        diag = report.diagnostics[0]
        assert diag.status is Status.UNAVAILABLE
        ev = _evidence(diag, "affected_context_empty")
        assert ev is not None
        assert len(ev.data["unreadable_files"]) == 2
        assert prov.calls == []


# ---------------------------------------------------------------------------
# Criterion 4 — assembled-content hash is the cache target content hash
# ---------------------------------------------------------------------------


class TestCriterion4CacheKey:
    def test_entry_hashes_assembled_content(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: dict(_ANTHROPIC_ENV))
        paths = _write_files(tmp_path, {"a.py": "alpha\n", "b.py": "beta\n"})
        rule = _rule()
        spec = _multi_spec(paths)
        entries = cache_mod._build_target_entries(rule, spec, None)
        assert len(entries) == 1
        assert entries[0]["id"] == "__affected_context__"
        assembly = llm_backend._assemble_affected_context(spec, rule, env=dict(_ANTHROPIC_ENV))
        expected = cache_mod._sha256_hex(assembly.assembled_text)
        assert entries[0]["content_sha256"] == expected

    def test_content_edit_changes_key(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: dict(_ANTHROPIC_ENV))
        paths = _write_files(tmp_path, {"a.py": "alpha\n", "b.py": "beta\n"})
        rule = _rule()
        before = cache_mod._build_target_entries(rule, _multi_spec(paths), None)[0]["content_sha256"]
        (tmp_path / "a.py").write_text("alpha EDITED\n", encoding="utf-8")
        after = cache_mod._build_target_entries(rule, _multi_spec(paths), None)[0]["content_sha256"]
        assert before != after

    def test_truncation_change_changes_key(self, tmp_path, monkeypatch):
        """A budget change that alters which files survive must miss the cache."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: dict(_ANTHROPIC_ENV))
        body = "z" * 60 + "\n"
        paths = _write_files(tmp_path, {"a.py": body, "b.py": body})
        spec = _multi_spec(paths)
        full = cache_mod._build_target_entries(_rule(), spec, None)[0]["content_sha256"]
        one_block = llm_backend._render_affected_block("a.py", body)
        narrow_rule = _rule(params={"token_budget": llm_backend._estimate_tokens(len(one_block))})
        narrowed = cache_mod._build_target_entries(narrow_rule, spec, None)[0]["content_sha256"]
        assert full != narrowed

    def test_cache_hit_avoids_second_provider_call(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        paths = _write_files(tmp_path, {"a.py": f"{_SHARED_QUOTE}\n", "b.py": f"{_SHARED_QUOTE}\n"})
        rule = _rule()
        spec = _multi_spec(paths)
        cache_dir = tmp_path / "cache"

        # First run: miss → one provider call → stored.
        d1 = validate(_make_ruleset(rule), spec, backend="llm-rubric", eval_cache=True).diagnostics[0]
        assert d1.status is Status.PASS
        assert len(prov.calls) == 1

        # Second run: hit → zero additional provider calls, cache_hit evidence.
        d2 = validate(_make_ruleset(rule), spec, backend="llm-rubric", eval_cache=True).diagnostics[0]
        assert d2.status is Status.PASS
        assert len(prov.calls) == 1  # unchanged: served from cache
        assert _evidence(d2, "cache_hit") is not None
        assert _evidence(d2, "affected_context") is not None
        _ = cache_dir  # cache lives under cwd .gate-keeper/


# ---------------------------------------------------------------------------
# Criterion 5 — mixed target_kind: assemble all in-scope files, no per-file gate
# ---------------------------------------------------------------------------


class TestCriterion5MixedKind:
    def test_mixed_kinds_all_assembled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        paths = _write_files(
            tmp_path,
            {"code.py": f"{_SHARED_QUOTE}\n", "notes.md": f"{_SHARED_QUOTE}\n"},
        )
        # Rule has no target_kind annotation → run-level precheck cannot fire; the
        # mixed .py/.md set is assembled in full (no per-file kind gating).
        report = validate(_make_ruleset(_rule()), _multi_spec(paths), backend="llm-rubric")
        diag = report.diagnostics[0]
        ev = _require(diag, "affected_context")
        assert ev.data["included_files"] == ["code.py", "notes.md"]
        assert len(prov.calls) == 1

    def test_run_level_precheck_still_gates_before_assembly(self, tmp_path, monkeypatch):
        """§9.12.3 / §9.7: the run-level #178 precheck is the only kind gate; a
        mismatch short-circuits before the dynamic path, so no file is assembled."""
        monkeypatch.chdir(tmp_path)
        prov = _patch_provider(monkeypatch)
        paths = _write_files(
            tmp_path,
            {"src/a.py": f"{_SHARED_QUOTE}\n", "src/b.py": f"{_SHARED_QUOTE}\n"},
        )
        rule = _rule(
            params={"target_scope": ["src/**/*.py"]},
            target_kind=TargetKind.COMMIT_MESSAGE,
        )
        report = validate(
            RuleSet(rules=[rule]),
            _multi_spec(paths),
            repo_root=tmp_path,
            artifact_kind=TargetKind.PR_DESCRIPTION,
        )
        diag = report.diagnostics[0]
        assert diag.status is Status.UNSUPPORTED
        assert _evidence(diag, "target_kind_mismatch") is not None
        assert _evidence(diag, "affected_context") is None
        assert prov.calls == []


# ---------------------------------------------------------------------------
# Criterion 6 — whole-closure vs narrowed token estimate for the same rule
# ---------------------------------------------------------------------------


class TestCriterion6WholeVsNarrowed:
    def test_narrowed_estimate_below_whole_closure(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _patch_provider(monkeypatch)
        # The whole reference closure the rule's scope governs: five src files.
        closure = {f"src/f{i}.py": f"{_SHARED_QUOTE} body number {i}\n" * 4 for i in range(5)}
        all_paths = _write_files(tmp_path, closure)
        rule = _rule(params={"target_scope": ["src/**/*.py"]})

        # Whole-closure run: candidate set = every scope file.
        whole = validate(RuleSet(rules=[rule]), _multi_spec(all_paths), repo_root=tmp_path).diagnostics[0]
        whole_ev = _require(whole, "affected_context")

        # Narrowed run: candidate set = only two "changed" files.
        changed = [p for p in all_paths if p.name in ("f0.py", "f1.py")]
        narrowed = validate(RuleSet(rules=[rule]), _multi_spec(changed), repo_root=tmp_path).diagnostics[0]
        narrowed_ev = _require(narrowed, "affected_context")

        assert whole_ev.data["included_count"] == 5
        assert narrowed_ev.data["included_count"] == 2
        # The narrowed affected context costs materially fewer tokens than the
        # whole closure for the very same rule — the point of incremental audit.
        assert narrowed_ev.data["tokens_estimated"] < whole_ev.data["tokens_estimated"]


def _make_ruleset(rule: Rule) -> RuleSet:
    return RuleSet(rules=[rule])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
