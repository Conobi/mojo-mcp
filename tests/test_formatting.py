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


class TestSearchRenderer:
    def test_normal_result_bulleted_list(self):
        result = {"result": ["foo", "bar", "baz"], "hint": "use lookup"}
        md = render(result, "md", tool="search")
        assert "- foo" in md or "**foo**" in md
        assert "use lookup" in md

    def test_list_of_name_description_dicts(self):
        result = {"result": [
            {"name": "Dict", "description": "Hash map."},
            {"name": "List", "description": "Dynamic array."},
        ], "hint": "use lookup"}
        md = render(result, "md", tool="search")
        assert "**Dict** — Hash map." in md
        assert "**List** — Dynamic array." in md

    def test_dict_result_renders_as_yaml_or_keyvalue(self):
        result = {"result": {"a": 1, "b": 2}, "hint": "..."}
        md = render(result, "md", tool="search")
        assert "a" in md and "b" in md

    def test_null_result_shows_message(self):
        result = {"result": None, "message": "Search returned no results.", "hint": "Try broader terms"}
        md = render(result, "md", tool="search")
        assert "no results" in md.lower()
        assert "Try broader terms" in md

    def test_truncated_includes_metadata(self):
        result = {"result_raw": "huge", "truncated": True, "total_bytes": 99999, "hint": "Narrow query"}
        md = render(result, "md", tool="search")
        assert "truncated" in md.lower()
        assert "99999" in md or "99,999" in md

    def test_error_shows_error_section(self):
        result = {"error": "search timed out after 5 seconds"}
        md = render(result, "md", tool="search")
        assert "timed out" in md.lower()


class TestExecuteRenderer:
    def test_success_shows_stdout_and_returncode(self):
        result = {"stdout": "hello\n", "returncode": 0, "duration_s": 0.42}
        md = render(result, "md", tool="execute")
        assert "### stdout" in md
        assert "hello" in md
        assert "0.42" in md or "0.4" in md
        assert "returncode" in md
        assert "### stderr" not in md

    def test_failure_shows_stderr_and_error_summary(self):
        result = {
            "stdout": "", "stderr": "error: bad\n", "returncode": 1, "duration_s": 0.1,
            "hint": "Run validate first.", "error_summary": "error: bad",
        }
        md = render(result, "md", tool="execute")
        assert "### stderr" in md
        assert "error: bad" in md
        assert "error_summary" in md or "Error summary" in md

    def test_includes_gotcha_hints_when_present(self):
        result = {
            "stdout": "", "stderr": "error: x", "returncode": 1, "duration_s": 0.0,
            "gotcha_hints": [{"id": "x", "title": "T", "fix": "F"}],
        }
        md = render(result, "md", tool="execute")
        assert "Gotcha hints" in md or "gotcha" in md.lower()
        assert "T" in md

    def test_handles_stdout_containing_triple_backticks(self):
        result = {"stdout": "code: ```mojo\nfn main()\n```", "returncode": 0, "duration_s": 0.0}
        md = render(result, "md", tool="execute")
        # Fence must be at least 4 backticks because content contains 3
        assert "````" in md

    def test_error_result_renders_message(self):
        result = {"error": "mojo not installed", "hint": "Run install_mojo first."}
        md = render(result, "md", tool="execute")
        assert "mojo not installed" in md
        assert "install_mojo" in md
