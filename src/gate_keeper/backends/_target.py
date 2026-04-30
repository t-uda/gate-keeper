"""GitHub PR target resolver for gate-keeper.

Provides:
- PrTarget         — frozen dataclass carrying owner, repo, number, and URL.
- parse_target()   — pure-string parser; no I/O.
- resolve_target() — live resolver that shells out through run_gh() to confirm
                     that the PR exists and returns a gh-canonical PrTarget.

Consumers (#10-#13) should call resolve_target() at the start of each
per-rule check and short-circuit with the returned Diagnostic on failure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from gate_keeper.backends._gh import (
    classify_gh_failure,
    failure_diag,
    gh_json_diag,
    gh_missing_field_diag,
    parse_json,
    run_gh,
)
from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, Status

# ---------------------------------------------------------------------------
# Compiled regexes for the two supported target formats.
#
# 1. HTTPS PR URL — optional http (upgraded to https canonical), optional
#    www., optional trailing slash, optional query/fragment.
#    Choice: http:// is accepted here and silently upgraded to https in the
#    canonical URL.  This is intentional — GH redirects http→https anyway and
#    refusing it would surprise callers with copy-pasted URLs.
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com"
    r"/([^/\s]+)"      # owner
    r"/([^/\s]+)"      # repo
    r"/pull/(\d+)"     # PR number
    r"/?(?:[?#].*)?"   # optional trailing slash, query, fragment
    r"$",
    re.IGNORECASE,
)

# 2. OWNER/REPO#NUMBER shorthand
_SHORTHAND_RE = re.compile(
    r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)$"
)


# ---------------------------------------------------------------------------
# PrTarget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrTarget:
    """Resolved, validated GitHub pull request coordinates."""

    owner: str
    repo: str
    number: int
    url: str


# ---------------------------------------------------------------------------
# parse_target — pure string parsing, no I/O
# ---------------------------------------------------------------------------


def parse_target(target: str) -> tuple[PrTarget | None, str | None]:
    """Parse *target* into a PrTarget without performing any I/O.

    Returns ``(PrTarget, None)`` on success, ``(None, reason)`` on failure.

    Accepted formats
    ----------------
    - HTTPS PR URL: ``https://github.com/OWNER/REPO/pull/NUMBER``
      (also tolerates ``http://``, ``www.``, trailing slash, query string,
      and fragment; the canonical URL stored on PrTarget is the https form
      with no trailing slash, query, or fragment).
    - Shorthand: ``OWNER/REPO#NUMBER``

    Validation
    ----------
    - Owner and repo must be non-empty after stripping whitespace.
    - PR number must be a positive integer (> 0).
    - URL must point at ``/pull/``, not ``/issues/`` or any other path.
    - Host must be github.com (not gitlab.com, etc.).
    """
    stripped = target.strip()
    if not stripped:
        return None, f"target {target!r} is not a recognized PR URL or OWNER/REPO#N shorthand"

    # --- Try URL form first ---
    m = _URL_RE.match(stripped)
    if m:
        owner, repo, num_str = m.group(1), m.group(2), m.group(3)

        # Reject whitespace-only captures (the regex already ensures non-empty
        # via [^/\s]+, but be explicit for clarity).
        if not owner.strip() or not repo.strip():
            return None, f"target {target!r} has an empty owner or repo segment"

        number = int(num_str)
        if number <= 0:
            return None, f"target {target!r} has PR number {number}; must be a positive integer"

        # Build canonical https URL (strip query, fragment, trailing slash).
        canonical_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        return PrTarget(owner=owner, repo=repo, number=number, url=canonical_url), None

    # --- Try shorthand form ---
    m = _SHORTHAND_RE.match(stripped)
    if m:
        owner, repo, num_str = m.group(1), m.group(2), m.group(3)

        # Owner/repo already validated non-empty by the character class [A-Za-z0-9._-]+.
        number = int(num_str)
        if number <= 0:
            return None, f"target {target!r} has PR number {number}; must be a positive integer"

        canonical_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        return PrTarget(owner=owner, repo=repo, number=number, url=canonical_url), None

    # --- No match ---
    return None, f"target {target!r} is not a recognized PR URL or OWNER/REPO#N shorthand"


# ---------------------------------------------------------------------------
# _not_found_stderr — detect PR/repo-not-found from gh stderr
# ---------------------------------------------------------------------------

_NOT_FOUND_PATTERNS = (
    "could not resolve to a repository",
    "could not resolve to a node",
    "no pull requests found",
    "not found",
)


def _looks_like_not_found(stderr: str) -> bool:
    lower = stderr.lower()
    return any(pat in lower for pat in _NOT_FOUND_PATTERNS)


# ---------------------------------------------------------------------------
# resolve_target — live resolver
# ---------------------------------------------------------------------------


def resolve_target(rule: Rule, target: str) -> tuple[PrTarget | None, Diagnostic | None]:
    """Resolve *target* to a confirmed PrTarget by calling ``gh pr view``.

    Step 1 — parse_target (pure string; no I/O).
    Step 2 — call ``gh pr view <NUMBER> -R OWNER/REPO --json number,url`` to
              confirm the PR exists and obtain the gh-canonical URL.

    Returns ``(PrTarget, None)`` on success, ``(None, Diagnostic)`` on any
    failure.  The caller must propagate the Diagnostic to the check result.
    """
    # Step 1: parse
    t, reason = parse_target(target)
    if t is None:
        diag = Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.GITHUB,
            status=Status.UNAVAILABLE,
            severity=rule.severity,
            message=f"Cannot resolve GitHub target: {reason}",
            evidence=[
                Evidence(
                    kind="target_parse_error",
                    data={"target": target, "reason": reason},
                )
            ],
        )
        return None, diag

    # Step 2: verify with gh
    argv = ["pr", "view", str(t.number), "-R", f"{t.owner}/{t.repo}", "--json", "number,url"]
    result = run_gh(argv)

    if result.binary_missing:
        return None, failure_diag(rule, "pr-view", result)

    if not result.ok:
        category = classify_gh_failure(result)
        if category == "auth":
            return None, failure_diag(rule, "pr-view", result)

        if _looks_like_not_found(result.stderr):
            stderr_excerpt = result.stderr[:300]
            if len(result.stderr) > 300:
                stderr_excerpt += "…"
            diag = Diagnostic(
                rule_id=rule.id,
                source=rule.source,
                backend=Backend.GITHUB,
                status=Status.UNAVAILABLE,
                severity=rule.severity,
                message=(
                    f"GitHub PR {t.owner}/{t.repo}#{t.number} could not be found "
                    f"or repository does not exist."
                ),
                evidence=[
                    Evidence(
                        kind="gh_pr_not_found",
                        data={
                            "op": "pr-view",
                            "owner": t.owner,
                            "repo": t.repo,
                            "number": t.number,
                            "stderr_excerpt": stderr_excerpt,
                        },
                    )
                ],
            )
            return None, diag

        return None, failure_diag(rule, "pr-view", result)

    # Parse JSON response
    data, err = parse_json(result.stdout)
    if err is not None:
        return None, gh_json_diag(rule, "pr-view", err)

    # Validate required fields. We check both presence AND that the value is of
    # the expected type so a malformed response (e.g. ``number: null``) is
    # surfaced as a Diagnostic rather than raising during int coercion.
    if "url" not in data:
        return None, gh_missing_field_diag(rule, "pr-view", "url")
    if "number" not in data:
        return None, gh_missing_field_diag(rule, "pr-view", "number")
    if not isinstance(data["url"], str) or not data["url"]:
        return None, gh_missing_field_diag(rule, "pr-view", "url")
    if not isinstance(data["number"], int) or isinstance(data["number"], bool):
        return None, gh_missing_field_diag(rule, "pr-view", "number")

    # Build refreshed PrTarget using gh-canonical values
    refreshed = PrTarget(
        owner=t.owner,
        repo=t.repo,
        number=data["number"],
        url=data["url"],
    )
    return refreshed, None


__all__ = [
    "PrTarget",
    "parse_target",
    "resolve_target",
]
