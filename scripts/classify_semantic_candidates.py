#!/usr/bin/env python3
"""One-off routing-audit script for llm-rubric fallback rules (#235, #75 slice 1).

Asks gpt-5 (minimal reasoning) whether each ``semantic_rubric``-fallback rule
could be evaluated deterministically by ``filesystem``, ``github``, or
``external+textlint``, and emits a JSON report for human triage.

This is a **read-only utility** — no classifier behavior is changed.

Usage::

    uv run python scripts/classify_semantic_candidates.py \\
        --rules docs/dogfooding-rules.md \\
        [--model openai:gpt-5] \\
        [--out /tmp/report.json]

    # dry-run: print prompts without calling the API
    uv run python scripts/classify_semantic_candidates.py \\
        --rules docs/dogfooding-rules.md \\
        --dry-run

Refs: issue #235, #75, umbrella #164.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow running as ``python scripts/classify_semantic_candidates.py`` without a
# prior ``uv pip install -e .`` by inserting the in-tree ``src/`` onto sys.path
# when the package is not already importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from gate_keeper.backends import llm_rubric as _llm  # noqa: E402
from gate_keeper.classifier import classify  # noqa: E402
from gate_keeper.models import Backend, RuleKind  # noqa: E402
from gate_keeper.parser import parse_file  # noqa: E402

# ---------------------------------------------------------------------------
# Routing-auditor prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a routing classifier auditor for the gate-keeper tool.
The tool routes rule texts to one of four backends:
  - filesystem: file/path/existence predicates (e.g. "README.md must exist", \
"must not contain text X")
  - github: PR/check/label/review-state predicates via `gh` (e.g. "PR must \
not be a draft")
  - external+textlint: mechanical prose patterns — keyword/terminology/style \
(e.g. "must not use passive voice")
  - llm-rubric: judgment-level quality checks that require reading prose and \
forming an opinion

Could this rule's verdict be determined WITHOUT LLM judgment?
Respond as JSON with exactly these keys:
  "can_be_deterministic": true or false
  "proposed_backend": one of "filesystem", "github", "external+textlint", or null
  "proposed_pattern": a regex or short description, or null
  "rationale": one sentence explaining your decision

Return only the JSON object, no surrounding text.\
"""

_USER_TEMPLATE = 'Rule text: "{rule_text}"'

# ---------------------------------------------------------------------------
# Filter: identify semantic_rubric fallback rules
# ---------------------------------------------------------------------------


def _is_semantic_fallback(rule: Any) -> bool:
    """Return True iff *rule* landed in the semantic_rubric fallback bucket.

    A rule is a semantic-rubric fallback when:
    - backend_hint == Backend.LLM_RUBRIC, AND
    - kind == RuleKind.SEMANTIC_RUBRIC.

    Rules explicitly authored as ``external_check`` (kind == EXTERNAL_CHECK)
    or classified deterministic (backend != LLM_RUBRIC) are excluded.
    """
    return rule.backend_hint == Backend.LLM_RUBRIC and rule.kind == RuleKind.SEMANTIC_RUBRIC


# ---------------------------------------------------------------------------
# Per-rule audit call
# ---------------------------------------------------------------------------


def _audit_rule(rule: Any, api_key: str, model: str, dry_run: bool) -> dict[str, Any]:
    """Call the routing-auditor prompt for one rule and return a result dict."""
    user_msg = _USER_TEMPLATE.format(rule_text=rule.text)

    if dry_run:
        print(f"[dry-run] rule_id={rule.id}")
        print(f"  SYSTEM: {_SYSTEM_PROMPT[:120]}...")
        print(f"  USER:   {user_msg}")
        return {
            "rule_id": rule.id,
            "rule_text": rule.text,
            "dry_run": True,
        }

    response_text, telemetry = _llm._call_openai(api_key, _SYSTEM_PROMPT, user_msg, model)

    # Parse JSON response; fall back to a raw record on parse error.
    try:
        parsed = json.loads(response_text)
        can_be_det = bool(parsed.get("can_be_deterministic", False))
        proposed_backend = parsed.get("proposed_backend") or "none"
        proposed_pattern = parsed.get("proposed_pattern")
        rationale = str(parsed.get("rationale", ""))
    except (json.JSONDecodeError, ValueError):
        can_be_det = False
        proposed_backend = "none"
        proposed_pattern = None
        rationale = response_text.strip()

    result: dict[str, Any] = {
        "rule_id": rule.id,
        "rule_text": rule.text,
        "can_be_deterministic": can_be_det,
        "proposed_backend": proposed_backend,
        "rationale": rationale,
    }
    if proposed_pattern is not None:
        result["suggested_pattern"] = proposed_pattern
    result["_telemetry"] = telemetry
    return result


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------


