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


class TestRunValidateDirectory:
    def test_validate_directory_recursive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Clean file
            Path(tmpdir, "clean.mojo").write_text("def main():\n    print('hi')\n")
            # File with issue
            Path(tmpdir, "bad.mojo").write_text("var x = 10\ndef main():\n    pass\n")
            # Nested file with issue
            sub = Path(tmpdir, "sub")
            sub.mkdir()
            Path(sub, "nested.mojo").write_text("var y = 20\ndef main():\n    pass\n")

            result = json.loads(run_validate(path=tmpdir))
            assert result["files_scanned"] == 3
            assert result["files_with_issues"] == 2
            assert result["total_issues"] >= 2
            paths = [r["path"] for r in result["results"]]
            assert any("bad.mojo" in p for p in paths)
            assert any("nested.mojo" in p for p in paths)

    def test_validate_directory_all_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "clean.mojo").write_text("def main():\n    print('hi')\n")
            result = json.loads(run_validate(path=tmpdir))
            assert result["files_scanned"] == 1
            assert result["files_with_issues"] == 0
            assert result["total_issues"] == 0
            assert "message" in result

    def test_validate_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = json.loads(run_validate(path=tmpdir))
            assert "error" in result

    def test_validate_directory_with_category(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "app.mojo").write_text(
                "def main():\n    var p = UnsafePointer[Int, MutAnyOrigin]\n"
            )
            result = json.loads(run_validate(path=tmpdir, category="security"))
            assert result.get("category") == "security"

    def test_validate_directory_omits_clean_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "clean.mojo").write_text("def main():\n    print('hi')\n")
            Path(tmpdir, "bad.mojo").write_text("var x = 10\ndef main():\n    pass\n")
            result = json.loads(run_validate(path=tmpdir))
            paths = [r["path"] for r in result["results"]]
            assert not any("clean.mojo" in p for p in paths)
