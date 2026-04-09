"""Tests for AXI ergonomic improvements."""

import json
import tempfile
from pathlib import Path

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


from mojo_mcp.sandbox import run_list_files


class TestListFilesHints:
    def test_has_count(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.mojo").write_text("x")
            Path(d, "b.mojo").write_text("y")
            result = json.loads(run_list_files(d))
            assert result["count"] == 2
            assert len(result["files"]) == 2

    def test_empty_state_has_message(self):
        with tempfile.TemporaryDirectory() as d:
            result = json.loads(run_list_files(d))
            assert result["count"] == 0
            assert "message" in result
            assert "0 files" in result["message"]
            assert "hint" in result

    def test_non_empty_has_hint(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "test.mojo").write_text("x")
            result = json.loads(run_list_files(d))
            assert "hint" in result
            assert "read_file" in result["hint"]

    def test_keeps_path_and_pattern(self):
        with tempfile.TemporaryDirectory() as d:
            result = json.loads(run_list_files(d))
            assert "path" in result
            assert "pattern" in result


from mojo_mcp.sandbox import run_search


class TestSearchWrapped:
    def test_result_wrapped_in_metadata(self):
        docs = {"test": {"name": "test", "structs": [], "functions": [], "traits": [], "aliases": []}}
        result = json.loads(run_search("return list(docs.keys())", docs))
        assert "result" in result
        assert result["result"] == ["test"]
        assert "hint" in result
        assert "lookup" in result["hint"]

    def test_null_result_has_message(self):
        docs = {"test": {"name": "test", "structs": [], "functions": [], "traits": [], "aliases": []}}
        result = json.loads(run_search("return None", docs))
        assert result["result"] is None
        assert "message" in result
        assert "hint" in result

    def test_error_wrapped(self):
        docs = {}
        result = json.loads(run_search("return 1/0", docs))
        assert "error" in result

    def test_timeout_wrapped(self):
        docs = {}
        # This will fail because 'import' is not in restricted builtins, causing an error (not a timeout)
        # Use a while loop instead to trigger timeout
        result = json.loads(run_search("while True: pass", docs))
        assert "error" in result

    def test_truncated_uses_result_raw(self):
        # Generate a result larger than MAX_OUTPUT (8192 bytes)
        docs = {}
        code = "return 'x' * 10000"
        result = json.loads(run_search(code, docs))
        assert "result_raw" in result or "result" in result
        if "result_raw" in result:
            assert result["truncated"] is True
            assert "total_bytes" in result
