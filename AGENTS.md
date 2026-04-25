# Repository Guidance

- Keep `gate-keeper` usable as a local CLI without GitHub or network access.
- Prefer deterministic checks over LLM judgement whenever the required evidence
  is available.
- Treat missing evidence as fail-closed.
- Keep GitHub support isolated behind a backend boundary.
- Do not make `gh aw` a package dependency; document composition instead.
- Use `uv` for dependency management and command execution.
