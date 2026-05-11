"""Fixture-corpus benchmark harness for the LLM-rubric backend (#130).

Loads benchmark entries from a directory of JSON files (schema documented in
``tests/fixtures/semantic/README.md``) and evaluates each entry against the
``llm-rubric`` backend, producing aggregate accuracy / reproducibility / token
metrics plus a per-entry breakdown.

The CLI surface is wired in ``cli.py`` via ``gate-keeper bench``; this module
contains the loader + evaluator + result aggregator so the logic is testable
in isolation and reusable from future tooling.

Entry shape (subset used here)::

    {
      "rule_text": "...",
      "target": {"kind": "path"|"inline", "value": "..."},
      "expected_judgment": "pass" | "fail",
      "expected_rationale_keywords": ["..."],
      "category": "clarity" | ...,
      "intended_backend": "llm-rubric" | ...,
      "notes": "..."
    }

When ``target.kind == "path"`` the value is resolved relative to a sibling
``targets/`` directory (``<entries_dir>/../targets/``). Inline targets are
passed through verbatim.
"""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from gate_keeper.backends import llm_rubric as _llm
from gate_keeper.models import (
    Backend,
    Confidence,
    Rule,
    RuleKind,
    Severity,
    SourceLocation,
    Status,
    TargetKind,
)

# ---------------------------------------------------------------------------
# Entry loader
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = frozenset(
    {
        "rule_text",
        "target",
        "expected_judgment",
        "expected_rationale_keywords",
        "category",
        "intended_backend",
    }
)
# ``rule_target_kind`` (#169) is optional. When set on an entry, the bench
# harness propagates the value into the synthesised :class:`Rule` so the
# rubric backend renders the v3 artifact-kind block. Distinct name from the
# pre-existing ``target.kind`` (path / inline) below — the two address
# different concerns (artifact kind vs. how the target value is encoded).
_OPTIONAL_FIELDS = frozenset({"notes", "rule_target_kind", "artifact_kind"})
_TARGET_FIELDS = frozenset({"kind", "value"})
_VALID_KINDS = ("path", "inline")
_VALID_JUDGMENTS = ("pass", "fail", "unsupported")


@dataclass(frozen=True)
class BenchEntry:
    """A single benchmark fixture entry, loaded from JSON."""

    id: str
    rule_text: str
    target_kind: str
    target_value: str
    expected_judgment: str
    expected_rationale_keywords: tuple[str, ...]
    category: str
    intended_backend: str
    notes: str | None
    source_path: Path
    # #169 — optional artifact-kind annotation on the synthesised rule.
    rule_target_kind: TargetKind = TargetKind.UNSPECIFIED
    # #204 — optional caller-declared kind for the target artifact. When set
    # and ``rule_target_kind`` is also set and differs, the bench harness fires
    # the deterministic precheck (#178) before invoking the LLM provider.
    artifact_kind: TargetKind | None = None

    def resolve_target(self, targets_root: Path) -> str:
        """Return the literal text the rule should be evaluated against."""
        if self.target_kind == "inline":
            return self.target_value
        path = targets_root / self.target_value
        if not path.is_file():
            raise FileNotFoundError(
                f"target path does not resolve to a file: {path} (referenced from {self.source_path.name})"
            )
        return path.read_text(encoding="utf-8")


