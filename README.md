# gate-keeper

`gate-keeper` compiles natural-language project rules into verifiable checks.

The first target is a small rule compiler that works locally and can also be
used beside GitHub Agentic Workflows (`gh aw`) without coupling itself to that
runtime. The intended shape is:

1. read a rule document such as `SKILL.md`, `AGENTS.md`, a PR checklist, or a
   compact project spec;
2. extract explicit rule units into a small intermediate representation;
3. route each rule to a deterministic backend when possible;
4. fall back to an LLM rubric backend for semantic checks;
5. emit compiler-style pass/fail output with enough evidence to stop safely.

## MVP

The 3-day MVP is intentionally narrow:

- local CLI installable with `uv`;
- Markdown rule extraction into JSON;
- filesystem/text validation backend for local use;
- GitHub PR validation backend powered by `gh` and GraphQL;
- explicit fail-closed results for unavailable context;
- advisory `gh aw` integration docs, not a hard runtime dependency.

See [docs/mvp-spec.md](docs/mvp-spec.md) and
[docs/issue-plan.md](docs/issue-plan.md).

## Development

```bash
uv sync
uv run gate-keeper --help
```
