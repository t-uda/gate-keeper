#!/usr/bin/env python3
"""Mode-A validator for declarative artifact-dependency gates (umbrella #159).

Reads a JSON ``{rule, target}`` payload on stdin per the ``command`` external
adapter contract (docs/backend-external.md § "command"). For each manifest
edge whose ``to`` node path matches the validated target, checks whether the
``from`` node was changed without the target also being changed and without a
matching ack file at ``.gate-keeper/acks/<edge-id>.yml``.

Always exits 0; pass/fail is carried in the emitted ``Diagnostic.status``.

Schema and contract: docs/design/dependency-gates.md.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dependency_gates._changed_files import (  # noqa: E402
        ChangedFilesError,
        compute_changed_files,
        resolve_base_ref,
    )
    from dependency_gates._stamp import (  # noqa: E402
        StampError,
        read_target_stamp,
    )
    from dependency_gates.manifest import (  # noqa: E402
        Edge,
        Manifest,
        ManifestError,
        Node,
        load_manifest,
    )
else:
    from ._changed_files import (
        ChangedFilesError,
        compute_changed_files,
        resolve_base_ref,
    )
    from ._stamp import (
        StampError,
        read_target_stamp,
    )
    from .manifest import (
        Edge,
        Manifest,
        ManifestError,
        Node,
        load_manifest,
    )

DEFAULT_MANIFEST_PATH = ".gate-keeper/dependency-manifest.yml"
DEFAULT_ACKS_DIR = ".gate-keeper/acks"


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str
    evidence_kind: str
    evidence_data: dict[str, Any]
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
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return Outcome(
            status="fail",
            message=f"manifest invalid: {exc}",
            evidence_kind="manifest_invalid",
            evidence_data={"manifest_path": str(manifest_path), "error": str(exc)},
            remediation=(f"Fix the manifest at {manifest_path} to match docs/design/dependency-gates.md §3."),
        )

    target_rel = _to_repo_relative(target, repo_root)
    edges = _edges_for_target(manifest, target_rel)
    if not edges:
        return Outcome(
            status="pass",
            message=(f"target {target_rel!r} is not the 'to' side of any manifest edge"),
            evidence_kind="edge_not_applicable",
            evidence_data={"target": target_rel},
        )

    # Mode A's manifest_target_missing check covers every node globally;
    # Mode B handles missing source/target per-edge (with `unavailable`
    # evidence) so the global check applies only when at least one applicable
    # edge needs Mode A semantics.
    has_mode_a_edge = any(edge.mode == "affected_set" for edge in edges)
    if has_mode_a_edge:
        missing = [node.path for node in manifest.nodes if not (repo_root / node.path).exists()]
        if missing:
            return Outcome(
                status="fail",
                message=f"manifest references missing path(s): {missing}",
                evidence_kind="manifest_target_missing",
                evidence_data={"missing_paths": missing},
                remediation=("Update the manifest entries or restore the referenced files."),
            )

    # Mode A needs the changed-file set; Mode B is a pure target-side check.
    # Defer the diff invocation until at least one applicable edge needs it,
    # so a manifest containing only stamped edges does not fail when git is
    # unavailable.
    changed: frozenset[str] = frozenset()
    if has_mode_a_edge:
        base_ref = resolve_base_ref()
        try:
            changed = compute_changed_files(repo_root, base_ref)
        except ChangedFilesError as exc:
            return Outcome(
                status="unavailable",
                message=f"cannot compute changed-file set: {exc}",
                evidence_kind="changed_file_source_unresolved",
                evidence_data={"base_ref": base_ref, "error": str(exc)},
                remediation=(
                    "Set GATE_KEEPER_BASE_REF to a resolvable git ref, or run inside a git working tree."
                ),
            )

    return _evaluate_edges(
        manifest=manifest,
        edges=edges,
        changed=changed,
        repo_root=repo_root,
    )


def _evaluate_edges(
    *,
    manifest: Manifest,
    edges: list[Edge],
    changed: frozenset[str],
    repo_root: Path,
) -> Outcome:
    """Evaluate every applicable edge and aggregate.

    Returns the first FAIL or UNAVAILABLE outcome encountered (so that any
    one bad edge is surfaced even when its sibling edges pass). When all
    edges pass, returns the outcome from the strongest signal in the order
    co-changed > acked > unaffected.
    """
    pass_outcomes: list[Outcome] = []
    for edge in edges:
        outcome = _evaluate_edge(
            edge=edge,
            manifest=manifest,
            changed=changed,
            repo_root=repo_root,
        )
        if outcome.status != "pass":
            return outcome
        pass_outcomes.append(outcome)

    # All edges passed; pick the most informative evidence kind.
    priority = {
        "dependent_artifact_co_changed": 3,
        "dependent_artifact_acked": 2,
        "dependent_artifact_unaffected": 1,
    }
    pass_outcomes.sort(key=lambda o: priority.get(o.evidence_kind, 0), reverse=True)
    return pass_outcomes[0]


def _evaluate_edge(
    *,
    edge: Edge,
    manifest: Manifest,
    changed: frozenset[str],
    repo_root: Path,
) -> Outcome:
    source_node = manifest.node(edge.from_id)
    target_node = manifest.node(edge.to_id)
    source_path = source_node.path
    target_path = target_node.path
    edge_id = _edge_id(edge, source_node, target_node)

    if edge.mode == "stamped":
        return _evaluate_stamped_edge(
            edge=edge,
            source_node=source_node,
            target_node=target_node,
            edge_id=edge_id,
            repo_root=repo_root,
        )

    if edge.mode != "affected_set":
        # Defensive: the manifest loader already restricts ``mode`` to the
        # documented enum, so unknown modes can only arrive via direct
        # construction (e.g. tests). Surface them rather than silently
        # mis-evaluating.
        return Outcome(
            status="unavailable",
            message=(f"edge {edge_id}: mode {edge.mode!r} is not a recognised dependency-gate mode"),
            evidence_kind="stamped_mode_not_implemented",
            evidence_data={
                "edge_id": edge_id,
                "mode": edge.mode,
            },
            remediation=(
                "Use 'affected_set' (Mode A) or 'stamped' (Mode B); see docs/design/dependency-gates.md §2.4."
            ),
        )

    if source_path not in changed:
        return Outcome(
            status="pass",
            message=(f"edge {edge_id}: source {source_path} unchanged; target {target_path} is up to date"),
            evidence_kind="dependent_artifact_unaffected",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
            },
        )

    if target_path in changed:
        return Outcome(
            status="pass",
            message=f"edge {edge_id}: source and target co-changed",
            evidence_kind="dependent_artifact_co_changed",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
            },
        )

    ack_outcome = _load_ack(repo_root, edge_id)
    source_sha = _file_sha256(repo_root / source_path)
    if isinstance(ack_outcome, Outcome):
        # Malformed ack file — surface as fail rather than silently ignoring.
        return ack_outcome
    ack = ack_outcome
    if ack is not None and _ack_matches(ack, edge_id, source_sha):
        return Outcome(
            status="pass",
            message=(f"edge {edge_id}: source changed; reviewer ack covers current source sha"),
            evidence_kind="dependent_artifact_acked",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
                "source_sha": source_sha,
                "ack_by": ack.get("ack_by"),
                "ack_at": ack.get("ack_at"),
                "reason": ack.get("reason"),
            },
        )

    return Outcome(
        status="fail",
        message=(
            f"edge {edge_id}: {source_path} changed but {target_path} "
            f"was not updated and no matching ack exists"
        ),
        evidence_kind="dependent_artifact_changed_without_target_update",
        evidence_data={
            "edge_id": edge_id,
            "source_path": source_path,
            "target_path": target_path,
            "source_sha": source_sha,
            "ack_path": str(_ack_path_for(edge_id)),
        },
        remediation=(
            f"Update {target_path} to reflect the change in {source_path}, "
            f"or commit a reviewer ack at "
            f".gate-keeper/acks/{edge_id}.yml with source_sha={source_sha}."
        ),
    )


def _evaluate_stamped_edge(
    *,
    edge: Edge,
    source_node: Node,
    target_node: Node,
    edge_id: str,
    repo_root: Path,
) -> Outcome:
    """Mode B: target frontmatter must record ``tracks: <source-id>@sha256:<digest>``.

    The validator compares the stamp digest against the current source file's
    SHA-256 and emits one of:

    - ``dependent_artifact_unaffected``        (pass) — stamp matches source.
    - ``dependent_artifact_stale_by_hash``     (fail) — stamp present but stale.
    - ``target_stamp_missing``                 (fail) — no ``tracks:`` in target frontmatter.
    - ``target_stamp_malformed``               (fail) — frontmatter or stamp unparseable.
    - ``dependent_artifact_source_missing``    (unavailable) — source path absent.
    - ``dependent_artifact_target_missing``    (unavailable) — target path absent.

    Slice 1 (issue #181): whole-file source hashing; one source per target;
    stamp must live in YAML frontmatter (no inline lines).
    """
    source_path = source_node.path
    target_path = target_node.path
    source_abs = repo_root / source_path
    target_abs = repo_root / target_path

    if not source_abs.is_file():
        return Outcome(
            status="unavailable",
            message=f"edge {edge_id}: source file {source_path} not found",
            evidence_kind="dependent_artifact_source_missing",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
            },
            remediation=(
                f"Restore {source_path} or fix the manifest 'from' node to reference an existing file."
            ),
        )
    if not target_abs.is_file():
        return Outcome(
            status="unavailable",
            message=f"edge {edge_id}: target file {target_path} not found",
            evidence_kind="dependent_artifact_target_missing",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
            },
            remediation=(
                f"Restore {target_path} or fix the manifest 'to' node to reference an existing file."
            ),
        )

    source_sha = _file_sha256(source_abs)
    expected_stamp = f"{source_node.id}@sha256:{source_sha}"

    try:
        stamp = read_target_stamp(target_abs)
    except StampError as exc:
        return Outcome(
            status="fail",
            message=f"edge {edge_id}: target stamp malformed: {exc}",
            evidence_kind="target_stamp_malformed",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
                "error": str(exc),
            },
            remediation=(f"Fix the YAML frontmatter of {target_path}; set 'tracks: {expected_stamp}'."),
        )

    if stamp is None:
        return Outcome(
            status="fail",
            message=f"edge {edge_id}: {target_path} has no 'tracks:' stamp in frontmatter",
            evidence_kind="target_stamp_missing",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
                "expected_stamp": expected_stamp,
            },
            remediation=(
                f"Add a YAML frontmatter block to {target_path} containing 'tracks: {expected_stamp}'."
            ),
        )

    if stamp.source_id != source_node.id:
        return Outcome(
            status="fail",
            message=(
                f"edge {edge_id}: target stamp source-id {stamp.source_id!r} "
                f"does not match manifest source {source_node.id!r}"
            ),
            evidence_kind="target_stamp_malformed",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
                "stamp_source_id": stamp.source_id,
                "expected_source_id": source_node.id,
            },
            remediation=(f"Update the 'tracks:' field in {target_path} to '{expected_stamp}'."),
        )

    if stamp.digest == source_sha:
        return Outcome(
            status="pass",
            message=(f"edge {edge_id}: target stamp matches current source sha"),
            evidence_kind="dependent_artifact_unaffected",
            evidence_data={
                "edge_id": edge_id,
                "source_path": source_path,
                "target_path": target_path,
                "source_sha": source_sha,
                "stamp": expected_stamp,
                "mode": "stamped",
            },
        )

    return Outcome(
        status="fail",
        message=(
            f"edge {edge_id}: target stamp tracks sha256:{stamp.digest} but "
            f"source {source_path} is now sha256:{source_sha}"
        ),
        evidence_kind="dependent_artifact_stale_by_hash",
        evidence_data={
            "edge_id": edge_id,
            "source_path": source_path,
            "target_path": target_path,
            "source_sha": source_sha,
            "stamp_digest": stamp.digest,
            "stamp_source_id": stamp.source_id,
            "expected_stamp": expected_stamp,
        },
        remediation=(
            f"Re-review {target_path} against the new source and update "
            f"the 'tracks:' field to '{expected_stamp}'."
        ),
    )


def _ack_matches(ack: dict[str, Any], edge_id: str, source_sha: str) -> bool:
    """Return True only when the ack's edge_id and source_sha both match."""
    return (
        isinstance(ack.get("edge_id"), str)
        and ack["edge_id"] == edge_id
        and isinstance(ack.get("source_sha"), str)
        and ack["source_sha"] == source_sha
    )


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


def _edges_for_target(manifest: Manifest, target_rel: str) -> list[Edge]:
    if not target_rel:
        return []
    out: list[Edge] = []
    for edge in manifest.edges:
        target_node = manifest.nodes_by_id[edge.to_id]
        if target_node.path == target_rel:
            out.append(edge)
    return out


def _edge_id(edge: Edge, source: Node, target: Node) -> str:
    if edge.pair_id:
        return f"{edge.pair_id}:{source.id}->{target.id}"
    return f"{source.id}-{edge.relation}-{target.id}"


def _ack_path_for(edge_id: str) -> Path:
    return Path(DEFAULT_ACKS_DIR) / f"{edge_id}.yml"


def _load_ack(repo_root: Path, edge_id: str) -> dict[str, Any] | None | Outcome:
    """Load and shallow-validate the ack file for *edge_id*.

    Returns:
    - ``None`` when no ack file exists (caller proceeds to FAIL or PASS).
    - ``dict`` when the file parses to a YAML mapping.
    - ``Outcome`` when the file exists but is malformed (parse error or
      non-mapping body); caller surfaces this directly as a FAIL so the
      author isn't left wondering why their ack was silently ignored.
    """
    ack_path = repo_root / _ack_path_for(edge_id)
    if not ack_path.is_file():
        return None
    try:
        data = yaml.safe_load(ack_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return Outcome(
            status="fail",
            message=f"ack file at {ack_path} could not be parsed: {exc}",
            evidence_kind="ack_invalid",
            evidence_data={
                "edge_id": edge_id,
                "ack_path": str(_ack_path_for(edge_id)),
                "error": str(exc),
            },
            remediation=(
                f"Fix or remove {ack_path}; YAML must parse to a mapping "
                "with edge_id, source_sha, ack_by, ack_at, reason."
            ),
        )
    if not isinstance(data, dict):
        return Outcome(
            status="fail",
            message=(f"ack file at {ack_path} must be a YAML mapping, got {type(data).__name__}"),
            evidence_kind="ack_invalid",
            evidence_data={
                "edge_id": edge_id,
                "ack_path": str(_ack_path_for(edge_id)),
                "actual_type": type(data).__name__,
            },
            remediation=(
                f"Fix {ack_path}; YAML must parse to a mapping with "
                "edge_id, source_sha, ack_by, ack_at, reason."
            ),
        )
    return data


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _emit(outcome: Outcome) -> None:
    diagnostic: dict[str, Any] = {
        "status": outcome.status,
        "message": outcome.message,
        "evidence": [{"kind": outcome.evidence_kind, "data": outcome.evidence_data}],
    }
    if outcome.remediation is not None:
        diagnostic["remediation"] = outcome.remediation
    sys.stdout.write(json.dumps(diagnostic))


if __name__ == "__main__":
    raise SystemExit(main())
