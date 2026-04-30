"""Tests for the rule classifier."""
from __future__ import annotations

import pytest

from gate_keeper.classifier import classify, classify_rule
from gate_keeper.models import Backend, Confidence, RuleKind, Severity, SourceLocation, Rule
from gate_keeper.parser import parse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(text: str, heading: str | None = None) -> Rule:
    """Construct a bare parser-default Rule for unit testing the classifier."""
    return Rule(
        id="rule-test-L1",
        title=text[:80],
        source=SourceLocation(path="test.md", line=1, heading=heading),
        text=text,
        kind=RuleKind.SEMANTIC_RUBRIC,
        severity=Severity.WARNING,
        backend_hint=Backend.LLM_RUBRIC,
        confidence=Confidence.LOW,
        params={},
    )


def _classify_text(text: str, heading: str | None = None) -> Rule:
    return classify_rule(_make_rule(text, heading))


def _parse_and_classify(md: str) -> list[Rule]:
    return classify(parse("test.md", md)).rules


# ---------------------------------------------------------------------------
# GitHub routing — high confidence
# ---------------------------------------------------------------------------


class TestGithubHighConfidence:
    def test_draft_routes_to_github(self):
        rule = _classify_text("PR must not be in draft state")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_NOT_DRAFT
        assert rule.confidence is Confidence.HIGH

    def test_not_draft_phrasing(self):
        rule = _classify_text("The pull request must not be a draft")
        assert rule.kind is RuleKind.GITHUB_NOT_DRAFT
        assert rule.confidence is Confidence.HIGH

    def test_pr_open_routes_to_github(self):
        rule = _classify_text("The PR must be open (not closed or merged)")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_PR_OPEN
        assert rule.confidence is Confidence.HIGH

    def test_not_merged_routes_to_pr_open(self):
        rule = _classify_text("Branch must not be merged before review")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_PR_OPEN
        assert rule.confidence is Confidence.HIGH

    def test_ci_checks_pass_routes_to_github(self):
        rule = _classify_text("CI checks must pass before merging")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_CHECKS_SUCCESS
        assert rule.confidence is Confidence.HIGH

    def test_status_checks_succeed_routes_to_github(self):
        rule = _classify_text("All status checks must succeed")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_CHECKS_SUCCESS
        assert rule.confidence is Confidence.HIGH

    def test_build_must_pass_routes_to_github(self):
        rule = _classify_text("The build must pass before the PR can be merged")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_CHECKS_SUCCESS
        assert rule.confidence is Confidence.HIGH

    def test_review_threads_resolved_routes_to_github(self):
        rule = _classify_text("All review threads must be resolved before merging")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_THREADS_RESOLVED
        assert rule.confidence is Confidence.HIGH

    def test_unresolved_threads_routes_to_github(self):
        rule = _classify_text("Unresolved threads block merging")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_THREADS_RESOLVED
        assert rule.confidence is Confidence.HIGH

    def test_non_author_approval_routes_to_github(self):
        rule = _classify_text("At least one non-author approval is required")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_NON_AUTHOR_APPROVAL
        assert rule.confidence is Confidence.HIGH

    def test_independent_review_routes_to_github(self):
        rule = _classify_text("An independent review must be completed")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_NON_AUTHOR_APPROVAL
        assert rule.confidence is Confidence.HIGH

    def test_blocking_labels_absent_routes_to_github(self):
        rule = _classify_text("No blocking labels must be present before merge")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_LABELS_ABSENT
        assert rule.confidence is Confidence.HIGH

    def test_do_not_merge_label_routes_to_github(self):
        rule = _classify_text("The do-not-merge label must not be applied")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_LABELS_ABSENT
        assert rule.confidence is Confidence.HIGH


# ---------------------------------------------------------------------------
# GitHub routing — medium confidence
# ---------------------------------------------------------------------------


class TestGithubMediumConfidence:
    def test_approval_alone_is_medium(self):
        rule = _classify_text("An approval is required before merge")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_NON_AUTHOR_APPROVAL
        assert rule.confidence is Confidence.MEDIUM

    def test_reviewer_keyword_is_medium(self):
        rule = _classify_text("A reviewer must sign off on each PR")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_NON_AUTHOR_APPROVAL
        assert rule.confidence is Confidence.MEDIUM

    def test_label_alone_is_medium(self):
        rule = _classify_text("Labels must be verified before merging")
        assert rule.backend_hint is Backend.GITHUB
        assert rule.kind is RuleKind.GITHUB_LABELS_ABSENT
        assert rule.confidence is Confidence.MEDIUM

    def test_item_under_pr_heading_is_medium(self):
        rule = _classify_text(
            "Branch is up to date with main",
            heading="PR Merge Gates > Required Checks",
        )
        assert rule.backend_hint is Backend.GITHUB
        assert rule.confidence is Confidence.MEDIUM

    def test_item_under_merge_gate_heading_is_medium(self):
        rule = _classify_text(
            "Documentation is up to date",
            heading="Merge Gate",
        )
        assert rule.backend_hint is Backend.GITHUB
        assert rule.confidence is Confidence.MEDIUM


