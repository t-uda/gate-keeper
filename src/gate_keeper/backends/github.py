"""GitHub backend for gate-keeper.

Implements deterministic PR state, draft, label, and tasklist checks
using the ``gh`` CLI (issue #10).  Issues #11-#13 (status checks, threads,
approvals) are still UNAVAILABLE stubs; they land in separate PRs.

All rule kinds here share a single ``gh pr view`` call per ``check()``
invocation; the result is parsed once and dispatched to the appropriate
handler.  Any failure before dispatch (missing gh, auth, JSON, missing
field) propagates as UNAVAILABLE — all paths fail closed.
"""
from __future__ import annotations

from pathlib import Path

from gate_keeper._md import (
    TASK_CHECKED_RE,
    TASK_UNCHECKED_RE,
    strip_fenced_blocks,
)
from gate_keeper.backends._gh import (  # noqa: F401  (re-export)
    GhResult,
    classify_gh_failure,
    failure_diag,
    gh_auth_diag,
    gh_failed_diag,
    gh_json_diag,
    gh_missing_diag,
    gh_missing_field_diag,
    gh_pagination_diag,
    parse_json,
    run_gh,
)
from gate_keeper.backends._target import (  # noqa: F401  (re-export)
    PrTarget,
    resolve_target,
)
from gate_keeper.models import Backend, Diagnostic, Evidence, Rule, RuleKind, Status

name = "github"

# ---------------------------------------------------------------------------
# Default blocking labels for github_labels_absent
# ---------------------------------------------------------------------------

_DEFAULT_BLOCKING_LABELS: list[str] = ["blocked", "do-not-merge", "needs-decision"]

# ---------------------------------------------------------------------------
# Shared diagnostic builder
# ---------------------------------------------------------------------------


def _diag(rule: Rule, status: Status, message: str, evidence: list[Evidence]) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.GITHUB,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# _fetch_pr_view — single gh pr view call shared by all four handlers
# ---------------------------------------------------------------------------

_PR_VIEW_FIELDS = "state,isDraft,labels,body"


def _fetch_pr_view(pr: PrTarget, rule: Rule) -> tuple[dict | None, Diagnostic | None]:
    """Call ``gh pr view`` and return (parsed_dict, None) or (None, error_diag).

    One call per check() invocation; the result is shared across all handlers.
    """
    argv = [
        "pr", "view", str(pr.number),
        "-R", f"{pr.owner}/{pr.repo}",
        "--json", _PR_VIEW_FIELDS,
    ]
    result = run_gh(argv)
    if not result.ok:
        return None, failure_diag(rule, "pr-view", result)

    data, err = parse_json(result.stdout)
    if err is not None:
        return None, gh_json_diag(rule, "pr-view", err)

    if not isinstance(data, dict):
        return None, gh_json_diag(rule, "pr-view", "expected a JSON object at top level")

    return data, None


# ---------------------------------------------------------------------------
# Per-kind handlers
# ---------------------------------------------------------------------------

def _pr_coords(pr: PrTarget) -> dict:
    """Return the shared coordinate fields appended to every evidence payload."""
    return {"owner": pr.owner, "repo": pr.repo, "number": pr.number, "url": pr.url}


def _check_pr_open(rule: Rule, pr: PrTarget, data: dict) -> Diagnostic:
    """Pass when ``state == "OPEN"``; fail otherwise.

    Evidence kind: ``pr_state``.
    """
    if "state" not in data:
        return gh_missing_field_diag(rule, "pr-view", "state")

    state = data["state"]
    evidence = [Evidence(kind="pr_state", data={"state": state, **_pr_coords(pr)})]

    if state == "OPEN":
        return _diag(rule, Status.PASS, f"PR {pr.owner}/{pr.repo}#{pr.number} is open.", evidence)
    return _diag(rule, Status.FAIL, f"PR {pr.owner}/{pr.repo}#{pr.number} is not open (state: {state!r}).", evidence)


def _check_not_draft(rule: Rule, pr: PrTarget, data: dict) -> Diagnostic:
    """Pass when ``isDraft`` is ``False``; fail when ``True``.

    Missing or non-bool value fails closed (UNAVAILABLE).
    Evidence kind: ``pr_draft``.
    """
    if "isDraft" not in data:
        return gh_missing_field_diag(rule, "pr-view", "isDraft")

    is_draft = data["isDraft"]
    if not isinstance(is_draft, bool):
        return gh_missing_field_diag(rule, "pr-view", "isDraft")

    evidence = [Evidence(kind="pr_draft", data={"is_draft": is_draft, **_pr_coords(pr)})]

    if not is_draft:
        return _diag(rule, Status.PASS, f"PR {pr.owner}/{pr.repo}#{pr.number} is not a draft.", evidence)
    return _diag(rule, Status.FAIL, f"PR {pr.owner}/{pr.repo}#{pr.number} is a draft.", evidence)


