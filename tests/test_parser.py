"""Tests for the Markdown rule document parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from gate_keeper.models import Backend, Confidence, RuleKind, Severity
from gate_keeper.parser import parse, parse_file

FIXTURES = Path(__file__).parent / "fixtures" / "parser"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse(content: str) -> list:
    return parse("test.md", content).rules


def _texts(content: str) -> list[str]:
    return [r.text for r in _parse(content)]


# ---------------------------------------------------------------------------
# ATX heading context
# ---------------------------------------------------------------------------

class TestHeadingContext:
    def test_rule_under_heading_carries_heading(self):
        md = "## Quality\n\n- Code must be reviewed.\n"
        rules = _parse(md)
        assert len(rules) == 1
        assert rules[0].source.heading == "Quality"

    def test_rule_under_nested_headings_uses_chain(self):
        md = "# Top\n\n## Sub\n\n- Tests must pass.\n"
        rules = _parse(md)
        assert rules[0].source.heading == "Top > Sub"

    def test_rule_before_any_heading_has_no_heading(self):
        md = "- Code must be reviewed.\n"
        rules = _parse(md)
        assert rules[0].source.heading is None

    def test_heading_resets_at_same_level(self):
        md = "## Section A\n\n## Section B\n\n- Must follow B.\n"
        rules = _parse(md)
        assert rules[0].source.heading == "Section B"

    def test_deeper_heading_nests_under_parent(self):
        md = "# Root\n\n## Child\n\n### Grandchild\n\n- Must comply.\n"
        rules = _parse(md)
        assert rules[0].source.heading == "Root > Child > Grandchild"


# ---------------------------------------------------------------------------
# Bullet list rules
# ---------------------------------------------------------------------------

class TestBulletRules:
    def test_normative_bullet_extracted(self):
        texts = _texts("- Code must be reviewed.\n")
        assert texts == ["Code must be reviewed."]

    def test_non_normative_bullet_ignored(self):
        assert _texts("- This is a note.\n") == []

    def test_must_not_bullet_extracted(self):
        texts = _texts("- You must not commit secrets.\n")
        assert len(texts) == 1
        assert "must not" in texts[0]

    def test_should_not_bullet_extracted(self):
        texts = _texts("- You should not skip tests.\n")
        assert len(texts) == 1

    def test_never_keyword_extracted(self):
        texts = _texts("- Never push directly to main.\n")
        assert len(texts) == 1

    def test_required_keyword_extracted(self):
        texts = _texts("- Tests are required before merge.\n")
        assert len(texts) == 1

    def test_forbidden_keyword_extracted(self):
        texts = _texts("- Force-push is forbidden on main.\n")
        assert len(texts) == 1

    def test_fail_keyword_extracted(self):
        texts = _texts("- Builds that fail must be fixed.\n")
        assert len(texts) == 1

    def test_block_keyword_extracted(self):
        texts = _texts("- Unresolved threads block merging.\n")
        assert len(texts) == 1

    def test_ensure_keyword_extracted(self):
        texts = _texts("- Ensure tests pass before review.\n")
        assert len(texts) == 1

    def test_require_keyword_extracted(self):
        texts = _texts("- PR rules require an approval.\n")
        assert len(texts) == 1

    def test_case_insensitive_match(self):
        texts = _texts("- MUST be reviewed.\n")
        assert len(texts) == 1

    def test_star_bullet(self):
        texts = _texts("* Tests must pass.\n")
        assert len(texts) == 1

    def test_plus_bullet(self):
        texts = _texts("+ Changes must be reviewed.\n")
        assert len(texts) == 1

    def test_multiple_normative_bullets(self):
        md = "- Must review.\n- Should test.\n- Just a note.\n"
        assert len(_texts(md)) == 2

    def test_partial_word_not_matched(self):
        # "required" inside "prerequisites" should not match
        assert _texts("- Check all prerequisites.\n") == []


# ---------------------------------------------------------------------------
# Ordered list rules
# ---------------------------------------------------------------------------

class TestOrderedListRules:
    def test_normative_ordered_item_extracted(self):
        texts = _texts("1. Every PR must have a description.\n")
        assert len(texts) == 1

    def test_non_normative_ordered_item_ignored(self):
        assert _texts("1. First step in the process.\n") == []

    def test_ordered_item_with_period_delimiter(self):
        texts = _texts("2. Tests should pass before review.\n")
        assert len(texts) == 1

    def test_ordered_item_with_paren_delimiter(self):
        texts = _texts("3) PRs must be reviewed.\n")
        assert len(texts) == 1

    def test_ordered_list_heading_context(self):
        md = "## Checks\n\n1. CI must succeed.\n"
        rules = _parse(md)
        assert rules[0].source.heading == "Checks"


# ---------------------------------------------------------------------------
# Task checkboxes
# ---------------------------------------------------------------------------

class TestTaskCheckboxes:
    def test_unchecked_task_extracted(self):
        texts = _texts("- [ ] Tests pass\n")
        assert texts == ["Tests pass"]

    def test_checked_task_extracted(self):
        texts = _texts("- [x] Code reviewed\n")
        assert texts == ["Code reviewed"]

    def test_uppercase_x_extracted(self):
        texts = _texts("- [X] Docs updated\n")
        assert texts == ["Docs updated"]

    def test_task_without_normative_keyword_still_extracted(self):
        # Task checkboxes are always candidates — they encode intent by structure.
        texts = _texts("- [ ] Deploy the release\n")
        assert texts == ["Deploy the release"]

    def test_task_heading_context(self):
        md = "## Merge Gate\n\n- [ ] CI passes\n"
        rules = _parse(md)
        assert rules[0].source.heading == "Merge Gate"

    def test_checked_and_unchecked_both_extracted(self):
        md = "- [ ] Step one\n- [x] Step two\n"
        assert len(_texts(md)) == 2


# ---------------------------------------------------------------------------
# Normative paragraphs
# ---------------------------------------------------------------------------

class TestNormativeParagraphs:
    def test_normative_paragraph_extracted(self):
        texts = _texts("Users must authenticate before accessing resources.\n")
        assert len(texts) == 1

    def test_non_normative_paragraph_ignored(self):
        assert _texts("This project started in 2023.\n") == []

    def test_multiline_paragraph_joined(self):
        md = "Users must authenticate\nbefore accessing resources.\n"
        rules = _parse(md)
        assert len(rules) == 1
        assert "Users must authenticate before accessing resources." == rules[0].text

    def test_paragraph_heading_context(self):
        md = "## Auth\n\nUsers must authenticate first.\n"
        rules = _parse(md)
        assert rules[0].source.heading == "Auth"

    def test_paragraph_split_by_blank_line(self):
        md = "First must pass.\n\nSecond should succeed.\n"
        assert len(_parse(md)) == 2

    def test_non_normative_paragraph_between_rules_ignored(self):
        md = "Must do A.\n\nThis is just a note.\n\nMust do B.\n"
        texts = _texts(md)
        assert len(texts) == 2
        assert "Must do A." in texts
        assert "Must do B." in texts


# ---------------------------------------------------------------------------
# Code fence exclusion
# ---------------------------------------------------------------------------

class TestCodeFenceExclusion:
    def test_backtick_fence_content_ignored(self):
        md = "```\nmust not parse this\n```\n"
        assert _texts(md) == []

    def test_tilde_fence_content_ignored(self):
        md = "~~~\nrequire this to be skipped\n~~~\n"
        assert _texts(md) == []

    def test_rule_before_and_after_fence_extracted(self):
        md = "- Must pass.\n```\nmust not be here\n```\n- Should follow up.\n"
        texts = _texts(md)
        assert len(texts) == 2
        assert "Must pass." in texts
        assert "Should follow up." in texts

    def test_long_fence_marker_handled(self):
        md = "````\nmust not parse this\n````\n"
        assert _texts(md) == []

    def test_indented_fence_skipped(self):
        md = "   ```\nmust not parse this\n   ```\n"
        assert _texts(md) == []

    def test_short_close_does_not_end_long_fence(self):
        # A ``` close must not terminate a ```` opener early.
        md = "````\nmust not parse this\n```\nstill inside fence\n````\n"
        assert _texts(md) == []

    def test_longer_close_ends_long_fence(self):
        # A ````` close is fine for a ```` opener (>= length).
        md = "````\nmust not parse this\n`````\n- Must come after.\n"
        texts = _texts(md)
        assert texts == ["Must come after."]


# ---------------------------------------------------------------------------
# Source location metadata
# ---------------------------------------------------------------------------

class TestSourceLocation:
    def test_line_number_is_one_based(self):
        rules = _parse("- Must review.\n")
        assert rules[0].source.line == 1

    def test_line_number_of_second_rule(self):
        md = "- Must review.\n- Should test.\n"
        rules = _parse(md)
        assert rules[1].source.line == 2

    def test_line_number_after_heading(self):
        md = "# Title\n\n- Must review.\n"
        rules = _parse(md)
        assert rules[0].source.line == 3

    def test_paragraph_line_number_at_start(self):
        md = "Line one.\nMust pass.\n"
        rules = _parse(md)
        assert rules[0].source.line == 1

    def test_source_path_preserved(self):
        rules = parse("docs/rules.md", "- Must follow.\n").rules
        assert rules[0].source.path == "docs/rules.md"

    def test_id_is_stable_for_same_input(self):
        rules_a = _parse("- Must review.\n")
        rules_b = _parse("- Must review.\n")
        assert rules_a[0].id == rules_b[0].id

    def test_ids_are_unique_within_ruleset(self):
        md = "- Must review.\n- Should test.\n- Never skip.\n"
        rules = _parse(md)
        ids = [r.id for r in rules]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# IR field defaults
# ---------------------------------------------------------------------------

class TestIRDefaults:
    def test_kind_is_semantic_rubric(self):
        rules = _parse("- Must review.\n")
        assert rules[0].kind is RuleKind.SEMANTIC_RUBRIC

    def test_backend_hint_is_llm_rubric(self):
        rules = _parse("- Must review.\n")
        assert rules[0].backend_hint is Backend.LLM_RUBRIC

    def test_confidence_is_low(self):
        rules = _parse("- Must review.\n")
        assert rules[0].confidence is Confidence.LOW

    def test_severity_is_warning(self):
        rules = _parse("- Must review.\n")
        assert rules[0].severity is Severity.WARNING

    def test_params_is_empty_dict(self):
        rules = _parse("- Must review.\n")
        assert rules[0].params == {}


# ---------------------------------------------------------------------------
# Fixture file integration
# ---------------------------------------------------------------------------

class TestFixtureFiles:
    def test_basic_rules_fixture(self):
        rules = parse_file(FIXTURES / "basic-rules.md").rules
        texts = [r.text for r in rules]
        # Three normative bullets + three task checkboxes; two normative ordered items
        # Non-normative bullets and narrative ignored
        normative_bullets = [t for t in texts if any(
            kw in t.lower() for kw in ("must", "should", "never")
        )]
        assert len(normative_bullets) >= 3
        # Task checkboxes always present regardless of keywords
        task_texts = {"Tests pass", "Code reviewed", "Documentation updated"}
        assert task_texts.issubset(set(texts))

    def test_narrative_only_fixture_yields_no_rules(self):
        rules = parse_file(FIXTURES / "narrative-only.md").rules
        assert rules == []

    def test_pr_checklist_fixture(self):
        rules = parse_file(FIXTURES / "pr-checklist.md").rules
        texts = [r.text for r in rules]
        # Normative bullet + 4 task checkboxes
        assert "CI checks pass" in texts
        assert "At least one non-author approval" in texts
        assert "Branch is up to date with main" in texts
        assert "No blocking labels present" in texts
        # Also the normative bullet "All of the following must be satisfied..."
        normative = [t for t in texts if "must" in t.lower()]
        assert len(normative) >= 1

    def test_normative_paragraphs_fixture(self):
        rules = parse_file(FIXTURES / "normative-paragraphs.md").rules
        texts = [r.text for r in rules]
        # Code fence content must not appear
        assert not any("must not parse" in t for t in texts)
        assert not any("should not extract" in t for t in texts)
        # Normative paragraphs must appear
        assert any("must authenticate" in t for t in texts)
        assert any("must expire" in t or "must" in t for t in texts)

    def test_fixture_rules_have_stable_ids(self):
        rules_a = parse_file(FIXTURES / "basic-rules.md").rules
        rules_b = parse_file(FIXTURES / "basic-rules.md").rules
        assert [r.id for r in rules_a] == [r.id for r in rules_b]

    def test_fixture_rules_have_correct_source_path(self):
        fixture_path = FIXTURES / "basic-rules.md"
        rules = parse_file(fixture_path).rules
        for rule in rules:
            assert rule.source.path == str(fixture_path)

    def test_fixture_rules_heading_context_present(self):
        rules = parse_file(FIXTURES / "basic-rules.md").rules
        # Rules under "Code Quality" section should carry that heading
        code_quality_rules = [r for r in rules if r.source.heading == "Contribution Rules > Code Quality"]
        assert len(code_quality_rules) >= 1
