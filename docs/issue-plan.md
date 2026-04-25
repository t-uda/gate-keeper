# Issue Plan

This is the intended GitHub issue structure for the 3-day MVP.

## Day 1: Local Compiler Core

1. Bootstrap uv package, MIT license, and developer workflow.
2. Define rule IR and diagnostic schema.
3. Implement Markdown rule extraction.
4. Implement `compile` CLI with JSON output.
5. Implement filesystem/text backend.
6. Add local fixtures and tests.

## Day 2: Validation Runtime and GitHub Backend

7. Implement validation orchestrator and backend routing.
8. Implement compiler-style diagnostics and exit codes.
9. Implement GitHub PR metadata checks through `gh pr view`.
10. Implement unresolved review thread check through GraphQL.
11. Implement independent review check and documented limits.
12. Add fail-closed handling for GitHub unavailable evidence.

## Day 3: Integration, Docs, and Hardening

13. Add LLM rubric backend interface.
14. Add `explain` command for rule-to-backend mapping.
15. Document `gh aw` composition patterns.
16. Add end-to-end examples and smoke tests.
17. Harden packaging, README, and contribution notes.
18. Cut MVP readiness checklist.