def _check_labels_absent(rule: Rule, pr: PrTarget, data: dict) -> Diagnostic:
    """Pass when none of the configured blocking labels appear on the PR.

    Blocking labels are configured via ``rule.params["labels"]`` (a list of
    strings).  If the key is absent or the list is empty the default blocking
    list is used: ``["blocked", "do-not-merge", "needs-decision"]``.  An
    explicit empty list in params means "no blocking labels configured" → PASS
    for any PR (the caller opted out of blocking label enforcement).

    Wait — re-reading the spec: "empty list = caller explicitly said no
    blocking labels → PASS".  So: missing key → use defaults; empty list →
    no blocking labels → PASS always.

    Evidence kind: ``pr_labels``.
    """
    if "labels" not in data:
        return gh_missing_field_diag(rule, "pr-view", "labels")

    raw_labels = data["labels"]
    if not isinstance(raw_labels, list):
        return gh_missing_field_diag(rule, "pr-view", "labels")

    # Extract label names; each element should be a dict with a "name" key.
    pr_label_names: list[str] = []
    for item in raw_labels:
        if isinstance(item, dict) and "name" in item and isinstance(item["name"], str):
            pr_label_names.append(item["name"])

    # Determine configured blocking labels.
    params_labels = rule.params.get("labels")
    if params_labels is None:
        # Key absent → use defaults.
        blocking: list[str] = list(_DEFAULT_BLOCKING_LABELS)
    else:
        # Key present; use as-is (including empty list which means no blocking).
        blocking = list(params_labels)

    # Case-insensitive intersection.
    blocking_lower = {b.lower() for b in blocking}
    matched = [name for name in pr_label_names if name.lower() in blocking_lower]

    evidence = [
        Evidence(
            kind="pr_labels",
            data={
                "pr_labels": pr_label_names,
                "blocking": blocking,
                "matched": matched,
                **_pr_coords(pr),
            },
        )
    ]

    if not matched:
        return _diag(
            rule,
            Status.PASS,
            f"PR {pr.owner}/{pr.repo}#{pr.number} has no blocking labels.",
            evidence,
        )
    return _diag(
        rule,
        Status.FAIL,
        f"PR {pr.owner}/{pr.repo}#{pr.number} has blocking label(s): {matched!r}.",
        evidence,
    )


def _check_tasks_complete(rule: Rule, pr: PrTarget, data: dict) -> Diagnostic:
    """Pass when the PR body has zero unchecked task boxes.

    A ``null`` body fails closed (UNAVAILABLE — treated as missing field).
    An empty string body has 0 tasks → PASS (vacuous truth).
    Task boxes inside fenced code blocks are ignored.
    Evidence kind: ``pr_tasks``.
    """
    if "body" not in data or data["body"] is None:
        return gh_missing_field_diag(rule, "pr-view", "body")

    body: str = data["body"]
    scannable = strip_fenced_blocks(body)
    checked = len(TASK_CHECKED_RE.findall(scannable))
    unchecked = len(TASK_UNCHECKED_RE.findall(scannable))
    total = checked + unchecked

    evidence = [
        Evidence(
            kind="pr_tasks",
            data={"checked": checked, "unchecked": unchecked, "total": total, **_pr_coords(pr)},
        )
    ]

    if unchecked == 0:
        return _diag(
            rule,
            Status.PASS,
            f"PR {pr.owner}/{pr.repo}#{pr.number} has all {total} task(s) checked.",
            evidence,
        )
    return _diag(
        rule,
        Status.FAIL,
        f"PR {pr.owner}/{pr.repo}#{pr.number} has {unchecked} unchecked task(s) of {total}.",
        evidence,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_HANDLED = {
    RuleKind.GITHUB_PR_OPEN: _check_pr_open,
    RuleKind.GITHUB_NOT_DRAFT: _check_not_draft,
    RuleKind.GITHUB_LABELS_ABSENT: _check_labels_absent,
    RuleKind.GITHUB_TASKS_COMPLETE: _check_tasks_complete,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Resolve the target, fetch PR view data, then dispatch by rule kind.

    Resolution failures (bad target, missing gh, auth error, PR not found)
    surface as UNAVAILABLE immediately.  A single ``gh pr view`` call is made
    for the four implemented kinds; unimplemented kinds (#11-#13) receive an
    UNAVAILABLE stub after resolution.
    """
    target_str = str(target)
    pr, diag = resolve_target(rule, target_str)
    if diag is not None:
        return diag

    handler = _HANDLED.get(rule.kind)
    if handler is None:
        # Rule kind not yet implemented — issues #11-#13.
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.GITHUB,
            status=Status.UNAVAILABLE,
            severity=rule.severity,
            message=(
                f"target resolved to {pr.owner}/{pr.repo}#{pr.number}; "
                f"rule kind {rule.kind.value!r} not yet implemented (#11-#13)"
            ),
            evidence=[
                Evidence(
                    kind="backend_stub",
                    data={
                        "backend": "github",
                        "rule_kind": rule.kind.value,
                        "owner": pr.owner,
                        "repo": pr.repo,
                        "number": pr.number,
                        "url": pr.url,
                    },
                )
            ],
        )

    # Fetch the PR view data (one call for all four handlers).
    data, fetch_diag = _fetch_pr_view(pr, rule)
    if fetch_diag is not None:
        return fetch_diag

    return handler(rule, pr, data)
