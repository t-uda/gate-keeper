# Resolve DOTENV_PATH relative to the user's home directory

## Summary

Make `llm_rubric.DOTENV_PATH` portable across host accounts and
devcontainer layouts by resolving it from the current user's home
directory instead of a hardcoded `/home/vscode/...` literal.

## What changed

- In `src/gate_keeper/backends/llm_rubric.py`, define
  `DOTENV_PATH = Path.home() / ".config/gate-keeper/.env"` so the
  backend works for any user account.
- Document the resolution rule in `docs/llm-rubric.md`.

## Validation

- `uv run pytest tests/test_llm_rubric_backend.py` passes on the
  devcontainer (`vscode` user) and on a host login with a different
  username.
