# gate-keeper

Compile natural-language rules into verifiable local and GitHub checks.

## Quickstart

1. Install with `uv sync`.
2. Write rules in `RULES.md` as a Markdown bullet list.
3. Run `uv run gate-keeper validate RULES.md --target ./` to check the local
   directory against your rules.

The command exits `0` on pass, `1` on fail, `2` on usage error.
