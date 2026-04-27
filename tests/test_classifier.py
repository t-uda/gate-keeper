from __future__ import annotations

from gate_keeper.classifier import classify_candidate, compile_document
from gate_keeper.models import Backend, Confidence, RuleKind
from gate_keeper.parser import ParsedRuleCandidate
from gate_keeper.models import SourceLocation


def _candidate(text: str, *, structure: str = "list", heading: str | None = None) -> ParsedRuleCandidate:
    return ParsedRuleCandidate(
        text=text,
        source=SourceLocation(path="rules.md", line=1, heading=heading),
        start_line=1,
        end_line=1,
        structure=structure,
        raw=text,
    )


def test_classifier_routes_filesystem_rules():
    rule, explanation = classify_candidate(
        _candidate("The repository must include a README.md file."),
        document_stem="rules",
        index=1,
    )
    assert rule.kind is RuleKind.FILE_EXISTS
    assert rule.backend_hint is Backend.FILESYSTEM
    assert rule.confidence is Confidence.HIGH
    assert rule.params["path"] == "README.md"
    assert "Filesystem" in explanation


def test_classifier_routes_github_rules():
    rule, _ = classify_candidate(
        _candidate("Pull requests must not be drafts."),
        document_stem="rules",
        index=1,
    )
    assert rule.kind is RuleKind.GITHUB_NOT_DRAFT
    assert rule.backend_hint is Backend.GITHUB


def test_classifier_routes_ambiguous_rules_to_semantic_rubric():
    rule, _ = classify_candidate(
        _candidate("This should generally be a good experience for humans."),
        document_stem="rules",
        index=1,
    )
    assert rule.kind is RuleKind.SEMANTIC_RUBRIC
    assert rule.backend_hint is Backend.LLM_RUBRIC
    assert rule.confidence is Confidence.LOW


def test_compile_document_produces_rule_set():
    content = """# Example Rules\n\n- The repository must include a README.md file.\n- AGENTS.md must mention `uv run pytest`.\n- checklist.md must have every task complete.\n"""
    ruleset, explanations = compile_document("docs/example-rules.md", content)
    assert len(ruleset.rules) == len(explanations)
    assert {rule.kind for rule in ruleset.rules} >= {
        RuleKind.FILE_EXISTS,
        RuleKind.TEXT_REQUIRED,
        RuleKind.MARKDOWN_TASKS_COMPLETE,
    }