# ---------------------------------------------------------------------------
# Filesystem routing — high confidence
# ---------------------------------------------------------------------------


class TestFilesystemHighConfidence:
    def test_file_exists_routes_to_filesystem(self):
        rule = _classify_text("The CHANGELOG file must exist before release")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.FILE_EXISTS
        assert rule.confidence is Confidence.HIGH

    def test_file_must_be_present_routes_to_filesystem(self):
        rule = _classify_text("A LICENSE file must be present in the repository")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.FILE_EXISTS
        assert rule.confidence is Confidence.HIGH

    def test_file_absent_routes_to_filesystem(self):
        rule = _classify_text("The debug.log file must not exist in production")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.FILE_ABSENT
        assert rule.confidence is Confidence.HIGH

    def test_file_must_be_removed_routes_to_filesystem(self):
        rule = _classify_text("The temp file must be absent before deployment")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.FILE_ABSENT
        assert rule.confidence is Confidence.HIGH

    def test_text_required_routes_to_filesystem(self):
        rule = _classify_text("The README must contain a usage section")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.TEXT_REQUIRED
        assert rule.confidence is Confidence.HIGH

    def test_text_must_include_routes_to_filesystem(self):
        rule = _classify_text("The config file should include a version field")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.TEXT_REQUIRED
        assert rule.confidence is Confidence.HIGH

    def test_text_forbidden_routes_to_filesystem(self):
        rule = _classify_text("The codebase must not contain TODO comments before release")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.TEXT_FORBIDDEN
        assert rule.confidence is Confidence.HIGH

    def test_must_not_include_routes_to_filesystem(self):
        rule = _classify_text("Source files should not include debug statements")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.TEXT_FORBIDDEN
        assert rule.confidence is Confidence.HIGH


# ---------------------------------------------------------------------------
# Filesystem routing — medium confidence
# ---------------------------------------------------------------------------


class TestFilesystemMediumConfidence:
    def test_path_mention_is_medium(self):
        rule = _classify_text("The output path must follow the naming convention")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.PATH_MATCHES
        assert rule.confidence is Confidence.MEDIUM

    def test_directory_mention_is_medium(self):
        rule = _classify_text("The build directory must be named correctly")
        assert rule.backend_hint is Backend.FILESYSTEM
        assert rule.kind is RuleKind.PATH_MATCHES
        assert rule.confidence is Confidence.MEDIUM


# ---------------------------------------------------------------------------
# LLM rubric fallback — low confidence
# ---------------------------------------------------------------------------


class TestLLMRubricFallback:
    def test_ambiguous_quality_rule_falls_back(self):
        rule = _classify_text("Code quality must be adequate for the project standards")
        assert rule.backend_hint is Backend.LLM_RUBRIC
        assert rule.kind is RuleKind.SEMANTIC_RUBRIC
        assert rule.confidence is Confidence.LOW

    def test_vague_process_rule_falls_back(self):
        rule = _classify_text("The team must follow agreed-upon conventions")
        assert rule.backend_hint is Backend.LLM_RUBRIC
        assert rule.kind is RuleKind.SEMANTIC_RUBRIC
        assert rule.confidence is Confidence.LOW

    def test_commit_secrets_rule_falls_back(self):
        # "Never commit secrets" — intent is clear but the evidence is semantic
        rule = _classify_text("Never commit secrets or credentials to the repository")
        assert rule.backend_hint is Backend.LLM_RUBRIC
        assert rule.kind is RuleKind.SEMANTIC_RUBRIC
        assert rule.confidence is Confidence.LOW


# ---------------------------------------------------------------------------
# Integration: parser output fed directly into the classifier
# ---------------------------------------------------------------------------


