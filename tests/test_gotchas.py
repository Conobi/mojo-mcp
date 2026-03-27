"""Unit tests for gotchas.py — pattern loading, matching, and version filtering."""

import re

import pytest

from mojo_mcp.gotchas import enrich_error, load_gotchas, validate_code


class TestLoadGotchas:
    def test_load_returns_list(self):
        gotchas = load_gotchas()
        assert isinstance(gotchas, list)

    def test_load_has_17_entries(self):
        gotchas = load_gotchas()
        assert len(gotchas) == 17

    def test_all_entries_have_required_fields(self):
        gotchas = load_gotchas()
        required = {"id", "title", "severity", "mojo_versions", "timeout_pattern", "description", "fix"}
        for g in gotchas:
            assert required.issubset(g.keys()), f"Missing fields in {g['id']}: {required - g.keys()}"

    def test_all_entries_have_at_least_one_pattern(self):
        gotchas = load_gotchas()
        for g in gotchas:
            has_pattern = (
                g.get("code_pattern") is not None
                or g.get("error_pattern") is not None
                or g.get("timeout_pattern") is True
            )
            assert has_pattern, f"Gotcha {g['id']} has no pattern (code, error, or timeout)"

    def test_all_code_patterns_are_valid_regex(self):
        gotchas = load_gotchas()
        for g in gotchas:
            if g.get("code_pattern"):
                try:
                    re.compile(g["code_pattern"], re.MULTILINE)
                except re.error as e:
                    pytest.fail(f"Invalid regex in {g['id']}.code_pattern: {e}")

    def test_all_error_patterns_are_valid_regex(self):
        gotchas = load_gotchas()
        for g in gotchas:
            if g.get("error_pattern"):
                try:
                    re.compile(g["error_pattern"])
                except re.error as e:
                    pytest.fail(f"Invalid regex in {g['id']}.error_pattern: {e}")

    def test_severity_values_are_valid(self):
        gotchas = load_gotchas()
        valid = {"critical", "warning", "info"}
        for g in gotchas:
            assert g["severity"] in valid, f"Invalid severity '{g['severity']}' in {g['id']}"

    def test_ids_are_unique(self):
        gotchas = load_gotchas()
        ids = [g["id"] for g in gotchas]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {[x for x in ids if ids.count(x) > 1]}"


class TestValidateCode:
    def test_clean_code_no_issues(self):
        code = "def main():\n    print('hello')\n"
        issues = validate_code(code, "0.26.2")
        assert issues == []

    def test_detects_module_level_var(self):
        code = "var x = 10\ndef main():\n    print(x)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "no-module-level-mutable" in ids

    def test_detects_dtypepointer(self):
        code = "def main():\n    var p = DTypePointer[DType.float32].alloc(10)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "dtypepointer-deprecated" in ids

    def test_detects_match_keyword(self):
        code = "def main():\n    match x:\n        case 1: pass\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "no-match-enum" in ids

    def test_detects_variant_get(self):
        code = "def main():\n    var v = Variant[Int, String](42)\n    var x = v.get[Int]()\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "variant-reference-not-copy" in ids

    def test_detects_multiple_issues(self):
        code = "var x = DTypePointer[DType.int8].alloc(1)\ndef main():\n    pass\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "no-module-level-mutable" in ids
        assert "dtypepointer-deprecated" in ids

    def test_version_filter_excludes_future(self):
        issues = validate_code("var x = 1\n", "0.26.2")
        assert any(i["id"] == "no-module-level-mutable" for i in issues)


class TestEnrichError:
    def test_no_enrichment_on_empty_stderr(self):
        hints = enrich_error("", timed_out=False, mojo_version="0.26.2")
        assert hints == []

    def test_enriches_module_level_var_error(self):
        stderr = "error: module-level variable 'x' must be declared as 'alias'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "no-module-level-mutable" in ids

    def test_enriches_getitem_error(self):
        stderr = "error: 'String' does not implement the '__getitem__' method\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "string-indexing" in ids

    def test_enriches_timeout(self):
        hints = enrich_error("", timed_out=True, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "variant-loop-hang" in ids
        assert "slow-compilation-hint" in ids

    def test_enriches_integer_overflow(self):
        stderr = "runtime error: integer overflow detected\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "integer-overflow-ub" in ids

    def test_hint_has_required_fields(self):
        stderr = "error: module-level variable 'x' must be declared as 'alias'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        assert len(hints) > 0
        hint = hints[0]
        assert "id" in hint
        assert "title" in hint
        assert "severity" in hint
        assert "description" in hint
        assert "fix" in hint
