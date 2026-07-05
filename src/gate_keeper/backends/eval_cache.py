"""Content-addressed local evaluation cache for the LLM-rubric backend.

Implements the per-working-directory, opt-in eval cache described in
``docs/design/eval-cache.md`` (issue #69 slice B). Caches the
post-aggregation ``Diagnostic`` for one ``(rule, target, model, prompt)``
tuple; on a hit returns the stored result with zero provider calls and a
``cache_hit`` evidence record appended (§6 of the design doc).

Key decisions (ratified in ``docs/design/eval-cache.md``):
- Cache unit: the single aggregated ``Diagnostic`` produced after strategy
  AND ``--reproducibility`` aggregation (§2.1). Never individual provider
  calls or ``_run_n`` iterations.
- Key: sha256 over a canonical sorted-key JSON preimage (§3).
- Fail-open on miss / corruption / unknown schema version (§2.3).
- Only PASS/FAIL verdicts are written (§2.4).
- Atomic tmp+rename, no locking; racing double-compute tolerated (§5).
- Rule identity (rule_id, source, severity) is rehydrated from the current
  rule on hit, not keyed (§2.5).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from gate_keeper.models import Diagnostic, Evidence, Rule, Status, TargetKind

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: On-disk entry format version.  Increment when the entry schema changes.
#: Unknown versions are treated as a miss and the entry is overwritten (§2.3).
_CACHE_SCHEMA_VERSION = 1

#: Truthy values for dotenv boolean keys (case-insensitive, matches the
#: existing convention for ``GATE_KEEPER_REQUIRE_MODEL`` etc.).
_TRUTHY_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: Default size cap for the eval-cache directory (§7.1).
_DEFAULT_MAX_MB = 500

#: Provider names understood by the llm-rubric backend.
_SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai"})

#: Strategy-shaping params captured in the strategy submap of the key (§3).
_STRATEGY_SHAPING_PARAMS: tuple[str, ...] = (
    "consensus_panel_size",
    "adaptive_escalate_on_quote_fabrication",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_cache_dir(working_dir: Path | None = None) -> Path:
    """Return the eval-cache directory relative to *working_dir* (or CWD).

    The path ``.gate-keeper/cache/eval/`` mirrors the working-directory
    ``.gate-keeper/`` tree that already holds ``dependency-manifest.yml``
    and ``acks/`` (§5 of the design doc).
    """
    base = working_dir if working_dir is not None else Path.cwd()
    return base / ".gate-keeper" / "cache" / "eval"


def is_cache_enabled_from_env(env: dict[str, str]) -> bool:
    """Return True iff ``GATE_KEEPER_EVAL_CACHE`` is set to a truthy value in *env*.

    Truthy values (case-insensitive): ``1``, ``true``, ``yes``, ``on``.
    Anything else — including ``0``, ``false``, a missing key, or an empty
    string — is treated as disabled, preserving the default-off behaviour (§7).
    """
    return env.get("GATE_KEEPER_EVAL_CACHE", "").strip().lower() in _TRUTHY_VALUES


def _canonical_json(obj: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8 safe."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(text: str) -> str:
    """Return the hex sha256 digest of the UTF-8 encoding of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache key construction (§3)
# ---------------------------------------------------------------------------


def _build_rule_content_hash(rule: Rule) -> str:
    """Hash the rule predicate fields that determine the LLM judgment (§3).

    Excluded from the hash (per the design doc's ``params∖{…}`` notation):
    - ``strategy`` — keyed separately in the strategy submap.
    - ``targets``  — keyed separately in the targets list.
    - Any key starting with ``adaptive_`` — strategy-shaping params keyed in
      the strategy submap.

    Also excluded (§2.5): ``rule_id``, ``source``, ``severity`` — these are
    rule-identity / reporting metadata rehydrated on hit, not judgment inputs.
    """
    pruned_params = {
        k: v
        for k, v in rule.params.items()
        if k not in ("strategy", "targets") and not k.startswith("adaptive_")
    }
    predicate: dict[str, Any] = {
        "text": rule.text,
        "kind": rule.kind.value,
        "target_kind": rule.target_kind.value,
        "params": pruned_params,
    }
    return _sha256_hex(_canonical_json(predicate))


