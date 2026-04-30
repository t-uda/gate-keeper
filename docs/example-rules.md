# Example Gate-Keeper Rules

A minimal rule document mixing filesystem and GitHub checks.
Used with: `gate-keeper compile docs/example-rules.md --format json`

## Filesystem Checks

- `README.md` must exist in the repository root.
- `CHANGELOG.md` must be present before release.
- Source files must not contain the string `DO NOT MERGE`.
- Source files must follow the `src/**/*.py` path glob pattern.

## GitHub PR Checks

- The PR must not be in draft state before merging.
- CI checks must pass for the PR to be merged.
- All review threads must be resolved before merge.
- Non-author approval is required from at least one reviewer.
- No blocking labels such as `do-not-merge` or `needs-decision` should be present.
