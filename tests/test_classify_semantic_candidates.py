"""Hermetic tests for ``scripts/classify_semantic_candidates.py`` (#235).

Tests cover:
- Filter logic: only semantic_rubric-fallback rules are evaluated.
- Report shape: JSON output matches the documented schema.
- Routing per stubbed response: canned _call_openai responses map correctly.
- Dry-run mode: no API call, expected dry_run flag in report.

The ``disable_llm_dotenv`` autouse fixture (conftest.py) ensures no host
credential file bleeds into the test run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from gate_keeper.backends import llm_rubric as _llm
from gate_keeper.models import Backend, Confidence, Rule, RuleKind, RuleSet, SourceLocation

# ---------------------------------------------------------------------------
# Module loader (script lives outside the importable package tree)
# ---------------------------------------------------------------------------


def _load_script_module():
    """Import ``scripts/classify_semantic_candidates.py`` by file path."""
    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "classify_semantic_candidates.py"
    spec = importlib.util.spec_from_file_location("classify_semantic_candidates", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["classify_semantic_candidates"] = module
    spec.loader.exec_module(module)
    return module


script = _load_script_module()

# ---------------------------------------------------------------------------
# Helpers to construct minimal Rule objects
# ---------------------------------------------------------------------------


def _make_rule(
    text: str,
    rule_id: str = "test-rule-001",
    kind: RuleKind = RuleKind.SEMANTIC_RUBRIC,
    backend: Backend = Backend.LLM_RUBRIC,
) -> Rule:
    """Build a minimal Rule for testing."""
    from gate_keeper.models import Severity

    source = SourceLocation(path="test-rules.md", line=1, heading="Test section")
    return Rule(
        id=rule_id,
        title=text[:80],
        text=text,
        kind=kind,
        backend_hint=backend,
        confidence=Confidence.LOW,
        severity=Severity.WARNING,
        source=source,
        params={},
    )


def _make_ruleset(rules: list[Rule]) -> RuleSet:
    return RuleSet(rules=rules)


# ---------------------------------------------------------------------------
# Test: _is_semantic_fallback filter logic
# ---------------------------------------------------------------------------


class TestIsSemanticFallback:
    def test_llm_rubric_semantic_rubric_is_fallback(self):
        rule = _make_rule("The PR description should be clear.")
        assert script._is_semantic_fallback(rule) is True

    def test_filesystem_backend_is_not_fallback(self):
        rule = _make_rule(
            "README.md must exist.",
            kind=RuleKind.FILE_EXISTS,
            backend=Backend.FILESYSTEM,
        )
        assert script._is_semantic_fallback(rule) is False

    def test_github_backend_is_not_fallback(self):
        rule = _make_rule(
            "PR must not be a draft.",
            kind=RuleKind.GITHUB_NOT_DRAFT,
            backend=Backend.GITHUB,
        )
        assert script._is_semantic_fallback(rule) is False

    def test_external_check_kind_is_not_fallback(self):
        rule = _make_rule(
            "Must not use passive voice.",
            kind=RuleKind.EXTERNAL_CHECK,
            backend=Backend.LLM_RUBRIC,
        )
        assert script._is_semantic_fallback(rule) is False

    def test_external_backend_is_not_fallback(self):
        rule = _make_rule(
            "Must not use passive voice.",
            kind=RuleKind.EXTERNAL_CHECK,
            backend=Backend.EXTERNAL,
        )
        assert script._is_semantic_fallback(rule) is False


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _stub_response_filesystem() -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "can_be_deterministic": True,
            "proposed_backend": "filesystem",
            "proposed_pattern": r"README\.md",
            "rationale": "This is a file-existence check.",
        }
    )
    return body, {"latency_ms": 10, "tokens_in": 40, "tokens_out": 20}


def _stub_response_github() -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "can_be_deterministic": True,
            "proposed_backend": "github",
            "proposed_pattern": "gh pr view --json isDraft",
            "rationale": "PR draft state is a deterministic GitHub API field.",
        }
    )
    return body, {"latency_ms": 12, "tokens_in": 45, "tokens_out": 22}


def _stub_response_none() -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "can_be_deterministic": False,
            "proposed_backend": None,
            "proposed_pattern": None,
            "rationale": "Requires judgment-level reading of the description.",
        }
    )
    return body, {"latency_ms": 8, "tokens_in": 38, "tokens_out": 18}


def _stub_response_textlint() -> tuple[str, dict[str, int]]:
    body = json.dumps(
        {
            "can_be_deterministic": True,
            "proposed_backend": "external+textlint",
            "proposed_pattern": "textlint-rule-no-passive-voice",
            "rationale": "Passive voice is a mechanical prose style pattern.",
        }
    )
    return body, {"latency_ms": 9, "tokens_in": 42, "tokens_out": 21}


# ---------------------------------------------------------------------------
# Test: report shape from stubbed responses
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_report_shape_from_four_results(self):
        """_build_report aggregates four rule results into the documented schema."""
        results = [
            {
                "rule_id": "r1",
                "rule_text": "File must exist.",
                "can_be_deterministic": True,
                "proposed_backend": "filesystem",
                "rationale": "file-existence predicate",
                "suggested_pattern": r"file\.txt",
                "_telemetry": {"latency_ms": 10, "tokens_in": 40, "tokens_out": 20},
            },
            {
                "rule_id": "r2",
                "rule_text": "PR must not be draft.",
                "can_be_deterministic": True,
                "proposed_backend": "github",
                "rationale": "GitHub API field",
                "suggested_pattern": "isDraft field",
                "_telemetry": {"latency_ms": 12, "tokens_in": 45, "tokens_out": 22},
            },
            {
                "rule_id": "r3",
                "rule_text": "Must not use passive voice.",
                "can_be_deterministic": True,
                "proposed_backend": "external+textlint",
                "rationale": "prose style pattern",
                "suggested_pattern": "no-passive-voice",
                "_telemetry": {"latency_ms": 9, "tokens_in": 42, "tokens_out": 21},
            },
            {
                "rule_id": "r4",
                "rule_text": "PR description should be clear.",
                "can_be_deterministic": False,
                "proposed_backend": "none",
                "rationale": "judgment-level check",
                "_telemetry": {"latency_ms": 8, "tokens_in": 38, "tokens_out": 18},
            },
        ]

        report = script._build_report(results, "openai:gpt-5")

        assert report["model"] == "openai:gpt-5"
        assert report["rules_evaluated"] == 4
        assert "candidates_by_backend" in report
        assert "total_cost_estimate_usd" in report

        cbb = report["candidates_by_backend"]
        assert set(cbb.keys()) == {"filesystem", "github", "external+textlint", "none"}
        assert len(cbb["filesystem"]) == 1
        assert cbb["filesystem"][0]["rule_id"] == "r1"
        assert len(cbb["github"]) == 1
        assert cbb["github"][0]["rule_id"] == "r2"
        assert len(cbb["external+textlint"]) == 1
        assert cbb["external+textlint"][0]["rule_id"] == "r3"
        assert len(cbb["none"]) == 1
        assert cbb["none"][0]["rule_id"] == "r4"

    def test_suggested_pattern_included_for_deterministic_rules(self):
        results = [
            {
                "rule_id": "r1",
                "rule_text": "README.md must exist.",
                "can_be_deterministic": True,
                "proposed_backend": "filesystem",
                "rationale": "existence check",
                "suggested_pattern": r"README\.md",
                "_telemetry": {"latency_ms": 10, "tokens_in": 40, "tokens_out": 20},
            }
        ]
        report = script._build_report(results, "openai:gpt-5")
        assert report["candidates_by_backend"]["filesystem"][0]["suggested_pattern"] == r"README\.md"

    def test_none_backend_has_no_suggested_pattern(self):
        results = [
            {
                "rule_id": "r1",
                "rule_text": "PR should read clearly.",
                "can_be_deterministic": False,
                "proposed_backend": "none",
                "rationale": "judgment-level",
                "_telemetry": {"latency_ms": 8, "tokens_in": 30, "tokens_out": 15},
            }
        ]
        report = script._build_report(results, "openai:gpt-5")
        entry = report["candidates_by_backend"]["none"][0]
        assert "suggested_pattern" not in entry


# ---------------------------------------------------------------------------
# Test: run_audit with monkeypatched _call_openai
# ---------------------------------------------------------------------------


_BASE_ENV = {
    "GATE_KEEPER_LLM_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-test-stub-not-real",
}


class TestRunAudit:
    def _write_semantic_rules(self, tmp_path: Path) -> Path:
        """Write a rules file with one semantic fallback rule."""
        rules_md = tmp_path / "rules.md"
        rules_md.write_text(
            "# Test rules\n\n"
            "- The PR description should name the user-visible change in the first sentence.\n",
            encoding="utf-8",
        )
        return rules_md

    def _write_mixed_rules(self, tmp_path: Path) -> Path:
        """Write a rules file with semantic + deterministic rules."""
        rules_md = tmp_path / "mixed.md"
        rules_md.write_text(
            "# Test rules\n\n"
            "## GitHub gates\n\n"
            "- PR must not be a draft.\n"
            "- CI checks must pass.\n\n"
            "## Filesystem gates\n\n"
            "- README.md must exist.\n\n"
            "## Semantic rules\n\n"
            "- The commit message should explain why the change was made.\n",
            encoding="utf-8",
        )
        return rules_md

    def test_filters_only_semantic_fallback_rules(self, monkeypatch, tmp_path):
        """Filter logic: only semantic_rubric rules are evaluated; deterministic rules are excluded."""
        call_count = 0

        def _stub_call(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
            nonlocal call_count
            call_count += 1
            return _stub_response_none()

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_call)

        rules_path = self._write_mixed_rules(tmp_path)
        report = script.run_audit(rules_path, model="openai:gpt-5")

        # The mixed file has 3 deterministic + 1 semantic rule.
        # Only the semantic rule should be audited.
        assert call_count == 1
        assert report["rules_evaluated"] == 1

    def test_report_shape_and_routing(self, monkeypatch, tmp_path):
        """run_audit returns report with correct shape when stub returns 'none'."""
        stub_calls: list[str] = []

        def _stub_call(api_key, system, user, model) -> tuple[str, dict[str, int]]:
            stub_calls.append(user)
            return _stub_response_none()

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_call)

        rules_path = self._write_semantic_rules(tmp_path)
        report = script.run_audit(rules_path, model="openai:gpt-5")

        assert report["model"] == "openai:gpt-5"
        assert report["rules_evaluated"] == 1
        assert set(report["candidates_by_backend"].keys()) == {
            "filesystem",
            "github",
            "external+textlint",
            "none",
        }
        assert len(report["candidates_by_backend"]["none"]) == 1
        assert len(stub_calls) == 1

    def test_filesystem_stub_routes_to_filesystem(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", lambda *a, **k: _stub_response_filesystem())

        rules_path = self._write_semantic_rules(tmp_path)
        report = script.run_audit(rules_path, model="openai:gpt-5")

        assert len(report["candidates_by_backend"]["filesystem"]) == 1
        assert len(report["candidates_by_backend"]["none"]) == 0

    def test_raises_without_api_key(self, monkeypatch, tmp_path):
        """run_audit raises RuntimeError when OPENAI_API_KEY is absent."""
        monkeypatch.setattr(
            _llm,
            "_load_env_file",
            lambda *a, **k: {"GATE_KEEPER_LLM_PROVIDER": "openai"},
        )

        rules_path = self._write_semantic_rules(tmp_path)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            script.run_audit(rules_path, model="openai:gpt-5")

    def test_dry_run_no_api_call(self, monkeypatch, tmp_path):
        """In dry-run mode, _call_openai is never called."""
        call_count = 0

        def _stub_call(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            return _stub_response_none()

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_call)

        rules_path = self._write_semantic_rules(tmp_path)
        report = script.run_audit(rules_path, model="openai:gpt-5", dry_run=True)

        assert call_count == 0
        assert report.get("dry_run") is True
        assert report["rules_evaluated"] == 0

    def test_empty_file_no_calls(self, monkeypatch, tmp_path):
        """An empty rules file produces zero evaluated rules."""
        call_count = 0

        def _stub_call(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            return _stub_response_none()

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _stub_call)

        rules_path = tmp_path / "empty.md"
        rules_path.write_text("# No rules here\n\n", encoding="utf-8")
        report = script.run_audit(rules_path, model="openai:gpt-5")

        assert call_count == 0
        assert report["rules_evaluated"] == 0


# ---------------------------------------------------------------------------
# Test: main() CLI entry point
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_main_writes_report_to_out(self, monkeypatch, tmp_path):
        rules_md = tmp_path / "rules.md"
        rules_md.write_text(
            "# Rules\n\n- The commit message should explain why the change was made.\n",
            encoding="utf-8",
        )
        out_path = tmp_path / "report.json"

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", lambda *a, **k: _stub_response_none())

        rc = script.main(
            [
                "--rules",
                str(rules_md),
                "--model",
                "openai:gpt-5",
                "--out",
                str(out_path),
            ]
        )
        assert rc == 0
        assert out_path.is_file()
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["rules_evaluated"] == 1
        assert "candidates_by_backend" in report

    def test_main_dry_run_exits_zero(self, monkeypatch, tmp_path):
        rules_md = tmp_path / "rules.md"
        rules_md.write_text(
            "# Rules\n\n- The PR description should be clear.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))

        rc = script.main(["--rules", str(rules_md), "--dry-run"])
        assert rc == 0

    def test_main_missing_rules_file_exits_one(self, monkeypatch, tmp_path):
        rc = script.main(["--rules", str(tmp_path / "nonexistent.md")])
        assert rc == 1


# ---------------------------------------------------------------------------
# Regression tests for copilot review feedback (#236)
# ---------------------------------------------------------------------------


class TestReviewRegressions:
    """Cover the three copilot review threads on PR #236."""

    def _write_semantic_rules(self, tmp_path: Path) -> Path:
        rules_md = tmp_path / "rules.md"
        rules_md.write_text(
            "# Rules\n\n- The PR description should be clear.\n",
            encoding="utf-8",
        )
        return rules_md

    def test_inconsistent_payload_normalised_to_none(self, monkeypatch, tmp_path):
        """can_be_deterministic=false + proposed_backend='github' is normalised to 'none'.

        Copilot thread 3223291517: trust-the-model bug — without this guard,
        an inconsistent payload would bucket the rule under a deterministic
        backend even though the model itself said it was not deterministic.
        """

        def _bad_stub(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
            body = json.dumps(
                {
                    "can_be_deterministic": False,
                    "proposed_backend": "github",
                    "proposed_pattern": "gh pr view",
                    "rationale": "model self-contradicts",
                }
            )
            return body, {"latency_ms": 10, "tokens_in": 30, "tokens_out": 15}

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _bad_stub)

        rules_path = self._write_semantic_rules(tmp_path)
        report = script.run_audit(rules_path, model="openai:gpt-5")

        # The inconsistent payload must be bucketed under "none", not "github".
        assert len(report["candidates_by_backend"]["github"]) == 0
        assert len(report["candidates_by_backend"]["none"]) == 1
        # The suggested_pattern must be dropped (deterministic-only field).
        none_entry = report["candidates_by_backend"]["none"][0]
        assert "suggested_pattern" not in none_entry

    def test_inconsistent_deterministic_true_but_none_backend(self, monkeypatch, tmp_path):
        """can_be_deterministic=true + proposed_backend=null is flagged in rationale.

        Copilot thread 3223291517: the other direction of the inconsistency
        — the model claims the rule is deterministic but does not name a
        deterministic backend. We fail-closed to 'none' and prefix the
        rationale with an [inconsistent payload] marker.
        """

        def _bad_stub(*_args, **_kwargs) -> tuple[str, dict[str, int]]:
            body = json.dumps(
                {
                    "can_be_deterministic": True,
                    "proposed_backend": None,
                    "proposed_pattern": None,
                    "rationale": "deterministic but unsure how",
                }
            )
            return body, {"latency_ms": 10, "tokens_in": 30, "tokens_out": 15}

        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))
        monkeypatch.setattr(_llm, "_call_openai", _bad_stub)

        rules_path = self._write_semantic_rules(tmp_path)
        report = script.run_audit(rules_path, model="openai:gpt-5")

        assert len(report["candidates_by_backend"]["none"]) == 1
        rationale = report["candidates_by_backend"]["none"][0]["rationale"]
        assert "inconsistent payload" in rationale

    def test_non_openai_provider_rejected(self, monkeypatch, tmp_path):
        """--model anthropic:* raises a clear ValueError; no OpenAI call attempted.

        Copilot thread 3223291536: the slice is openai-only. Honouring an
        ``anthropic:`` prefix would silently invoke ``_call_openai`` with a
        non-openai model name. Validate at the boundary instead.
        """
        rules_path = self._write_semantic_rules(tmp_path)

        # Even without an API key, the provider check must fire first.
        monkeypatch.setattr(_llm, "_load_env_file", lambda *a, **k: dict(_BASE_ENV))

        with pytest.raises(ValueError, match="openai-only"):
            script.run_audit(rules_path, model="anthropic:claude-haiku-4-5")

    def test_dotenv_path_in_error_uses_module_constant(self, monkeypatch, tmp_path):
        """RuntimeError for missing OPENAI_API_KEY references _llm.DOTENV_PATH.

        Copilot thread 3223291543: hard-coded path string risks drift.
        Use the module-level constant so the message stays in sync.
        """
        monkeypatch.setattr(
            _llm,
            "_load_env_file",
            lambda *a, **k: {"GATE_KEEPER_LLM_PROVIDER": "openai"},
        )

        rules_path = self._write_semantic_rules(tmp_path)
        with pytest.raises(RuntimeError, match=str(_llm.DOTENV_PATH)):
            script.run_audit(rules_path, model="openai:gpt-5")
