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
    find_first_fenced_block_after_heading,
    heading_present,
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
    @pytest.mark.parametrize(
        "line",
        [
            "- [ ] item",
            "* [ ] item",
            "+ [ ] item",
            "  - [ ] indented",
            "\t- [ ] tab-indented",
        ],
    )
    def test_unchecked_matches(self, line):
        assert TASK_UNCHECKED_RE.search(line) is not None

    @pytest.mark.parametrize(
        "line",
        [
            "- [x] done",
            "- [X] done",
            "* [x] done",
            "  - [X] indented",
        ],
    )
    def test_checked_matches(self, line):
        assert TASK_CHECKED_RE.search(line) is not None

    def test_unchecked_does_not_match_checked(self):
        assert TASK_UNCHECKED_RE.search("- [x] done") is None

    def test_checked_does_not_match_unchecked(self):
        assert TASK_CHECKED_RE.search("- [ ] todo") is None


class TestStripFencedBlocksCommonMark:
    """Regression tests for stricter CommonMark fence handling."""

    def test_indented_more_than_3_spaces_is_not_a_fence(self):
        """A line with 4+ leading spaces is code-block-by-indentation, not a fence."""
        text = "before\n    ```python\n- [ ] outside any fence\n    ```\nafter\n"
        result = strip_fenced_blocks(text)
        # Because ``    \`\`\`python`` is NOT a fence, the task box that follows
        # remains visible.
        assert "outside any fence" in result

    def test_backtick_fence_with_backtick_in_info_string_rejected(self):
        """A backtick fence whose info string contains a backtick is not an opening fence."""
        # ```` ```py` ```` is not a valid opening fence per CommonMark.
        text = "before\n```py`bad\n- [ ] still outside\n```\nafter\n"
        result = strip_fenced_blocks(text)
        assert "still outside" in result

    def test_three_space_indent_is_still_a_fence(self):
        """0–3 spaces of indentation is allowed for a fence."""
        text = "before\n   ```\n- [ ] inside\n   ```\nafter\n"
        result = strip_fenced_blocks(text)
        assert "inside" not in result

    def test_closing_fence_with_trailing_text_is_not_a_close(self):
        """A line `\\`\\`\\`stuff` while inside a fence is not a closing fence."""
        text = "before\n```\nfirst\n```not-closing\nstill inside\n```\nafter\n"
        result = strip_fenced_blocks(text)
        assert "first" not in result
        assert "still inside" not in result
        assert "after" in result


class TestHeadingPresent:
    def test_exact_match(self):
        assert heading_present("# Policy evidence\n\nbody\n", "Policy evidence") is True

    def test_subheading_level(self):
        assert heading_present("## Policy evidence\n\nbody\n", "Policy evidence") is True

    def test_no_match(self):
        assert heading_present("# Other\n", "Policy evidence") is False

    def test_case_sensitive(self):
        assert heading_present("# policy evidence\n", "Policy evidence") is False

    def test_trailing_hashes_stripped(self):
        assert heading_present("## Policy evidence ##\n", "Policy evidence") is True


class TestFindFirstFencedBlockAfterHeading:
    def test_returns_block_body_and_line(self):
        text = "# Title\n\n## Policy evidence\n\n```yaml\nfoo: 1\n```\n"
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None
        body, fence_line, info = out
        assert body == "foo: 1"
        assert fence_line == 5
        assert info == "yaml"

    def test_missing_heading_returns_none(self):
        text = "# Title\n\n## Other\n\n```yaml\nfoo: 1\n```\n"
        assert find_first_fenced_block_after_heading(text, "Policy evidence") is None

    def test_missing_block_returns_none(self):
        text = "## Policy evidence\n\nprose only\n\n## Next\n\n```yaml\nfoo: 1\n```\n"
        assert find_first_fenced_block_after_heading(text, "Policy evidence") is None

    def test_block_before_heading_does_not_count(self):
        text = "```yaml\nbefore: 1\n```\n\n## Policy evidence\n\nprose\n"
        assert find_first_fenced_block_after_heading(text, "Policy evidence") is None

    def test_block_under_subheading_is_picked_up(self):
        # A deeper heading does NOT terminate the search; the next fenced block
        # encountered before a sibling/parent heading wins.
        text = "## Policy evidence\n\n### Detail\n\n```yaml\nfoo: 1\n```\n\n## Next\n"
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None
        body, _, info = out
        assert body == "foo: 1"
        assert info == "yaml"

    def test_unterminated_fence_returns_partial_body(self):
        text = "## Policy evidence\n\n```yaml\nfoo: 1\nbar: 2\n"
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None
        body, _, _ = out
        assert "foo: 1" in body
        assert "bar: 2" in body

    def test_empty_info_string_yields_none_info(self):
        text = "## Policy evidence\n\n```\nfoo: 1\n```\n"
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None
        _, _, info = out
        assert info is None

    def test_tilde_fence_supported(self):
        text = "## Policy evidence\n\n~~~yaml\nfoo: 1\n~~~\n"
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None
        body, _, info = out
        assert body == "foo: 1"
        assert info == "yaml"

    def test_heading_inside_earlier_fence_is_ignored(self):
        # The ``## Policy evidence`` line inside an earlier fenced sample
        # block must NOT be treated as the section heading. Otherwise the
        # backend would parse the fenced sample as the YAML body or report
        # ``block_missing`` against the wrong section.
        text = (
            "# Title\n\n"
            "Sample document showing the rule:\n\n"
            "````markdown\n"
            "## Policy evidence\n"
            "\n"
            "```yaml\n"
            "decoy: in-sample\n"
            "```\n"
            "````\n\n"
            "## Policy evidence\n\n"
            "```yaml\n"
            "real: yes\n"
            "```\n"
        )
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None
        body, _, info = out
        assert body == "real: yes"
        assert info == "yaml"

    def test_heading_with_terminal_hash_preserves_hash(self):
        # ``## C#`` must match heading "C#", not "C". A naive ``\\s*#*\\s*$``
        # regex would strip the trailing ``#`` and produce ``"C"``, breaking
        # exact-match semantics.
        text = "## C#\n\n```yaml\nlanguage: csharp\n```\n"
        out = find_first_fenced_block_after_heading(text, "C#")
        assert out is not None
        # The wrong-title lookup does NOT match this section.
        assert find_first_fenced_block_after_heading(text, "C") is None

    def test_heading_closing_sequence_still_normalized(self):
        # Proper CommonMark closing sequence (whitespace + #+) is still
        # stripped, so ``## Policy evidence ##`` matches title "Policy
        # evidence".
        text = "## Policy evidence ##\n\n```yaml\nx: 1\n```\n"
        out = find_first_fenced_block_after_heading(text, "Policy evidence")
        assert out is not None


class TestHeadingPresentInsideFence:
    def test_heading_inside_fence_is_ignored(self):
        text = "# Title\n\n```markdown\n## Policy evidence\n```\n"
        # No real section heading exists outside the fence.
        assert heading_present(text, "Policy evidence") is False

    def test_heading_with_terminal_hash(self):
        assert heading_present("## C#\n", "C#") is True
        assert heading_present("## C#\n", "C") is False
