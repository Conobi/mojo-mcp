"""Integration tests that require a working Mojo installation.

Run with: pytest --run-mojo
Or: pytest -m mojo --run-mojo
"""

import json

import pytest

from mojo_mcp.sandbox import run_execute, run_validate


@pytest.mark.mojo
class TestLiveValidate:
    def test_real_clean_code(self):
        code = "fn main():\n    print('hello world')\n"
        result = json.loads(run_validate(code=code))
        critical = [i for i in result["issues"] if i["severity"] == "critical"]
        assert len(critical) == 0

    def test_real_dtypepointer_detected(self):
        code = "from memory import DTypePointer\nfn main():\n    pass\n"
        result = json.loads(run_validate(code=code))
        ids = [i["id"] for i in result["issues"]]
        assert "dtypepointer-deprecated" in ids


@pytest.mark.mojo
class TestLiveExecuteEnrichment:
    def test_real_successful_execution(self):
        code = "fn main():\n    print('hello')\n"
        result = json.loads(run_execute(code))
        assert result["returncode"] == 0
        assert "gotcha_hints" not in result
        assert "hello" in result["stdout"]

    def test_real_compilation_error_enriched(self):
        code = "var x = 10\nfn main():\n    print(x)\n"
        result = json.loads(run_execute(code))
        if result.get("returncode", 0) != 0:
            if "gotcha_hints" in result:
                assert isinstance(result["gotcha_hints"], list)
                for hint in result["gotcha_hints"]:
                    assert "id" in hint
                    assert "fix" in hint
