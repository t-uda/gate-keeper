"""Model-matrix config loader and driver for ``bench --model-matrix`` (#205, slice 2a).

This module defines the minimum-viable YAML config DSL for matrix runs and
the driver function that reads a config file, iterates over models, and
collects results into a flat JSON-serialisable structure.

YAML config shape
-----------------

::

    models:
      - openai:gpt-4o-mini
      - openai:gpt-4o
    fixtures: tests/fixtures/semantic/entries/  # relative to the config file
    reproducibility: 1   # optional, default 1

The output is a flat JSON array — one record per (model, fixture) result —
reusing the ``PerRuleResult.to_dict()`` shape from ``bench`` augmented with
``provider`` and ``model_label`` fields so downstream consumers don't need
new parsers.

Out of scope for slice 2a
--------------------------

- Per-category metrics rollup (slice 2b).
- Markdown summary output (slice 2b).
- Anthropic provider plumbing (slice 2c).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gate_keeper.backends import llm_rubric as _llm

# ---------------------------------------------------------------------------
# Config DSL
# ---------------------------------------------------------------------------

_REQUIRED_CONFIG_KEYS = frozenset({"models"})
_OPTIONAL_CONFIG_KEYS = frozenset({"fixtures", "reproducibility"})

_SUPPORTED_PROVIDERS = ("openai",)


class MatrixConfigError(ValueError):
    """Raised when the YAML config does not conform to the expected schema."""


def load_config(config_path: Path) -> dict[str, Any]:
    """Read and validate a matrix YAML config file.

    Returns the validated config as a plain dict with the following keys:

    - ``models``: list of ``"provider:model"`` strings (non-empty).
    - ``fixtures``: ``Path | None`` — resolved entries directory, or ``None``
      when omitted (the CLI resolves the default).
    - ``reproducibility``: int >= 1.

    Raises :class:`MatrixConfigError` on schema violations (including I/O
    errors and malformed YAML — both are wrapped into :class:`MatrixConfigError`
    with a descriptive message).
    """
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MatrixConfigError(f"cannot read config {config_path}: {exc.strerror}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MatrixConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise MatrixConfigError(
            f"config {config_path}: expected a YAML mapping at the top level, got {type(data).__name__}"
        )

    keys = set(data)
    missing = _REQUIRED_CONFIG_KEYS - keys
    if missing:
        raise MatrixConfigError(f"config {config_path}: missing required keys: {sorted(missing)}")
    unknown = keys - _REQUIRED_CONFIG_KEYS - _OPTIONAL_CONFIG_KEYS
    if unknown:
        raise MatrixConfigError(f"config {config_path}: unknown keys: {sorted(unknown)}")

    # models
    models_raw = data["models"]
    if not isinstance(models_raw, list) or not models_raw:
        raise MatrixConfigError(
            f"config {config_path}: 'models' must be a non-empty list of 'provider:model' strings"
        )
    for i, entry in enumerate(models_raw):
        if not isinstance(entry, str) or not entry.strip():
            raise MatrixConfigError(f"config {config_path}: 'models[{i}]' must be a non-empty string")

    # fixtures (optional — CLI provides the default entries_dir when absent)
    fixtures_raw = data.get("fixtures")
    if fixtures_raw is not None:
        if not isinstance(fixtures_raw, str) or not fixtures_raw.strip():
            raise MatrixConfigError(f"config {config_path}: 'fixtures' must be a non-empty path string")
        fixtures_path: Path | None = Path(fixtures_raw)
        if not fixtures_path.is_absolute():
            # Resolve relative to the config file's parent directory.
            fixtures_path = (config_path.parent / fixtures_path).resolve()
    else:
        fixtures_path = None

    # reproducibility (optional, default 1)
    reproducibility_raw = data.get("reproducibility", 1)
    if not isinstance(reproducibility_raw, int) or isinstance(reproducibility_raw, bool):
        raise MatrixConfigError(
            f"config {config_path}: 'reproducibility' must be an integer, "
            f"got {type(reproducibility_raw).__name__}"
        )
    if reproducibility_raw < 1:
        raise MatrixConfigError(
            f"config {config_path}: 'reproducibility' must be >= 1, got {reproducibility_raw}"
        )

    return {
        "models": [m.strip() for m in models_raw],
        "fixtures": fixtures_path,
        "reproducibility": reproducibility_raw,
    }


# ---------------------------------------------------------------------------
# Model spec helpers (mirrored from scripts/llm_rubric_model_matrix.py so the
# package does not depend on the scripts/ tree at runtime)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """A single (provider, model) pair parsed from a ``"provider:model"`` string."""

    provider: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_model_specs(raw_models: list[str]) -> list[ModelSpec]:
    """Parse a list of ``"provider:model"`` strings into :class:`ModelSpec` objects.

    Bare model names (no colon) are treated as ``openai:<name>`` for
    convenience. Duplicates are de-duplicated (first occurrence wins).

    Raises :class:`MatrixConfigError` on unknown providers or empty model names.
    """
    out: list[ModelSpec] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_models:
        token = raw.strip()
        if not token:
            continue
        if ":" not in token:
            # Bare name — default to openai provider.
            provider, model = "openai", token
        else:
            provider, model = token.split(":", 1)
            provider = provider.strip()
            model = model.strip()
        if provider not in _SUPPORTED_PROVIDERS:
            raise MatrixConfigError(
                f"model {token!r}: provider {provider!r} not supported in this slice; "
                f"expected one of {list(_SUPPORTED_PROVIDERS)}"
            )
        if not model:
            raise MatrixConfigError(f"model {token!r}: model identifier is empty")
        key = (provider, model)
        if key in seen:
            continue
        seen.add(key)
        out.append(ModelSpec(provider=provider, model=model))
    if not out:
        raise MatrixConfigError("models list must contain at least one provider:model entry")
    return out


def _build_env_for_model(spec: ModelSpec, base_env: dict[str, str]) -> dict[str, str]:
    """Return a per-model env dict for the llm_rubric loader."""
    env = dict(base_env)
    env["GATE_KEEPER_LLM_PROVIDER"] = spec.provider
    if spec.provider == "openai":
        env["GATE_KEEPER_OPENAI_MODEL"] = spec.model
        if not env.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError(
                "llm-rubric dotenv has no OPENAI_API_KEY (or it is blank); configure it before "
                "running the model matrix (see docs/llm-rubric.md)."
            )
    else:  # pragma: no cover — guarded by parse_model_specs
        raise RuntimeError(f"unsupported provider: {spec.provider!r}")
    return env


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_matrix(
    config: dict[str, Any],
    entries_dir: Path,
) -> list[dict[str, Any]]:
    """Run the bench corpus for each model in the config and return flat JSON rows.

    *config* must be the validated dict returned by :func:`load_config` — the
    caller is expected to parse the config once and pass it here, so there is no
    duplicate I/O or TOCTOU risk.

    Each element of the returned list is a ``PerRuleResult.to_dict()`` record
    augmented with ``"provider"`` and ``"model_label"`` fields identifying
    which model produced the row. This flat shape lets downstream consumers
    (jq, pandas, etc.) filter/group without needing to unwrap a nested
    per-model envelope.

    The loader monkey-patches ``llm_rubric._load_env_file`` once per model so
    ``bench.run_bench`` picks up the per-model override without requiring a new
    injection seam in the backend. The original loader is always restored after
    each iteration.
    """
    from gate_keeper import bench as _bench

    model_specs = parse_model_specs(config["models"])

    base_env = _llm._load_env_file()
    original_loader = _llm._load_env_file

    flat_rows: list[dict[str, Any]] = []
    try:
        for spec in model_specs:
            env = _build_env_for_model(spec, base_env)
            _llm._load_env_file = lambda *_a, _env=env, **_k: dict(_env)  # type: ignore[assignment]
            try:
                bench_result = _bench.run_bench(
                    entries_dir,
                    reproducibility=config["reproducibility"],
                )
            finally:
                _llm._load_env_file = original_loader  # type: ignore[assignment]

            for row in bench_result.per_rule:
                record = row.to_dict()
                record["provider"] = spec.provider
                record["model_label"] = spec.label
                flat_rows.append(record)
    finally:
        _llm._load_env_file = original_loader  # type: ignore[assignment]

    return flat_rows


def render_flat_json(rows: list[dict[str, Any]]) -> str:
    """Render the flat row list as deterministic JSON."""
    return json.dumps(rows, sort_keys=True, indent=2)


__all__ = [
    "MatrixConfigError",
    "ModelSpec",
    "load_config",
    "parse_model_specs",
    "render_flat_json",
    "run_matrix",
]
