"""Tests for the semantic self-gating advisory rule pool (#72).

These tests pin four contracts:

1. ``docs/dogfooding-rules.md`` extracts to exactly three semantic rules.
2. The classifier routes all three to ``semantic_rubric`` / ``llm-rubric``
   (no deterministic-backend mis-routing).
3. With no LLM provider configured, ``llm_rubric.check`` returns
   ``Status.UNAVAILABLE`` with ``provider_unconfigured`` evidence — the
   fail-closed advisory contract documented in ``docs/llm-rubric.md``.
4. (#169) Each rule carries the expected ``target_kind`` annotation
   (PR-description rules vs commit-message rules) so the rubric backend
   can recognise the target-kind-mismatch case.

The DOTENV_PATH used by the llm_rubric backend is monkeypatched to a
non-existent tmpdir so the test does not depend on the developer's local
provider configuration.
"""

from __future__ import annotations

from pathlib import Path

from gate_keeper import classifier, parser
from gate_keeper.backends import llm_rubric
from gate_keeper.models import Backend, RuleKind, Status, TargetKind

_RULES_DOC = Path(__file__).resolve().parent.parent / "docs" / "dogfooding-rules.md"


def _ruleset():
    content = _RULES_DOC.read_text(encoding="utf-8")
    return classifier.classify(parser.parse(str(_RULES_DOC), content))


class TestParserExtraction:
    def test_dogfooding_rules_doc_exists(self):
        assert _RULES_DOC.is_file(), f"missing rules doc: {_RULES_DOC}"

    def test_extracts_exactly_three_rules(self):
        ruleset = _ruleset()
        assert len(ruleset.rules) == 3, [r.text for r in ruleset.rules]

    def test_rule_texts_match_authored_phrasings(self):
        # The phrasings adapt docs/semantic-rules.md §3.1 / §3.2 / §3.6 with
        # a `should` prefix so the bullets are picked up by the parser as
        # candidates. Filesystem-style verbs (`contain` / `include`) are
        # avoided so the classifier does not mis-route to FILESYSTEM.
        ruleset = _ruleset()
        texts = [r.text for r in ruleset.rules]
        assert "The PR description should name the user-visible change in the first sentence." in texts
        assert "The PR description should state how the change was tested." in texts
        assert (
            "The commit message should explain why the change was made, not only what was changed." in texts
        )


class TestClassifierRouting:
    def test_all_rules_route_to_semantic_rubric(self):
        ruleset = _ruleset()
        for rule in ruleset.rules:
            assert rule.kind is RuleKind.SEMANTIC_RUBRIC, f"{rule.text!r} mis-routed to {rule.kind.value}"

    def test_all_rules_route_to_llm_rubric_backend(self):
        ruleset = _ruleset()
        for rule in ruleset.rules:
            assert rule.backend_hint is Backend.LLM_RUBRIC, (
                f"{rule.text!r} mis-routed to {rule.backend_hint.value}"
            )


class TestTargetKindAnnotations:
    """#169 — every rule in dogfooding-rules.md must carry an explicit target_kind.

    The orchestrator runs the rule doc against multiple artifact kinds (PR
    bodies and commit messages); the annotation is what lets the rubric
    backend recognise the target-kind-mismatch case rather than parroting
    a PR-description rule's wording onto a commit message.
    """

    def test_pr_description_rules_carry_pr_description_kind(self):
        ruleset = _ruleset()
        pr_rules = [r for r in ruleset.rules if r.text.startswith("The PR description should")]
        assert pr_rules, "expected at least one PR-description rule"
        for rule in pr_rules:
            assert rule.target_kind is TargetKind.PR_DESCRIPTION, (
                f"{rule.text!r} carries {rule.target_kind.value} (expected pr_description)"
            )

    def test_commit_message_rule_carries_commit_message_kind(self):
        ruleset = _ruleset()
        commit_rules = [r for r in ruleset.rules if r.text.startswith("The commit message should")]
        assert commit_rules, "expected at least one commit-message rule"
        for rule in commit_rules:
            assert rule.target_kind is TargetKind.COMMIT_MESSAGE, (
                f"{rule.text!r} carries {rule.target_kind.value} (expected commit_message)"
            )

    def test_no_rule_has_target_kind_parse_warning(self):
        """Annotations in the doc must use known values (no typo recovery)."""
        ruleset = _ruleset()
        for rule in ruleset.rules:
            assert "target_kind_parse_warning" not in rule.params, (
                f"{rule.text!r} produced a target_kind parse warning: "
                f"{rule.params.get('target_kind_parse_warning')}"
            )


class TestLlmRubricFailClosed:
    def test_unconfigured_provider_yields_unavailable_for_each_rule(self, monkeypatch):
        # Force `_is_configured()` to see no provider state regardless of
        # what dotenv exists on the developer's machine.
        #
        # Why monkeypatch `_load_env_file` rather than `DOTENV_PATH`:
        # `_load_env_file(path: Path = DOTENV_PATH)` captures the module-
        # level `DOTENV_PATH` as a default-argument value at function
        # definition time, so re-binding `llm_rubric.DOTENV_PATH` does not
        # change the path the function actually reads. Replacing the
        # function itself with a stub that returns an empty mapping is
        # the reliable way to neutralise the dotenv contribution.
        monkeypatch.setattr(llm_rubric, "_load_env_file", lambda *a, **kw: {})
        ruleset = _ruleset()
        assert ruleset.rules, "ruleset must be non-empty for this assertion to be meaningful"
        for rule in ruleset.rules:
            diag = llm_rubric.check(rule, "an inline target string")
            assert diag.status is Status.UNAVAILABLE, (
                f"{rule.text!r} produced {diag.status.value} instead of UNAVAILABLE"
            )
            kinds = [e.kind for e in diag.evidence]
            assert "provider_unconfigured" in kinds, (
                f"{rule.text!r} missing provider_unconfigured evidence; got {kinds}"
            )
