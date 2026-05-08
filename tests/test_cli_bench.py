"""Smoke + unit tests for ``gate-keeper bench`` (#130).

Real provider calls are out of scope for the test suite (they cost money and
require a configured dotenv). The ``_call_openai`` helper is monkeypatched so
the bench harness exercises the full eval → aggregate → render path against a
deterministic stub, mirroring the pattern in
``tests/test_llm_rubric_backend.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper import bench as _bench
from gate_keeper.backends import llm_rubric as _llm
from gate_keeper.cli import main

REPO_ROOT = Path(__file__).parent.parent
ENTRIES_DIR = REPO_ROOT / "tests" / "fixtures" / "semantic" / "entries"
TARGETS_DIR = REPO_ROOT / "tests" / "fixtures" / "semantic" / "targets"


# ---------------------------------------------------------------------------
# Stub helpers (mirror tests/test_llm_rubric_backend.py)
# ---------------------------------------------------------------------------


def _patch_env(monkeypatch, env: dict[str, str]) -> None:
    monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: env)


def _stub_openai_pass(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
    """Mock OpenAI helper returning a structured `pass` judgment."""
    body = json.dumps(
        {
            "judgment": "pass",
            "primary_reason": "The artefact satisfies the rule.",
            "supporting_evidence_quotes": [],
            "suggested_action": None,
        }
    )
    return body, {"latency_ms": 10, "tokens_in": 50, "tokens_out": 12}


def _stub_openai_fail(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "judgment": "fail",
            "primary_reason": "The artefact violates the rule.",
            "supporting_evidence_quotes": ["evidence quote"],
            "suggested_action": "Fix it.",
        }
    )
    return body, {"latency_ms": 20, "tokens_in": 60, "tokens_out": 18}


_OPENAI_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-test-stub-not-real",
}


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoader:
    def test_load_all_entries_round_trip(self):
        entries = list(_bench.iter_entries(ENTRIES_DIR))
        assert len(entries) > 0
        ids = [e.id for e in entries]
        # Stable ordering: filename-sorted.
        assert ids == sorted(ids)

    def test_resolve_inline_target(self):
        entry = next(e for e in _bench.iter_entries(ENTRIES_DIR) if e.target_kind == "inline")
        text = entry.resolve_target(TARGETS_DIR)
        assert text == entry.target_value

    def test_resolve_path_target_reads_file(self):
        entry = next(e for e in _bench.iter_entries(ENTRIES_DIR) if e.target_kind == "path")
        text = entry.resolve_target(TARGETS_DIR)
        assert text  # non-empty

    def test_parse_entry_rejects_unknown_field(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "rule_text": "x",
                    "target": {"kind": "inline", "value": "y"},
                    "expected_judgment": "pass",
                    "expected_rationale_keywords": [],
                    "category": "clarity",
                    "intended_backend": "llm-rubric",
                    "extra_field": "should fail",
                }
            )
        )
        with pytest.raises(ValueError, match="unknown fields"):
            _bench.load_entry(bad)

    def test_parse_entry_rejects_invalid_judgment(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "rule_text": "x",
                    "target": {"kind": "inline", "value": "y"},
                    "expected_judgment": "MAYBE",
                    "expected_rationale_keywords": [],
                    "category": "clarity",
                    "intended_backend": "llm-rubric",
                }
            )
        )
        with pytest.raises(ValueError, match="expected_judgment"):
            _bench.load_entry(bad)


# ---------------------------------------------------------------------------
# run_bench (single-entry smoke)
# ---------------------------------------------------------------------------


class TestRunBenchSmoke:
    def _single_entry_dir(self, tmp_path: Path) -> Path:
        """Build a one-entry fixture dir whose target is inline (no file deps)."""
        entries = tmp_path / "entries"
        entries.mkdir()
        entry = {
            "rule_text": "The artefact satisfies the example rule.",
            "target": {"kind": "inline", "value": "example artefact body"},
            "expected_judgment": "pass",
            "expected_rationale_keywords": ["satisfies"],
            "category": "clarity",
            "intended_backend": "llm-rubric",
        }
        (entries / "smoke-01.json").write_text(json.dumps(entry))
        return entries

    def test_run_bench_pass(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._single_entry_dir(tmp_path)
        result = _bench.run_bench(entries, reproducibility=1)
        assert result.summary.entries == 1
        assert result.summary.correct == 1
        assert result.summary.accuracy == 1.0
        assert result.summary.tokens_in == 50
        assert result.summary.tokens_out == 12
        assert result.summary.model == _llm.OPENAI_DEFAULT_MODEL
        assert result.summary.prompt_version == _llm.PROMPT_VERSION
        assert len(result.per_rule) == 1
        row = result.per_rule[0]
        assert row.id == "smoke-01"
        assert row.status == "PASS"
        assert row.actual == "pass"
        assert row.expected == "pass"
        assert row.reproducibility == 1.0

    def test_run_bench_mismatch_marks_fail(self, monkeypatch, tmp_path):
        # Stub returns `fail` but expected is `pass` → status=FAIL.
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_fail)
        entries = self._single_entry_dir(tmp_path)
        result = _bench.run_bench(entries, reproducibility=1)
        assert result.summary.correct == 0
        assert result.summary.accuracy == 0.0
        row = result.per_rule[0]
        assert row.status == "FAIL"
        assert row.actual == "fail"
        assert row.expected == "pass"
        assert row.failure_mode == "llm_fail"

    def test_run_bench_reproducibility_three(self, monkeypatch, tmp_path):
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._single_entry_dir(tmp_path)
        result = _bench.run_bench(entries, reproducibility=3)
        # All 3 runs return pass → reproducibility=1.0, telemetry sums.
        assert result.summary.reproducibility_n == 3
        assert result.per_rule[0].reproducibility == 1.0
        assert result.per_rule[0].tokens_in == 50 * 3
        assert result.per_rule[0].tokens_out == 12 * 3

    def test_run_bench_primary_reason_matches_majority(self, monkeypatch, tmp_path):
        """primary_reason must come from a run that matches the majority outcome (P1).

        Sequence: pass (first) → fail → fail.  Majority = fail (2/3).
        primary_reason must NOT be the pass rationale from the first run.
        """
        _patch_env(monkeypatch, _OPENAI_ENV)
        call_count = 0

        def _stub_pass_then_fail(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                body = json.dumps(
                    {
                        "judgment": "pass",
                        "primary_reason": "PASS rationale — should not appear",
                        "supporting_evidence_quotes": [],
                        "suggested_action": None,
                    }
                )
                return body, {"latency_ms": 10, "tokens_in": 50, "tokens_out": 12}
            body = json.dumps(
                {
                    "judgment": "fail",
                    "primary_reason": "FAIL rationale — majority side",
                    "supporting_evidence_quotes": ["evidence of violation"],
                    "suggested_action": "Fix it.",
                }
            )
            return body, {"latency_ms": 20, "tokens_in": 60, "tokens_out": 18}

        monkeypatch.setattr(_llm, "_call_openai", _stub_pass_then_fail)
        entries = self._single_entry_dir(tmp_path)
        result = _bench.run_bench(entries, reproducibility=3)
        row = result.per_rule[0]
        # Majority is fail (2/3 runs).
        assert row.actual == "fail"
        assert row.primary_reason == "FAIL rationale — majority side"
        assert "PASS rationale" not in (row.primary_reason or "")

    def test_run_bench_unconfigured_marks_unavailable(self, monkeypatch, tmp_path):
        # No provider env → backend returns UNAVAILABLE.
        _patch_env(monkeypatch, {})
        entries = self._single_entry_dir(tmp_path)
        result = _bench.run_bench(entries, reproducibility=1)
        row = result.per_rule[0]
        assert row.actual == "unavailable"
        assert row.status == "FAIL"  # mismatch counts as fail
        assert result.summary.unavailable == 1


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCliBench:
    def _smoke_entries(self, tmp_path: Path) -> Path:
        entries = tmp_path / "entries"
        entries.mkdir()
        (entries / "cli-smoke.json").write_text(
            json.dumps(
                {
                    "rule_text": "The artefact satisfies the example rule.",
                    "target": {"kind": "inline", "value": "example body"},
                    "expected_judgment": "pass",
                    "expected_rationale_keywords": [],
                    "category": "clarity",
                    "intended_backend": "llm-rubric",
                }
            )
        )
        return entries

    def test_bench_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["bench", "--help"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "entries_dir" in captured.out
        assert "--reproducibility" in captured.out
        assert "--baseline" in captured.out

    def test_bench_text_format(self, monkeypatch, tmp_path, capsys):
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._smoke_entries(tmp_path)
        rc = main(["bench", str(entries)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "entries: 1" in captured.out
        assert "accuracy:" in captured.out
        assert "cli-smoke" in captured.out

    def test_bench_json_format(self, monkeypatch, tmp_path, capsys):
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._smoke_entries(tmp_path)
        rc = main(["bench", str(entries), "--format", "json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "summary" in data
        assert "per_rule" in data
        assert data["summary"]["entries"] == 1
        assert data["summary"]["correct"] == 1
        assert data["per_rule"][0]["id"] == "cli-smoke"

    def test_bench_missing_dir_exit_2(self, capsys):
        rc = main(["bench", "/nonexistent/does-not-exist"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "error:" in captured.err

    def test_bench_negative_reproducibility_exit_2(self, tmp_path, capsys):
        entries = tmp_path / "entries"
        entries.mkdir()
        rc = main(["bench", str(entries), "--reproducibility", "0"])
        assert rc == 2
        captured = capsys.readouterr()
        assert "reproducibility" in captured.err

    def test_bench_baseline_diff(self, monkeypatch, tmp_path, capsys):
        """`--baseline` accepts a JSON file with the bench shape and prints a delta."""
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._smoke_entries(tmp_path)

        # Build a synthetic baseline that matches the current run shape but
        # claims a previous PASS status — so a current PASS is a wash.
        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps(
                {
                    "summary": {
                        "entries": 1,
                        "correct": 1,
                        "accuracy": 1.0,
                        "reproducibility_avg": 1.0,
                        "tokens_in": 50,
                        "tokens_out": 12,
                        "latency_ms": 10,
                        "model": "gpt-4o-mini",
                        "prompt_version": "v1",
                        "reproducibility_n": 1,
                        "unavailable": 0,
                        "errors": 0,
                    },
                    "per_rule": [
                        {
                            "id": "cli-smoke",
                            "category": "clarity",
                            "intended_backend": "llm-rubric",
                            "expected": "pass",
                            "actual": "pass",
                            "status": "PASS",
                            "reproducibility": 1.0,
                            "primary_reason": "ok",
                            "failure_mode": None,
                            "tokens_in": 50,
                            "tokens_out": 12,
                            "latency_ms": 10,
                            "model": "gpt-4o-mini",
                            "prompt_version": "v1",
                        }
                    ],
                }
            )
        )

        rc = main(["bench", str(entries), "--baseline", str(baseline)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "baseline delta:" in captured.out
        assert "accuracy:" in captured.out

    def test_bench_json_baseline_single_object(self, monkeypatch, tmp_path, capsys):
        """--format json + --baseline must emit exactly one parseable JSON object (P2)."""
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._smoke_entries(tmp_path)

        baseline = tmp_path / "baseline.json"
        baseline.write_text(
            json.dumps(
                {
                    "summary": {
                        "entries": 1,
                        "correct": 1,
                        "accuracy": 1.0,
                        "reproducibility_avg": 1.0,
                        "tokens_in": 50,
                        "tokens_out": 12,
                        "latency_ms": 10,
                        "model": "gpt-4o-mini",
                        "prompt_version": "v1",
                        "reproducibility_n": 1,
                        "unavailable": 0,
                        "errors": 0,
                    },
                    "per_rule": [
                        {
                            "id": "cli-smoke",
                            "category": "clarity",
                            "intended_backend": "llm-rubric",
                            "expected": "pass",
                            "actual": "pass",
                            "status": "PASS",
                            "reproducibility": 1.0,
                            "primary_reason": "ok",
                            "failure_mode": None,
                            "tokens_in": 50,
                            "tokens_out": 12,
                            "latency_ms": 10,
                            "model": "gpt-4o-mini",
                            "prompt_version": "v1",
                        }
                    ],
                }
            )
        )

        rc = main(["bench", str(entries), "--format", "json", "--baseline", str(baseline)])
        assert rc == 0
        captured = capsys.readouterr()
        # Must parse as a single JSON value — json.loads raises if there are two top-level objects.
        data = json.loads(captured.out)
        assert "summary" in data
        assert "per_rule" in data
        assert "baseline_delta" in data
        assert "accuracy_delta" in data["baseline_delta"]

    def test_bench_baseline_missing_exit_2(self, monkeypatch, tmp_path, capsys):
        _patch_env(monkeypatch, _OPENAI_ENV)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)
        entries = self._smoke_entries(tmp_path)
        rc = main(
            [
                "bench",
                str(entries),
                "--baseline",
                str(tmp_path / "no-such-baseline.json"),
            ]
        )
        assert rc == 2
        captured = capsys.readouterr()
        assert "baseline" in captured.err


# ---------------------------------------------------------------------------
# Real-corpus loader smoke (no LLM call — just shape check)
# ---------------------------------------------------------------------------


def test_real_corpus_loads_and_targets_resolve():
    """The 24 fixture entries must load and every path target must resolve.

    This guards against drift between fixture files and the bench loader's
    schema when someone adds or renames an entry.
    """
    entries = list(_bench.iter_entries(ENTRIES_DIR))
    assert len(entries) >= 12  # plan baseline says ~24, accept >=12 to allow growth
    for e in entries:
        # Each call resolves either inline value or reads the targets/ file.
        text = e.resolve_target(TARGETS_DIR)
        assert isinstance(text, str)
