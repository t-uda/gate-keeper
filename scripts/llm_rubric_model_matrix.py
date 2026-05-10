#!/usr/bin/env python3
"""Cross-model benchmark matrix for the llm-rubric backend (#177, first slice).

Runs the existing semantic fixture corpus across a small list of configured
models and emits a JSON report. Intentionally bounded: the goal of this slice
is to prove the matrix-loop architecture works without committing to surface-
area decisions (CLI integration, YAML config, full metrics).

Out of scope (deferred to follow-ups):

- ``gate-keeper bench --model-matrix`` CLI surface.
- YAML config (``docs/llm-model-matrix.yml``).
- Per-category accuracy, fabrication rate, target-kind correctness, latency
  summary stats, cost-estimation aggregates.
- Multi-provider (Anthropic) plumbing — this slice supports the OpenAI
  provider only, which is enough to validate the matrix shape across at
  least two distinct models (e.g. ``gpt-4o-mini`` vs ``gpt-4o``).

Usage::

    uv run python scripts/llm_rubric_model_matrix.py \\
        --models openai:gpt-4o-mini,openai:gpt-4o \\
        --entries-dir tests/fixtures/semantic/entries \\
        [--reproducibility 1] \\
        [--out report.json]

The script monkey-patches ``llm_rubric._load_env_file`` once per model so the
existing ``bench.run_bench`` evaluator picks up the per-model override without
needing a new model-injection seam in the backend. Tests use the same
mechanism, so a hermetic test never touches the network.

Refs: issue #177 / umbrella #164.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow running as ``python scripts/llm_rubric_model_matrix.py`` without a prior
# ``uv pip install -e .`` by inserting the in-tree ``src/`` onto ``sys.path``
# when the package is not already importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from gate_keeper import bench as _bench  # noqa: E402
from gate_keeper.backends import llm_rubric as _llm  # noqa: E402

_SUPPORTED_PROVIDERS = ("openai",)


@dataclass(frozen=True)
class ModelSpec:
    """A single (provider, model) pair from the ``--models`` argument."""

    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_models(spec: str) -> list[ModelSpec]:
    """Parse a ``--models`` argument like ``openai:gpt-4o-mini,openai:gpt-4o``.

    Order is preserved; duplicates are de-duplicated (first occurrence wins) so
    the report rows stay aligned with the user-declared sequence.
    """
    out: list[ModelSpec] = []
    seen: set[tuple[str, str]] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(
                f"--models entry {token!r} must be of the form 'provider:model' (e.g. 'openai:gpt-4o-mini')"
            )
        provider, model = token.split(":", 1)
        provider = provider.strip()
        model = model.strip()
        if provider not in _SUPPORTED_PROVIDERS:
            raise ValueError(
                f"--models entry {token!r}: provider {provider!r} not supported in this slice; "
                f"expected one of {list(_SUPPORTED_PROVIDERS)}"
            )
        if not model:
            raise ValueError(f"--models entry {token!r}: model identifier is empty")
        key = (provider, model)
        if key in seen:
            continue
        seen.add(key)
        out.append(ModelSpec(provider=provider, model=model))
    if not out:
        raise ValueError("--models must contain at least one provider:model entry")
    return out


def _build_env_for_model(spec: ModelSpec, base_env: dict[str, str]) -> dict[str, str]:
    """Return a per-model env dict for the llm_rubric loader.

    Reuses the API key from *base_env* (the on-disk dotenv) and overrides the
    provider + per-provider model identifier so ``_resolve_model`` returns
    ``spec.model`` regardless of what the dotenv had for the default.
    """
    env = dict(base_env)
    env["GATE_KEEPER_LLM_PROVIDER"] = spec.provider
    if spec.provider == "openai":
        env["GATE_KEEPER_OPENAI_MODEL"] = spec.model
        # Surface a clearer message than KeyError if the dotenv lacks the key.
        if "OPENAI_API_KEY" not in env:
            raise RuntimeError(
                "llm-rubric dotenv has no OPENAI_API_KEY; configure it before "
                "running the model matrix (see docs/llm-rubric.md)."
            )
    else:  # pragma: no cover — guarded by parse_models
        raise RuntimeError(f"unsupported provider: {spec.provider!r}")
    return env


def run_matrix(
    entries_dir: Path,
    models: list[ModelSpec],
    *,
    reproducibility: int = 1,
) -> dict[str, Any]:
    """Run the bench corpus once per model and assemble the JSON report.

    Each model loop monkey-patches the module-level ``_load_env_file`` to a
    closure returning the per-model env dict, then calls ``bench.run_bench``
    (which internally drives the existing llm_rubric ``check`` path). The
    original loader is restored after every iteration so a partial run does
    not leak the override onto subsequent code paths.
    """
    base_env = _llm._load_env_file()
    original_loader = _llm._load_env_file

    per_model_rows: list[dict[str, Any]] = []
    total_entries = 0
    total_correct = 0
    try:
        for spec in models:
            env = _build_env_for_model(spec, base_env)
            _llm._load_env_file = lambda *_a, _env=env, **_k: dict(_env)  # type: ignore[assignment]
            try:
                bench_result = _bench.run_bench(entries_dir, reproducibility=reproducibility)
            finally:
                _llm._load_env_file = original_loader  # type: ignore[assignment]

            entries_count = bench_result.summary.entries
            correct = bench_result.summary.correct
            accuracy = bench_result.summary.accuracy
            total_entries += entries_count
            total_correct += correct

            per_rule_rows: list[dict[str, Any]] = []
            for row in bench_result.per_rule:
                per_rule_rows.append(
                    {
                        "id": row.id,
                        "category": row.category,
                        "expected": row.expected,
                        "actual": row.actual,
                        "status": row.status,
                        "failure_mode": row.failure_mode,
                        "latency_ms": row.latency_ms,
                        "tokens_in": row.tokens_in,
                        "tokens_out": row.tokens_out,
                        "model": row.model,
                    }
                )

            per_model_rows.append(
                {
                    "provider": spec.provider,
                    "model": spec.model,
                    "label": spec.label,
                    "summary": {
                        "entries": entries_count,
                        "correct": correct,
                        "accuracy": accuracy,
                        "reproducibility_n": bench_result.summary.reproducibility_n,
                        "tokens_in": bench_result.summary.tokens_in,
                        "tokens_out": bench_result.summary.tokens_out,
                        "latency_ms": bench_result.summary.latency_ms,
                        "unavailable": bench_result.summary.unavailable,
                        "errors": bench_result.summary.errors,
                    },
                    "per_rule": per_rule_rows,
                }
            )
    finally:
        _llm._load_env_file = original_loader  # type: ignore[assignment]

    aggregate_accuracy = (total_correct / total_entries) if total_entries else 0.0
    return {
        "schema_version": 1,
        "prompt_version": _llm.PROMPT_VERSION,
        "reproducibility_n": reproducibility,
        "entries_dir": str(entries_dir),
        "models": per_model_rows,
        "aggregate": {
            "total": total_entries,
            "correct": total_correct,
            "accuracy": aggregate_accuracy,
        },
    }


def render_json(report: dict[str, Any]) -> str:
    """Render the matrix report as deterministic JSON."""
    return json.dumps(report, sort_keys=True, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm_rubric_model_matrix",
        description=(
            "Run the semantic fixture corpus across multiple llm-rubric models "
            "and emit a JSON report. First-slice scope (issue #177): single "
            "provider (OpenAI), minimal aggregate stats, no CLI integration."
        ),
    )
    parser.add_argument(
        "--models",
        required=True,
        help=("Comma-separated provider:model list, e.g. 'openai:gpt-4o-mini,openai:gpt-4o'."),
    )
    parser.add_argument(
        "--entries-dir",
        default=str(_REPO_ROOT / "tests" / "fixtures" / "semantic" / "entries"),
        help=(
            "Path to the semantic fixture entries directory. Defaults to the "
            "repo's tests/fixtures/semantic/entries/."
        ),
    )
    parser.add_argument(
        "--reproducibility",
        type=int,
        default=1,
        help="Number of provider calls per entry (majority vote). Defaults to 1.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the JSON report. Defaults to stdout.",
    )
    args = parser.parse_args(argv)

    try:
        models = parse_models(args.models)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover — argparse exits

    entries_dir = Path(args.entries_dir)
    if not entries_dir.is_dir():
        parser.error(f"--entries-dir does not exist or is not a directory: {entries_dir}")
        return 2  # pragma: no cover — argparse exits

    if args.reproducibility < 1:
        parser.error(f"--reproducibility must be >= 1, got {args.reproducibility}")
        return 2  # pragma: no cover — argparse exits

    report = run_matrix(entries_dir, models, reproducibility=args.reproducibility)
    rendered = render_json(report)

    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
