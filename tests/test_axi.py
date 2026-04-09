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


from unittest.mock import patch, MagicMock
import subprocess

from mojo_mcp.sandbox import run_execute


class TestExecutePhaseA:
    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_success_omits_empty_stderr(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(stdout="hello\n", stderr="", returncode=0)
        result = json.loads(run_execute("def main():\n    print('hello')\n"))
        assert result["returncode"] == 0
        assert "stderr" not in result

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_failure_keeps_stderr(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="error: bad\n", returncode=1)
        result = json.loads(run_execute("bad code\n"))
        assert result["returncode"] == 1
        assert "stderr" in result

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_has_duration(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(stdout="ok\n", stderr="", returncode=0)
        result = json.loads(run_execute("def main(): pass\n"))
        assert "duration_s" in result
        assert isinstance(result["duration_s"], (int, float))
        assert result["duration_s"] >= 0

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_failure_has_hint(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="error: x\n", returncode=1)
        result = json.loads(run_execute("bad\n"))
        assert "hint" in result
        assert "validate" in result["hint"]

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_success_no_hint(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(stdout="ok\n", stderr="", returncode=0)
        result = json.loads(run_execute("def main(): pass\n"))
        assert "hint" not in result
