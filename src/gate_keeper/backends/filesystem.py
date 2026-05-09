"""Filesystem and text backend for gate-keeper.

Evaluates a single compiled Rule against a local target path.

Targets
-------
``check`` accepts either:

- a single path-like target (``str`` or :class:`pathlib.Path`) — preserves the
  pre-#146 behaviour exactly; or
- a :class:`gate_keeper.targets.TargetSpec` carrying one or more resolved
  paths (issue #146).  When the spec contains a single path that came from a
  literal file argument, behaviour is identical to the single-path case.
  Otherwise, the rule is evaluated against each resolved file and the results
  are aggregated into a single ``Diagnostic`` per the contract documented in
  ``docs/design/multi-target.md`` §6:

  * ``PASS`` only if every per-file evaluation passes.
  * ``FAIL`` if any per-file evaluation fails.
  * ``UNAVAILABLE`` if the resolved file set is empty (fail-closed) or if
    any per-file evaluation produces an ``UNAVAILABLE`` /
    ``UNSUPPORTED`` / ``ERROR`` status.
  * Per-file outcomes are recorded in ``Evidence(kind="file_result", ...)``
    items, capped to keep diagnostic JSON bounded; a
    ``multi_target_summary`` evidence record carries the file-count totals.

Never raises — all exceptions are translated into diagnostics.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from gate_keeper._md import (
    TASK_CHECKED_RE as _TASK_CHECKED_RE,
)
from gate_keeper._md import (
    TASK_UNCHECKED_RE as _TASK_UNCHECKED_RE,
)
from gate_keeper._md import (
    find_first_fenced_block_after_heading as _find_first_fenced_block_after_heading,
)
from gate_keeper._md import (
    heading_present as _heading_present,
)
from gate_keeper._md import (
    strip_fenced_blocks as _strip_fenced_blocks_impl,
)
from gate_keeper.models import (
    Backend,
    Diagnostic,
    Evidence,
    Rule,
    RuleKind,
    Status,
)
from gate_keeper.targets import TargetSpec

# Cap the number of per-file evidence records included in an aggregated
# diagnostic.  Beyond this point we summarise the remainder so the JSON output
# stays bounded even when the file-count cap is raised in future work.
_PER_FILE_EVIDENCE_LIMIT = 50

name = "filesystem"

_FILESYSTEM_KINDS = frozenset(
    {
        RuleKind.FILE_EXISTS,
        RuleKind.FILE_ABSENT,
        RuleKind.PATH_MATCHES,
        RuleKind.TEXT_REQUIRED,
        RuleKind.TEXT_FORBIDDEN,
        RuleKind.MARKDOWN_TASKS_COMPLETE,
        RuleKind.MARKDOWN_EVIDENCE_BLOCK,
    }
)

#: Formats supported by ``markdown_evidence_block``. Extending this set is a
#: deliberate IR change — keep it small and reviewed.
_EVIDENCE_BLOCK_FORMATS = frozenset({"yaml"})

#: A "sentinel-shaped" string is a lowercase-ASCII machine token: starts with
#: a-z, contains only ``[a-z0-9_]``, and is at least one character long.
#: Free-form values (with spaces, hyphens, uppercase, slashes, etc.) are not
#: sentinel-shaped and bypass the ``allowed_sentinel_values`` check entirely.
_SENTINEL_SHAPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _diag(rule: Rule, status: Status, message: str, evidence: list[Evidence]) -> Diagnostic:
    return Diagnostic(
        rule_id=rule.id,
        source=rule.source,
        backend=Backend.FILESYSTEM,
        status=status,
        severity=rule.severity,
        message=message,
        evidence=evidence,
    )


def check(rule: Rule, target: str | Path | TargetSpec) -> Diagnostic:
    """Evaluate *rule* against *target*. Returns a Diagnostic; never raises.

    *target* may be a single path-like value (legacy single-target call) or a
    :class:`gate_keeper.targets.TargetSpec` (multi-target call introduced in
    issue #146).  See the module docstring for the multi-target aggregation
    contract.
    """
    try:
        if isinstance(target, TargetSpec):
            if not target.is_multi:
                # Single literal-file spec: preserve the per-file dispatch
                # path exactly so existing diagnostics remain bit-identical.
                if target.paths:
                    return _dispatch(rule, target.paths[0])
                # Empty single-target spec is structurally impossible (the
                # CLI requires at least one --target) but we still fail
                # closed defensively.
                return _multi_unavailable_empty(rule, target)
            return _multi_check(rule, target)
        return _dispatch(rule, Path(target))
    except Exception as exc:  # noqa: BLE001
        return _diag(
            rule,
            Status.ERROR,
            f"internal error: {exc}",
            [Evidence(kind="exception", data={"type": type(exc).__name__, "message": str(exc)})],
        )


# ---------------------------------------------------------------------------
# Multi-target aggregation (issue #146)
# ---------------------------------------------------------------------------


def _multi_unavailable_empty(rule: Rule, spec: TargetSpec) -> Diagnostic:
    """Return an UNAVAILABLE diagnostic for an empty resolved file set."""
    return _diag(
        rule,
        Status.UNAVAILABLE,
        (
            "multi-target evaluation resolved to zero files; "
            "fail-closed (refine the target or check the glob)."
        ),
        [
            Evidence(
                kind="multi_target_summary",
                data={
                    "raw_targets": list(spec.raw_targets),
                    "file_count": 0,
                    "pass_count": 0,
                    "fail_count": 0,
                    "unavailable_count": 0,
                },
            )
        ],
    )


def _multi_check(rule: Rule, spec: TargetSpec) -> Diagnostic:
    """Aggregate per-file evaluations of *rule* across ``spec.paths``.

    Contract is documented in the module docstring; implementation follows
    ``docs/design/multi-target.md`` §6 (filesystem backend).
    """
    # Empty file set after expansion: fail-closed.
    if not spec.paths:
        return _multi_unavailable_empty(rule, spec)

    # Special-case kinds that depend on the rule.kind being supported.  An
    # unsupported kind never reaches per-file dispatch — emit UNSUPPORTED once.
    if rule.kind not in _FILESYSTEM_KINDS:
        return _diag(
            rule,
            Status.UNSUPPORTED,
            f"rule kind {rule.kind.value!r} is not supported by the filesystem backend",
            [
                Evidence(
                    kind="backend_capability",
                    data={"backend": "filesystem", "kind": rule.kind.value},
                )
            ],
        )

    pass_count = 0
    fail_count = 0
    unavailable_count = 0
    other_count = 0
    file_results: list[Evidence] = []
    failing_paths: list[str] = []
    unavailable_paths: list[str] = []

    for path in spec.paths:
        per = _dispatch(rule, path)
        status = per.status
        if status is Status.PASS:
            pass_count += 1
        elif status is Status.FAIL:
            fail_count += 1
            failing_paths.append(str(path))
        elif status is Status.UNAVAILABLE:
            unavailable_count += 1
            unavailable_paths.append(str(path))
        else:
            other_count += 1

        if len(file_results) < _PER_FILE_EVIDENCE_LIMIT:
            file_results.append(
                Evidence(
                    kind="file_result",
                    data={
                        "path": str(path),
                        "status": status.value,
                        "message": per.message,
                    },
                )
            )

    total = len(spec.paths)
    truncated = total - len(file_results)
    summary_data: dict[str, object] = {
        "raw_targets": list(spec.raw_targets),
        "file_count": total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "unavailable_count": unavailable_count,
    }
    if other_count:
        summary_data["other_count"] = other_count
    if truncated > 0:
        summary_data["evidence_truncated"] = truncated

    evidence: list[Evidence] = [
        Evidence(kind="multi_target_summary", data=summary_data),
        *file_results,
    ]

    # Aggregation policy:
    # - any UNAVAILABLE / UNSUPPORTED / ERROR result short-circuits to
    #   UNAVAILABLE (fail-closed for evaluator-state failures).
    # - otherwise: FAIL if any per-file FAIL; PASS only when every file PASSed.
    if unavailable_count > 0 or other_count > 0:
        sample_unavailable = unavailable_paths[:3]
        message_parts = [
            f"multi-target evaluation could not complete for {unavailable_count + other_count}",
            f"of {total} file(s)",
        ]
        if sample_unavailable:
            message_parts.append(f"(first: {', '.join(sample_unavailable)})")
        return _diag(
            rule,
            Status.UNAVAILABLE,
            " ".join(message_parts) + ".",
            evidence,
        )

    if fail_count > 0:
        sample_failing = failing_paths[:3]
        msg = (
            f"{fail_count} of {total} file(s) failed rule"
            + (f" (first: {', '.join(sample_failing)})" if sample_failing else "")
            + "."
        )
        return _diag(rule, Status.FAIL, msg, evidence)

    return _diag(
        rule,
        Status.PASS,
        f"all {total} file(s) passed rule.",
        evidence,
    )


def _dispatch(rule: Rule, target: Path) -> Diagnostic:
    kind = rule.kind

    if kind not in _FILESYSTEM_KINDS:
        return _diag(
            rule,
            Status.UNSUPPORTED,
            f"rule kind {kind.value!r} is not supported by the filesystem backend",
            [Evidence(kind="backend_capability", data={"backend": "filesystem", "kind": kind.value})],
        )

    if kind is RuleKind.FILE_EXISTS:
        return _file_exists(rule, target)
    if kind is RuleKind.FILE_ABSENT:
        return _file_absent(rule, target)
    if kind is RuleKind.PATH_MATCHES:
        return _path_matches(rule, target)
    if kind is RuleKind.TEXT_REQUIRED:
        return _text_required(rule, target)
    if kind is RuleKind.TEXT_FORBIDDEN:
        return _text_forbidden(rule, target)
    if kind is RuleKind.MARKDOWN_TASKS_COMPLETE:
        return _markdown_tasks_complete(rule, target)
    # RuleKind.MARKDOWN_EVIDENCE_BLOCK
    return _markdown_evidence_block(rule, target)


def _file_exists(rule: Rule, target: Path) -> Diagnostic:
    path_str = str(target)
    exists = target.exists()
    evidence = [Evidence(kind="file_stat", data={"path": path_str, "exists": exists})]
    if exists:
        return _diag(rule, Status.PASS, f"{path_str} exists.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} does not exist.", evidence)


def _file_absent(rule: Rule, target: Path) -> Diagnostic:
    path_str = str(target)
    exists = target.exists()
    evidence = [Evidence(kind="file_stat", data={"path": path_str, "exists": exists})]
    if not exists:
        return _diag(rule, Status.PASS, f"{path_str} is absent.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} exists but must be absent.", evidence)


def _path_matches(rule: Rule, target: Path) -> Diagnostic:
    pattern = rule.params.get("pattern")
    if not pattern:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.pattern is required for path_matches but was not provided",
            [Evidence(kind="params_error", data={"missing": "pattern"})],
        )
    path_str = str(target)
    matched = fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(target.name, pattern)
    evidence = [Evidence(kind="path_match", data={"path": path_str, "pattern": pattern, "matched": matched})]
    if matched:
        return _diag(rule, Status.PASS, f"{path_str} matches pattern {pattern!r}.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} does not match pattern {pattern!r}.", evidence)


def _read_file(rule: Rule, target: Path) -> tuple[str | None, Diagnostic | None]:
    """Return (content, None) on success or (None, unavailable_diagnostic) on failure."""
    path_str = str(target)
    if not target.exists():
        return None, _diag(
            rule,
            Status.UNAVAILABLE,
            f"{path_str} does not exist; cannot read for text check.",
            [Evidence(kind="file_stat", data={"path": path_str, "exists": False})],
        )
    if not target.is_file():
        return None, _diag(
            rule,
            Status.UNAVAILABLE,
            f"{path_str} is not a regular file.",
            [Evidence(kind="file_stat", data={"path": path_str, "is_file": False})],
        )
    try:
        return target.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, _diag(
            rule,
            Status.UNAVAILABLE,
            f"cannot read {path_str}: {exc}",
            [Evidence(kind="io_error", data={"path": path_str, "error": str(exc)})],
        )


def _text_required(rule: Rule, target: Path) -> Diagnostic:
    pattern = rule.params.get("pattern")
    if not pattern:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.pattern is required for text_required but was not provided",
            [Evidence(kind="params_error", data={"missing": "pattern"})],
        )
    content, err = _read_file(rule, target)
    if err is not None:
        return err
    assert content is not None
    path_str = str(target)
    use_regex = bool(rule.params.get("regex", False))
    if use_regex:
        match_count = len(re.findall(pattern, content))
    else:
        match_count = content.count(pattern)
    evidence = [
        Evidence(kind="text_match", data={"path": path_str, "pattern": pattern, "match_count": match_count})
    ]
    if match_count > 0:
        return _diag(rule, Status.PASS, f"{path_str} contains {pattern!r}.", evidence)
    return _diag(rule, Status.FAIL, f"{path_str} does not contain {pattern!r}.", evidence)


def _text_forbidden(rule: Rule, target: Path) -> Diagnostic:
    pattern = rule.params.get("pattern")
    if not pattern:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.pattern is required for text_forbidden but was not provided",
            [Evidence(kind="params_error", data={"missing": "pattern"})],
        )
    content, err = _read_file(rule, target)
    if err is not None:
        return err
    assert content is not None
    path_str = str(target)
    use_regex = bool(rule.params.get("regex", False))
    if use_regex:
        match_count = len(re.findall(pattern, content))
    else:
        match_count = content.count(pattern)
    evidence = [
        Evidence(kind="text_match", data={"path": path_str, "pattern": pattern, "match_count": match_count})
    ]
    if match_count == 0:
        return _diag(
            rule, Status.PASS, f"{path_str} does not contain forbidden pattern {pattern!r}.", evidence
        )
    return _diag(rule, Status.FAIL, f"{path_str} contains forbidden pattern {pattern!r}.", evidence)


def _strip_fenced_blocks(text: str) -> str:
    """Return *text* with fenced code block contents removed (fence lines included).

    Delegates to ``gate_keeper._md.strip_fenced_blocks``; kept here for backward
    compatibility with any internal callers in this module.
    """
    return _strip_fenced_blocks_impl(text)


def _lookup_dotted_key(data: object, key: str) -> tuple[bool, object]:
    """Resolve dotted *key* against *data*.

    Returns ``(found, value)``. ``found`` is ``True`` only when every segment
    of *key* resolves to a present mapping key on a ``dict``-shaped node.
    Resolution stops at the first segment that cannot be looked up (missing
    key, non-dict node, or empty key segment) and returns ``(False, None)``.
    """
    node: object = data
    if not key:
        return False, None
    for segment in key.split("."):
        if not segment:
            return False, None
        if not isinstance(node, dict) or segment not in node:
            return False, None
        node = node[segment]
    return True, node


def _markdown_evidence_block(rule: Rule, target: Path) -> Diagnostic:
    heading = rule.params.get("heading")
    if not isinstance(heading, str) or not heading:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.heading is required for markdown_evidence_block but was not provided",
            [Evidence(kind="params_error", data={"missing": "heading"})],
        )
    fmt = rule.params.get("format")
    if not isinstance(fmt, str) or not fmt:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.format is required for markdown_evidence_block but was not provided",
            [Evidence(kind="params_error", data={"missing": "format"})],
        )
    if fmt not in _EVIDENCE_BLOCK_FORMATS:
        return _diag(
            rule,
            Status.UNSUPPORTED,
            f"params.format {fmt!r} is not supported; expected one of {sorted(_EVIDENCE_BLOCK_FORMATS)}",
            [
                Evidence(
                    kind="params_error",
                    data={"field": "format", "value": fmt, "supported": sorted(_EVIDENCE_BLOCK_FORMATS)},
                )
            ],
        )
    required_keys_raw = rule.params.get("required_keys")
    if not isinstance(required_keys_raw, list) or not required_keys_raw:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.required_keys is required for markdown_evidence_block "
            "and must be a non-empty list of dotted-key strings",
            [Evidence(kind="params_error", data={"missing": "required_keys"})],
        )
    required_keys: list[str] = []
    for k in required_keys_raw:
        if not isinstance(k, str) or not k:
            return _diag(
                rule,
                Status.UNAVAILABLE,
                "params.required_keys entries must be non-empty strings",
                [
                    Evidence(
                        kind="params_error",
                        data={"field": "required_keys", "invalid_entry": repr(k)},
                    )
                ],
            )
        required_keys.append(k)
    sentinel_raw = rule.params.get("allowed_sentinel_values", [])
    if not isinstance(sentinel_raw, list):
        return _diag(
            rule,
            Status.UNAVAILABLE,
            "params.allowed_sentinel_values must be a list of strings when provided",
            [
                Evidence(
                    kind="params_error",
                    data={"field": "allowed_sentinel_values", "type": type(sentinel_raw).__name__},
                )
            ],
        )
    allowed_sentinels: list[str] = []
    for s in sentinel_raw:
        if not isinstance(s, str):
            return _diag(
                rule,
                Status.UNAVAILABLE,
                "params.allowed_sentinel_values entries must be strings",
                [
                    Evidence(
                        kind="params_error",
                        data={"field": "allowed_sentinel_values", "invalid_entry": repr(s)},
                    )
                ],
            )
        allowed_sentinels.append(s)

    content, err = _read_file(rule, target)
    if err is not None:
        return err
    assert content is not None
    path_str = str(target)

    found = _find_first_fenced_block_after_heading(content, heading)
    if found is None:
        # Distinguish missing heading from missing block to make diagnostics
        # actionable.
        if not _heading_present(content, heading):
            return _diag(
                rule,
                Status.FAIL,
                f"{path_str}: heading {heading!r} not found.",
                [
                    Evidence(
                        kind="evidence_block",
                        data={
                            "path": path_str,
                            "heading": heading,
                            "failure": "heading_missing",
                        },
                    )
                ],
            )
        return _diag(
            rule,
            Status.FAIL,
            f"{path_str}: no fenced code block follows heading {heading!r}.",
            [
                Evidence(
                    kind="evidence_block",
                    data={
                        "path": path_str,
                        "heading": heading,
                        "failure": "block_missing",
                    },
                )
            ],
        )
    block_body, fence_line, info_string = found

    # Parse the block per the requested format. PyYAML is a runtime dependency
    # but if it is missing/misinstalled we surface a deterministic
    # ``unavailable`` (with ``dependency_missing`` evidence) instead of letting
    # ``ModuleNotFoundError`` bubble up to the generic ``Status.ERROR`` branch.
    try:
        import yaml  # local import keeps PyYAML out of the import path until needed
    except ImportError as exc:
        return _diag(
            rule,
            Status.UNAVAILABLE,
            f"{path_str}: cannot parse evidence block at line {fence_line}: "
            f"PyYAML is required for format='yaml' but is not importable ({exc})",
            [
                Evidence(
                    kind="dependency_missing",
                    data={"package": "pyyaml", "format": fmt, "error": str(exc)},
                )
            ],
        )

    try:
        parsed = yaml.safe_load(block_body) if block_body.strip() else None
    except yaml.YAMLError as exc:
        # yaml.YAMLError exposes problem_mark for many subclasses (parser/scanner errors).
        block_relative_line = None
        problem_mark = getattr(exc, "problem_mark", None)
        if problem_mark is not None:
            block_relative_line = int(problem_mark.line) + 1
        absolute_line = fence_line + block_relative_line if block_relative_line is not None else fence_line
        return _diag(
            rule,
            Status.FAIL,
            f"{path_str}: malformed {fmt} in evidence block at line {fence_line}: {exc}",
            [
                Evidence(
                    kind="evidence_block",
                    data={
                        "path": path_str,
                        "heading": heading,
                        "format": fmt,
                        "fence_line": fence_line,
                        "info_string": info_string,
                        "failure": "malformed",
                        "error": str(exc),
                        "error_line": absolute_line,
                    },
                )
            ],
        )

    if not isinstance(parsed, dict):
        return _diag(
            rule,
            Status.FAIL,
            (
                f"{path_str}: evidence block at line {fence_line} did not parse as a "
                f"{fmt} mapping (got {type(parsed).__name__})."
            ),
            [
                Evidence(
                    kind="evidence_block",
                    data={
                        "path": path_str,
                        "heading": heading,
                        "format": fmt,
                        "fence_line": fence_line,
                        "info_string": info_string,
                        "failure": "not_a_mapping",
                        "parsed_type": type(parsed).__name__,
                    },
                )
            ],
        )

    missing: list[str] = []
    invalid_sentinels: list[dict[str, str]] = []
    for key in required_keys:
        present, value = _lookup_dotted_key(parsed, key)
        if not present:
            missing.append(key)
            continue
        # Sentinel validation only runs when an allowlist is supplied AND the
        # value is a *sentinel-shaped* string leaf (see ``_SENTINEL_SHAPE_RE``).
        # Non-string values (mappings, lists, numbers, bools, null) and free-
        # form strings (e.g. ``"spread-applicant-ai/v3"``) bypass the check.
        if (
            allowed_sentinels
            and isinstance(value, str)
            and _SENTINEL_SHAPE_RE.match(value) is not None
            and value not in allowed_sentinels
        ):
            invalid_sentinels.append({"key": key, "value": value})

    if missing or invalid_sentinels:
        msg_parts: list[str] = []
        if missing:
            msg_parts.append(f"missing required keys: {missing}")
        if invalid_sentinels:
            invalid_summary = [f"{item['key']}={item['value']!r}" for item in invalid_sentinels]
            msg_parts.append(f"invalid sentinel value(s): {invalid_summary}")
        return _diag(
            rule,
            Status.FAIL,
            f"{path_str}: evidence block at line {fence_line}: " + "; ".join(msg_parts),
            [
                Evidence(
                    kind="evidence_block",
                    data={
                        "path": path_str,
                        "heading": heading,
                        "format": fmt,
                        "fence_line": fence_line,
                        "info_string": info_string,
                        "failure": "key_or_sentinel",
                        "missing_keys": missing,
                        "invalid_sentinels": invalid_sentinels,
                    },
                )
            ],
        )
    return _diag(
        rule,
        Status.PASS,
        (
            f"{path_str}: evidence block under {heading!r} at line {fence_line} has "
            f"all required keys ({len(required_keys)})."
        ),
        [
            Evidence(
                kind="evidence_block",
                data={
                    "path": path_str,
                    "heading": heading,
                    "format": fmt,
                    "fence_line": fence_line,
                    "info_string": info_string,
                    "required_keys": required_keys,
                    "allowed_sentinel_values": allowed_sentinels,
                },
            )
        ],
    )


def _markdown_tasks_complete(rule: Rule, target: Path) -> Diagnostic:
    content, err = _read_file(rule, target)
    if err is not None:
        return err
    assert content is not None
    path_str = str(target)
    scannable = _strip_fenced_blocks(content)
    checked = len(_TASK_CHECKED_RE.findall(scannable))
    unchecked = len(_TASK_UNCHECKED_RE.findall(scannable))
    total = checked + unchecked
    evidence = [
        Evidence(
            kind="markdown_tasks",
            data={"path": path_str, "checked": checked, "unchecked": unchecked, "total": total},
        )
    ]
    if unchecked:
        return _diag(rule, Status.FAIL, f"{path_str} has {unchecked} unchecked task(s) of {total}.", evidence)
    return _diag(rule, Status.PASS, f"{path_str} has all {total} task(s) checked.", evidence)
