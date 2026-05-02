"""GitHub backend for gate-keeper.

Implements deterministic PR state, draft, label, tasklist, status-check
rollup, review thread validation, and independent (non-author) approval
checks using the ``gh`` CLI (issues #10–#13).

Six rule kinds share a single ``gh pr view`` call per ``check()``
invocation; the result is parsed once and dispatched to the appropriate
handler.  The seventh kind (``github_threads_resolved``) makes its own
GraphQL call via ``gh api graphql`` and does NOT use ``_fetch_pr_view``.

Any failure before dispatch (missing gh, auth, JSON, missing field)
propagates as UNAVAILABLE — all paths fail closed.
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
    _redact,
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
# _fetch_pr_view — narrow gh pr view call per rule kind
# ---------------------------------------------------------------------------
#
# Each rule only needs the JSON fields its handler consumes. We declare the
# field set per rule kind and ask gh for only those fields. This avoids
# downloading an entire review history for a state/draft/label/task/check rule
# (which can be several KB on heavily-reviewed PRs) and lets the approval rule
# use the precomputed ``latestReviews`` field — one entry per reviewer, latest
# state — eliminating any pagination concern on the reviews connection.

_FIELDS_BY_KIND: dict[RuleKind, str] = {
    RuleKind.GITHUB_PR_OPEN: "state",
    RuleKind.GITHUB_NOT_DRAFT: "isDraft",
    RuleKind.GITHUB_LABELS_ABSENT: "labels",
    RuleKind.GITHUB_TASKS_COMPLETE: "body",
    RuleKind.GITHUB_CHECKS_SUCCESS: "statusCheckRollup",
    RuleKind.GITHUB_NON_AUTHOR_APPROVAL: "latestReviews,author",
}


def _fetch_pr_view(pr: PrTarget, rule: Rule) -> tuple[dict | None, Diagnostic | None]:
    """Call ``gh pr view`` for *only* the fields needed by ``rule.kind``.

    Returns ``(parsed_dict, None)`` or ``(None, error_diag)``. One call per
    ``check()`` invocation. The narrow field selection keeps the payload small
    for the common rules and lets the approval rule receive ``latestReviews``
    (one row per reviewer, gh-precomputed) instead of paginating ``reviews``.
    """
    fields = _FIELDS_BY_KIND.get(rule.kind, "")
    if not fields:
        # Defensive: a kind that reaches this path without a declared field set
        # is a programmer error. Fail closed instead of running a meaningless
        # gh call.
        return None, gh_missing_field_diag(rule, "pr-view", "<unknown rule kind>")

    argv = [
        "pr",
        "view",
        str(pr.number),
        "-R",
        f"{pr.owner}/{pr.repo}",
        "--json",
        fields,
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
    return _diag(
        rule,
        Status.FAIL,
        f"PR {pr.owner}/{pr.repo}#{pr.number} is not open (state: {state!r}).",
        evidence,
    )


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
    elif isinstance(params_labels, list) and all(isinstance(item, str) for item in params_labels):
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


def _normalize_rollup(raw: object) -> list | None:
    """Return a flat list of rollup entries, or ``None`` if shape is unrecognized.

    ``gh pr view --json statusCheckRollup`` flattens the GraphQL
    ``StatusCheckRollup`` object to a list of entries (one per
    ``CheckRun``/``StatusContext``). Future gh versions or alternate access
    paths could conceivably return the nested object shape, e.g.::

        {"contexts": {"nodes": [...]}}

    or simply ``{"nodes": [...]}``. Be tolerant: accept the flat list (current
    behaviour), and fall back to either nested-nodes shape so the rule still
    evaluates instead of failing closed on a pure shape change.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        # Try ``contexts.nodes`` first (the documented GraphQL relay shape),
        # then a top-level ``nodes`` fallback.
        contexts = raw.get("contexts")
        if isinstance(contexts, dict):
            nodes = contexts.get("nodes")
            if isinstance(nodes, list):
                return nodes
        nodes = raw.get("nodes")
        if isinstance(nodes, list):
            return nodes
    return None


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

    rollup_raw = data["statusCheckRollup"]
    rollup = _normalize_rollup(rollup_raw)
    if rollup is None:
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
        (f"PR {pr.owner}/{pr.repo}#{pr.number}: {summary}; non-successful checks: {non_names!r}."),
        evidence,
    )


# ---------------------------------------------------------------------------
# Review threads GraphQL handler (issue #12)
# ---------------------------------------------------------------------------

