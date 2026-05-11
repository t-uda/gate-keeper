"""Unit tests for ``gate_keeper.matrix`` — YAML config parser and CLI integration.

Real provider calls are not made here (paid API). The ``_call_openai`` helper
and ``_load_env_file`` are monkeypatched to stub responses, mirroring the
pattern in ``tests/test_cli_bench.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_keeper import matrix as _matrix
from gate_keeper.backends import llm_rubric as _llm
from gate_keeper.cli import main

_OPENAI_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-test-stub-not-real",
}


def _stub_openai_pass(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "judgment": "pass",
            "primary_reason": "The artefact satisfies the rule.",
            "supporting_evidence_quotes": ["example artefact body"],
            "suggested_action": None,
        }
    )
    return body, {"latency_ms": 10, "tokens_in": 50, "tokens_out": 12}


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


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_minimal_config(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\n")
        result = _matrix.load_config(cfg)
        assert result["models"] == ["openai:gpt-4o-mini"]
        assert result["fixtures"] is None
        assert result["reproducibility"] == 1

    def test_full_config_relative_fixtures(self, tmp_path):
        entries = tmp_path / "entries"
        entries.mkdir()
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\nfixtures: entries\nreproducibility: 3\n")
        result = _matrix.load_config(cfg)
        assert result["models"] == ["openai:gpt-4o-mini"]
        assert result["fixtures"] == entries.resolve()
        assert result["reproducibility"] == 3

    def test_absolute_fixtures_path(self, tmp_path):
        entries = tmp_path / "entries"
        entries.mkdir()
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"models:\n  - openai:gpt-4o-mini\nfixtures: {entries}\n")
        result = _matrix.load_config(cfg)
        assert result["fixtures"] == entries

    def test_missing_models_key_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("reproducibility: 1\n")
        with pytest.raises(_matrix.MatrixConfigError, match="missing required keys"):
            _matrix.load_config(cfg)

    def test_unknown_key_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\nextra_key: oops\n")
        with pytest.raises(_matrix.MatrixConfigError, match="unknown keys"):
            _matrix.load_config(cfg)

    def test_empty_models_list_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models: []\n")
        with pytest.raises(_matrix.MatrixConfigError, match="non-empty list"):
            _matrix.load_config(cfg)

    def test_reproducibility_zero_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\nreproducibility: 0\n")
        with pytest.raises(_matrix.MatrixConfigError, match=">= 1"):
            _matrix.load_config(cfg)

    def test_reproducibility_float_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\nreproducibility: 1.5\n")
        with pytest.raises(_matrix.MatrixConfigError, match="integer"):
            _matrix.load_config(cfg)

    def test_invalid_yaml_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models: [\n")
        with pytest.raises(_matrix.MatrixConfigError, match="invalid YAML"):
            _matrix.load_config(cfg)

    def test_nonexistent_file_raises(self, tmp_path):
        cfg = tmp_path / "no-such.yaml"
        with pytest.raises(_matrix.MatrixConfigError, match="cannot read config"):
            _matrix.load_config(cfg)

    def test_top_level_not_mapping_raises(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("- just\n- a\n- list\n")
        with pytest.raises(_matrix.MatrixConfigError, match="expected a YAML mapping"):
            _matrix.load_config(cfg)


# ---------------------------------------------------------------------------
# parse_model_specs
# ---------------------------------------------------------------------------


class TestParseModelSpecs:
    def test_two_qualified_models(self):
        specs = _matrix.parse_model_specs(["openai:gpt-4o-mini", "openai:gpt-4o"])
        assert [s.label for s in specs] == ["openai:gpt-4o-mini", "openai:gpt-4o"]

    def test_bare_name_defaults_to_openai(self):
        specs = _matrix.parse_model_specs(["gpt-4o-mini"])
        assert specs[0].provider == "openai"
        assert specs[0].model == "gpt-4o-mini"

    def test_deduplication(self):
        specs = _matrix.parse_model_specs(["openai:gpt-4o", "openai:gpt-4o"])
        assert len(specs) == 1

    def test_unsupported_provider_raises(self):
        with pytest.raises(_matrix.MatrixConfigError, match="not supported"):
            _matrix.parse_model_specs(["anthropic:claude-haiku-4-5"])

    def test_empty_list_raises(self):
        with pytest.raises(_matrix.MatrixConfigError, match="at least one"):
            _matrix.parse_model_specs([])


# ---------------------------------------------------------------------------
# run_matrix (hermetic — monkeypatched provider)
# ---------------------------------------------------------------------------


class TestRunMatrix:
    def test_flat_rows_two_models(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_OPENAI_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            f"models:\n  - openai:gpt-4o-mini\n  - openai:gpt-4o\n"
            f"fixtures: {entries_dir}\n"
            f"reproducibility: 1\n"
        )

        rows = _matrix.run_matrix(_matrix.load_config(cfg), entries_dir)

        assert len(rows) == 2
        labels = [r["model_label"] for r in rows]
        assert labels == ["openai:gpt-4o-mini", "openai:gpt-4o"]
        for row in rows:
            assert row["id"] == "smoke-01"
            assert row["status"] == "PASS"
            assert row["provider"] == "openai"
            assert "model_label" in row
            # Standard PerRuleResult fields must be present.
            assert "category" in row
            assert "expected" in row
            assert "actual" in row

    def test_restores_loader_after_run(self, monkeypatch, tmp_path):
        sentinel = dict(_OPENAI_ENV)

        def _sentinel_loader(*_a, **_k):
            return dict(sentinel)

        monkeypatch.setattr(_llm, "_load_env_file", _sentinel_loader)
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"models:\n  - openai:gpt-4o-mini\nfixtures: {entries_dir}\n")

        _matrix.run_matrix(_matrix.load_config(cfg), entries_dir)
        assert _llm._load_env_file is _sentinel_loader


# ---------------------------------------------------------------------------
# CLI integration — bench --model-matrix
# ---------------------------------------------------------------------------


class TestCliBenchModelMatrix:
    def test_help_shows_model_matrix_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["bench", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--model-matrix" in out

    def test_matrix_emits_flat_json(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_OPENAI_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"models:\n  - openai:gpt-4o-mini\nfixtures: {entries_dir}\n")

        rc = main(["bench", "--model-matrix", str(cfg)])
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert rows[0]["model_label"] == "openai:gpt-4o-mini"
        assert rows[0]["status"] == "PASS"

    def test_matrix_entries_dir_from_positional(self, monkeypatch, tmp_path, capsys):
        """entries_dir positional arg is used when 'fixtures' key is absent from config."""
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_OPENAI_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_openai_pass)

        entries_dir = _single_entry_dir(tmp_path)
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\n")

        rc = main(["bench", str(entries_dir), "--model-matrix", str(cfg)])
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 1

    def test_matrix_missing_config_exit_2(self, capsys):
        rc = main(["bench", "--model-matrix", "/no/such/config.yaml"])
        assert rc == 2
        assert "no such file" in capsys.readouterr().err

    def test_matrix_no_entries_dir_exit_2(self, tmp_path, capsys):
        """Neither 'fixtures' in config nor positional entries_dir → usage error."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("models:\n  - openai:gpt-4o-mini\n")
        rc = main(["bench", "--model-matrix", str(cfg)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "entries_dir" in err or "fixtures" in err

    def test_standard_bench_still_requires_entries_dir(self, capsys):
        """Omitting entries_dir without --model-matrix must still give usage error."""
        rc = main(["bench"])
        assert rc == 2
        assert "entries_dir" in capsys.readouterr().err or rc == 2
