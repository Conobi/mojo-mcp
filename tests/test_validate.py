"""Unit tests for the validate tool."""

import json
import tempfile
from pathlib import Path

from mojo_mcp.sandbox import run_validate


class TestRunValidate:
    def test_validate_code_string_clean(self):
        result = json.loads(run_validate(code="def main():\n    print('hi')\n"))
        assert result["count"] == 0
        assert result["issues"] == []

    def test_validate_code_string_with_issue(self):
        result = json.loads(run_validate(code="var x = 10\ndef main():\n    pass\n"))
        assert result["count"] > 0
        ids = [i["id"] for i in result["issues"]]
        assert "no-module-level-mutable" in ids

    def test_validate_file_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mojo", delete=False) as f:
            f.write("var x = 10\ndef main():\n    pass\n")
            f.flush()
            result = json.loads(run_validate(path=f.name))
        Path(f.name).unlink()
        assert result["count"] > 0
        ids = [i["id"] for i in result["issues"]]
        assert "no-module-level-mutable" in ids

    def test_validate_code_takes_precedence_over_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mojo", delete=False) as f:
            f.write("var x = 10\n")
            f.flush()
            result = json.loads(run_validate(code="def main():\n    pass\n", path=f.name))
        Path(f.name).unlink()
        assert result["count"] == 0

    def test_validate_nonexistent_path(self):
        result = json.loads(run_validate(path="/nonexistent/file.mojo"))
        assert "error" in result

    def test_validate_no_code_no_path(self):
        result = json.loads(run_validate())
        assert "error" in result

    def test_validate_multiple_issues(self):
        code = "var p = DTypePointer[DType.int8].alloc(1)\ndef main():\n    pass\n"
        result = json.loads(run_validate(code=code))
        assert result["count"] >= 2
