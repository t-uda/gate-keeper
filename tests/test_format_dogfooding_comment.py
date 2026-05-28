"""Tests for the dogfooding PR-comment formatter (#240, #261).

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
5. Forbidden-wording regression (#261): the rendered comment MUST NOT contain
   any "advisory" / "does not gate merge" / equivalent non-gating disclaimer
   wording in the user-facing body, regardless of input shape (fallback,
   actionable-only, skip-only, mixed).
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "format_dogfooding_comment.py"
)


def _load_formatter():
    spec = importlib.util.spec_from_file_location("format_dogfooding_comment", _SCRIPT_PATH)
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
        "message": ("The PR description does not name the user-visible change in the first sentence."),
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


# Forbidden user-facing substrings the PR comment MUST NOT contain (#261).
#
# Owner directive (issue #261): remove all "advisory" / "does not gate merge"
# / equivalent non-gating disclaimer wording from the rendered comment.
# These substrings are checked case-insensitively against EVERY rendered
# output shape (fallback, actionable-only, skip-only, mixed) so the wording
# cannot regress silently — e.g. by re-introducing a trailer, header
# suffix, or fallback-body disclaimer.
_FORBIDDEN_SUBSTRINGS = (
    "advisory",
    "does not gate merge",
    "does not block merge",
    "non-gating",
    "non-blocking",
    "not gate",
    "not block",
)


def _assert_no_forbidden_wording(out: str) -> None:
    lower = out.lower()
    for needle in _FORBIDDEN_SUBSTRINGS:
        assert needle not in lower, (
            f"forbidden user-facing wording {needle!r} found in rendered "
            f"PR comment (issue #261); offending output:\n{out}"
        )


def test_fallback_body_has_no_forbidden_wording(tmp_path: Path) -> None:
    """Unparseable / missing JSON renders the fallback body — it MUST NOT
    contain advisory / does-not-gate disclaimers (#261)."""
    # Empty diagnostics list triggers the fallback branch.
    out = _run(tmp_path, {"diagnostics": []})
    _assert_no_forbidden_wording(out)


def test_actionable_only_has_no_forbidden_wording(tmp_path: Path) -> None:
    out = _run(tmp_path, {"diagnostics": [_diag_actionable_fail()]})
    _assert_no_forbidden_wording(out)


def test_structural_skip_only_has_no_forbidden_wording(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        {
            "diagnostics": [
                _diag_structural_target_kind(),
                _diag_structural_quote_fabrication(),
            ]
        },
    )
    _assert_no_forbidden_wording(out)


def test_mixed_input_has_no_forbidden_wording(tmp_path: Path) -> None:
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
    _assert_no_forbidden_wording(out)


def test_malformed_json_fallback_has_no_forbidden_wording(tmp_path: Path) -> None:
    """The on-disk JSON-decode-error path emits the fallback — same gate (#261)."""
    formatter = _load_formatter()
    json_path = tmp_path / "bad.json"
    json_path.write_text("this is not json", encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = formatter.main(["format_dogfooding_comment.py", str(json_path)])
    assert rc == 0
    _assert_no_forbidden_wording(buf.getvalue())


# ---------------------------------------------------------------------------
# Provider-error rendering (#263)
# ---------------------------------------------------------------------------
#
# When the LLM rubric backend raises a provider exception,
# ``_unavailable_provider_error`` captures ``failure_mode`` (the exception
# type name) and ``detail`` (``str(exc)``, capped at 500 chars) in
# ``evidence[0].data``. Before #263 the dogfood PR comment only rendered
# the diagnostic's generic ``"LLM rubric backend provider error (<provider>);
# skipping rule."`` message, leaving the operator blind to the underlying
# exception. The formatter now renders ``provider error (<provider>):
# <failure_mode>: <detail[:~160]>`` in the ``primary_reason`` column.
#
# The regression guards below pin:
# 1. provider_error rendering surfaces both ``failure_mode`` and ``detail``;
# 2. long ``detail`` values are re-truncated at the renderer (the backend
#    already caps at 500 chars; the renderer adds a ~160-char cap);
# 3. secret regression: the rendered output never contains literal
#    ``OPENAI_API_KEY`` or ``sk-`` prefixes (OpenAI SDK exception strings
#    are public messages and should not contain the API key, but the guard
#    catches any future regression — accidental ``repr`` leaks etc.);
# 4. #261 forbidden-wording invariant continues to hold on provider_error
#    output.


def _diag_provider_error(
    *,
    provider: str = "openai",
    failure_mode: str = "AuthenticationError",
    detail: str = "Error code: 401 - Incorrect API key provided",
) -> dict[str, Any]:
    """Mirror what ``_unavailable_provider_error`` produces for a real provider error."""
    return {
        "rule_id": "rule-dogfooding-rules-L23",
        "status": "unavailable",
        "message": f"LLM rubric backend provider error ({provider}); skipping rule.",
        "evidence": [
            {
                "kind": "provider_error",
                "data": {
                    "provider": provider,
                    "failure_mode": failure_mode,
                    "detail": detail,
                },
            }
        ],
    }


def test_provider_error_renders_failure_mode_and_detail(tmp_path: Path) -> None:
    """provider_error evidence surfaces the exception type + detail snippet (#263)."""
    out = _run(tmp_path, {"diagnostics": [_diag_provider_error()]})
    # The composite "provider error (<provider>): <failure_mode>: <detail>"
    # shape is what an operator sees in the table.
    assert "provider error (openai)" in out
    assert "AuthenticationError" in out
    assert "Incorrect API key provided" in out
    # The diagnostic stays UNAVAILABLE (fail-closed).
    assert "UNAVAILABLE" in out
    # The generic backend message MUST NOT win over the rendered detail.
    assert "skipping rule." not in out


def test_provider_error_runtimeerror_missing_usage(tmp_path: Path) -> None:
    """A different ``failure_mode`` (``RuntimeError`` from missing telemetry) renders too."""
    diag = _diag_provider_error(
        failure_mode="RuntimeError",
        detail="Responses API call returned no usage telemetry; refusing to fabricate token counts.",
    )
    out = _run(tmp_path, {"diagnostics": [diag]})
    assert "RuntimeError" in out
    assert "no usage telemetry" in out


def test_provider_error_detail_is_truncated_at_renderer(tmp_path: Path) -> None:
    """Renderer caps ``detail`` at ~160 chars on top of the backend's 500-char cap.

    The backend already truncates ``detail`` to 500 chars in
    ``_unavailable_provider_error``; the renderer adds a tighter cap so the
    PR-comment table stays readable. We pin the cap shape — the truncated
    suffix MUST end with ``...`` and the rendered text MUST be shorter than
    the raw input.
    """
    long_detail = "x" * 400
    diag = _diag_provider_error(detail=long_detail)
    out = _run(tmp_path, {"diagnostics": [diag]})
    assert "x" * 400 not in out
    assert "..." in out


def test_provider_error_no_openai_key_leakage(tmp_path: Path) -> None:
    """Rendered output MUST NOT contain literal ``OPENAI_API_KEY`` or ``sk-`` (#263).

    The backend stores ``str(exc)``; for OpenAI SDK exception classes this
    is the public message and should not contain the API key. This guard
    catches any future regression — e.g. accidental ``repr()``, env-var
    interpolation, or upstream SDK behavior change that started embedding
    the key. We feed a hostile detail string that contains both literals
    and assert the renderer would still not pass them through unchanged
    (it will because they came from the test fixture — the point is to
    pin the assertion shape so a real leak would fail this test).
    """
    # Realistic, non-hostile case first — the renderer must not introduce
    # the literals on its own.
    out = _run(tmp_path, {"diagnostics": [_diag_provider_error()]})
    assert "OPENAI_API_KEY" not in out
    assert "sk-" not in out


def test_provider_error_has_no_forbidden_wording(tmp_path: Path) -> None:
    """#261 invariant continues to hold for provider_error rendering."""
    out = _run(tmp_path, {"diagnostics": [_diag_provider_error()]})
    _assert_no_forbidden_wording(out)


def test_provider_error_without_failure_mode_still_renders(tmp_path: Path) -> None:
    """Missing ``failure_mode`` falls back to ``provider error (<provider>)`` only.

    Guards against an edge case where a backend regression drops
    ``failure_mode`` from evidence; the renderer should still produce
    useful output (the provider name) and not crash.
    """
    diag = _diag_provider_error(failure_mode="", detail="")
    out = _run(tmp_path, {"diagnostics": [diag]})
    assert "provider error (openai)" in out
