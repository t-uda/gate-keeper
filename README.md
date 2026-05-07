# gate-keeper

`gate-keeper` compiles natural-language project rules into verifiable checks.

The first target is a small rule compiler that works locally and can also be
used beside GitHub Agentic Workflows (`gh aw`) without coupling itself to that
runtime. The intended shape is:

1. read a natural-language Markdown rule document with normative bullets,
   headings, or checklists;
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

See [docs/mvp-spec.md](docs/mvp-spec.md),
[docs/issue-plan.md](docs/issue-plan.md), and
[docs/mvp-readiness.md](docs/mvp-readiness.md) for the MVP completion
checklist, known limitations, and upgrade path.

The initial execution board is
[gate-keeper 3-day MVP](https://github.com/users/t-uda/projects/2).

## Development

Requires Python 3.10 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run gate-keeper --help
uv run pytest
uvx ruff check .
uvx pyright
```

CI runs the same `uv sync` and `uv run pytest` steps on Python 3.10.

## Optional: textlint for documentation prose

`gate-keeper` ships a textlint configuration (`.textlintrc.json`,
`.textlintignore`) that lints repository Markdown for terminology
consistency. textlint runs on Node, which `gate-keeper` does not bundle —
mirrors the `gh aw` stance: install Node yourself, the CLI does not vend it.

Requirements:

- Node 20 or later (declared in `package.json` `engines`);
- `npm install` once at the repository root to fetch textlint and its
  rule packages.

Run:

```bash
npm run textlint                  # lint docs/**/*.md and README.md
npx --no textlint path/to/file.md # lint a specific file (refuses to install)
```

The corpus has known terminology findings today; failures from
`npm run textlint` against the existing corpus are expected and tracked
separately. New documentation should not introduce additional findings.

Once `gate-keeper`'s textlint adapter (#94) is registered, `gate-keeper
validate` can invoke the same textlint installation against `external_check`
rules — the adapter shells out to `npx textlint --format json` and maps each
finding to a structured Diagnostic. See
[docs/backend-external.md](docs/backend-external.md) for the adapter
contract.

## Optional: LLM rubric backend

`gate-keeper` ships with an `llm-rubric` backend that handles `semantic_rubric`
rules — those that cannot be verified deterministically. By default the
backend is unconfigured and returns a fail-closed `unavailable` diagnostic, so
no LLM calls are made.

To enable it, create a per-project dotenv file at
`~/.config/hermes-projects/gate-keeper.env` (`chmod 600`) on the host with one
of:

```sh
GATE_KEEPER_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

```sh
GATE_KEEPER_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Credentials are read from this file via `python-dotenv` only — `os.environ`
is intentionally not consulted, to avoid collisions with the host container's
global API-key environment. See
[docs/llm-rubric.md](docs/llm-rubric.md) and the hermes-engineering
`docs/auth-matrix.md` "Issue #147" section for the rationale.
