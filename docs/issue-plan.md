# Issue Plan

This is the intended GitHub issue structure for the 3-day MVP.

Project: https://github.com/users/t-uda/projects/2

Tracker: https://github.com/t-uda/gate-keeper/issues/21

## Source of Truth

Implementation should follow the precedence in
[docs/mvp-spec.md](mvp-spec.md#planning-authority). Individual issues describe
the work slice, but they must not redefine the shared CLI, IR, diagnostic, or
backend contracts.

## Dependency Order

- #1 blocks #2, #3, #4, #5, #6, #8, #15, and #16.
- #2 blocks #3, #4, #7, #16, and #18.
- #3 blocks #6, #7, #8, and #10 through #16.
- #4 blocks #7, #16, and #18.
- #5 blocks #6, #8 through #15, and #18.
- #6 and #7 block #8.
- #8 blocks #9 through #15.
- #9 and #14 block #10 through #13.
- #17 through #20 are final hardening and must not redefine core contracts.

## Day 1: Local Compiler Core

1. [Define rule IR and diagnostic schema](https://github.com/t-uda/gate-keeper/issues/1).
2. [Implement Markdown rule document parser](https://github.com/t-uda/gate-keeper/issues/2).
3. [Add rule classification and backend hinting](https://github.com/t-uda/gate-keeper/issues/3).
4. [Implement `compile` CLI with JSON output](https://github.com/t-uda/gate-keeper/issues/4).
5. [Implement diagnostic renderer and exit codes](https://github.com/t-uda/gate-keeper/issues/5).
6. [Implement filesystem and text backend](https://github.com/t-uda/gate-keeper/issues/6).
7. [Add local fixtures and parser/backend tests](https://github.com/t-uda/gate-keeper/issues/7).

## Day 2: Validation Runtime and GitHub Backend

8. [Implement validation orchestrator and backend registry](https://github.com/t-uda/gate-keeper/issues/8).
9. [Implement GitHub target resolver around `gh`](https://github.com/t-uda/gate-keeper/issues/9).
10. [Implement PR state, draft, label, and tasklist checks](https://github.com/t-uda/gate-keeper/issues/10).
11. [Implement status check rollup validation](https://github.com/t-uda/gate-keeper/issues/11).
12. [Implement unresolved review thread GraphQL check](https://github.com/t-uda/gate-keeper/issues/12).
13. [Implement independent review check and limits](https://github.com/t-uda/gate-keeper/issues/13).
14. [Add GitHub fail-closed error handling and pagination policy](https://github.com/t-uda/gate-keeper/issues/14).

## Day 3: Integration, Docs, and Hardening

15. [Add LLM rubric backend interface](https://github.com/t-uda/gate-keeper/issues/15).
16. [Implement `explain` command for rule-to-backend mapping](https://github.com/t-uda/gate-keeper/issues/16).
17. [Document gh-aw composition patterns](https://github.com/t-uda/gate-keeper/issues/17).
18. [Add end-to-end examples and smoke tests](https://github.com/t-uda/gate-keeper/issues/18).
19. [Add CI and development workflow hardening](https://github.com/t-uda/gate-keeper/issues/19).
20. [Cut MVP readiness checklist](https://github.com/t-uda/gate-keeper/issues/20).