_THREADS_QUERY = """query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          comments(first: 1) { nodes { author { login } body url } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

_GRAPHQL_ERROR_MSG_LIMIT = 200


def _fetch_review_threads(
    pr: PrTarget, rule: Rule
) -> tuple[list[dict] | None, dict | None, Diagnostic | None]:
    """Run the threads GraphQL query.

    Returns exactly one populated branch:
    - ``(nodes, pageInfo, None)`` on success.
    - ``(None, None, diag)`` on any failure.
    """
    argv = [
        "api",
        "graphql",
        "-f",
        f"query={_THREADS_QUERY}",
        "-f",
        f"owner={pr.owner}",
        "-f",
        f"repo={pr.repo}",
        "-F",
        f"number={pr.number}",
    ]
    result = run_gh(argv)

    if not result.ok:
        return None, None, failure_diag(rule, "graphql", result)

    data, err = parse_json(result.stdout)
    if err is not None:
        return None, None, gh_json_diag(rule, "graphql", err)

    # Top-level GraphQL errors array → UNAVAILABLE
    if isinstance(data, dict) and "errors" in data:
        raw_errors = data["errors"]
        if not isinstance(raw_errors, list):
            raw_errors = [raw_errors]
        summaries = []
        for e in raw_errors:
            if isinstance(e, dict):
                msg = str(e.get("message", repr(e)))
            else:
                msg = str(e)
            msg = _redact(msg)
            if len(msg) > _GRAPHQL_ERROR_MSG_LIMIT:
                msg = msg[:_GRAPHQL_ERROR_MSG_LIMIT] + "…"
            summaries.append(msg)
        diag = _base_gh_diag(
            rule,
            Status.UNAVAILABLE,
            "gh 'graphql' returned GraphQL errors; evaluation is unavailable.",
            [
                Evidence(
                    kind="gh_graphql_error",
                    data={
                        "op": "graphql",
                        "errors": summaries,
                        "owner": pr.owner,
                        "repo": pr.repo,
                        "number": pr.number,
                    },
                )
            ],
        )
        return None, None, diag

    # Navigate to reviewThreads
    if not isinstance(data, dict) or "data" not in data:
        return None, None, gh_missing_field_diag(rule, "graphql", "data")

    repo_data = data["data"]
    if not isinstance(repo_data, dict) or "repository" not in repo_data:
        return None, None, gh_missing_field_diag(rule, "graphql", "data.repository")

    repository = repo_data["repository"]
    if not isinstance(repository, dict) or "pullRequest" not in repository:
        return None, None, gh_missing_field_diag(rule, "graphql", "data.repository.pullRequest")

    pull_request = repository["pullRequest"]
    if not isinstance(pull_request, dict) or "reviewThreads" not in pull_request:
        return (
            None,
            None,
            gh_missing_field_diag(rule, "graphql", "data.repository.pullRequest.reviewThreads"),
        )

    review_threads = pull_request["reviewThreads"]
    if not isinstance(review_threads, dict):
        return (
            None,
            None,
            gh_missing_field_diag(rule, "graphql", "data.repository.pullRequest.reviewThreads"),
        )

    nodes = review_threads.get("nodes")
    page_info = review_threads.get("pageInfo")

    if not isinstance(nodes, list):
        return (
            None,
            None,
            gh_missing_field_diag(rule, "graphql", "data.repository.pullRequest.reviewThreads.nodes"),
        )

    if not isinstance(page_info, dict):
        return (
            None,
            None,
            gh_missing_field_diag(rule, "graphql", "data.repository.pullRequest.reviewThreads.pageInfo"),
        )

    # Pagination guard: hasNextPage must be an explicit ``False`` to treat the
    # first page as complete. ``True`` *and* any missing/malformed value
    # (``None``, a string, absent key) all fail closed as UNAVAILABLE — a
    # truncated first page must never silently PASS.
    has_next = page_info.get("hasNextPage")
    if has_next is not False:
        end_cursor = page_info.get("endCursor")
        if not isinstance(end_cursor, str):
            end_cursor = None
        return None, None, gh_pagination_diag(rule, "graphql", end_cursor=end_cursor)

    return nodes, page_info, None


def _base_gh_diag(rule: Rule, status: Status, message: str, evidence: list[Evidence]) -> Diagnostic:
    """Shared builder for diagnostics that bypass _diag (which sets backend=GITHUB already)."""
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.GITHUB,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


def _check_threads_resolved(rule: Rule, pr: PrTarget) -> Diagnostic:
    """Pass when all review threads are resolved; fail if any are unresolved.

    Evidence kind: ``review_threads``.
    """
    nodes, _page_info, diag = _fetch_review_threads(pr, rule)
    if diag is not None:
        return diag

    unresolved: list[dict] = []
    for node in nodes:  # type: ignore[union-attr]
        if not isinstance(node, dict):
            # Treat non-dict nodes conservatively as unresolved
            unresolved.append(
                {"path": None, "line": None, "first_comment_author": None, "first_comment_url": None}
            )
            continue

        is_resolved = node.get("isResolved")
        # Non-bool → treat as not-resolved (conservative)
        if not isinstance(is_resolved, bool) or not is_resolved:
            path = node.get("path")
            line = node.get("line")
            if not isinstance(path, str):
                path = None
            if not isinstance(line, int) or isinstance(line, bool):
                line = None

            # Extract first comment metadata
            first_comment_author = None
            first_comment_url = None
            comments = node.get("comments")
            if isinstance(comments, dict):
                comment_nodes = comments.get("nodes")
                if isinstance(comment_nodes, list) and comment_nodes:
                    first = comment_nodes[0]
                    if isinstance(first, dict):
                        author = first.get("author")
                        if isinstance(author, dict):
                            login = author.get("login")
                            if isinstance(login, str):
                                first_comment_author = login
                        url = first.get("url")
                        if isinstance(url, str):
                            first_comment_url = url

            unresolved.append(
                {
                    "path": path,
                    "line": line,
                    "first_comment_author": first_comment_author,
                    "first_comment_url": first_comment_url,
                }
            )

    total = len(nodes)  # type: ignore[arg-type]
    unresolved_count = len(unresolved)

    evidence = [
        Evidence(
            kind="review_threads",
            data={
                "total": total,
                "unresolved_count": unresolved_count,
                "unresolved": unresolved,
                **_pr_coords(pr),
            },
        )
    ]

    if unresolved_count == 0:
        return _diag(
            rule,
            Status.PASS,
            f"PR {pr.owner}/{pr.repo}#{pr.number}: all {total} review thread(s) are resolved.",
            evidence,
        )

    return _diag(
        rule,
        Status.FAIL,
        f"PR {pr.owner}/{pr.repo}#{pr.number}: {unresolved_count} unresolved review thread(s) of {total}.",
        evidence,
    )


# ---------------------------------------------------------------------------
# Independent (non-author) approval handler (issue #13)
# ---------------------------------------------------------------------------


def _is_bot_reviewer(login: str, is_bot: object) -> bool:
    """Return True if the reviewer is a bot.

    A reviewer is considered a bot when ``is_bot is True`` OR when their
    login ends with ``[bot]``.  The ``is_bot`` field may be missing on
    older payloads (hence ``object`` type rather than ``bool``).
    """
    if is_bot is True:
        return True
    if isinstance(login, str) and login.endswith("[bot]"):
        return True
    return False


def _check_non_author_approval(rule: Rule, pr: PrTarget, data: dict) -> Diagnostic:
    """Pass when at least one non-author, non-bot reviewer has an APPROVED state.

    Uses ``gh pr view --json latestReviews`` which returns one entry per
    reviewer with that reviewer's *latest* review state — pre-computed by
    GitHub. This sidesteps the pagination concern on the full ``reviews``
    connection: ``latestReviews`` is bounded by reviewer count rather than
    review-event count.

    DISMISSED, COMMENTED, CHANGES_REQUESTED, and PENDING all fail the rule.
    Bot reviews (``is_bot=True`` or login ending in ``[bot]``) and self-reviews
    (reviewer login == PR author login) are excluded entirely.

    Evidence kind: ``pr_independent_review``.
    """
    # --- Validate required fields ---
    if "author" not in data or not isinstance(data["author"], dict):
        return gh_missing_field_diag(rule, "pr-view", "author")

    author_dict = data["author"]
    author_login = author_dict.get("login")
    if not isinstance(author_login, str) or not author_login:
        return gh_missing_field_diag(rule, "pr-view", "author.login")

    if "latestReviews" not in data or not isinstance(data["latestReviews"], list):
        return gh_missing_field_diag(rule, "pr-view", "latestReviews")

    reviews_raw = data["latestReviews"]

    # --- Normalize reviews ---
    # Each entry: {login, is_bot, state}
    # We no longer need submittedAt because latestReviews is already the
    # latest-per-reviewer slice.
    normalized: list[dict] = []
    for entry in reviews_raw:
        if not isinstance(entry, dict):
            continue
        entry_author = entry.get("author")
        if not isinstance(entry_author, dict):
            continue
        login = entry_author.get("login")
        if not isinstance(login, str) or not login:
            continue
        state = entry.get("state")
        if not isinstance(state, str) or not state:
            continue
        is_bot = entry_author.get("is_bot")
        normalized.append({"login": login, "is_bot": is_bot, "state": state})

    # --- Filter bots and self-reviews; classify approved vs not ---
    ignored_bot_logins: list[str] = []
    ignored_self_reviews = 0
    approved_by: list[str] = []
    non_approved_reviewers: list[dict] = []
    seen_qualifying: set[str] = set()

    for rev in normalized:
        login = rev["login"]
        if _is_bot_reviewer(login, rev["is_bot"]):
            if login not in ignored_bot_logins:
                ignored_bot_logins.append(login)
            continue
        if login == author_login:
            ignored_self_reviews += 1
            continue
        # Defensive: if the same login appears more than once (shouldn't on
        # latestReviews but cheap to handle), keep the first occurrence.
        if login in seen_qualifying:
            continue
        seen_qualifying.add(login)
        if rev["state"] == "APPROVED":
            approved_by.append(login)
        else:
            non_approved_reviewers.append({"login": login, "state": rev["state"]})

    # --- Build evidence and diagnostic ---
    evidence_data: dict = {
        "author": author_login,
        "approved_by": approved_by,
        "non_approved_reviewers": non_approved_reviewers,
        "ignored_bot_reviewers": ignored_bot_logins,
        "ignored_self_reviews": ignored_self_reviews,
        **_pr_coords(pr),
    }
    evidence = [Evidence(kind="pr_independent_review", data=evidence_data)]

    if approved_by:
        return _diag(
            rule,
            Status.PASS,
            (f"PR {pr.owner}/{pr.repo}#{pr.number} has independent approval from: {approved_by!r}."),
            evidence,
        )

    return _diag(
        rule,
        Status.FAIL,
        (
            f"PR {pr.owner}/{pr.repo}#{pr.number} has no qualifying independent "
            f"APPROVED review (non-author, non-bot, latest state)."
        ),
        evidence,
    )


# ---------------------------------------------------------------------------
# Dispatch tables
# ---------------------------------------------------------------------------

# Handlers that require a prior _fetch_pr_view call (takes rule, pr, data).
_PR_VIEW_HANDLERS = {
    RuleKind.GITHUB_PR_OPEN: _check_pr_open,
    RuleKind.GITHUB_NOT_DRAFT: _check_not_draft,
    RuleKind.GITHUB_LABELS_ABSENT: _check_labels_absent,
    RuleKind.GITHUB_TASKS_COMPLETE: _check_tasks_complete,
    RuleKind.GITHUB_CHECKS_SUCCESS: _check_checks_success,
    RuleKind.GITHUB_NON_AUTHOR_APPROVAL: _check_non_author_approval,
}

# Handlers that make their own gh call directly (takes rule, pr only).
_DIRECT_HANDLERS = {
    RuleKind.GITHUB_THREADS_RESOLVED: _check_threads_resolved,
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check(rule: Rule, target: str | Path) -> Diagnostic:
    """Resolve the target, then dispatch by rule kind.

    Resolution failures (bad target, missing gh, auth error, PR not found)
    surface as UNAVAILABLE immediately.

    - Six rule kinds share a single ``gh pr view`` call (_PR_VIEW_HANDLERS).
    - ``github_threads_resolved`` makes its own GraphQL call (_DIRECT_HANDLERS).
    - Unrecognised kinds receive a defensive UNAVAILABLE fall-through.
    """
    target_str = str(target)
    pr, diag = resolve_target(rule, target_str)
    if diag is not None:
        return diag
    assert pr is not None

    # Direct handlers: make their own gh call, don't need pr-view.
    direct = _DIRECT_HANDLERS.get(rule.kind)
    if direct is not None:
        return direct(rule, pr)

    handler = _PR_VIEW_HANDLERS.get(rule.kind)
    if handler is None:
        # Defensive fall-through: no known handler for this rule kind.
        # A future malformed or unsupported RuleKind should not crash.
        _supported = ", ".join(sorted(k.value for k in list(_PR_VIEW_HANDLERS) + list(_DIRECT_HANDLERS)))
        return Diagnostic(
            rule_id=rule.id,
            source=rule.source,
            backend=Backend.GITHUB,
            status=Status.UNAVAILABLE,
            severity=rule.severity,
            message=(
                f"GitHub backend does not implement rule kind {rule.kind.value!r}; supported: {_supported}"
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

    # Fetch the PR view data (one call for all five pr-view handlers).
    data, fetch_diag = _fetch_pr_view(pr, rule)
    if fetch_diag is not None:
        return fetch_diag
    assert data is not None

    return handler(rule, pr, data)
