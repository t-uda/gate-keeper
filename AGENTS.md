# Agent Guidelines

<!-- Do not restructure or delete sections. Update individual values in-place when they change. -->

## Core Principles

- Keep this file under 20-30 lines of visible guidance.
- Keep only repo-specific, non-obvious instructions here.

## Project Overview

<!-- Replace this section in-place. Remove the placeholder line once filled. -->
- Local-first CLI that compiles rule documents (AGENTS.md, PR checklists) into compiler-style pass/fail with evidence.

## Commands

<!-- Replace this section in-place. Remove the placeholder block once filled. -->
~~~sh
uv sync
uv run gate-keeper --help
uv run pytest
~~~

## Code Conventions

<!-- Replace this section in-place. Remove the placeholder line once filled. -->
- Prefer deterministic backends over LLM rubric whenever evidence is available.
- Treat missing evidence as fail-closed; do not paper over with defaults.
- Keep `gh aw` out of package dependencies; document composition only.
- Self-gating on this repo is advisory by default; promote per-rule to required, see docs/dogfooding.md.

## Architecture

<!-- Replace this section in-place. Remove the placeholder line once filled. -->
- Pipeline: rule extraction -> IR -> backend routing -> evidence-bearing result.
- IR contract: src/gate_keeper/models.py (schema in docs/rule-ir.md).
- GitHub support sits behind a backend boundary so core works without network or `gh`.
- `uv` is the supported workflow runner.

## Maintenance Notes

<!-- This section is permanent. Do not delete. -->
- Delete stale or inferable guidance.
- Update commands and architecture when workflows change.
- Keep durable rules here; move detail to dedicated docs.
