"""Tests for the semantic self-gating advisory rule pool (#72, #253, #242, #246).

These tests pin four contracts:

1. ``docs/dogfooding-rules.md`` extracts to exactly three semantic rules.
   The v2 pool (#253) keeps the two PR-description rules and drops the
   former L25 commit-message rule per #242 Option C — the dogfood
   workflow only dispatches ``--artifact-kind pr_description`` so a
   ``commit_message`` rule would short-circuit to ``UNSUPPORTED`` on
   every run. #246 adds a third PR-description rule that flags PRs
   landing hardcoded user-specific / container-layout paths in product
   code (anchor case #245).
2. The classifier routes all three to ``semantic_rubric`` / ``llm-rubric``
   (no deterministic-backend mis-routing).
3. With no LLM provider configured, ``llm_rubric.check`` returns
   ``Status.UNAVAILABLE`` with ``provider_unconfigured`` evidence — the
   fail-closed advisory contract documented in ``docs/llm-rubric.md``.
4. (#169) Each rule carries the expected ``target_kind`` annotation
   (``pr_description``) so the rubric backend can recognise the
   target-kind-mismatch case.

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
        # The first two phrasings adapt docs/semantic-rules.md §3.1 and
        # §3.2. The v2 wording (#253) describes the user-visible change
        # and the verification method anywhere in the body, matching the
        # real `Closes #N` + `## Summary` + `## Validation` template both
        # this repo and uda-lab/hermes-engineering follow. Filesystem-
        # style verbs (`contain` / `include`) are avoided so the
        # classifier does not mis-route to FILESYSTEM. L25 (commit-
        # message rule) was dropped per #242 Option C.
        #
        # The third rule (#246, anchor case #245) extends the pool to
        # cover hardcoded user/container-layout filesystem paths in
        # product code. The wording preserves the full boundary
        # definition — acceptable spellings and the test-fixture
        # exemption — so the rubric prompt has unambiguous context.
        ruleset = _ruleset()
        texts = [r.text for r in ruleset.rules]
        # Identify each rule by a substring rather than exact match — v2
        # wording carries embedded examples and the long form changes
        # whenever rationale grows.
        _l23_prefix = "The PR description body should describe the user-visible change introduced by this PR."
        _l24_prefix = "The PR description should describe how the change was verified"
        _env_safety_prefix = "Product code must not hardcode absolute filesystem locations"
        assert any(t.startswith(_l23_prefix) for t in texts), texts
        assert any(t.startswith(_l24_prefix) for t in texts), texts
        assert any(t.startswith(_env_safety_prefix) for t in texts), texts
        # No commit-message rule must remain in the pool.
        assert not any(t.lower().startswith("the commit message") for t in texts), texts


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

    After the v2 rewrite (#253) and #242 Option C, the pool is exclusively
    PR-description rules; the former commit-message rule (L25) was dropped
    because the dogfood workflow only dispatches ``--artifact-kind
    pr_description``. The target_kind annotation still exists so the
    rubric backend can recognise a target-kind-mismatch case if a
    `commit_message` rule is reintroduced in the future.
    """

    def test_all_rules_carry_pr_description_kind(self):
        ruleset = _ruleset()
        assert ruleset.rules, "expected at least one rule"
        for rule in ruleset.rules:
            assert rule.target_kind is TargetKind.PR_DESCRIPTION, (
                f"{rule.text!r} carries {rule.target_kind.value} (expected pr_description)"
            )

    def test_no_commit_message_rule_in_pool(self):
        """#242 Option C — L25 (commit-message rule) is dropped until the
        dogfood workflow grows a ``commit_message`` dispatch."""
        ruleset = _ruleset()
        commit_rules = [r for r in ruleset.rules if r.target_kind is TargetKind.COMMIT_MESSAGE]
        assert not commit_rules, (
            f"unexpected commit-message rule(s) in dogfood pool: {[r.text for r in commit_rules]}"
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