def _expect_str(value: Any, ctx: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{ctx}: expected str, got {type(value).__name__}")
    return value


def _expect_list_of_str(value: Any, ctx: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{ctx}: expected list, got {type(value).__name__}")
    out: list[str] = []
    for i, item in enumerate(value):
        out.append(_expect_str(item, f"{ctx}[{i}]"))
    return out


def parse_entry(data: Any, *, source_path: Path) -> BenchEntry:
    """Parse a single fixture entry; raise ``ValueError`` on schema violation."""
    if not isinstance(data, dict):
        raise ValueError(f"BenchEntry({source_path.name}): expected mapping, got {type(data).__name__}")
    keys = set(data)
    missing = _REQUIRED_FIELDS - keys
    if missing:
        raise ValueError(f"BenchEntry({source_path.name}): missing required fields: {sorted(missing)}")
    unknown = keys - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    if unknown:
        raise ValueError(f"BenchEntry({source_path.name}): unknown fields: {sorted(unknown)}")

    target = data["target"]
    if not isinstance(target, dict):
        raise ValueError(
            f"BenchEntry({source_path.name}).target: expected mapping, got {type(target).__name__}"
        )
    target_keys = set(target)
    missing_target = _TARGET_FIELDS - target_keys
    if missing_target:
        raise ValueError(f"BenchEntry({source_path.name}).target: missing fields: {sorted(missing_target)}")
    unknown_target = target_keys - _TARGET_FIELDS
    if unknown_target:
        raise ValueError(f"BenchEntry({source_path.name}).target: unknown fields: {sorted(unknown_target)}")

    target_kind = _expect_str(target["kind"], "target.kind")
    if target_kind not in _VALID_KINDS:
        raise ValueError(
            f"BenchEntry({source_path.name}).target.kind: expected one of {list(_VALID_KINDS)}, "
            f"got {target_kind!r}"
        )
    target_value = _expect_str(target["value"], "target.value")

    expected_judgment = _expect_str(data["expected_judgment"], "expected_judgment")
    if expected_judgment not in _VALID_JUDGMENTS:
        raise ValueError(
            f"BenchEntry({source_path.name}).expected_judgment: expected one of {list(_VALID_JUDGMENTS)}, "
            f"got {expected_judgment!r}"
        )

    notes_value = data.get("notes")
    if notes_value is not None and not isinstance(notes_value, str):
        raise ValueError(
            f"BenchEntry({source_path.name}).notes: expected str or absent, got {type(notes_value).__name__}"
        )

    rule_target_kind_value = data.get("rule_target_kind")
    if rule_target_kind_value is None:
        rule_target_kind = TargetKind.UNSPECIFIED
    else:
        if not isinstance(rule_target_kind_value, str):
            raise ValueError(
                f"BenchEntry({source_path.name}).rule_target_kind: expected str, got "
                f"{type(rule_target_kind_value).__name__}"
            )
        try:
            rule_target_kind = TargetKind(rule_target_kind_value)
        except ValueError as exc:
            valid = sorted(member.value for member in TargetKind)
            raise ValueError(
                f"BenchEntry({source_path.name}).rule_target_kind: "
                f"{rule_target_kind_value!r} is not a valid TargetKind; expected one of {valid}"
            ) from exc

    artifact_kind_value = data.get("artifact_kind")
    if artifact_kind_value is None:
        artifact_kind: TargetKind | None = None
    else:
        if not isinstance(artifact_kind_value, str):
            raise ValueError(
                f"BenchEntry({source_path.name}).artifact_kind: expected str, got "
                f"{type(artifact_kind_value).__name__}"
            )
        try:
            artifact_kind = TargetKind(artifact_kind_value)
        except ValueError as exc:
            valid = sorted(member.value for member in TargetKind)
            raise ValueError(
                f"BenchEntry({source_path.name}).artifact_kind: "
                f"{artifact_kind_value!r} is not a valid TargetKind; expected one of {valid}"
            ) from exc

    return BenchEntry(
        id=source_path.stem,
        rule_text=_expect_str(data["rule_text"], "rule_text"),
        target_kind=target_kind,
        target_value=target_value,
        expected_judgment=expected_judgment,
        expected_rationale_keywords=tuple(
            _expect_list_of_str(data["expected_rationale_keywords"], "expected_rationale_keywords")
        ),
        category=_expect_str(data["category"], "category"),
        intended_backend=_expect_str(data["intended_backend"], "intended_backend"),
        notes=notes_value,
        source_path=source_path,
        rule_target_kind=rule_target_kind,
        artifact_kind=artifact_kind,
    )


def load_entry(path: Path) -> BenchEntry:
    """Load and validate a single entry JSON file."""
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_entry(data, source_path=path)


def iter_entries(entries_dir: Path) -> Iterable[BenchEntry]:
    """Yield all entries under *entries_dir* in stable filename order."""
    for path in sorted(entries_dir.glob("*.json")):
        yield load_entry(path)


# ---------------------------------------------------------------------------
# Rule synthesis (entry → IR Rule)
# ---------------------------------------------------------------------------


def _entry_to_rule(entry: BenchEntry) -> Rule:
    """Synthesise a ``Rule`` IR object from a benchmark entry.

    Bench entries are decoupled from on-disk Markdown documents: the entry's
    ``rule_text`` is the rule, and the rest of the IR fields take fixed defaults
    (kind ``SEMANTIC_RUBRIC``, backend hint ``LLM_RUBRIC``, severity
    ``WARNING``). The ``source`` path is the entry filename so diagnostic
    output remains traceable to a fixture file. The optional
    ``rule_target_kind`` annotation (#169) is propagated so the rubric
    backend renders the v3 artifact-kind block when set.
    """
    return Rule(
        id=entry.id,
        title=entry.rule_text[:80],
        source=SourceLocation(path=entry.source_path.name, line=1),
        text=entry.rule_text,
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.WARNING,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params={},
        target_kind=entry.rule_target_kind,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class PerRuleResult:
    """Result for a single bench entry after N evaluations."""

    id: str
    category: str
    intended_backend: str
    expected: str
    actual: str  # "pass" | "fail" | "unavailable" | "error"
    status: str  # "PASS" | "FAIL" — does *actual* match *expected*?
    reproducibility: float
    primary_reason: str | None
    failure_mode: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str | None
    prompt_version: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "intended_backend": self.intended_backend,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
            "reproducibility": self.reproducibility,
            "primary_reason": self.primary_reason,
            "failure_mode": self.failure_mode,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


@dataclass
class BenchSummary:
    """Aggregate counters across all bench entries."""

    entries: int
    correct: int
    accuracy: float
    reproducibility_avg: float
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str | None
    prompt_version: str | None
    reproducibility_n: int
    unavailable: int
    errors: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "reproducibility_avg": self.reproducibility_avg,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "reproducibility_n": self.reproducibility_n,
            "unavailable": self.unavailable,
            "errors": self.errors,
        }


@dataclass
class BenchResult:
    """Top-level bench output, JSON-serialisable."""

    summary: BenchSummary
    per_rule: list[PerRuleResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_dict(),
            "per_rule": [p.to_dict() for p in self.per_rule],
        }


def _evaluate_entry(entry: BenchEntry, targets_root: Path, n: int) -> PerRuleResult:
    """Evaluate a single entry ``n`` times, aggregating via majority vote.

    Mirrors ``llm_rubric.run_n``'s aggregation semantics but inlines the loop so
    we can keep raw per-run telemetry sums (the run_n diagnostic only carries
    a single representative's telemetry, not the cumulative cost across runs).

    Tie-break for the majority judgment is fail-closed (fail wins ties).
    Non-pass/fail outcomes (UNAVAILABLE / ERROR) abort aggregation early — the
    entry is reported with its actual status and reproducibility 0.0, so a
    misconfigured provider does not silently inflate accuracy.
    """
    if n < 1:
        raise ValueError(f"reproducibility n must be >= 1, got {n}")

    rule = _entry_to_rule(entry)
    target_text = entry.resolve_target(targets_root)

    # #204 — deterministic target-kind-mismatch precheck (#178). When the
    # fixture declares ``artifact_kind`` and the rule carries a differing
    # ``target_kind`` annotation, short-circuit to UNSUPPORTED without
    # invoking the LLM provider — mirrors the precheck in validator.validate.
    if (
        entry.artifact_kind is not None
        and rule.target_kind is not TargetKind.UNSPECIFIED
        and rule.target_kind is not entry.artifact_kind
    ):
        from gate_keeper.validator import _target_kind_mismatch_diagnostic  # noqa: PLC0415

        diag = _target_kind_mismatch_diagnostic(rule, entry.artifact_kind)
        matched = entry.expected_judgment == "unsupported"
        return PerRuleResult(
            id=entry.id,
            category=entry.category,
            intended_backend=entry.intended_backend,
            expected=entry.expected_judgment,
            actual="unsupported",
            status="PASS" if matched else "FAIL",
            reproducibility=1.0 if matched else 0.0,
            primary_reason=diag.message,
            failure_mode=None if matched else "kind_mismatch_unexpected",
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
            model=None,
            prompt_version=None,
        )

    pass_count = 0
    fail_count = 0
    # Collect per-run (judgment, primary_reason) so we can pick a rationale
    # that matches the majority judgment after aggregation (P1: avoid reporting
    # a PASS rationale when the majority outcome is FAIL, or vice-versa).
    per_run_reasons: list[tuple[str, str | None]] = []
    failure_mode: str | None = None
    tokens_in_total = 0
    tokens_out_total = 0
    latency_ms_total = 0
    model: str | None = None
    prompt_version: str | None = None

    # Evidence kinds that carry the standard telemetry fields (#76, #133,
    # #169, #172). Provider-error / provider_unconfigured are deliberately
    # excluded — those paths do not carry telemetry by contract.
    _TELEMETRY_BEARING_KINDS = (
        "llm_judgment",
        "target_kind_mismatch",
        "llm_quote_fabrication",
    )

    for _ in range(n):
        diag = _llm.check(rule, target_text)

        # Telemetry is recorded on every evidence kind that represents a
        # successful provider call (judgment-bearing, target-kind-mismatch,
        # or grounding-violation rejection). provider_error /
        # provider_unconfigured carry no telemetry.
        run_reason: str | None = None
        for ev in diag.evidence:
            if ev.kind in _TELEMETRY_BEARING_KINDS:
                tokens_in_total += int(ev.data.get("tokens_in", 0) or 0)
                tokens_out_total += int(ev.data.get("tokens_out", 0) or 0)
                latency_ms_total += int(ev.data.get("latency_ms", 0) or 0)
                if model is None:
                    model = ev.data.get("model")
                if prompt_version is None:
                    prompt_version = ev.data.get("prompt_version")
                if run_reason is None:
                    run_reason = ev.data.get("primary_reason")

        if diag.status is Status.PASS:
            pass_count += 1
            per_run_reasons.append(("pass", run_reason))
        elif diag.status is Status.FAIL:
            fail_count += 1
            per_run_reasons.append(("fail", run_reason))
        else:
            # UNAVAILABLE / UNSUPPORTED / ERROR — fail-closed, report immediately.
            #
            # #169 special-case: when the rule carries ``target_kind`` and
            # the model returns an ``unsupported`` verdict (mapped to
            # ``Status.UNSUPPORTED`` with ``target_kind_mismatch`` evidence),
            # that is an expected outcome for entries authored with
            # ``expected_judgment == "unsupported"``. Treat such an entry
            # as PASS and record the model-side primary_reason; we still
            # short-circuit aggregation because reproducibility scoring
            # for a third verdict is not modeled.
            tk_mismatch_evidence: dict | None = None
            for ev in diag.evidence:
                if ev.kind == "target_kind_mismatch":
                    tk_mismatch_evidence = ev.data
                if ev.kind == "provider_error":
                    failure_mode = str(ev.data.get("failure_mode", "provider_error"))
                    break
                if ev.kind == "provider_unconfigured":
                    failure_mode = "provider_unconfigured"
                    break

            if tk_mismatch_evidence is not None and entry.expected_judgment == "unsupported":
                return PerRuleResult(
                    id=entry.id,
                    category=entry.category,
                    intended_backend=entry.intended_backend,
                    expected=entry.expected_judgment,
                    actual="unsupported",
                    status="PASS",
                    reproducibility=1.0,
                    primary_reason=tk_mismatch_evidence.get("primary_reason") or diag.message,
                    failure_mode=None,
                    tokens_in=tokens_in_total,
                    tokens_out=tokens_out_total,
                    latency_ms=latency_ms_total,
                    model=model,
                    prompt_version=prompt_version,
                )

            actual = diag.status.value
            return PerRuleResult(
                id=entry.id,
                category=entry.category,
                intended_backend=entry.intended_backend,
                expected=entry.expected_judgment,
                actual=actual,
                status="FAIL",  # mismatch with expected pass/fail
                reproducibility=0.0,
                primary_reason=run_reason or diag.message,
                failure_mode=failure_mode or actual,
                tokens_in=tokens_in_total,
                tokens_out=tokens_out_total,
                latency_ms=latency_ms_total,
                model=model,
                prompt_version=prompt_version,
            )

    total = pass_count + fail_count
    majority_is_pass = pass_count > fail_count
    actual = "pass" if majority_is_pass else "fail"
    majority_count = pass_count if majority_is_pass else fail_count
    reproducibility = majority_count / total if total > 0 else 0.0
    correct = actual == entry.expected_judgment

    # Pick the primary_reason from the first run that matches the majority
    # judgment, so the rationale is internally consistent with the reported
    # outcome (P1: avoids a PASS rationale when majority outcome is FAIL).
    primary_reason: str | None = next(
        (reason for judgment, reason in per_run_reasons if judgment == actual),
        None,
    )

    return PerRuleResult(
        id=entry.id,
        category=entry.category,
        intended_backend=entry.intended_backend,
        expected=entry.expected_judgment,
        actual=actual,
        status="PASS" if correct else "FAIL",
        reproducibility=reproducibility,
        primary_reason=primary_reason,
        failure_mode=None if correct else f"llm_{actual}",
        tokens_in=tokens_in_total,
        tokens_out=tokens_out_total,
        latency_ms=latency_ms_total,
        model=model,
        prompt_version=prompt_version,
    )


def run_bench(entries_dir: Path, *, reproducibility: int = 1) -> BenchResult:
    """Evaluate every entry under *entries_dir* and aggregate the results.

    Targets resolve relative to ``<entries_dir>/../targets/`` (matching the
    repo layout under ``tests/fixtures/semantic/``).
    """
    if reproducibility < 1:
        raise ValueError(f"reproducibility must be >= 1, got {reproducibility}")

    targets_root = entries_dir.parent / "targets"

    per_rule: list[PerRuleResult] = []
    for entry in iter_entries(entries_dir):
        per_rule.append(_evaluate_entry(entry, targets_root, reproducibility))

    entries_count = len(per_rule)
    correct = sum(1 for r in per_rule if r.status == "PASS")
    accuracy = correct / entries_count if entries_count else 0.0
    # Reproducibility average is taken across entries that produced a pass/fail
    # outcome (i.e. exclude unavailable/error rows whose reproducibility is
    # mechanically 0.0 and would skew the metric downward).
    judged = [r for r in per_rule if r.actual in ("pass", "fail")]
    if judged:
        reproducibility_avg = sum(r.reproducibility for r in judged) / len(judged)
    else:
        reproducibility_avg = 0.0
    tokens_in = sum(r.tokens_in for r in per_rule)
    tokens_out = sum(r.tokens_out for r in per_rule)
    latency_ms = sum(r.latency_ms for r in per_rule)

    model = next((r.model for r in per_rule if r.model), None)
    prompt_version = next((r.prompt_version for r in per_rule if r.prompt_version), None)

    unavailable = sum(1 for r in per_rule if r.actual == "unavailable")
    errors = sum(1 for r in per_rule if r.actual == "error")

    summary = BenchSummary(
        entries=entries_count,
        correct=correct,
        accuracy=accuracy,
        reproducibility_avg=reproducibility_avg,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        model=model,
        prompt_version=prompt_version,
        reproducibility_n=reproducibility,
        unavailable=unavailable,
        errors=errors,
    )
    return BenchResult(summary=summary, per_rule=per_rule)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(result: BenchResult) -> str:
    """Render a bench result as a compact human-readable summary."""
    s = result.summary
    lines: list[str] = []
    lines.append(f"entries: {s.entries}")
    accuracy_pct = s.accuracy * 100.0
    lines.append(f"correct: {s.correct}    accuracy: {accuracy_pct:.1f}%")
    lines.append(f"reproducibility (avg): {s.reproducibility_avg:.2f}  (n={s.reproducibility_n})")
    if s.unavailable or s.errors:
        lines.append(f"unavailable: {s.unavailable}    errors: {s.errors}")
    if result.per_rule:
        # Determine column width for entry ids
        id_width = max(len(r.id) for r in result.per_rule)
        lines.append("per-rule:")
        for r in result.per_rule:
            note = ""
            if r.status == "FAIL":
                if r.actual in ("pass", "fail"):
                    note = f"  (expected {r.expected}, got {r.actual})"
                else:
                    note = f"  ({r.actual}: {r.failure_mode or '?'})"
            lines.append(f"  {r.id:<{id_width}}  {r.status}  rep={r.reproducibility:.2f}{note}")
    model_part = f" model={s.model}" if s.model else ""
    prompt_part = f" prompt_version={s.prompt_version}" if s.prompt_version else ""
    lines.append(
        f"total: tokens_in={s.tokens_in} tokens_out={s.tokens_out} "
        f"latency_ms={s.latency_ms}{model_part}{prompt_part}"
    )
    return "\n".join(lines)


def render_json(result: BenchResult) -> str:
    """Render a bench result as deterministic JSON."""
    return json.dumps(result.to_dict(), sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# Baseline diff (#132 lands the actual baseline.json; this is the framework)
# ---------------------------------------------------------------------------


@dataclass
class BaselineDelta:
    """Diff between the current bench result and a stored baseline."""

    accuracy_delta: float
    reproducibility_avg_delta: float
    regressed: list[str]  # entry ids that flipped PASS → FAIL
    fixed: list[str]  # entry ids that flipped FAIL → PASS
    new_entries: list[str]  # ids present in current but not baseline
    removed_entries: list[str]  # ids present in baseline but not current

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def diff_baseline(current: BenchResult, baseline_path: Path) -> BaselineDelta:
    """Compute a delta between the current bench result and a stored baseline.

    The baseline file is expected to be a JSON document with the same shape as
    ``BenchResult.to_dict()`` (i.e. produced by ``gate-keeper bench --format
    json``). #132 lands the canonical ``tests/fixtures/semantic/baseline.json``;
    this function provides the diff plumbing now so the CLI surface is stable.
    """
    if not baseline_path.is_file():
        raise FileNotFoundError(f"baseline not found: {baseline_path}")
    with baseline_path.open("r", encoding="utf-8") as fh:
        baseline_data = json.load(fh)
    baseline_summary = baseline_data.get("summary", {})
    baseline_rules = {r["id"]: r for r in baseline_data.get("per_rule", [])}
    current_rules = {r.id: r for r in current.per_rule}

    regressed: list[str] = []
    fixed: list[str] = []
    for entry_id, current_row in current_rules.items():
        baseline_row = baseline_rules.get(entry_id)
        if baseline_row is None:
            continue
        baseline_status = baseline_row.get("status")
        if baseline_status == "PASS" and current_row.status == "FAIL":
            regressed.append(entry_id)
        elif baseline_status == "FAIL" and current_row.status == "PASS":
            fixed.append(entry_id)

    new_entries = sorted(set(current_rules) - set(baseline_rules))
    removed_entries = sorted(set(baseline_rules) - set(current_rules))

    accuracy_delta = current.summary.accuracy - float(baseline_summary.get("accuracy", 0.0))
    reproducibility_avg_delta = current.summary.reproducibility_avg - float(
        baseline_summary.get("reproducibility_avg", 0.0)
    )

    return BaselineDelta(
        accuracy_delta=accuracy_delta,
        reproducibility_avg_delta=reproducibility_avg_delta,
        regressed=sorted(regressed),
        fixed=sorted(fixed),
        new_entries=new_entries,
        removed_entries=removed_entries,
    )


def render_baseline_delta_text(delta: BaselineDelta) -> str:
    """Human-readable summary of a baseline delta."""
    lines: list[str] = ["baseline delta:"]
    lines.append(f"  accuracy:          {delta.accuracy_delta:+.4f}")
    lines.append(f"  reproducibility:   {delta.reproducibility_avg_delta:+.4f}")
    if delta.regressed:
        lines.append(f"  regressed ({len(delta.regressed)}): {', '.join(delta.regressed)}")
    else:
        lines.append("  regressed (0): -")
    if delta.fixed:
        lines.append(f"  fixed ({len(delta.fixed)}): {', '.join(delta.fixed)}")
    else:
        lines.append("  fixed (0): -")
    if delta.new_entries:
        lines.append(f"  new ({len(delta.new_entries)}): {', '.join(delta.new_entries)}")
    if delta.removed_entries:
        lines.append(f"  removed ({len(delta.removed_entries)}): {', '.join(delta.removed_entries)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Walltime helper (kept here so the CLI command can attach it cheaply)
# ---------------------------------------------------------------------------


def wall_now() -> float:
    """Indirection around ``time.perf_counter`` for testability."""
    return time.perf_counter()


__all__ = [
    "BenchEntry",
    "BenchResult",
    "BenchSummary",
    "PerRuleResult",
    "BaselineDelta",
    "diff_baseline",
    "iter_entries",
    "load_entry",
    "parse_entry",
    "render_baseline_delta_text",
    "render_json",
    "render_text",
    "run_bench",
    "wall_now",
]
