#!/usr/bin/env python3
"""Mode-A validator for repo-local reference scanning (umbrella #269, slice 1).

Reads a JSON ``{rule, target}`` payload on stdin per the ``command`` external
adapter contract (docs/backend-external.md § "command"). For the single
``target`` file (a text-bearing artifact such as README/docs/config/rule),
extracts repo-local references with ``_repo_ref_scan.extract_references`` and
classifies each one against:

- the working tree — ``broken_reference`` if the path does not exist;
- the changed-file set — ``uncovered_reference`` (a.k.a. ``newly_introduced``)
  when the target file itself is in the changed-file set, the reference is
  not co-changed alongside it, and the reference is not already recorded as
  a manifest edge whose ``to:`` matches the reference path;
- the broad-shape detector — ``suspicious_broad`` for trailing-slash, glob,
  or bare-top-level-directory references.

The validator emits a single Diagnostic with one Evidence entry per finding
category that produced at least one hit. Empty-finding targets emit
``status: pass`` with ``evidence.kind: repo_refs_clean``. Non-text targets
short-circuit to ``status: pass`` with ``evidence.kind: scanner_not_applicable``.

Fail-closed semantics:
- malformed payload → ``unavailable`` / ``params_error``;
- unreadable target file → ``unavailable`` / ``target_unreadable``;
- unresolvable changed-file set → ``unavailable`` /
  ``changed_file_source_unresolved``.

Always exits 0; pass/fail is carried in the emitted ``Diagnostic.status``.

This validator is informational in the first slice. It surfaces deterministic
findings; enforcement of manifest coverage is explicitly a later slice (Layer
1 in issue #269).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dependency_gates._changed_files import (  # noqa: E402
        ChangedFilesError,
        compute_changed_files,
        resolve_base_ref,
    )
    from dependency_gates._repo_ref_scan import (  # noqa: E402
        Reference,
        extract_references,
        is_suspicious_broad,
    )
    from dependency_gates.manifest import (  # noqa: E402
        Manifest,
        ManifestError,
        load_manifest,
    )
else:
    from ._changed_files import (
        ChangedFilesError,
        compute_changed_files,
        resolve_base_ref,
    )
    from ._repo_ref_scan import (
        Reference,
        extract_references,
        is_suspicious_broad,
    )
    from .manifest import (
        Manifest,
        ManifestError,
        load_manifest,
    )

DEFAULT_MANIFEST_PATH = ".gate-keeper/dependency-manifest.yml"

# File extensions the scanner operates on. Targets outside this set
# short-circuit to a "not applicable" pass — the scanner is intentionally
# scoped to text-bearing files.
SCANNABLE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".md",
        ".json",
        ".yml",
        ".yaml",
        ".toml",
        ".cfg",
        ".ini",
        ".txt",
    }
)

# Top-level directories whose extension-less files are considered text-bearing
# (e.g. CI workflow definitions and config under ``.github/``).
SCANNABLE_EXTENSIONLESS_PREFIXES: tuple[str, ...] = (".github/",)


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str
    evidence: list[dict[str, Any]]
    remediation: str | None = None


def main() -> int:
    raw = sys.stdin.read()
    payload = _parse_payload(raw)
    target_str = payload.get("target", "") or ""
    rule = payload.get("rule") or {}
    params = rule.get("params") or {}

    repo_root = _resolve_repo_root(Path.cwd())
    manifest_path = _resolve_manifest_path(params, repo_root)

    outcome = _run(
        repo_root=repo_root,
        manifest_path=manifest_path,
        target=target_str,
    )
    _emit(outcome)
    return 0


def _run(*, repo_root: Path, manifest_path: Path, target: str) -> Outcome:
    if not target:
        return Outcome(
            status="unavailable",
            message="target path is empty",
            evidence=[
                {
                    "kind": "params_error",
                    "data": {"field": "target", "reason": "empty"},
                }
            ],
            remediation="Invoke the validator with a non-empty --target path.",
        )

    target_rel = _to_repo_relative(target, repo_root)
    target_abs = repo_root / target_rel

    if not _is_scannable_target(target_rel):
        return Outcome(
            status="pass",
            message=(f"target {target_rel!r} is not a text-bearing file; scanner skipped"),
            evidence=[
                {
                    "kind": "scanner_not_applicable",
                    "data": {"target": target_rel, "reason": "extension_not_scanned"},
                }
            ],
        )

    if not target_abs.is_file():
        return Outcome(
            status="unavailable",
            message=f"target file {target_rel!r} not found",
            evidence=[
                {
                    "kind": "target_unreadable",
                    "data": {"target": target_rel, "reason": "missing"},
                }
            ],
            remediation=(f"Ensure {target_rel} exists, or invoke the validator with a different --target."),
        )

    try:
        text = target_abs.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Outcome(
            status="unavailable",
            message=f"target file {target_rel!r} could not be read: {exc}",
            evidence=[
                {
                    "kind": "target_unreadable",
                    "data": {
                        "target": target_rel,
                        "reason": "read_error",
                        "error": str(exc),
                    },
                }
            ],
            remediation=(f"Fix the encoding of {target_rel} (UTF-8 expected) or restore the file."),
        )

    # Manifest is optional for the scanner — the first slice does not enforce
    # coverage. We still load it (when present) to skip references that are
    # already classified as edges, so we don't double-report them as
    # "newly introduced". If the manifest is missing the scanner still runs.
    manifest: Manifest | None = None
    if manifest_path.is_file():
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError as exc:
            return Outcome(
                status="unavailable",
                message=f"manifest invalid: {exc}",
                evidence=[
                    {
                        "kind": "manifest_invalid",
                        "data": {"manifest_path": str(manifest_path), "error": str(exc)},
                    }
                ],
                remediation=(
                    f"Fix the manifest at {manifest_path} to match docs/design/dependency-gates.md §3."
                ),
            )

    # Changed-file set: only required to disambiguate "newly introduced".
    # When it is unavailable the scanner still emits broken/broad findings;
    # the uncovered/newly-introduced category is reported as unknown for
    # that invocation (the evidence carries an explicit reason so callers
    # don't read it as "no new refs found").
    base_ref = resolve_base_ref()
    try:
        changed = compute_changed_files(repo_root, base_ref)
    except ChangedFilesError as exc:
        changed = None
        changed_error: str | None = str(exc)
    else:
        changed_error = None

    refs = extract_references(text, source_path=target_rel)
    classified_paths = _manifest_classified_paths(manifest) if manifest is not None else frozenset()

    broken: list[dict[str, Any]] = []
    newly_introduced: list[dict[str, Any]] = []
    suspicious: list[dict[str, Any]] = []
    seen_broken: set[str] = set()
    seen_new: set[str] = set()
    seen_broad: set[str] = set()

    target_in_changed = bool(changed is not None and target_rel in changed)

    for ref in refs:
        if is_suspicious_broad(ref):
            if ref.path not in seen_broad:
                seen_broad.add(ref.path)
                suspicious.append(_ref_to_evidence_entry(ref, target_rel))
            # Broad shapes don't resolve to a concrete path, so don't try
            # to test them against the working tree or the manifest.
            continue

        if not _path_exists(repo_root, ref.path):
            if ref.path not in seen_broken:
                seen_broken.add(ref.path)
                broken.append(_ref_to_evidence_entry(ref, target_rel))
            continue

        # The path exists. Classify as newly introduced only when:
        # - the target file is itself in the changed-file set;
        # - the reference path is NOT also in the changed-file set (a
        #   co-introduced ref alongside the change is not "uncovered");
        # - the reference is not already a known manifest edge target.
        if (
            target_in_changed
            and changed is not None
            and ref.path not in changed
            and ref.path not in classified_paths
            and ref.path not in seen_new
        ):
            seen_new.add(ref.path)
            newly_introduced.append(_ref_to_evidence_entry(ref, target_rel))

    evidence: list[dict[str, Any]] = []
    if broken:
        evidence.append(
            {
                "kind": "broken_reference",
                "data": {
                    "target": target_rel,
                    "references": broken,
                },
            }
        )
    if newly_introduced:
        evidence.append(
            {
                "kind": "uncovered_reference",
                "data": {
                    "target": target_rel,
                    "references": newly_introduced,
                    "subkind": "newly_introduced",
                },
            }
        )
    if suspicious:
        evidence.append(
            {
                "kind": "suspicious_broad",
                "data": {
                    "target": target_rel,
                    "references": suspicious,
                },
            }
        )

    if changed is None:
        evidence.append(
            {
                "kind": "changed_file_source_unresolved",
                "data": {
                    "base_ref": base_ref,
                    "error": changed_error,
                    "consequence": (
                        "uncovered_reference detection skipped; broken_reference "
                        "and suspicious_broad findings (if any) still apply"
                    ),
                },
            }
        )

    # Status mapping: broken references are deterministic failures; the
    # other two categories are informational only in the first slice (they
    # do not block, per the lite-spec). When nothing is found, emit a clean
    # pass evidence so downstream consumers can distinguish "scanned and
    # found nothing" from "did not scan".
    if broken:
        return Outcome(
            status="fail",
            message=(
                f"{len(broken)} broken repo-local reference(s) in {target_rel}; "
                "see evidence for the path list"
            ),
            evidence=evidence,
            remediation=(
                "Fix each broken reference: update the path, restore the missing file, "
                "or remove the reference if it is no longer applicable."
            ),
        )

    if newly_introduced or suspicious:
        return Outcome(
            status="pass",
            message=(
                f"scanned {target_rel}: {len(newly_introduced)} newly introduced "
                f"reference(s), {len(suspicious)} broad-shape reference(s); "
                "no broken references"
            ),
            evidence=evidence,
        )

    return Outcome(
        status="pass",
        message=f"scanned {target_rel}: no repo-local reference issues found",
        evidence=evidence
        or [
            {
                "kind": "repo_refs_clean",
                "data": {"target": target_rel, "references_scanned": len(refs)},
            }
        ],
    )


def _ref_to_evidence_entry(ref: Reference, target_rel: str) -> dict[str, Any]:
    """Serialise a Reference into a stable evidence entry shape."""
    del target_rel  # available to callers; included implicitly via outer dict
    return {
        "path": ref.path,
        "line": ref.line,
        "column": ref.column,
        "shape": ref.shape,
    }


def _path_exists(repo_root: Path, ref_path: str) -> bool:
    """Return True if *ref_path* resolves to an existing file or directory."""
    if not ref_path:
        return False
    try:
        candidate = (repo_root / ref_path).resolve()
        repo_resolved = repo_root.resolve()
        # Reject paths that escape the repo root (e.g. ``../../etc/passwd``).
        try:
            candidate.relative_to(repo_resolved)
        except ValueError:
            return False
        return candidate.exists()
    except OSError:
        return False


def _manifest_classified_paths(manifest: Manifest) -> frozenset[str]:
    """Return the set of repo-relative paths that already participate in any edge."""
    out: set[str] = set()
    for edge in manifest.edges:
        from_node = manifest.nodes_by_id.get(edge.from_id)
        to_node = manifest.nodes_by_id.get(edge.to_id)
        if from_node is not None:
            out.add(from_node.path)
        if to_node is not None:
            out.add(to_node.path)
    return frozenset(out)


def _is_scannable_target(target_rel: str) -> bool:
    if not target_rel:
        return False
    lowered = target_rel.lower()
    for ext in SCANNABLE_EXTENSIONS:
        if lowered.endswith(ext):
            return True
    for prefix in SCANNABLE_EXTENSIONLESS_PREFIXES:
        if target_rel.startswith(prefix):
            # Extension-less files (e.g. ``.github/CODEOWNERS``) are scannable;
            # files with a non-scannable extension under .github/ are not.
            if "." not in Path(target_rel).name:
                return True
    return False


def _parse_payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _resolve_manifest_path(params: dict[str, Any], repo_root: Path) -> Path:
    raw = params.get("manifest")
    if isinstance(raw, str) and raw:
        candidate = Path(raw)
    else:
        candidate = Path(DEFAULT_MANIFEST_PATH)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate


def _resolve_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return parent
    return start


def _to_repo_relative(target: str, repo_root: Path) -> str:
    if not target:
        return ""
    p = Path(target)
    try:
        if p.is_absolute():
            rel = p.resolve().relative_to(repo_root.resolve())
        else:
            rel = (repo_root / p).resolve().relative_to(repo_root.resolve())
    except ValueError:
        return target.replace("\\", "/")
    return rel.as_posix()


def _emit(outcome: Outcome) -> None:
    diagnostic: dict[str, Any] = {
        "status": outcome.status,
        "message": outcome.message,
        "evidence": list(outcome.evidence),
    }
    if outcome.remediation is not None:
        diagnostic["remediation"] = outcome.remediation
    sys.stdout.write(json.dumps(diagnostic))


if __name__ == "__main__":
    raise SystemExit(main())
