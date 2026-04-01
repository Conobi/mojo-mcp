"""Unit tests for automatic error enrichment in execute."""

import json
from unittest.mock import patch, MagicMock
import subprocess

from mojo_mcp.sandbox import run_execute


class TestExecuteEnrichment:
    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_enrichment_on_compiler_error(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="error: module-level variable 'x' must be declared as 'alias'\n",
            returncode=1,
        )
        result = json.loads(run_execute("var x = 10\ndef main(): pass\n"))
        assert result["returncode"] == 1
        assert "gotcha_hints" in result
        ids = [h["id"] for h in result["gotcha_hints"]]
        assert "no-module-level-mutable" in ids

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_enrichment_on_timeout(self, mock_rmtree, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mojo", timeout=30)
        result = json.loads(run_execute("for i in range(10):\n    var v = Variant[Int](i)\n"))
        assert "error" in result
        assert "gotcha_hints" in result
        ids = [h["id"] for h in result["gotcha_hints"]]
        assert "variant-loop-hang" in ids
        assert "slow-compilation-hint" in ids

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_no_enrichment_on_success(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(
            stdout="hello\n",
            stderr="",
            returncode=0,
        )
        result = json.loads(run_execute("def main():\n    print('hello')\n"))
        assert result["returncode"] == 0
        assert "gotcha_hints" not in result

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_enrichment_on_getitem_error(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="error: 'String' does not implement the '__getitem__' method\n",
            returncode=1,
        )
        result = json.loads(run_execute("def main():\n    var s = 'hi'\n    print(s[0])\n"))
        assert "gotcha_hints" in result
        ids = [h["id"] for h in result["gotcha_hints"]]
        assert "string-indexing" in ids

    @patch("mojo_mcp.sandbox.subprocess.run")
    @patch("mojo_mcp.sandbox.shutil.rmtree")
    def test_enrichment_hint_fields(self, mock_rmtree, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="error: module-level variable 'x' must be declared as 'alias'\n",
            returncode=1,
        )
        result = json.loads(run_execute("var x = 10\ndef main(): pass\n"))
        hint = result["gotcha_hints"][0]
        assert "id" in hint
        assert "title" in hint
        assert "severity" in hint
        assert "description" in hint
        assert "fix" in hint
