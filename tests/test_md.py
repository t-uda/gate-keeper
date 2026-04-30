"""Unit tests for gate_keeper._md — shared Markdown helpers.

Focused tests for strip_fenced_blocks and the task-box regexes.
The task-box patterns are also exercised indirectly via
test_filesystem_backend.py::TestMarkdownTasksComplete.
"""
from __future__ import annotations

import pytest

from gate_keeper._md import (
    TASK_CHECKED_RE,
    TASK_UNCHECKED_RE,
    strip_fenced_blocks,
)


class TestStripFencedBlocks:
    def test_plain_text_unchanged(self):
        text = "Hello\nWorld\n"
        assert strip_fenced_blocks(text) == text

    def test_backtick_fence_removed(self):
        text = "before\n```\n- [ ] inside fence\n```\nafter\n"
        result = strip_fenced_blocks(text)
        assert "inside fence" not in result
        assert "before" in result
        assert "after" in result

    def test_tilde_fence_removed(self):
        text = "before\n~~~\n- [ ] inside tilde\n~~~\nafter\n"
        result = strip_fenced_blocks(text)
        assert "inside tilde" not in result
        assert "before" in result
        assert "after" in result

    def test_longer_fence_closes_shorter_fence(self):
        """A longer fence (4 ticks) closes a shorter fence (3 ticks) per CommonMark.

        The closing rule requires at least as many characters as the opening.
        So ```` closes ```, leaving "still inside" visible outside the fence.
        """
        # Open with ``` (3 ticks), encounter ```` (4 ticks) which closes it.
        text = "```\ninner\n````\nstill inside\n```\nafter\n"
        result = strip_fenced_blocks(text)
        # "inner" is inside the first fence (closed by ````), so it's stripped.
        assert "inner" not in result
        # "still inside" is outside the (now-closed) first fence.
        assert "still inside" in result
        # "after" is outside a second (3-tick) fence that re-opens and is unterminated.
        # Actually: after ```` closes the fence, then ``` opens a new fence (unterminated).
        # So "after" is inside the unterminated second fence and gets stripped.
        assert "after" not in result

    def test_shorter_fence_does_not_close_longer_fence(self):
        """A shorter fence (3 ticks) does NOT close a longer fence (4 ticks)."""
        # Open with ```` (4 ticks); encounter ``` (3 ticks) — does not close.
        text = "````\ninner\n```\nstill inside\n````\nafter\n"
        result = strip_fenced_blocks(text)
        assert "inner" not in result
        assert "still inside" not in result
        assert "after" in result

    def test_unterminated_fence_consumes_rest(self):
        """An unterminated fence strips everything to end of document."""
        text = "visible\n```\nhidden line\nanother hidden\n"
        result = strip_fenced_blocks(text)
        assert "visible" in result
        assert "hidden" not in result

    def test_empty_string(self):
        assert strip_fenced_blocks("") == ""

    def test_task_boxes_outside_fence_preserved(self):
        """Task-box lines outside a fence survive strip_fenced_blocks."""
        text = "- [ ] todo\n- [x] done\n"
        result = strip_fenced_blocks(text)
        assert "- [ ] todo" in result
        assert "- [x] done" in result

    def test_mixed_task_boxes_inside_and_outside(self):
        """Task boxes inside a fence are ignored; outside ones survive."""
        text = (
            "- [ ] outside unchecked\n"
            "```\n"
            "- [ ] inside unchecked\n"
            "- [x] inside checked\n"
            "```\n"
            "- [x] outside checked\n"
        )
        result = strip_fenced_blocks(text)
        unchecked = TASK_UNCHECKED_RE.findall(result)
        checked = TASK_CHECKED_RE.findall(result)
        assert len(unchecked) == 1
        assert len(checked) == 1

    def test_four_backtick_fence(self):
        """Fences with four or more backticks work correctly."""
        text = "outside\n````\nhidden\n````\nback outside\n"
        result = strip_fenced_blocks(text)
        assert "hidden" not in result
        assert "outside" in result
        assert "back outside" in result

    def test_indented_fence(self):
        """Indented fences (with leading spaces/tabs) are recognized."""
        text = "text\n  ```\nhidden\n  ```\nvisible\n"
        result = strip_fenced_blocks(text)
        assert "hidden" not in result
        assert "visible" in result


class TestTaskBoxRegexes:
    @pytest.mark.parametrize("line", [
        "- [ ] item",
        "* [ ] item",
        "+ [ ] item",
        "  - [ ] indented",
        "\t- [ ] tab-indented",
    ])
    def test_unchecked_matches(self, line):
        assert TASK_UNCHECKED_RE.search(line) is not None

    @pytest.mark.parametrize("line", [
        "- [x] done",
        "- [X] done",
        "* [x] done",
        "  - [X] indented",
    ])
    def test_checked_matches(self, line):
        assert TASK_CHECKED_RE.search(line) is not None

    def test_unchecked_does_not_match_checked(self):
        assert TASK_UNCHECKED_RE.search("- [x] done") is None

    def test_checked_does_not_match_unchecked(self):
        assert TASK_CHECKED_RE.search("- [ ] todo") is None
