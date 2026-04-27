from __future__ import annotations

from pathlib import Path

from gate_keeper.parser import extract_candidates


def test_parser_extracts_normative_lists_and_tasks_from_agents_md():
    content = Path("AGENTS.md").read_text(encoding="utf-8")
    parsed = extract_candidates("AGENTS.md", content)

    assert parsed.candidates
    assert any("fail-closed" in candidate.text for candidate in parsed.candidates)
    assert all(candidate.start_line == candidate.end_line for candidate in parsed.candidates)
    assert all(candidate.source.path == "AGENTS.md" for candidate in parsed.candidates)


def test_parser_ignores_code_blocks():
    content = """# Example\n\n```md\n- must not be extracted\n```\n\nNarrative text without a rule.\n"""
    parsed = extract_candidates("snippet.md", content)
    assert parsed.candidates == []


def test_parser_handles_pr_checklist_snippet():
    content = """## PR checklist\n\n- [ ] Write tests\n- [x] Update docs\n- [ ] Ensure release notes are correct\n"""
    parsed = extract_candidates("pr.md", content)
    texts = [candidate.text for candidate in parsed.candidates]
    assert texts == ["Write tests", "Update docs", "Ensure release notes are correct"]
    assert all(candidate.source.heading == "PR checklist" for candidate in parsed.candidates)


def test_parser_skips_narrative_paragraphs_without_normative_words():
    content = """# Notes\n\nThis paragraph is just background information.\n\nAnother plain sentence.\n"""
    parsed = extract_candidates("notes.md", content)
    assert parsed.candidates == []
