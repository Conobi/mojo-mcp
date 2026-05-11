"""Tests for the formatting module (R3)."""

import json

import pytest

from mojo_mcp.formatting import _fence, render


class TestFence:
    def test_default_three_backticks_when_no_backticks_in_content(self):
        assert _fence("hello world") == "```"

    def test_escalates_to_four_when_content_has_three_backticks(self):
        content = "code: ```python\nprint('x')\n```"
        assert _fence(content) == "````"

    def test_escalates_to_five_when_content_has_four_backticks(self):
        content = "````nested````"
        assert _fence(content) == "`````"

    def test_minimum_is_three_even_for_empty(self):
        assert _fence("") == "```"

    def test_handles_only_singles_and_doubles(self):
        assert _fence("`one` and ``two``") == "```"


class TestRenderJsonPassthrough:
    def test_json_format_returns_compact_json(self):
        result = render({"foo": "bar", "n": 1}, "json", tool="search")
        assert result == '{"foo":"bar","n":1}'

    def test_json_format_unknown_tool_still_works(self):
        # JSON path is tool-agnostic
        result = render({"k": "v"}, "json", tool="nonexistent_tool")
        assert result == '{"k":"v"}'

    def test_md_format_unknown_tool_raises(self):
        with pytest.raises(KeyError):
            render({"foo": "bar"}, "md", tool="nonexistent_tool")
