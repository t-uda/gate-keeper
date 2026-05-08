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

## Documentation

### For users

- [docs/getting-started.md](docs/getting-started.md) — install, write your first rule document, and run your first validation
- [docs/llm-rubric.md](docs/llm-rubric.md) — LLM rubric backend: configuration, evidence shape, and credential setup
- [docs/gh-aw.md](docs/gh-aw.md) — GitHub Agentic Workflows (`gh aw`) integration guide
- [docs/example-rules.md](docs/example-rules.md) — annotated rule examples across all backends

### Reference

- [docs/rule-ir.md](docs/rule-ir.md) — rule intermediate representation schema
- [docs/backend-external.md](docs/backend-external.md) — external tool adapter contract (`Backend.EXTERNAL`)
- [docs/semantic-rules.md](docs/semantic-rules.md) — semantic rubric rule authoring guide

### Internal (process artifacts)

These files record design decisions and planning context. They are not
user-facing reference material.

- [docs/mvp-spec.md](docs/mvp-spec.md) — original MVP task specification
- [docs/mvp-readiness.md](docs/mvp-readiness.md) — MVP completion checklist
- [docs/issue-plan.md](docs/issue-plan.md) — 3-day MVP issue structure
- [docs/dogfooding.md](docs/dogfooding.md) — self-gating policy and advisory rules
- [docs/dogfooding-rules.md](docs/dogfooding-rules.md) — seed semantic rule pool
- [docs/design/hybrid-rule.md](docs/design/hybrid-rule.md) — hybrid rule kind design (#73)
- [docs/design/multi-target.md](docs/design/multi-target.md) — multi-target evaluation design (#74)
- [docs/textlint/severity-policy.md](docs/textlint/severity-policy.md) — textlint severity mapping decision record (#85)
- [docs/textlint/per-doc-type-policy.md](docs/textlint/per-doc-type-policy.md) — per-doc-type textlint config policy (#91)
- [docs/textlint/package-set.md](docs/textlint/package-set.md) — textlint initial package set decision record (#81)
- [docs/textlint/ja-preset-evaluation.md](docs/textlint/ja-preset-evaluation.md) — Japanese preset evaluation decision record (#83)
- [docs/textlint/exception-policy.md](docs/textlint/exception-policy.md) — false-positive handling and exception policy (#90)
- [docs/textlint/prh-process.md](docs/textlint/prh-process.md) — `prh.yml` contribution process (#84)

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
