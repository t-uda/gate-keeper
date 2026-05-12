"""Tests for the dogfooding advisory-comment formatter (#240).

The formatter lives outside ``src/`` (it is workflow glue, not a public
package surface), so we load it via ``importlib`` from the repo-relative
path. The tests pin:

1. Structural-skip diagnostics (``target_kind_mismatch`` / ``llm_quote_fabrication``)
   are filtered out of the main table and surfaced in a ``<details>`` block.
2. Actionable-only inputs render a plain table with no ``<details>`` block.
3. Structural-skip-only inputs render the "no author-actionable findings"
   placeholder plus the ``<details>`` block.
4. Mixed inputs render only the actionable rows in the main table, with the
   skipped rows under the ``<details>`` block.
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "format_dogfooding_comment.py"
)


def _load_formatter():
    spec = importlib.util.spec_from_file_location(
        "format_dogfooding_comment", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path, payload: dict[str, Any]) -> str:
    formatter = _load_formatter()
    json_path = tmp_path / "report.json"
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = formatter.main(["format_dogfooding_comment.py", str(json_path)])
    assert rc == 0
    return buf.getvalue()


def _diag_actionable_fail() -> dict[str, Any]:
    return {
        "rule_id": "rule-dogfooding-rules-L23",
        "status": "fail",
        "message": (
            "The PR description does not name the user-visible change in "
            "the first sentence."
        ),
        "evidence": [
            {
                "kind": "llm_judgment",
                "data": {
                    "judgment": "fail",
                    "primary_reason": "missing user-visible change",
                    "tokens_in": 100,
                    "tokens_out": 50,
                    "latency_ms": 1234,
                    "model": "gpt-4o-mini",
                    "prompt_version": "v6",
                },
            }
        ],
    }


def _diag_structural_target_kind() -> dict[str, Any]:
    return {
        "rule_id": "rule-dogfooding-rules-L25",
        "status": "unsupported",
        "message": (
            "Rule annotated `target_kind: commit_message` does not apply to "
            "artifact of kind `pr_description`; skipping without invoking "
            "the LLM."
        ),
        "evidence": [
            {
                "kind": "target_kind_mismatch",
                "data": {
                    "rule_target_kind": "commit_message",
                    "artifact_kind": "pr_description",
                    "dispatch": "deterministic_precheck",
                    "llm_called": False,
                },
            }
        ],
    }


def _diag_structural_quote_fabrication() -> dict[str, Any]:
    return {
        "rule_id": "rule-dogfooding-rules-L24",
        "status": "unsupported",
        "message": (
            "LLM rubric verdict rejected: the model returned 2 of 2 "
            "supporting quotes that are not substrings of the artifact "
            "(quote fabrication)."
        ),
        "evidence": [
            {
                "kind": "llm_quote_fabrication",
                "data": {"fabricated_quotes": 2, "total_quotes": 2},
            }
        ],
    }


def test_actionable_only_no_details(tmp_path: Path) -> None:
    """A FAIL-only input renders the main table and no <details> block."""
    out = _run(tmp_path, {"diagnostics": [_diag_actionable_fail()]})
    assert "rule-dogfooding-rules-L23" in out
    assert "<details>" not in out
    assert "structurally skipped" not in out
    # The main table is present, not the placeholder body.
    assert "| rule | status | judgment | primary_reason |" in out
    assert "No author-actionable findings" not in out


def test_structural_skip_only_collapses_into_details(tmp_path: Path) -> None:
    """When only structural skips are present the main table is replaced by the
    placeholder body and the <details> block lists the two skipped rules."""
    out = _run(
        tmp_path,
        {
            "diagnostics": [
                _diag_structural_target_kind(),
                _diag_structural_quote_fabrication(),
            ]
        },
    )
    assert "No author-actionable findings" in out
    assert "<details>" in out
    assert "</details>" in out
    # Both kinds appear in the breakdown summary.
    assert "target_kind_mismatch=1" in out
    assert "llm_quote_fabrication=1" in out
    # Both rule IDs appear inside the collapsed table, not the main one.
    assert "rule-dogfooding-rules-L24" in out
    assert "rule-dogfooding-rules-L25" in out
    # The main "| rule | status | judgment | primary_reason |" header should NOT
    # appear because there are no actionable rows.
    assert "| rule | status | judgment | primary_reason |" not in out


def test_mixed_input_keeps_only_actionable_in_main_table(tmp_path: Path) -> None:
    """The PR #238 example: FAIL + two structural skips → table shows only FAIL,
    skips go under <details>."""
    out = _run(
        tmp_path,
        {
            "diagnostics": [
                _diag_actionable_fail(),
                _diag_structural_quote_fabrication(),
                _diag_structural_target_kind(),
            ]
        },
    )
    # Main table present with the FAIL row.
    assert "| rule | status | judgment | primary_reason |" in out
    main_table_idx = out.index("| rule | status | judgment | primary_reason |")
    details_idx = out.index("<details>")
    assert main_table_idx < details_idx
    # FAIL row appears before <details>.
    fail_idx = out.index("rule-dogfooding-rules-L23")
    assert fail_idx < details_idx
    # Skipped rule IDs appear after <details>.
    assert out.index("rule-dogfooding-rules-L24") > details_idx
    assert out.index("rule-dogfooding-rules-L25") > details_idx
    # Placeholder body should not appear since we have an actionable row.
    assert "No author-actionable findings" not in out


def test_non_structural_unsupported_is_not_filtered(tmp_path: Path) -> None:
    """An UNSUPPORTED diag with a non-structural evidence kind (e.g.
    ``provider_unconfigured``) stays in the main table — only the minimum
    set defined in #240 is filtered."""
    diag = {
        "rule_id": "rule-some-unavailable",
        "status": "unsupported",
        "message": "Provider not configured.",
        "evidence": [{"kind": "provider_unconfigured", "data": {}}],
    }
    out = _run(tmp_path, {"diagnostics": [diag]})
    assert "rule-some-unavailable" in out
    assert "<details>" not in out
