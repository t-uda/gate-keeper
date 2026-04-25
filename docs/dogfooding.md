# Dogfooding gate-keeper on itself

`gate-keeper` is exercised against its own PRs to surface compiler/backend gaps
early. The rule of thumb in `AGENTS.md` is: **self-gating is advisory by default;
promote per-rule to required.** This document defines the promotion path.

## Why advisory first

Hard-gating an unproven rule on its own repo creates a deadlock: a bug in
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

## Issue hygiene

Every false positive, false negative, or unclear failure observed during
self-gating is filed as an issue with the `area:dogfood` label, ideally from
the PR that surfaced it. Without an issue the signal is lost; the advisory
phase is worthless if findings are not captured.

## Out of scope

- External repos consuming `gate-keeper` set their own promotion policy.
- This document does not list specific rules. Rule-level state lives next to
  the rule definitions.