def _build_report(results: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Aggregate per-rule results into the final JSON report shape."""
    candidates_by_backend: dict[str, list[dict[str, Any]]] = {
        "filesystem": [],
        "github": [],
        "external+textlint": [],
        "none": [],
    }

    total_tokens_in = 0
    total_tokens_out = 0
    has_unknown_cost = False

    for r in results:
        if r.get("dry_run"):
            continue
        backend_key = r.get("proposed_backend", "none")
        if backend_key not in candidates_by_backend:
            backend_key = "none"

        entry: dict[str, Any] = {
            "rule_id": r["rule_id"],
            "rationale": r.get("rationale", ""),
        }
        if backend_key != "none" and "suggested_pattern" in r:
            entry["suggested_pattern"] = r["suggested_pattern"]

        candidates_by_backend[backend_key].append(entry)

        tel = r.get("_telemetry", {})
        total_tokens_in += tel.get("tokens_in", 0)
        total_tokens_out += tel.get("tokens_out", 0)
        if not tel:
            has_unknown_cost = True

    # Derive model name from "provider:model" or plain model string.
    raw_model = model
    if ":" in model:
        _, raw_model = model.split(":", 1)

    cost: float | None = _llm._estimate_cost(raw_model, total_tokens_in, total_tokens_out)
    if has_unknown_cost:
        cost = None

    report: dict[str, Any] = {
        "model": model,
        "rules_evaluated": len([r for r in results if not r.get("dry_run")]),
        "candidates_by_backend": candidates_by_backend,
        "total_cost_estimate_usd": cost,
    }
    return report


# ---------------------------------------------------------------------------
# Core run function (importable for tests)
# ---------------------------------------------------------------------------


def run_audit(
    rules_path: str | Path,
    model: str = "openai:gpt-5",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Parse *rules_path*, filter to semantic-rubric fallback rules, audit each.

    Returns the JSON report dict. In dry-run mode the API is not called and
    the report contains ``"dry_run": true`` and an empty ``candidates_by_backend``.

    Raises ``RuntimeError`` if the provider is not configured (unless dry_run).
    """
    # Parse + classify the rules file.
    ruleset = parse_file(rules_path)
    classified = classify(ruleset)
    fallback_rules = [r for r in classified.rules if _is_semantic_fallback(r)]

    if not dry_run:
        env = _llm._load_env_file()
        if not env.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY not found in dotenv. "
                "Configure it in /home/vscode/.config/hermes-projects/gate-keeper.env "
                "or use --dry-run."
            )
        api_key = env["OPENAI_API_KEY"]
    else:
        api_key = ""

    # Strip "provider:" prefix to pass bare model name to _call_openai.
    raw_model = model
    if ":" in model:
        _, raw_model = model.split(":", 1)

    results: list[dict[str, Any]] = []
    for rule in fallback_rules:
        result = _audit_rule(rule, api_key, raw_model, dry_run)
        results.append(result)

    if dry_run:
        return {
            "model": model,
            "rules_evaluated": 0,
            "dry_run": True,
            "fallback_rules_found": len(fallback_rules),
            "candidates_by_backend": {
                "filesystem": [],
                "github": [],
                "external+textlint": [],
                "none": [],
            },
            "total_cost_estimate_usd": None,
        }

    return _build_report(results, model)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Routing-audit script: ask gpt-5 whether llm-rubric fallback "
        "rules could be deterministic. Emits a JSON report."
    )
    parser.add_argument(
        "--rules",
        required=True,
        metavar="PATH",
        help="Path to a Markdown rules file (e.g. docs/dogfooding-rules.md).",
    )
    parser.add_argument(
        "--model",
        default="openai:gpt-5",
        metavar="PROVIDER:MODEL",
        help="Model to use for routing audit (default: openai:gpt-5).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="Write JSON report to this file (stdout if omitted).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts without calling the API.",
    )

    args = parser.parse_args(argv)
    rules_path = Path(args.rules)

    if not rules_path.exists():
        print(f"error: rules file not found: {rules_path}", file=sys.stderr)
        return 1

    try:
        report = run_audit(rules_path, model=args.model, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report_json = json.dumps(report, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(report_json, encoding="utf-8")
        print(f"Report written to {out_path}", file=sys.stderr)
    else:
        print(report_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
