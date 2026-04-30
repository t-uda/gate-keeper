"""GitHub backend for gate-keeper.

Implements deterministic PR state, draft, label, tasklist, and status-check
rollup validation using the ``gh`` CLI (issues #10–#11).  Issues #12–#13
(threads, approvals) are still UNAVAILABLE stubs; they land in separate PRs.

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

_PR_VIEW_FIELDS = "state,isDraft,labels,body,statusCheckRollup"


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

    # Determine configured blocking labels. ``params.labels`` MUST be either
    # absent (use defaults) or a list of strings. Anything else (a bare string,
    # a list with non-string items, etc.) fails closed as UNAVAILABLE — the IR
    # is the contract; tolerating drift here would silently change rule
    # semantics.
    params_labels = rule.params.get("labels")
    if params_labels is None:
        blocking: list[str] = list(_DEFAULT_BLOCKING_LABELS)
    elif (
        isinstance(params_labels, list)
        and all(isinstance(item, str) for item in params_labels)
    ):
        blocking = list(params_labels)
    else:
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.GITHUB,
            status=Status.UNAVAILABLE,
            severity=rule.severity,
            message=(
                f"rule.params.labels for {rule.id!r} must be a list of strings; "
                f"got {type(params_labels).__name__}"
            ),
            evidence=[
                Evidence(
                    kind="rule_params_invalid",
                    data={
                        "param": "labels",
                        "expected": "list[str]",
                        "actual_type": type(params_labels).__name__,
                    },
                )
            ],
        )

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

    body = data["body"]
    if not isinstance(body, str):
        # Type mismatch; fail closed instead of letting strip_fenced_blocks raise.
        return gh_missing_field_diag(rule, "pr-view", "body")

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
# Status check rollup handler (issue #11)
# ---------------------------------------------------------------------------


def _classify_check_entry(entry: dict) -> tuple[str, str, bool]:
    """Classify a single statusCheckRollup entry into (name, label, ok).

    Two shapes are supported:

    - ``StatusContext`` (legacy commit statuses): passes only when
      ``state == "SUCCESS"``.
    - ``CheckRun`` (GitHub Actions / Checks API): passes only when
      ``status == "COMPLETED"`` **and** ``conclusion == "SUCCESS"``.

    Entries with an unrecognised ``__typename``, or with missing fields that
    prevent classification, yield ``label = "UNKNOWN"`` and ``ok = False``.
    The name falls back to ``"<unnamed>"`` when neither ``name`` nor
    ``context`` is present.
    """
    if not isinstance(entry, dict):
        return "<unnamed>", "UNKNOWN", False

    name = entry.get("name") or entry.get("context") or "<unnamed>"

    typename = entry.get("__typename", "")

    if typename == "StatusContext":
        state = entry.get("state")
        if not isinstance(state, str):
            return name, "UNKNOWN", False
        ok = state == "SUCCESS"
        return name, state, ok

    if typename == "CheckRun":
        status = entry.get("status")
        conclusion = entry.get("conclusion")
        if not isinstance(status, str):
            return name, "UNKNOWN", False
        if status != "COMPLETED":
            # Still in progress, queued, etc.
            label = status if status else "UNKNOWN"
            return name, label, False
        # status == COMPLETED — conclusion is authoritative.
        if not isinstance(conclusion, str):
            return name, "MISSING_CONCLUSION", False
        ok = conclusion == "SUCCESS"
        return name, conclusion, ok

    # Unknown __typename or missing it entirely.
    return name, "UNKNOWN", False


def _check_checks_success(rule: Rule, pr: PrTarget, data: dict) -> Diagnostic:
    """Pass when every entry in ``statusCheckRollup`` resolves to success.

    Evidence kind: ``checks_rollup``.

    - ``statusCheckRollup`` absent → UNAVAILABLE (gh_missing_field).
    - Empty rollup list → PASS (vacuous — zero checks defined; noted in message).
    - Any non-successful entry → FAIL; non-successful names listed in evidence.
    - Branch protection remains the authoritative control plane; this check
      is advisory evidence only.
    """
    if "statusCheckRollup" not in data:
        return gh_missing_field_diag(rule, "pr-view", "statusCheckRollup")

    rollup = data["statusCheckRollup"]
    if not isinstance(rollup, list):
        return gh_missing_field_diag(rule, "pr-view", "statusCheckRollup")

    successful: list[str] = []
    non_successful: list[dict] = []

    for entry in rollup:
        entry_name, label, ok = _classify_check_entry(entry)
        if ok:
            successful.append(entry_name)
        else:
            non_successful.append({"name": entry_name, "state": label})

    total = len(rollup)
    n_success = len(successful)
    summary = f"{n_success}/{total} checks passed"

    evidence = [
        Evidence(
            kind="checks_rollup",
            data={
                "total": total,
                "successful": successful,
                "non_successful": non_successful,
                "summary": summary,
                **_pr_coords(pr),
            },
        )
    ]

    if not non_successful:
        if total == 0:
            msg = f"PR {pr.owner}/{pr.repo}#{pr.number}: 0 checks defined; vacuous pass."
        else:
            msg = f"PR {pr.owner}/{pr.repo}#{pr.number}: {summary}."
        return _diag(rule, Status.PASS, msg, evidence)

    non_names = [e["name"] for e in non_successful]
    return _diag(
        rule,
        Status.FAIL,
        (
            f"PR {pr.owner}/{pr.repo}#{pr.number}: {summary}; "
            f"non-successful checks: {non_names!r}."
        ),
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
    RuleKind.GITHUB_CHECKS_SUCCESS: _check_checks_success,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Resolve the target, fetch PR view data, then dispatch by rule kind.

    Resolution failures (bad target, missing gh, auth error, PR not found)
    surface as UNAVAILABLE immediately.  A single ``gh pr view`` call is made
    for the five implemented kinds; unimplemented kinds (#12-#13) receive an
    UNAVAILABLE stub after resolution.
    """
    target_str = str(target)
    pr, diag = resolve_target(rule, target_str)
    if diag is not None:
        return diag

    handler = _HANDLED.get(rule.kind)
    if handler is None:
        # Rule kind not yet implemented — issues #12-#13.
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.GITHUB,
            status=Status.UNAVAILABLE,
            severity=rule.severity,
            message=(
                f"target resolved to {pr.owner}/{pr.repo}#{pr.number}; "
                f"rule kind {rule.kind.value!r} not yet implemented (#12-#13)"
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
