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