def _build_target_entries(
    rule: Rule,
    target: Any,
    artifact_kind: TargetKind | None,
) -> list[dict[str, Any]]:
    """Build the ``targets`` component of the cache key preimage (§3.1).

    For multi-target rules (``params.targets`` non-empty): one entry per
    declared spec in list order, each carrying ``id``, ``kind``, ``path``,
    and ``content_sha256``.

    For single-target rules: one entry with ``id=null`` carrying ``path`` (the
    string the prompt's ``Path:`` line renders, or ``null`` for inline /
    path-less artifacts) and ``content_sha256`` (hash of the rendered artifact
    text ``_resolve_artifact_input`` returned).

    Both components are required (§3.1): ``content_sha256`` guards edits,
    ``path`` guards renames / distinct-path collisions.
    """
    # Late import to avoid circular dependencies and to ensure monkeypatching
    # in tests works correctly (module-level attribute access, not bound name).
    import gate_keeper.backends.llm_rubric as _llm  # noqa: PLC0415

    specs = _llm._parse_multi_targets(rule)
    if specs:
        texts, _placeholder_ids = _llm._load_multi_target_texts(specs)
        return [
            {
                "id": spec.id,
                "kind": spec.kind.value,
                "path": spec.path,
                "content_sha256": _sha256_hex(texts.get(spec.id, "")),
            }
            for spec in specs
        ]

    # S5 (#281) dynamic affected-context path: a multi-file TargetSpec with no
    # literal params.targets is assembled into a single prompt. Acceptance
    # criterion 4 requires the *assembled-content* hash to be the target
    # content hash, so a dynamic set with the same surviving files and budget
    # hits, and any change to content / paths / order / truncation misses. We
    # re-run the same deterministic assembler llm_rubric.check() uses so both
    # sides agree on the surviving set (eval-cache.md §9).
    from gate_keeper.targets import TargetSpec  # noqa: PLC0415

    if isinstance(target, TargetSpec) and target.is_multi:
        assembly = _llm._assemble_affected_context(target, rule)
        return [
            {
                "id": _llm._AFFECTED_CONTEXT_CACHE_ID,
                # The header labels of the surviving files, in assembly order —
                # redundant with the labels embedded in the hashed assembled
                # text, but kept explicit for collision triage / auditability.
                "path": [af.label for af in assembly.included],
                "content_sha256": _sha256_hex(assembly.assembled_text),
                # Truncation-omitted and unreadable files never enter the
                # assembled body, but their labels DO surface in the cached
                # diagnostic's affected_context / truncation_warning evidence.
                # If they were left out of the key, adding or editing a file that
                # stays omitted under the same budget would hit an old entry and
                # serve stale evidence that omits it (codex P2, #290). Hash the
                # omitted identity + content, the unreadable labels, and the
                # token budget so any change to the omitted / unreadable set or
                # the budget misses and recomputes the correct evidence.
                "omitted": [
                    {"path": af.label, "content_sha256": _sha256_hex(af.text)}
                    for af in assembly.omitted_files
                ],
                "unreadable": list(assembly.unreadable),
                "token_budget": assembly.token_budget,
            }
        ]

    # Single-target path.
    # Mirror the unwrapping that llm_rubric.check() applies: a single-file
    # TargetSpec (is_multi=False) is normalised to its underlying path so that
    # _resolve_artifact_input reads the real file bytes (not str(TargetSpec))
    # and _resolve_single_target_path returns the actual path (not None).
    # Without this, edits to the file leave the cache key unchanged — stale hit.
    #
    # Resolve to str | Path before passing to _resolve_artifact_input /
    # _resolve_single_target_path (both typed str | Path).  multi-target specs
    # never reach this branch (validator skips them — llm_rubric returns
    # UNSUPPORTED, §2.4 skips storage); we still normalise them defensively.
    effective: str | Path
    if isinstance(target, TargetSpec):
        effective = target.paths[0] if target.paths else Path("")
    elif isinstance(target, (str, Path)):
        effective = target
    else:
        effective = str(target)
    rendered_text = _llm._resolve_artifact_input(effective, artifact_kind)
    path = _llm._resolve_single_target_path(effective)
    return [{"id": None, "path": path, "content_sha256": _sha256_hex(rendered_text)}]


