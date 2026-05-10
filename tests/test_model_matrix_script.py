"""Hermetic tests for ``scripts/llm_rubric_model_matrix.py`` (#177).

The matrix script wraps the existing bench harness in a per-model loop. We
exercise the wrapper end-to-end against a one-entry inline fixture corpus
with the OpenAI provider stubbed out, mirroring the pattern in
``tests/test_cli_bench.py`` and ``tests/test_llm_rubric_backend.py``.

No real API key is required and no network call is made.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from gate_keeper.backends import llm_rubric as _llm

# ---------------------------------------------------------------------------
# Module loader (the script lives outside the importable package tree)
# ---------------------------------------------------------------------------


def _load_matrix_module():
    """Import ``scripts/llm_rubric_model_matrix.py`` as a module by file path."""
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "llm_rubric_model_matrix.py"
    spec = importlib.util.spec_from_file_location("llm_rubric_model_matrix", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["llm_rubric_model_matrix"] = module
    spec.loader.exec_module(module)
    return module


matrix = _load_matrix_module()


# ---------------------------------------------------------------------------
# Stubs (mirror tests/test_cli_bench.py)
# ---------------------------------------------------------------------------


def _stub_openai_pass(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "judgment": "pass",
            "primary_reason": "The artefact satisfies the rule.",
            "supporting_evidence_quotes": ["example artefact body"],
            "suggested_action": None,
        }
    )
    return body, {"latency_ms": 11, "tokens_in": 50, "tokens_out": 12}


def _single_entry_dir(tmp_path: Path) -> Path:
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


_BASE_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-test-stub-not-real",
}


# ---------------------------------------------------------------------------
# parse_models
# ---------------------------------------------------------------------------


class TestParseModels:
    def test_parses_two_entries_in_order(self):
        specs = matrix.parse_models("openai:gpt-4o-mini,openai:gpt-4o")
        assert [s.label for s in specs] == ["openai:gpt-4o-mini", "openai:gpt-4o"]

    def test_dedupes_repeated_pairs(self):
        specs = matrix.parse_models("openai:gpt-4o,openai:gpt-4o")
        assert [s.label for s in specs] == ["openai:gpt-4o"]

    def test_rejects_missing_colon(self):
        with pytest.raises(ValueError, match="provider:model"):
            matrix.parse_models("gpt-4o-mini")

    def test_rejects_unsupported_provider(self):
        with pytest.raises(ValueError, match="not supported"):
            matrix.parse_models("anthropic:claude-haiku-4-5")

    def test_rejects_empty_input(self):
        with pytest.raises(ValueError, match="at least one"):
            matrix.parse_models("")


# ---------------------------------------------------------------------------
# run_matrix
# ---------------------------------------------------------------------------


class TestRunMatrix:
    def test_emits_expected_shape_two_models(self, monkeypatch, tmp_path):
        # Hermetic: stub both the dotenv loader and the provider helper.
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        specs = matrix.parse_models("openai:gpt-4o-mini,openai:gpt-4o")

        report = matrix.run_matrix(entries_dir, specs, reproducibility=1)

        assert report["schema_version"] == 1
        assert report["prompt_version"] == _llm.PROMPT_VERSION
        assert report["reproducibility_n"] == 1
        assert report["entries_dir"] == str(entries_dir)

        models_section = report["models"]
        assert len(models_section) == 2
        assert [m["label"] for m in models_section] == [
            "openai:gpt-4o-mini",
            "openai:gpt-4o",
        ]

        for row, expected_model in zip(models_section, ["gpt-4o-mini", "gpt-4o"]):
            assert row["provider"] == "openai"
            assert row["model"] == expected_model
            assert row["summary"]["entries"] == 1
            assert row["summary"]["correct"] == 1
            assert row["summary"]["accuracy"] == 1.0
            assert row["summary"]["tokens_in"] == 50
            assert row["summary"]["tokens_out"] == 12
            assert len(row["per_rule"]) == 1
            entry_row = row["per_rule"][0]
            assert entry_row["id"] == "smoke-01"
            assert entry_row["status"] == "PASS"
            assert entry_row["expected"] == "pass"
            assert entry_row["actual"] == "pass"
            # The bench harness records the resolved model on each row; this
            # is the seam the matrix script depends on.
            assert entry_row["model"] == expected_model

        aggregate = report["aggregate"]
        assert aggregate["total"] == 2
        assert aggregate["correct"] == 2
        assert aggregate["accuracy"] == 1.0

    def test_restores_loader_after_run(self, monkeypatch, tmp_path):
        sentinel_env = dict(_BASE_ENV)

        def _sentinel_loader(*_a, **_k):
            return dict(sentinel_env)

        monkeypatch.setattr(_llm, "_load_env_file", _sentinel_loader)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        specs = matrix.parse_models("openai:gpt-4o-mini")

        matrix.run_matrix(entries_dir, specs, reproducibility=1)

        # After the matrix run completes, the original loader must be back in
        # place so subsequent code paths see the unmodified env.
        assert _llm._load_env_file is _sentinel_loader

    def test_raises_when_api_key_missing(self, monkeypatch, tmp_path):
        # No OPENAI_API_KEY in the simulated dotenv → script raises a clear
        # message rather than crashing inside _call_openai.
        monkeypatch.setattr(
            _llm,
            "_load_env_file",
            lambda *a, **k: {"GATE_KEEPER_LLM_PROVIDER": "openai"},
        )

        entries_dir = _single_entry_dir(tmp_path)
        specs = matrix.parse_models("openai:gpt-4o-mini")

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            matrix.run_matrix(entries_dir, specs, reproducibility=1)


# ---------------------------------------------------------------------------
# main (CLI entry point)
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_main_writes_report_to_out(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        out_path = tmp_path / "report.json"

        rc = matrix.main(
            [
                "--models",
                "openai:gpt-4o-mini",
                "--entries-dir",
                str(entries_dir),
                "--out",
                str(out_path),
            ]
        )
        assert rc == 0
        assert out_path.is_file()

        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["aggregate"]["total"] == 1
        assert report["aggregate"]["correct"] == 1
        assert len(report["models"]) == 1
        assert report["models"][0]["label"] == "openai:gpt-4o-mini"

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_main_rejects_non_positive_reproducibility(self, tmp_path, capsys, bad):
        """--reproducibility <= 0 must exit cleanly via argparse, not propagate
        a ValueError out of bench.run_bench as an uncaught traceback (#177
        codex P2)."""
        entries_dir = _single_entry_dir(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            matrix.main(
                [
                    "--models",
                    "openai:gpt-4o-mini",
                    "--entries-dir",
                    str(entries_dir),
                    "--reproducibility",
                    bad,
                ]
            )
        assert excinfo.value.code == 2
        captured = capsys.readouterr()
        assert "--reproducibility" in captured.err
