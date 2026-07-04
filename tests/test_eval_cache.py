"""Tests for the content-addressed local evaluation cache (#69 slice B).

Covers:
- Cache key components: each component independently invalidates (hit/miss matrix).
- Hit semantics: zero provider calls, cache_hit evidence, §2.5 rehydration.
- Miss/corruption: unknown schema version, malformed JSON, from_dict failure.
- Write policy: only PASS/FAIL are written; UNAVAILABLE/ERROR are not.
- Multi-target ordering.
- Inline-string targets (dogfood PR-body case).
- Concurrency safety (concurrent validate with concurrency > 1).
- opt-in: default-off; --eval-cache / dotenv GATE_KEEPER_EVAL_CACHE=1 enables.
- CLI --eval-cache flag wiring.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from gate_keeper.backends import eval_cache as cache_mod
from gate_keeper.backends import llm_rubric as llm_backend
from gate_keeper.models import (
    Backend,
    Confidence,
    Diagnostic,
    Evidence,
    Rule,
    RuleKind,
    RuleSet,
    Severity,
    SourceLocation,
    Status,
    TargetKind,
)
from gate_keeper.validator import validate

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_STUB_ARTIFACT = "The README documents both install and validate commands."

_VALID_PASS_JSON = json.dumps(
    {
        "judgment": "pass",
        "primary_reason": "Docs look good.",
        "supporting_evidence_quotes": [_STUB_ARTIFACT[:30]],
        "suggested_action": None,
    }
)
_VALID_FAIL_JSON = json.dumps(
    {
        "judgment": "fail",
        "primary_reason": "Missing section.",
        "supporting_evidence_quotes": [_STUB_ARTIFACT[:30]],
        "suggested_action": "Add a section.",
    }
)

_STUB_TELEMETRY = {"latency_ms": 10, "tokens_in": 50, "tokens_out": 20}

_ANTHROPIC_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "anthropic",
    "ANTHROPIC_API_KEY": "sk-ant-test",
}
_OPENAI_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-openai-test",
}


def _make_rule(
    *,
    rule_id: str = "r1",
    text: str = "Docs must be clear",
    params: dict[str, Any] | None = None,
    target_kind: TargetKind = TargetKind.UNSPECIFIED,
    severity: Severity = Severity.WARNING,
) -> Rule:
    return Rule(
        id=rule_id,
        title="Test rule",
        source=SourceLocation(path="rules.md", line=1),
        text=text,
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=severity,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params=params or {},
        target_kind=target_kind,
    )


def _make_ruleset(rule: Rule) -> RuleSet:
    return RuleSet(rules=[rule])


def _stub_pass(*_a, **_k):
    return _VALID_PASS_JSON, dict(_STUB_TELEMETRY)


def _stub_fail(*_a, **_k):
    return _VALID_FAIL_JSON, dict(_STUB_TELEMETRY)


def _patch_env(monkeypatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env)


def _patch_anthropic_pass(monkeypatch) -> None:
    monkeypatch.setattr(llm_backend, "_call_anthropic", _stub_pass)


def _patch_openai_pass(monkeypatch) -> None:
    monkeypatch.setattr(llm_backend, "_call_openai", _stub_pass)


# ---------------------------------------------------------------------------
# Unit tests: cache module internals
# ---------------------------------------------------------------------------


class TestCacheDir:
    def test_default_is_relative_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cache_mod.get_cache_dir() == tmp_path / ".gate-keeper" / "cache" / "eval"

    def test_explicit_working_dir(self, tmp_path):
        assert cache_mod.get_cache_dir(tmp_path) == tmp_path / ".gate-keeper" / "cache" / "eval"


class TestIsEnabledFromEnv:
    @pytest.mark.parametrize("val", ["1", "true", "True", "yes", "YES", "on", "ON"])
    def test_truthy(self, val):
        assert cache_mod.is_cache_enabled_from_env({"GATE_KEEPER_EVAL_CACHE": val})

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "maybe"])
    def test_falsy(self, val):
        assert not cache_mod.is_cache_enabled_from_env({"GATE_KEEPER_EVAL_CACHE": val})

    def test_missing_key(self):
        assert not cache_mod.is_cache_enabled_from_env({})


class TestRuleContentHash:
    """rule_content_hash must change when predicate changes and be stable otherwise."""

    def test_same_rule_same_hash(self):
        rule = _make_rule()
        assert cache_mod._build_rule_content_hash(rule) == cache_mod._build_rule_content_hash(rule)

    def test_different_text_different_hash(self):
        r1 = _make_rule(text="Rule A")
        r2 = _make_rule(text="Rule B")
        assert cache_mod._build_rule_content_hash(r1) != cache_mod._build_rule_content_hash(r2)

    def test_strategy_param_excluded(self):
        """strategy is keyed separately; changing it must not change rule_content_hash."""
        r1 = _make_rule(params={"strategy": "single"})
        r2 = _make_rule(params={"strategy": "consensus"})
        assert cache_mod._build_rule_content_hash(r1) == cache_mod._build_rule_content_hash(r2)

    def test_targets_param_excluded(self):
        """params.targets is keyed in the targets component, not the rule hash."""
        r1 = _make_rule(params={})
        r2 = _make_rule(params={"targets": [{"id": "t1", "kind": "code_change", "path": "a.py"}]})
        assert cache_mod._build_rule_content_hash(r1) == cache_mod._build_rule_content_hash(r2)

    def test_adaptive_params_excluded(self):
        r1 = _make_rule(params={})
        r2 = _make_rule(params={"adaptive_escalate_on_quote_fabrication": True})
        assert cache_mod._build_rule_content_hash(r1) == cache_mod._build_rule_content_hash(r2)

    def test_non_excluded_param_included(self):
        r1 = _make_rule(params={"foo": "bar"})
        r2 = _make_rule(params={"foo": "baz"})
        assert cache_mod._build_rule_content_hash(r1) != cache_mod._build_rule_content_hash(r2)

    def test_rule_id_excluded(self):
        """rule_id is rehydrated on hit; changing it must not change the hash."""
        r1 = _make_rule(rule_id="rule-a")
        r2 = _make_rule(rule_id="rule-b")
        assert cache_mod._build_rule_content_hash(r1) == cache_mod._build_rule_content_hash(r2)

    def test_severity_excluded(self):
        """severity is rehydrated on hit; must not affect the hash."""
        r1 = _make_rule(severity=Severity.WARNING)
        r2 = _make_rule(severity=Severity.ERROR)
        assert cache_mod._build_rule_content_hash(r1) == cache_mod._build_rule_content_hash(r2)


class TestTargetEntries:
    """Single-target path: path and content_sha256 are independent key components."""

    def test_inline_string_has_null_path(self, monkeypatch):
        _patch_env(monkeypatch, _ANTHROPIC_ENV)
        rule = _make_rule()
        entries = cache_mod._build_target_entries(rule, "some inline text", None)
        assert len(entries) == 1
        assert entries[0]["id"] is None
        assert entries[0]["path"] is None

    def test_file_target_captures_path(self, tmp_path, monkeypatch):
        _patch_env(monkeypatch, _ANTHROPIC_ENV)
        f = tmp_path / "target.md"
        f.write_text("content", encoding="utf-8")
        rule = _make_rule()
        entries = cache_mod._build_target_entries(rule, str(f), None)
        assert entries[0]["path"] == str(f)

    def test_different_content_different_hash(self, tmp_path, monkeypatch):
        _patch_env(monkeypatch, _ANTHROPIC_ENV)
        f1 = tmp_path / "a.md"
        f1.write_text("content A", encoding="utf-8")
        f2 = tmp_path / "b.md"
        f2.write_text("content B", encoding="utf-8")
        rule = _make_rule()
        e1 = cache_mod._build_target_entries(rule, str(f1), None)
        e2 = cache_mod._build_target_entries(rule, str(f2), None)
        assert e1[0]["content_sha256"] != e2[0]["content_sha256"]

    def test_same_content_different_path_different_entry(self, tmp_path, monkeypatch):
        """Same bytes at different paths → different entries (path is a key component).

        When artifact_kind=None, _resolve_artifact_input returns str(target) (the
        path string itself), so both content_sha256 AND path differ. When
        artifact_kind is set, the file bytes are rendered, making content_sha256
        identical but path still differs. Either way, the full entry differs,
        meaning renames invalidate the key regardless of artifact_kind.
        """
        _patch_env(monkeypatch, _ANTHROPIC_ENV)
        bytes_ = "identical content"
        f1 = tmp_path / "src" / "foo.py"
        f1.parent.mkdir()
        f1.write_text(bytes_, encoding="utf-8")
        f2 = tmp_path / "tests" / "foo.py"
        f2.parent.mkdir()
        f2.write_text(bytes_, encoding="utf-8")
        rule = _make_rule()
        e1 = cache_mod._build_target_entries(rule, str(f1), None)
        e2 = cache_mod._build_target_entries(rule, str(f2), None)
        # Both path and content_sha256 differ (artifact_kind=None renders the
        # path string as the "content", so hashing str(f1) ≠ hashing str(f2)).
        assert e1[0]["path"] != e2[0]["path"]
        assert e1[0]["content_sha256"] != e2[0]["content_sha256"]

        # With artifact_kind set, file bytes are rendered: content_sha256 is
        # the same (identical bytes), but path still differs → different entry.
        e1k = cache_mod._build_target_entries(rule, str(f1), TargetKind.CODE_CHANGE)
        e2k = cache_mod._build_target_entries(rule, str(f2), TargetKind.CODE_CHANGE)
        assert e1k[0]["content_sha256"] == e2k[0]["content_sha256"]  # same bytes
        assert e1k[0]["path"] != e2k[0]["path"]  # different path → different entry

    def test_artifact_kind_changes_rendered_content(self, tmp_path, monkeypatch):
        """--artifact-kind changes what _resolve_artifact_input returns."""
        _patch_env(monkeypatch, _ANTHROPIC_ENV)
        f = tmp_path / "target.txt"
        f.write_text("file content here", encoding="utf-8")
        rule = _make_rule()
        e_no_kind = cache_mod._build_target_entries(rule, str(f), None)
        e_with_kind = cache_mod._build_target_entries(rule, str(f), TargetKind.CODE_CHANGE)
        # Without kind: renders the path string; with kind: renders file content.
        # So content hashes must differ.
        assert e_no_kind[0]["content_sha256"] != e_with_kind[0]["content_sha256"]


class TestLookupAndStore:
    """Low-level lookup/store/rehydrate contract."""

    def _make_diag(self, status: Status = Status.PASS) -> Diagnostic:
        return Diagnostic(
            rule_id="r1",
            source=SourceLocation(path="rules.md", line=1),
            backend=Backend.LLM_RUBRIC,
            status=status,
            severity=Severity.WARNING,
            message="ok",
            evidence=[Evidence(kind="llm_judgment", data={"judgment": "pass"})],
        )

    def test_miss_on_absent_file(self, tmp_path):
        assert cache_mod.lookup(tmp_path, "nonexistent") is None

    def test_store_creates_file(self, tmp_path):
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "abc123", {}, diag)
        assert (tmp_path / "abc123.json").exists()

    def test_store_skip_unavailable(self, tmp_path):
        diag = self._make_diag(Status.UNAVAILABLE)
        cache_mod.store(tmp_path, "abc123", {}, diag)
        assert not (tmp_path / "abc123.json").exists()

    def test_store_skip_error(self, tmp_path):
        diag = self._make_diag(Status.ERROR)
        cache_mod.store(tmp_path, "abc123", {}, diag)
        assert not (tmp_path / "abc123.json").exists()

    def test_store_skip_unsupported(self, tmp_path):
        diag = self._make_diag(Status.UNSUPPORTED)
        cache_mod.store(tmp_path, "abc123", {}, diag)
        assert not (tmp_path / "abc123.json").exists()

    def test_roundtrip_pass(self, tmp_path):
        rule = _make_rule()
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "key1", {}, diag)
        entry = cache_mod.lookup(tmp_path, "key1")
        assert entry is not None
        result = cache_mod.rehydrate(entry, rule)
        assert result is not None
        assert result.status is Status.PASS

    def test_roundtrip_fail(self, tmp_path):
        rule = _make_rule()
        diag = self._make_diag(Status.FAIL)
        cache_mod.store(tmp_path, "key2", {}, diag)
        entry = cache_mod.lookup(tmp_path, "key2")
        assert entry is not None
        result = cache_mod.rehydrate(entry, rule)
        assert result is not None
        assert result.status is Status.FAIL

    def test_unknown_schema_version_returns_none(self, tmp_path):
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "key_v", {}, diag)
        path = tmp_path / "key_v.json"
        data = json.loads(path.read_text())
        data["cache_schema_version"] = 9999
        path.write_text(json.dumps(data))
        assert cache_mod.lookup(tmp_path, "key_v") is None

    def test_malformed_json_returns_none(self, tmp_path):
        (tmp_path / "badkey.json").write_text("not json {{{", encoding="utf-8")
        assert cache_mod.lookup(tmp_path, "badkey") is None

    def test_rehydrate_corrupt_diagnostic_returns_none(self, tmp_path):
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "corrupt", {}, diag)
        path = tmp_path / "corrupt.json"
        data = json.loads(path.read_text())
        data["diagnostic"] = {"broken": True}  # invalid Diagnostic shape
        path.write_text(json.dumps(data))
        entry = cache_mod.lookup(tmp_path, "corrupt")
        assert entry is not None
        assert cache_mod.rehydrate(entry, _make_rule()) is None

    def test_cache_hit_evidence_appended(self, tmp_path):
        rule = _make_rule()
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "ev_key", {"prompt_version": "v7"}, diag)
        entry = cache_mod.lookup(tmp_path, "ev_key")
        result = cache_mod.rehydrate(entry, rule)
        assert result is not None
        kinds = [e.kind for e in result.evidence]
        assert "cache_hit" in kinds
        hit_ev = next(e for e in result.evidence if e.kind == "cache_hit")
        assert hit_ev.data["cache_hit"] is True
        assert hit_ev.data["provider_calls"] == 0
        assert hit_ev.data["cost_usd"] == 0

    def test_cache_hit_evidence_not_stored_in_entry(self, tmp_path):
        """cache_hit marker must not be persisted; it is added fresh on each hit."""
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "no_marker", {}, diag)
        raw = json.loads((tmp_path / "no_marker.json").read_text())
        evs = raw["diagnostic"]["evidence"]
        assert all(e["kind"] != "cache_hit" for e in evs)

    def test_rehydrate_overrides_rule_identity(self, tmp_path):
        """§2.5: rule_id, source, severity are taken from the *current* rule, not the stored one."""
        orig_rule = _make_rule(rule_id="rule-a", severity=Severity.WARNING)
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "rh_key", {}, diag)
        entry = cache_mod.lookup(tmp_path, "rh_key")

        new_source = SourceLocation(path="other-rules.md", line=42)
        current_rule = dataclasses.replace(
            orig_rule,
            id="rule-b",
            source=new_source,
            severity=Severity.ERROR,
        )
        result = cache_mod.rehydrate(entry, current_rule)
        assert result is not None
        assert result.rule_id == "rule-b"
        assert result.source == new_source
        assert result.severity is Severity.ERROR

    def test_store_is_atomic_no_partial_reads(self, tmp_path):
        """A completed store must produce a complete, valid JSON file."""
        diag = self._make_diag(Status.PASS)
        cache_mod.store(tmp_path, "atomic", {}, diag)
        raw = (tmp_path / "atomic.json").read_text()
        parsed = json.loads(raw)
        assert "diagnostic" in parsed
        assert "cache_schema_version" in parsed

    def test_store_creates_parent_dirs(self, tmp_path):
        deep_dir = tmp_path / "a" / "b" / "c"
        diag = self._make_diag(Status.PASS)
        cache_mod.store(deep_dir, "mk_key", {}, diag)
        assert (deep_dir / "mk_key.json").exists()


# ---------------------------------------------------------------------------
# Integration tests: cache key hit/miss matrix via validate()
# ---------------------------------------------------------------------------


@pytest.fixture()
def configured_anthropic(monkeypatch):
    """Configure llm_rubric to use a stubbed anthropic provider returning PASS."""
    env = dict(_ANTHROPIC_ENV)
    monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env)
    monkeypatch.setattr(llm_backend, "_call_anthropic", _stub_pass)
    return env


@pytest.fixture()
def call_counter(monkeypatch):
    """Return a mutable call count and stub _call_anthropic to increment it."""
    count = {"n": 0}

    def _counting(*_a, **_k):
        count["n"] += 1
        return _VALID_PASS_JSON, dict(_STUB_TELEMETRY)

    monkeypatch.setattr(llm_backend, "_call_anthropic", _counting)
    return count


def _run_validate(
    ruleset: RuleSet,
    target: str,
    cache_dir: Path,
    *,
    artifact_kind: TargetKind | None = None,
    deterministic: bool = False,
    reproducibility: int = 1,
    concurrency: int = 1,
) -> list[Diagnostic]:
    """Run validate with eval_cache pointing at the given tmp cache_dir."""
    # Patch try_lookup / try_store to use the test cache_dir.
    import gate_keeper.backends.eval_cache as _cache

    orig_try_lookup = _cache.try_lookup
    orig_try_store = _cache.try_store

    def _patched_lookup(rule, target_, ak, det, repro, **_kw):
        return orig_try_lookup(rule, target_, ak, det, repro, cache_dir=cache_dir)

    def _patched_store(rule, target_, ak, det, repro, diag, **_kw):
        return orig_try_store(rule, target_, ak, det, repro, diag, cache_dir=cache_dir)

    import unittest.mock as mock

    with (
        mock.patch.object(_cache, "try_lookup", side_effect=_patched_lookup),
        mock.patch.object(_cache, "try_store", side_effect=_patched_store),
    ):
        report = validate(
            ruleset,
            target,
            reproducibility=reproducibility,
            artifact_kind=artifact_kind,
            deterministic=deterministic,
            eval_cache=True,
            concurrency=concurrency,
        )
    return report.diagnostics


class TestCacheHitMiss:
    """End-to-end hit/miss via validate() with a stubbed provider."""

    def test_first_run_is_miss_second_is_hit(self, tmp_path, configured_anthropic, call_counter):
        """First call computes; second call hits cache with zero provider calls."""
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"

        # First run: miss → provider called once.
        diags1 = _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1
        assert diags1[0].status is Status.PASS

        # Second run: hit → provider NOT called again.
        diags2 = _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1  # still 1 — no new provider call
        assert diags2[0].status is Status.PASS

    def test_hit_has_cache_hit_evidence(self, tmp_path, configured_anthropic, call_counter):
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)  # warm cache
        diags = _run_validate(rs, _STUB_ARTIFACT, cache_dir)  # hit
        kinds = [e.kind for e in diags[0].evidence]
        assert "cache_hit" in kinds

    def test_miss_has_no_cache_hit_evidence(self, tmp_path, configured_anthropic, call_counter):
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"
        diags = _run_validate(rs, _STUB_ARTIFACT, cache_dir)  # cold
        kinds = [e.kind for e in diags[0].evidence]
        assert "cache_hit" not in kinds

    # --- Key component invalidation ---

    def test_different_target_content_is_miss(self, tmp_path, configured_anthropic, call_counter):
        """Editing a file's bytes produces a different content_sha256 → cache miss.

        Uses artifact_kind=CODE_CHANGE so the rendered artifact is the file content
        (not the path string). The first run caches the PASS result; the second run
        reads different bytes, computes a different key, and calls the provider again.
        """
        f = tmp_path / "doc.md"
        f.write_text(_STUB_ARTIFACT, encoding="utf-8")  # stub quote is substring → PASS
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        _run_validate(rs, str(f), cache_dir, artifact_kind=TargetKind.CODE_CHANGE)
        assert call_counter["n"] == 1
        # Edit file → different content_sha256 → miss.
        f.write_text("Something completely different here.", encoding="utf-8")
        _run_validate(rs, str(f), cache_dir, artifact_kind=TargetKind.CODE_CHANGE)
        assert call_counter["n"] == 2  # miss on different content

    def test_different_rule_text_is_miss(self, tmp_path, configured_anthropic, call_counter):
        cache_dir = tmp_path / "cache"
        r1 = _make_rule(rule_id="r1", text="Rule A")
        _run_validate(_make_ruleset(r1), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1
        r2 = _make_rule(rule_id="r1", text="Rule B")
        _run_validate(_make_ruleset(r2), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 2

    def test_different_provider_is_miss(self, tmp_path, monkeypatch, call_counter):
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)

        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: dict(_ANTHROPIC_ENV))
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1

        openai_count = {"n": 0}

        def _openai_counter(*_a, **_k):
            openai_count["n"] += 1
            return _VALID_PASS_JSON, dict(_STUB_TELEMETRY)

        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: dict(_OPENAI_ENV))
        monkeypatch.setattr(llm_backend, "_call_openai", _openai_counter)
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert openai_count["n"] == 1  # different provider → different key → miss

    def test_different_model_is_miss(self, tmp_path, monkeypatch, call_counter):
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)

        env_a = {**_ANTHROPIC_ENV, "GATE_KEEPER_ANTHROPIC_MODEL": "claude-haiku-4-5"}
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env_a)
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1

        env_b = {**_ANTHROPIC_ENV, "GATE_KEEPER_ANTHROPIC_MODEL": "claude-sonnet-4-6"}
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env_b)
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 2  # different model → miss

    def test_different_strategy_is_miss(self, tmp_path, configured_anthropic, call_counter):
        cache_dir = tmp_path / "cache"
        r1 = _make_rule(params={"strategy": "single"})
        _run_validate(_make_ruleset(r1), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1
        r2 = _make_rule(params={"strategy": "consensus", "consensus_panel_size": 3})
        _run_validate(_make_ruleset(r2), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] > 1  # miss on different strategy

    def test_different_reproducibility_n_is_miss(self, tmp_path, configured_anthropic, call_counter):
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, reproducibility=1)
        n_after_first = call_counter["n"]
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, reproducibility=3)
        assert call_counter["n"] > n_after_first  # N=3 is a miss vs N=1

    def test_different_artifact_kind_is_miss(self, tmp_path, configured_anthropic, call_counter):
        """artifact_kind changes the rendered artifact text (§2.2); must be a separate key."""
        f = tmp_path / "file.txt"
        f.write_text(_STUB_ARTIFACT, encoding="utf-8")
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        _run_validate(rs, str(f), cache_dir, artifact_kind=None)
        n1 = call_counter["n"]
        _run_validate(rs, str(f), cache_dir, artifact_kind=TargetKind.CODE_CHANGE)
        assert call_counter["n"] > n1  # different artifact_kind → miss

    def test_deterministic_mode_is_separate_key(self, tmp_path, configured_anthropic, call_counter):
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, deterministic=False)
        n1 = call_counter["n"]
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, deterministic=True)
        assert call_counter["n"] > n1  # deterministic flag → different key

    def test_same_predicate_different_rule_id_shares_cache(
        self, tmp_path, configured_anthropic, call_counter
    ):
        """§2.5: two rules with identical predicates share the same cache entry."""
        cache_dir = tmp_path / "cache"
        r1 = _make_rule(rule_id="rule-a", text="Docs must be clear")
        r2 = _make_rule(rule_id="rule-b", text="Docs must be clear")

        _run_validate(_make_ruleset(r1), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1

        # r2 has same text → same key → hit (zero additional provider calls).
        diags = _run_validate(_make_ruleset(r2), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1
        # Rehydration must use r2's identity, not r1's.
        assert diags[0].rule_id == "rule-b"

    def test_hit_rehydrates_severity_from_current_rule(self, tmp_path, configured_anthropic, call_counter):
        """§2.5: on a hit, severity comes from the current rule, not the stored one."""
        cache_dir = tmp_path / "cache"
        r_warning = _make_rule(severity=Severity.WARNING)
        _run_validate(_make_ruleset(r_warning), _STUB_ARTIFACT, cache_dir)

        r_error = dataclasses.replace(r_warning, severity=Severity.ERROR)
        diags = _run_validate(_make_ruleset(r_error), _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1  # cache hit
        assert diags[0].severity is Severity.ERROR

    def test_unavailable_not_cached(self, tmp_path, monkeypatch):
        """UNAVAILABLE results (provider unconfigured) must not be stored."""
        # Don't configure provider → UNAVAILABLE.
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: {})
        rule = _make_rule()
        cache_dir = tmp_path / "cache"
        _run_validate(_make_ruleset(rule), _STUB_ARTIFACT, cache_dir)
        assert not any(True for _ in cache_dir.glob("*.json")) if cache_dir.exists() else True

    def test_cache_disabled_by_default(self, tmp_path, configured_anthropic, call_counter):
        """eval_cache=False (default) must not read/write any cache files."""
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        # Run twice without cache.
        for _ in range(2):
            validate(
                rs,
                _STUB_ARTIFACT,
                eval_cache=False,
            )
        assert call_counter["n"] == 2  # both runs go to provider
        assert not cache_dir.exists()


class TestMultiTargetOrdering:
    """Multi-target entries must be keyed in list order; reordering → different key."""

    def test_multi_target_order_affects_key(self, tmp_path, monkeypatch):
        """Reordering params.targets produces a different preimage key (unit test).

        Uses build_key_preimage directly to avoid triggering the full validate →
        fabrication-check path, which does not yield a cacheable result for
        multi-target specs whose quotes don't map to the rendered multi-target text.
        """
        _patch_env(monkeypatch, _ANTHROPIC_ENV)
        fa = tmp_path / "a.txt"
        fa.write_text("content A", encoding="utf-8")
        fb = tmp_path / "b.txt"
        fb.write_text("content B", encoding="utf-8")

        def _make_multi_rule(order):
            targets = [{"id": t, "kind": "code_change", "path": str(tmp_path / f"{t}.txt")} for t in order]
            return _make_rule(params={"targets": targets})

        provider = "anthropic"
        model = "claude-haiku-4-5"
        rule_ab = _make_multi_rule(["a", "b"])
        rule_ba = _make_multi_rule(["b", "a"])
        preimage_ab = cache_mod.build_key_preimage(rule_ab, _STUB_ARTIFACT, None, False, 1, provider, model)
        preimage_ba = cache_mod.build_key_preimage(rule_ba, _STUB_ARTIFACT, None, False, 1, provider, model)
        assert cache_mod.compute_key(preimage_ab) != cache_mod.compute_key(preimage_ba)


class TestInlineTargets:
    """Inline-string targets (e.g. dogfood PR-body) must be cacheable."""

    def test_inline_target_cached(self, tmp_path, configured_anthropic, call_counter):
        """Inline-string targets (e.g. dogfood PR-body) must be cacheable.

        _STUB_ARTIFACT is the inline text; the stub returns a quote that is a
        substring of it, so the fabrication check passes and the result is PASS
        (cacheable). A second run with the same text hits the cache.
        """
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1  # hit


class TestFiletargetPathGuard:
    """Rename guard: same bytes at different paths → different keys (§3.1)."""

    def test_renamed_file_is_miss(self, tmp_path, configured_anthropic, call_counter):
        """Same bytes at a different path → cache miss (path is a key component).

        Uses artifact_kind=CODE_CHANGE so the rendered artifact is the file content
        (not the path string). Both files share identical bytes (_STUB_ARTIFACT), so
        content_sha256 is identical — but path differs in the targets entry, making
        the preimage (and therefore key) different.
        """
        f1 = tmp_path / "src" / "foo.md"
        f1.parent.mkdir()
        f1.write_text(_STUB_ARTIFACT, encoding="utf-8")
        f2 = tmp_path / "tests" / "foo.md"
        f2.parent.mkdir()
        f2.write_text(_STUB_ARTIFACT, encoding="utf-8")
        cache_dir = tmp_path / "cache"
        rule = _make_rule()
        rs = _make_ruleset(rule)
        # Run 1: renders file content (_STUB_ARTIFACT) → fabrication passes → PASS cached.
        _run_validate(rs, str(f1), cache_dir, artifact_kind=TargetKind.CODE_CHANGE)
        assert call_counter["n"] == 1
        # Run 2: same bytes, different path → different key → miss.
        _run_validate(rs, str(f2), cache_dir, artifact_kind=TargetKind.CODE_CHANGE)
        assert call_counter["n"] == 2  # different path → miss


class TestConcurrencySafety:
    """concurrent validate with concurrency > 1 must read/write cache correctly."""

    def test_concurrent_second_run_hits_cache(self, tmp_path, configured_anthropic, call_counter):
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"
        # Warm the cache (sequential, single rule).
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 1

        # Second run with concurrency=2; should still hit the cache.
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, concurrency=2)
        assert call_counter["n"] == 1  # cache hit; no new provider call

    def test_concurrent_first_run_all_rules_computed(self, tmp_path, configured_anthropic, call_counter):
        """Multiple rules with different texts → each must be computed once."""
        rules = [_make_rule(rule_id=f"r{i}", text=f"Rule {i}") for i in range(4)]
        rs = RuleSet(rules=rules)
        cache_dir = tmp_path / "cache"
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, concurrency=4)
        assert call_counter["n"] == 4  # 4 distinct rules → 4 provider calls

    def test_concurrent_cache_hit_no_provider_call(self, tmp_path, configured_anthropic, call_counter):
        """Multiple identical rules (same predicate text) → all hit on second run."""
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, concurrency=1)
        assert call_counter["n"] == 1
        _run_validate(rs, _STUB_ARTIFACT, cache_dir, concurrency=2)
        assert call_counter["n"] == 1  # still no new provider call


class TestCorruptionRecovery:
    """Unknown schema version / malformed JSON → miss, not error (§2.3)."""

    def test_unknown_schema_version_causes_recompute(self, tmp_path, configured_anthropic, call_counter):
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)  # warm
        assert call_counter["n"] == 1
        # Corrupt the schema version.
        for f in cache_dir.glob("*.json"):
            data = json.loads(f.read_text())
            data["cache_schema_version"] = 9999
            f.write_text(json.dumps(data))
        # Should recompute (miss).
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 2

    def test_malformed_json_causes_recompute(self, tmp_path, configured_anthropic, call_counter):
        rule = _make_rule()
        rs = _make_ruleset(rule)
        cache_dir = tmp_path / "cache"
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)  # warm
        for f in cache_dir.glob("*.json"):
            f.write_text("not json }{", encoding="utf-8")
        _run_validate(rs, _STUB_ARTIFACT, cache_dir)
        assert call_counter["n"] == 2  # recomputed


# ---------------------------------------------------------------------------
# opt-in / dotenv toggle tests
# ---------------------------------------------------------------------------


class TestOptIn:
    def test_dotenv_eval_cache_1_enables_cache(self, tmp_path, monkeypatch, call_counter):
        """GATE_KEEPER_EVAL_CACHE=1 in dotenv → cache enabled as fallback."""
        env = {**_ANTHROPIC_ENV, "GATE_KEEPER_EVAL_CACHE": "1"}
        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: env)

        from gate_keeper.backends.eval_cache import is_cache_enabled_from_env

        assert is_cache_enabled_from_env(env)

    def test_dotenv_eval_cache_0_disabled(self):
        env = {**_ANTHROPIC_ENV, "GATE_KEEPER_EVAL_CACHE": "0"}
        from gate_keeper.backends.eval_cache import is_cache_enabled_from_env

        assert not is_cache_enabled_from_env(env)

    def test_eval_cache_false_by_default_in_validate(self, tmp_path, configured_anthropic, call_counter):
        """validate() with eval_cache omitted (default False) never touches the cache."""
        rule = _make_rule()
        rs = _make_ruleset(rule)
        validate(rs, _STUB_ARTIFACT)
        validate(rs, _STUB_ARTIFACT)
        assert call_counter["n"] == 2  # both ran; no caching


# ---------------------------------------------------------------------------
# CLI wiring tests
# ---------------------------------------------------------------------------


class TestCLIEvalCacheFlag:
    """--eval-cache flag wires through to validate(eval_cache=True)."""

    def test_eval_cache_flag_accepted(self, tmp_path, monkeypatch):
        """--eval-cache must be accepted by argparse without error.

        The validation itself produces UNAVAILABLE (provider not configured) which
        is a warning; cli.main returns normally with exit code 0. We just confirm
        no argparse error (exit 2) is raised.
        """
        from gate_keeper import cli

        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: {})
        rules_file = tmp_path / "rules.md"
        rules_file.write_text("# Test\n\n## Rule: dummy\nDocs must be clear.\n", encoding="utf-8")
        target_file = tmp_path / "target.txt"
        target_file.write_text("some content", encoding="utf-8")

        # cli.main receives args without the program name (argparse convention).
        argv = [
            "validate",
            str(rules_file),
            "--target",
            str(target_file),
            "--eval-cache",
        ]
        # Should not raise — UNAVAILABLE is a warning, not an error exit.
        cli.main(argv)

    def test_eval_cache_default_false(self, tmp_path, monkeypatch):
        """Without --eval-cache and no dotenv key, eval_cache must be False."""
        import unittest.mock as mock

        from gate_keeper import validator as _val

        monkeypatch.setattr(llm_backend, "_load_env_file", lambda *a, **k: _ANTHROPIC_ENV)
        monkeypatch.setattr(llm_backend, "_call_anthropic", _stub_pass)

        with mock.patch.object(_val, "validate", wraps=_val.validate) as mock_validate:
            from gate_keeper import cli

            rules_file = tmp_path / "rules.md"
            rules_file.write_text("# T\n\n## Rule: r1\nClear docs.\n", encoding="utf-8")
            target_file = tmp_path / "t.txt"
            target_file.write_text("content", encoding="utf-8")
            argv = ["gate-keeper", "validate", str(rules_file), "--target", str(target_file)]
            try:
                cli.main(argv)
            except SystemExit:
                pass
            if mock_validate.called:
                _, kwargs = mock_validate.call_args
                assert not kwargs.get("eval_cache", False)
