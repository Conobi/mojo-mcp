"""Tests for AXI ergonomic improvements."""

import json

from mojo_mcp.sandbox import _json


class TestJsonHelper:
    def test_compact_no_whitespace(self):
        result = _json({"key": "value", "num": 42})
        assert result == '{"key":"value","num":42}'

    def test_handles_path_via_default_str(self):
        from pathlib import Path
        result = _json({"p": Path("/tmp/test")})
        parsed = json.loads(result)
        assert parsed["p"] == "/tmp/test"

    def test_no_indent(self):
        result = _json({"a": [1, 2, 3]})
        assert "\n" not in result
        assert "  " not in result


from mojo_mcp.sandbox import run_validate


class TestValidateHints:
    def test_clean_code_has_message_and_hint(self):
        result = json.loads(run_validate(code="def main():\n    print('hi')\n"))
        assert result["count"] == 0
        assert "message" in result
        assert "No known gotcha patterns matched" in result["message"]
        assert "hint" in result
        assert "execute" in result["hint"]

    def test_issues_found_has_hint(self):
        result = json.loads(run_validate(code="var x = 10\ndef main():\n    pass\n"))
        assert result["count"] > 0
        assert "hint" in result
        assert "execute" in result["hint"]

    def test_error_has_hint(self):
        result = json.loads(run_validate())
        assert "error" in result
        assert "hint" in result
        assert "validate(" in result["hint"]
