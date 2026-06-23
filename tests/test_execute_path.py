"""Tests for execute(path=...) alternative (R4)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestExecutePathXOR:
    @pytest.mark.asyncio
    async def test_both_code_and_path_returns_error(self, tmp_path):
        from mojo_mcp.server import call_tool
        f = tmp_path / "x.mojo"
        f.write_text("def main(): pass\n")
        result = await call_tool("execute", {"code": "x", "path": str(f), "format": "json"})
        parsed = json.loads(result.content[0].text)
        assert "error" in parsed
        assert "either" in parsed["error"].lower() or "not both" in parsed["error"].lower()

    @pytest.mark.asyncio
    async def test_neither_returns_error(self):
        from mojo_mcp.server import call_tool
        result = await call_tool("execute", {"format": "json"})
        parsed = json.loads(result.content[0].text)
        assert "error" in parsed

    @pytest.mark.asyncio
    async def test_path_missing_file_returns_error(self, tmp_path):
        from mojo_mcp.server import call_tool
        result = await call_tool("execute", {"path": str(tmp_path / "missing.mojo"), "format": "json"})
        parsed = json.loads(result.content[0].text)
        assert "error" in parsed
        assert "missing.mojo" in parsed["error"] or "no such" in parsed["error"].lower() or "not found" in parsed["error"].lower()


class TestExecutePathHappyPath:
    @pytest.mark.asyncio
    async def test_path_resolved_against_cwd(self, tmp_path):
        from mojo_mcp.server import call_tool
        (tmp_path / "main.mojo").write_text("def main():\n    print('hi')\n")
        with patch("mojo_mcp.sandbox.subprocess.run") as mock_run, \
             patch("mojo_mcp.sandbox.shutil.rmtree"):
            mock_run.return_value = MagicMock(stdout="hi\n", stderr="", returncode=0)
            result = await call_tool("execute", {
                "path": "main.mojo", "cwd": str(tmp_path), "format": "json",
            })
        parsed = json.loads(result.content[0].text)
        assert parsed["returncode"] == 0
        assert parsed["stdout"] == "hi\n"

    @pytest.mark.asyncio
    async def test_path_absolute_works(self, tmp_path):
        from mojo_mcp.server import call_tool
        f = tmp_path / "abs.mojo"
        f.write_text("def main(): pass\n")
        with patch("mojo_mcp.sandbox.subprocess.run") as mock_run, \
             patch("mojo_mcp.sandbox.shutil.rmtree"):
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            result = await call_tool("execute", {"path": str(f), "format": "json"})
        parsed = json.loads(result.content[0].text)
        assert parsed["returncode"] == 0

    @pytest.mark.asyncio
    async def test_large_file_not_capped(self, tmp_path):
        from mojo_mcp.server import call_tool
        big = tmp_path / "big.mojo"
        # 200KB of source — well above read_file's 100KB cap
        big.write_text("def main():\n    pass\n" + ("# pad\n" * 30000))
        captured = {}
        def fake_run(cmd, *a, **k):
            try:
                captured["bytes"] = Path(cmd[-1]).stat().st_size
            except Exception:
                captured["bytes"] = -1
            return MagicMock(stdout="", stderr="", returncode=0)
        with patch("mojo_mcp.sandbox.subprocess.run", side_effect=fake_run), \
             patch("mojo_mcp.sandbox.shutil.rmtree"):
            await call_tool("execute", {"path": str(big), "format": "json"})
        # Source must be passed in full — not capped at 100KB
        assert captured["bytes"] > 150_000

    @pytest.mark.asyncio
    async def test_code_only_still_works_backward_compat(self):
        from mojo_mcp.server import call_tool
        with patch("mojo_mcp.sandbox.subprocess.run") as mock_run, \
             patch("mojo_mcp.sandbox.shutil.rmtree"):
            mock_run.return_value = MagicMock(stdout="ok\n", stderr="", returncode=0)
            result = await call_tool("execute", {"code": "def main(): pass\n", "format": "json"})
        parsed = json.loads(result.content[0].text)
        assert parsed["returncode"] == 0