def _build_strategy_map(rule: Rule) -> dict[str, Any]:
    """Build the strategy component (id + shaping params) of the key (§3)."""
    import gate_keeper.backends.llm_rubric as _llm  # noqa: PLC0415

    strategy_id = _llm._resolve_strategy_id(rule)
    shaping: dict[str, Any] = {
        key: rule.params[key] for key in _STRATEGY_SHAPING_PARAMS if key in rule.params
    }
    return {"id": strategy_id, "shaping": shaping}


def _build_sampling_map(provider: str, model: str, *, deterministic: bool) -> dict[str, Any]:
    """Build the sampling descriptor component of the key (§3).

    Non-deterministic (default) mode: ``{"mode": "provider_default"}``.
    Deterministic mode: ``{"mode": "deterministic", "params": <resolved params>}``.
    The *effective* descriptor (post capability-check) is keyed, not the raw flag,
    so a reasoning-class model that ignores ``temperature`` produces a distinct key
    from a standard model that accepts it.
    """
    if not deterministic:
        return {"mode": "provider_default"}
    import gate_keeper.backends.llm_rubric as _llm  # noqa: PLC0415

    params = _llm._deterministic_sampling_params(provider, model)
    return {"mode": "deterministic", "params": params}


def build_key_preimage(
    rule: Rule,
    target: Any,
    artifact_kind: TargetKind | None,
    deterministic: bool,
    reproducibility_n: int,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Build the full cache key preimage dict (§3).

    Returns a JSON-serializable dict whose sha256 digest (via
    :func:`compute_key`) is the cache filename stem.

    Parameters
    ----------
    rule:
        The semantic-rubric rule being evaluated.
    target:
        The resolved target artifact (file path or inline string).
    artifact_kind:
        Caller-declared artifact kind, or ``None``.
    deterministic:
        Whether ``--deterministic`` mode was active.
    reproducibility_n:
        The ``--reproducibility N`` value (≥ 1).
    provider:
        Provider name (e.g. ``"anthropic"``, ``"openai"``).
    model:
        Resolved model identifier (post default-fallback resolution).
    """
    import gate_keeper.backends.llm_rubric as _llm  # noqa: PLC0415

    return {
        "prompt_version": _llm.PROMPT_VERSION,
        "provider": provider.lower(),
        "resolved_model_id": model,
        "rule_content_hash": _build_rule_content_hash(rule),
        "targets": _build_target_entries(rule, target, artifact_kind),
        "strategy": _build_strategy_map(rule),
        "sampling": _build_sampling_map(provider, model, deterministic=deterministic),
        "artifact_kind": artifact_kind.value if artifact_kind is not None else "unspecified",
        "reproducibility_n": reproducibility_n,
    }


def compute_key(preimage: dict[str, Any]) -> str:
    """Return the hex sha256 digest of the canonical JSON preimage (§3).

    The digest is the cache filename stem (``<hex>.json``).
    """
    return _sha256_hex(_canonical_json(preimage))


# ---------------------------------------------------------------------------
# Storage and retrieval (§4, §5, §6)
# ---------------------------------------------------------------------------


def lookup(cache_dir: Path, key: str) -> dict[str, Any] | None:
    """Return the parsed cache entry for *key*, or ``None`` on miss / corruption.

    Failure modes that return ``None`` (§2.3 — all are fail-open to recompute):
    - File absent (key miss).
    - OS-level read error.
    - JSON decode failure (corrupt entry).
    - Unknown ``cache_schema_version`` (forward-compat miss).
    - Entry is not a dict.
    """
    path = cache_dir / f"{key}.json"
    try:
        raw = path.read_text(encoding="utf-8")
        entry = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("cache_schema_version") != _CACHE_SCHEMA_VERSION:
        # Unknown version → miss (never error). The entry will be overwritten
        # by the real evaluation that follows (§2.3).
        return None
    return entry


def rehydrate(entry: dict[str, Any], rule: Rule) -> Diagnostic | None:
    """Reconstruct a ``Diagnostic`` from *entry* for the current *rule*.

    Returns ``None`` on any deserialization failure (§2.3 — corrupt entry is
    treated as a miss). On success:

    - Applies §2.5 rehydration: overrides ``rule_id``, ``source``, and
      ``severity`` with the *current* rule's values so a duplicated-predicate
      hit reports the current rule's identity, not the producer's.
    - Appends a ``cache_hit`` evidence record (§6) describing the zero-cost
      nature of this run. The record is added fresh on every hit and is NOT
      stored in the cache entry, so repeated hits do not accrete markers.
    """
    try:
        diag = Diagnostic.from_dict(entry["diagnostic"])
    except (KeyError, ValueError, TypeError):
        return None

    # §2.5: rehydrate rule identity from the current rule.
    diag = dataclasses.replace(
        diag,
        rule_id=rule.id,
        source=rule.source,
        severity=rule.severity,
    )

    # §6: append the cache_hit evidence record.  Not stored in the entry;
    # added fresh on each hit.  ``cost_usd: 0`` and ``provider_calls: 0``
    # describe *this run's* incremental spend.  The original ``llm_judgment``
    # evidence retains its ``cost_estimate_usd`` / ``latency_ms`` / token
    # counts, so cost observability of *what the verdict cost to produce*
    # survives alongside what *this run* paid (nothing).
    hit_evidence = Evidence(
        kind="cache_hit",
        data={
            "cache_hit": True,
            "cached_at": entry.get("created_at", ""),
            "provider_calls": 0,
            "cost_usd": 0,
        },
    )
    return dataclasses.replace(diag, evidence=[*diag.evidence, hit_evidence])


def store(
    cache_dir: Path,
    key: str,
    preimage: dict[str, Any],
    diagnostic: Diagnostic,
    *,
    max_mb: int = _DEFAULT_MAX_MB,
) -> None:
    """Write *diagnostic* to the cache using atomic tmp+rename (§5).

    Only PASS/FAIL verdicts are written (§2.4). Transient / configuration
    states (UNAVAILABLE, ERROR, UNSUPPORTED) are silently skipped.

    The write is atomic: the entry is first written to a temp file in the
    same directory, then renamed into place via ``os.replace``. This
    guarantees a reader sees either the prior entry or a complete new entry —
    never a torn write. There is no lockfile; racing double-compute is
    tolerated (§5 — both writers produce the same logical verdict under the
    same key; the last rename wins, both files are internally complete).

    After a successful write, :func:`_maybe_evict` runs best-effort LRU
    eviction (§7.1). Any write-side failure is silently swallowed — the cache
    is a cost optimization, never a correctness requirement.
    """
    if diagnostic.status not in (Status.PASS, Status.FAIL):
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entry: dict[str, Any] = {
            "cache_schema_version": _CACHE_SCHEMA_VERSION,
            "key": key,
            "created_at": created_at,
            "prompt_version": preimage.get("prompt_version", ""),
            "key_preimage": preimage,
            # Stored BEFORE the cache_hit marker is attached (§4): the artifact
            # is the genuine evaluation result; the hit marker is added fresh
            # on each subsequent hit by :func:`rehydrate`.
            "diagnostic": diagnostic.to_dict(),
        }
        raw = _canonical_json(entry)
        final_path = cache_dir / f"{key}.json"
        # Atomic write: temp file in same directory → os.replace.
        fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw)
            os.replace(tmp, final_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _maybe_evict(cache_dir, max_mb)
    except Exception:
        pass  # Cache writes are never fatal.


def _maybe_evict(cache_dir: Path, max_mb: int) -> None:
    """Best-effort LRU eviction by file mtime after a successful write (§7.1).

    Scans the cache directory, sums entry sizes, and unlinks the
    oldest-mtime entries until the directory is under *max_mb* megabytes.
    An unlink race (another process already removed the file) is ignored.
    Any error is swallowed — eviction never blocks a ``validate`` run.
    """
    try:
        max_bytes = max_mb * 1024 * 1024
        # Gather (mtime, path, size) sorted oldest-first.
        entries: list[tuple[float, Path, int]] = []
        for p in cache_dir.glob("*.json"):
            try:
                st = p.stat()
                entries.append((st.st_mtime, p, st.st_size))
            except OSError:
                continue
        entries.sort()  # ascending mtime → oldest first
        total = sum(sz for _, _, sz in entries)
        for _, path, sz in entries:
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= sz
            except OSError:
                pass  # Already removed; ignore.
    except Exception:
        pass


# ---------------------------------------------------------------------------
# High-level helpers (called from validator.py) — fail-open on any error
# ---------------------------------------------------------------------------


def try_lookup(
    rule: Rule,
    target: Any,
    artifact_kind: TargetKind | None,
    deterministic: bool,
    reproducibility_n: int,
    *,
    cache_dir: Path | None = None,
) -> Diagnostic | None:
    """Try to return a cached ``Diagnostic``; return ``None`` on miss or any error.

    Loads the project dotenv via ``llm_rubric._load_env_file()``, resolves
    provider and model, builds the key preimage, and delegates to
    :func:`lookup` + :func:`rehydrate`. Returns ``None`` when:

    - Provider is not configured (the real evaluation will return UNAVAILABLE,
      which §2.4 will not cache, so skipping the lookup is correct).
    - Key is a miss.
    - Entry is corrupt / unknown schema version (§2.3).
    - Any unexpected exception (fail-open).

    The *cache_dir* keyword argument is for testing: pass a ``tmp_path``-based
    directory to avoid touching the real ``.gate-keeper/cache/eval/``.
    """
    try:
        import gate_keeper.backends.llm_rubric as _llm  # noqa: PLC0415

        env = _llm._load_env_file()
        # Mirror _is_configured: require both a recognised provider name AND the
        # matching API key.  A cache hit while the key is absent would mask a
        # credential failure (the real backend would produce UNAVAILABLE).
        if not _llm._is_configured(env):
            return None
        provider = env.get("GATE_KEEPER_LLM_PROVIDER", "").lower().strip()
        model = _llm._resolve_model(provider, env)

        _dir = cache_dir if cache_dir is not None else get_cache_dir()
        preimage = build_key_preimage(
            rule, target, artifact_kind, deterministic, reproducibility_n, provider, model
        )
        key = compute_key(preimage)
        entry = lookup(_dir, key)
        if entry is None:
            return None
        return rehydrate(entry, rule)
    except Exception:  # noqa: BLE001
        return None  # Any failure → miss (§2.3).


def try_store(
    rule: Rule,
    target: Any,
    artifact_kind: TargetKind | None,
    deterministic: bool,
    reproducibility_n: int,
    diagnostic: Diagnostic,
    *,
    cache_dir: Path | None = None,
) -> None:
    """Store *diagnostic* in the cache; never raises (§5 — cache is best-effort).

    Only PASS/FAIL verdicts are written (§2.4 — delegated to :func:`store`).
    Skips silently when the provider is not configured or any error occurs.

    The *cache_dir* keyword argument is for testing.
    """
    try:
        import gate_keeper.backends.llm_rubric as _llm  # noqa: PLC0415

        env = _llm._load_env_file()
        # Same guard as try_lookup: require full provider configuration (name +
        # API key) before storing.  A missing key means the real backend would
        # have returned UNAVAILABLE which §2.4 would not cache — skip storage.
        if not _llm._is_configured(env):
            return
        provider = env.get("GATE_KEEPER_LLM_PROVIDER", "").lower().strip()
        model = _llm._resolve_model(provider, env)

        raw_mb = env.get("GATE_KEEPER_EVAL_CACHE_MAX_MB", "").strip()
        max_mb = int(raw_mb) if raw_mb.isdigit() else _DEFAULT_MAX_MB

        _dir = cache_dir if cache_dir is not None else get_cache_dir()
        preimage = build_key_preimage(
            rule, target, artifact_kind, deterministic, reproducibility_n, provider, model
        )
        key = compute_key(preimage)
        store(_dir, key, preimage, diagnostic, max_mb=max_mb)
    except Exception:  # noqa: BLE001
        pass  # Cache failures are never fatal.
