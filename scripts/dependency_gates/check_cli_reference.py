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

    missing = [node.path for node in manifest.nodes if not (repo_root / node.path).exists()]
    if missing:
        return Outcome(
            status="fail",
            message=f"manifest references missing path(s): {missing}",
            evidence_kind="manifest_target_missing",
            evidence_data={"missing_paths": missing},
            remediation=("Update the manifest entries or restore the referenced files."),
        )

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
    for edge in edges:
        source_node = manifest.node(edge.from_id)
        target_node = manifest.node(edge.to_id)
        source_path = source_node.path
        target_path = target_node.path
        edge_id = _edge_id(edge, source_node, target_node)

        if source_path not in changed:
            return Outcome(
                status="pass",
                message=(
                    f"edge {edge_id}: source {source_path} unchanged; target {target_path} is up to date"
                ),
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
                message=(f"edge {edge_id}: source and target co-changed"),
                evidence_kind="dependent_artifact_co_changed",
                evidence_data={
                    "edge_id": edge_id,
                    "source_path": source_path,
                    "target_path": target_path,
                },
            )

        ack = _load_ack(repo_root, edge_id)
        source_sha = _file_sha256(repo_root / source_path)
        if ack is not None and ack.get("source_sha") == source_sha:
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

    return Outcome(  # pragma: no cover -- _edges_for_target guarantees non-empty
        status="pass",
        message="no applicable edges",
        evidence_kind="edge_not_applicable",
        evidence_data={},
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


def _load_ack(repo_root: Path, edge_id: str) -> dict[str, Any] | None:
    ack_path = repo_root / _ack_path_for(edge_id)
    if not ack_path.is_file():
        return None
    try:
        data = yaml.safe_load(ack_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
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
