# Local Validation Rules

Filesystem-only rule document for end-to-end tests.
No GitHub rules — every rule here routes to the filesystem backend.

## File Presence

- `README.md` must exist.
- `BANNED.txt` must not exist.

## Content Checks

- `manifest.txt` must contain the version string.
- Source files must not contain banned strings.
- Source files must follow the `src/**/*.py` path glob pattern.

## Task Completion

- [x] All checklist items complete