class TestParserIntegration:
    def test_pr_checklist_items_classify_correctly(self):
        md = (
            "# PR Merge Gates\n\n"
            "## Required Checks\n\n"
            "All of the following must be satisfied before merging.\n\n"
            "- [ ] CI checks pass\n"
            "- [ ] At least one non-author approval\n"
            "- [x] Branch is up to date with main\n"
            "- [ ] No blocking labels present\n"
        )
        rules = _parse_and_classify(md)
        by_text = {r.text: r for r in rules}

        ci = by_text["CI checks pass"]
        assert ci.backend_hint is Backend.GITHUB
        assert ci.kind is RuleKind.GITHUB_CHECKS_SUCCESS
        assert ci.confidence is Confidence.HIGH

        approval = by_text["At least one non-author approval"]
        assert approval.backend_hint is Backend.GITHUB
        assert approval.kind is RuleKind.GITHUB_NON_AUTHOR_APPROVAL
        assert approval.confidence is Confidence.HIGH

        labels = by_text["No blocking labels present"]
        assert labels.backend_hint is Backend.GITHUB
        assert labels.kind is RuleKind.GITHUB_LABELS_ABSENT
        assert labels.confidence is Confidence.HIGH

        # "Branch is up to date with main" has no specific pattern → medium via heading
        branch = by_text["Branch is up to date with main"]
        assert branch.backend_hint is Backend.GITHUB
        assert branch.confidence is Confidence.MEDIUM

    def test_file_rule_integration(self):
        md = "- The CHANGELOG file must exist in the repo root.\n"
        rules = _parse_and_classify(md)
        assert len(rules) == 1
        assert rules[0].backend_hint is Backend.FILESYSTEM
        assert rules[0].kind is RuleKind.FILE_EXISTS
        assert rules[0].confidence is Confidence.HIGH

    def test_semantic_rule_remains_low(self):
        md = "- Code quality must meet team standards.\n"
        rules = _parse_and_classify(md)
        assert len(rules) == 1
        assert rules[0].backend_hint is Backend.LLM_RUBRIC
        assert rules[0].kind is RuleKind.SEMANTIC_RUBRIC
        assert rules[0].confidence is Confidence.LOW

    def test_mixed_ruleset_preserves_all_rules(self):
        md = (
            "- The CHANGELOG file must exist.\n"
            "- CI checks must pass.\n"
            "- Code quality must meet standards.\n"
        )
        rules = _parse_and_classify(md)
        assert len(rules) == 3
        backends = {r.backend_hint for r in rules}
        assert Backend.FILESYSTEM in backends
        assert Backend.GITHUB in backends
        assert Backend.LLM_RUBRIC in backends


# ---------------------------------------------------------------------------
# Explanation is stored in params for classified rules
# ---------------------------------------------------------------------------


class TestExplanationInParams:
    def test_classified_rule_has_explanation(self):
        rule = _classify_text("PR must not be in draft state")
        assert "classifier_explanation" in rule.params
        assert isinstance(rule.params["classifier_explanation"], str)
        assert len(rule.params["classifier_explanation"]) > 0

    def test_fallback_rule_has_no_explanation(self):
        # Parser default is returned as-is; no explanation is injected
        rule = _classify_text("Code quality must be adequate")
        assert "classifier_explanation" not in rule.params


# ---------------------------------------------------------------------------
# Low-confidence rules remain visible — not dropped
# ---------------------------------------------------------------------------


class TestLowConfidenceVisible:
    def test_low_confidence_rules_present_in_mixed_ruleset(self):
        md = (
            "- CI checks must pass.\n"
            "- Code quality must meet team standards.\n"
        )
        rules = _parse_and_classify(md)
        confidences = {r.confidence for r in rules}
        assert Confidence.HIGH in confidences
        assert Confidence.LOW in confidences
        low = [r for r in rules if r.confidence is Confidence.LOW]
        assert len(low) == 1
        assert low[0].backend_hint is Backend.LLM_RUBRIC

    def test_high_medium_low_all_represented(self):
        md = (
            "- CI checks must pass.\n"                          # HIGH github
            "- An approval is required before merge.\n"         # MEDIUM github
            "- Code quality must be adequate.\n"                # LOW llm-rubric
        )
        rules = _parse_and_classify(md)
        conf_map = {r.confidence for r in rules}
        assert Confidence.HIGH in conf_map
        assert Confidence.MEDIUM in conf_map
        assert Confidence.LOW in conf_map

    def test_classify_preserves_rule_count(self):
        md = (
            "- Must review.\n"
            "- Should test.\n"
            "- Never skip CI.\n"
            "- The README file must exist.\n"
        )
        original = parse("test.md", md)
        classified = classify(original)
        assert len(classified.rules) == len(original.rules)
