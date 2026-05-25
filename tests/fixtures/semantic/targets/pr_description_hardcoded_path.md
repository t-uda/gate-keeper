# Resolve DOTENV_PATH from a fixed devcontainer location

## Summary

Switch `llm_rubric.DOTENV_PATH` to a hardcoded module-level constant so
the backend always reads provider credentials from the same place on
this devcontainer.

## What changed

- In `src/gate_keeper/backends/llm_rubric.py`, set
  `DOTENV_PATH = Path("/home/vscode/.config/gate-keeper/.env")` at
  module load time.
- Drop the previous `Path.home() / ".config/gate-keeper/.env"`
  resolution.

## Validation

- `uv run pytest tests/test_llm_rubric_backend.py` passes locally on
  the devcontainer.
