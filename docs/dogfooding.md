# Dogfooding gate-keeper on itself

> Status: internal process artifact — not user-facing reference.

`gate-keeper` is exercised against its own PRs to surface compiler/backend gaps
early. The rule of thumb in `AGENTS.md` is: **self-gating is advisory by default;
promote per-rule to required.** This document defines the promotion path.

## Why advisory first

Hard-gating an unproven rule on its own repository creates a deadlock: a bug in
`gate-keeper` blocks the PR that would fix it. Advisory mode runs the same
checks but reports as PR comments only, never as a required status.

## Promotion criteria (per rule)

A single rule may be promoted from advisory to required when **all** hold:

1. The rule has produced no false positive across the trailing 10 merged PRs
   that touched the relevant scope.
2. The rule has produced at least one true positive (caught a real violation),
   or its absence has been explicitly justified in an issue.
3. The deterministic backend is the source of truth for the rule. LLM-rubric
   rules stay advisory until a deterministic equivalent exists or an explicit
   exception is recorded here.
4. Fail-closed behavior on missing evidence has been validated against a
   synthetic PR that withholds the evidence.

Promotion is per-rule, not per-ruleset. Demotion is always allowed and does
not require ceremony beyond an issue noting the trigger.

## Artifact kind for self-gating runs (#178)

When the dogfood loop validates a PR body or a commit message, it must
pass `--artifact-kind` so rules whose `target_kind` annotation does not
match the artifact short-circuit deterministically (`Status.UNSUPPORTED`
with `evidence.kind=target_kind_mismatch`, `llm_called=false`) instead
of routing through the LLM. Use `pr_description` for PR bodies and
`commit_message` for commit messages:

```sh
# Validating a PR description.
uv run gate-keeper validate docs/dogfooding-rules.md \
  --target "$(gh pr view <N> --json body --jq .body)" \
  --artifact-kind pr_description

# Validating a commit message.
uv run gate-keeper validate docs/dogfooding-rules.md \
  --target "$(git log -1 --format=%B)" \
  --artifact-kind commit_message
```

Rules whose `target_kind` is `unspecified` ignore the flag — only
annotated rules participate in the deterministic dispatch. See
[`cli-reference.md` — Deterministic target_kind mismatch
(#178)](cli-reference.md#deterministic-target_kind-mismatch-178) for
the evidence shape and full vocabulary.

## Issue hygiene

Every false positive, false negative, or unclear failure observed during
self-gating is filed as an issue with the `area:dogfood` label, ideally from
the PR that surfaced it. Without an issue the signal is lost; the advisory
phase is worthless if findings are not captured.

## Initial advisory rule pool

The seed pool of semantic self-gating rules lives in
[`dogfooding-rules.md`](dogfooding-rules.md). All three rules in that
document are advisory only at the time of authoring; none meets the
promotion criteria in this document yet.

## Dependency-gate dogfood (umbrella #159)

Slice 1 of umbrella #159 ships a project-local `external_check` rule that
gates `src/gate_keeper/cli.py` against `docs/cli-reference.md`. The rule
lives in [`dependency-gate-rules.json`](dependency-gate-rules.json) and runs
through the `command` adapter. Because `command` adapter rules are disabled
by default, exercising it requires `--allow-command-adapter`:

```sh
uv run gate-keeper validate \
  --rules-format ir docs/dependency-gate-rules.json \
  --target docs/cli-reference.md \
  --allow-command-adapter
```

The validator script (`scripts/dependency_gates/check_cli_reference.py`)
is general — it reads `.gate-keeper/dependency-manifest.yml` and validates
any edge whose `to` matches the supplied target. Adding new dependency
edges does not require a new rule. The contract is documented in
[`design/dependency-gates.md`](design/dependency-gates.md). This rule is
**advisory only**; promotion follows the §"Promotion criteria" path above.

## Out of scope

- External repos consuming `gate-keeper` set their own promotion policy.
- This document does not list specific rules. Rule-level state lives next to
  the rule definitions: see [`dogfooding-rules.md`](dogfooding-rules.md) for
  the semantic advisory pool, [`example-rules.md`](example-rules.md) for
  the deterministic examples, and
  [`dependency-gate-rules.json`](dependency-gate-rules.json) for the
  declarative artifact-dependency gates.
