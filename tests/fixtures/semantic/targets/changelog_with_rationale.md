# Changelog

## [Unreleased]

### Changed

- `classify_rule` now uses longest-match dispatch instead of first-match.
  This fixes a silent misrouting bug where review-gate rules were classified
  as `github_pr_open` (#211); the greedy first-match favored shorter,
  earlier-registered patterns over the more specific review patterns.
