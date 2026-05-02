# Switch rule classifier to longest-match dispatch

## Why

The greedy first-match in `classifier.py` was misrouting `"non-author approval"`
rules to `github_pr_open` because the substring matched earlier. Users hit this
in #211 when a review-gate rule silently passed against draft PRs.

## What

- Replace the first-match loop in `classify_rule` with a longest-match scan.
- Add regression test covering the #211 phrasing.
- No public IR or CLI surface change.

## Verification

- `uv run pytest tests/test_classifier.py` passes (40 existing + 1 new).
- Manual run of `gate-keeper validate docs/dogfood-rules.md --target https://github.com/t-uda/gate-keeper/pull/211` now reports `fail` for the review gate, matching expectations.
