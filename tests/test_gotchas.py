"""Unit tests for gotchas.py — pattern loading, matching, and version filtering."""

import re

import pytest

from mojo_mcp.gotchas import (
    _parse_version,
    _strip_comments_and_strings,
    enrich_error,
    load_gotchas,
    validate_code,
)


class TestLoadGotchas:
    def test_load_returns_list(self):
        gotchas = load_gotchas()
        assert isinstance(gotchas, list)

    def test_load_has_49_entries(self):
        gotchas = load_gotchas()
        assert len(gotchas) == 49

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

    # -- 2026-05-11 audit additions --

    def test_enriches_implicitly_copyable_required(self):
        stderr = "error: value of type 'Error' cannot be implicitly copied, it does not conform to 'ImplicitlyCopyable'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "implicitly-copyable-required" in ids

    def test_enriches_raise_in_non_raising_context(self):
        stderr = "error: cannot call function that may raise in a context that cannot raise\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "raise-in-non-raising-context" in ids

    def test_enriches_expression_no_origin(self):
        stderr = "error: expression does not designate a value with an origin\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "expression-no-origin" in ids

    def test_enriches_unqualified_struct_parameter(self):
        stderr = "error: unqualified access to struct parameter 'min'; use 'Self.min' instead\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "unqualified-struct-parameter" in ids

    def test_enriches_main_in_package(self):
        stderr = "error: defining 'main' within a package is not yet supported\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "main-in-package" in ids

    def test_enriches_failed_to_resolve_parent_package(self):
        stderr = "error: failed to resolve parent package body\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "failed-to-resolve-parent-package" in ids

    def test_enriches_global_vars_not_supported(self):
        stderr = "error: global vars are not supported\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "global-vars-not-supported" in ids

    def test_enriches_unknown_declaration_str(self):
        stderr = "error: use of unknown declaration 'str'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "unknown-declaration-str" in ids

    def test_enriches_copyable_movable_required(self):
        stderr = "error: value of type 'QuantLinear' cannot be copied or moved; consider conforming it to 'Movable'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "copyable-movable-required" in ids

    def test_enriches_raw_pointer_needs_unsafe(self):
        stderr = "error: this public function might dereference a raw pointer but is not marked `unsafe`\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="0.26.2")
        ids = [h["id"] for h in hints]
        assert "raw-pointer-needs-unsafe" in ids


class TestParseVersion:
    """Version parsing must tolerate pre-release suffixes (Mojo 1.0.0b1 etc.)."""

    def test_plain_semver(self):
        assert _parse_version("0.26.2") == (0, 26, 2)

    def test_two_segments(self):
        assert _parse_version("26.2") == (26, 2)

    def test_pre_release_beta(self):
        # 1.0.0b1 is the Mojo 1.0 beta — must not crash, treated as 1.0.0
        assert _parse_version("1.0.0b1") == (1, 0, 0)

    def test_pre_release_alpha(self):
        assert _parse_version("1.0.0a2") == (1, 0, 0)

    def test_pre_release_in_range(self):
        # Gotchas keyed to ">=1.0.0" should match the 1.0.0 beta
        from mojo_mcp.gotchas import _version_matches
        assert _version_matches("1.0.0b1", [">=1.0.0"])

    def test_enrich_error_does_not_raise_on_pre_release(self):
        # Regression: execute()'s error-enrichment path passed raw "1.0.0b1"
        # straight into _parse_version, which threw int('0b1') ValueError.
        hints = enrich_error("error: some error\n", timed_out=False, mojo_version="1.0.0b1")
        assert isinstance(hints, list)

    def test_validate_code_does_not_raise_on_pre_release(self):
        issues = validate_code("def main():\n    pass\n", "1.0.0b1")
        assert isinstance(issues, list)


class TestValidateCodeAuditAdditions:
    """Code-pattern (code-only) tests for 2026-05-11 audit additions."""

    def test_detects_unknown_declaration_str_code(self):
        code = "def main():\n    var x = str(42)\n    print(x)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "unknown-declaration-str" in ids


class TestMojo100b1IdiomAudit:
    """2026-05-19 audit: idioms introduced in Mojo 1.0.0b1."""

    def test_detects_alias_deprecated(self):
        code = "alias FOO = 42\ndef main():\n    print(FOO)\n"
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "alias-deprecated" in ids

    def test_alias_deprecated_filtered_out_pre_1_0(self):
        code = "alias FOO = 42\ndef main():\n    print(FOO)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "alias-deprecated" not in ids

    def test_enriches_alias_deprecated(self):
        stderr = "warning: 'alias' is deprecated; use 'comptime'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="1.0.0b1")
        ids = [h["id"] for h in hints]
        assert "alias-deprecated" in ids

    def test_detects_del_var_self(self):
        code = "struct Foo:\n    def __del__(var self): pass\ndef main(): pass\n"
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "del-var-self-hangs" in ids

    def test_del_var_self_fires_on_timeout(self):
        hints = enrich_error("", timed_out=True, mojo_version="1.0.0b1")
        ids = [h["id"] for h in hints]
        assert "del-var-self-hangs" in ids

    def test_detects_sizeof(self):
        code = "def main():\n    print(sizeof[Int]())\n"
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "sizeof-renamed" in ids

    def test_enriches_sizeof(self):
        stderr = "error: use of unknown declaration 'sizeof'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="1.0.0b1")
        ids = [h["id"] for h in hints]
        assert "sizeof-renamed" in ids

    def test_detects_mutable_any_origin(self):
        code = "def main():\n    var p: UnsafePointer[Int, MutableAnyOrigin]\n"
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "mutable-any-origin-renamed" in ids

    def test_enriches_mutable_any_origin(self):
        stderr = "error: use of unknown declaration 'MutableAnyOrigin'\n"
        hints = enrich_error(stderr, timed_out=False, mojo_version="1.0.0b1")
        ids = [h["id"] for h in hints]
        assert "mutable-any-origin-renamed" in ids

    def test_detects_unsafe_pointer_null_sentinel(self):
        code = "def main():\n    var p = UnsafePointer[Int]()\n"
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "unsafe-pointer-null-sentinel" in ids

    def test_detects_equatable_eq_raises(self):
        code = (
            "struct S(Equatable, Copyable):\n"
            "    def __eq__(self, other: Self) raises -> Bool:\n"
            "        return True\n"
            "def main(): pass\n"
        )
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "equatable-eq-raises" in ids

    def test_enriches_equatable_eq_raises(self):
        stderr = (
            "error: could not derive Equatable for Wrapper — "
            "member field 'storage' does not implement Equatable\n"
        )
        hints = enrich_error(stderr, timed_out=False, mojo_version="1.0.0b1")
        ids = [h["id"] for h in hints]
        assert "equatable-eq-raises" in ids

    def test_detects_writer_bound_without_some(self):
        code = (
            "struct S:\n"
            "    def write_to[W: Writer](self, mut writer: W): pass\n"
            "def main(): pass\n"
        )
        issues = validate_code(code, "1.0.0b1")
        ids = [i["id"] for i in issues]
        assert "writer-bound-without-some" in ids


class TestSecurityRules:
    """Security rules adapted from the ANSSI secure Mojo guide."""

    def test_detects_forget_deinit(self):
        code = "from std.memory import forget_deinit\ndef main():\n    forget_deinit(x^)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-forget-deinit" in ids

    def test_detects_abort(self):
        code = "def handle_error():\n    abort()\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-abort-in-library" in ids

    def test_detects_memset_zero(self):
        code = "def clear():\n    memset_zero(ptr, 64)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-memset-zero-sensitive" in ids

    def test_detects_unsafe_pointer(self):
        code = "def main():\n    var p = UnsafePointer[Int, MutAnyOrigin]\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-unsafe-pointer-app-code" in ids

    def test_detects_unsafe_union(self):
        code = "def main():\n    var u = UnsafeUnion[Int32, Float32]()\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-unsafe-union" in ids

    def test_detects_unsafe_maybe_uninit(self):
        code = "def main():\n    var m = UnsafeMaybeUninit[Int]()\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-unsafe-maybe-uninit" in ids

    def test_detects_implicit_copy_sensitive(self):
        code = "struct SecretKey(ImplicitlyCopyable):\n    var data: List[UInt8]\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-implicit-copy-sensitive" in ids

    def test_security_issues_have_category(self):
        code = "def main():\n    forget_deinit(x^)\n"
        issues = validate_code(code, "0.26.2")
        sec = [i for i in issues if i.get("category") == "security"]
        assert len(sec) > 0

    def test_category_filter_returns_only_security(self):
        code = "var x = 1\ndef main():\n    forget_deinit(x^)\n"
        all_issues = validate_code(code, "0.26.2")
        sec_issues = validate_code(code, "0.26.2", category="security")
        assert len(sec_issues) < len(all_issues)
        for issue in sec_issues:
            assert issue.get("category") == "security"

    def test_category_filter_none_returns_all(self):
        code = "var x = 1\ndef main():\n    forget_deinit(x^)\n"
        all_issues = validate_code(code, "0.26.2", category=None)
        assert any(i.get("category") == "security" for i in all_issues)
        assert any(i.get("category") is None for i in all_issues)


class TestStripCommentsAndStrings:
    """Verify that comment/string stripping prevents false-positive matches."""

    def test_strips_triple_double_quoted_docstrings(self):
        source = '"""An owned I/O resource handle."""\ndef main(): pass\n'
        stripped = _strip_comments_and_strings(source)
        assert "owned" not in stripped

    def test_strips_triple_single_quoted_docstrings(self):
        source = "'''An owned I/O resource handle.'''\ndef main(): pass\n"
        stripped = _strip_comments_and_strings(source)
        assert "owned" not in stripped

    def test_strips_single_line_strings(self):
        source = 'var x = "owned data"\ndef main(): pass\n'
        stripped = _strip_comments_and_strings(source)
        assert "owned data" not in stripped

    def test_strips_comments(self):
        source = "# owned resources need cleanup\ndef main(): pass\n"
        stripped = _strip_comments_and_strings(source)
        assert "owned" not in stripped

    def test_preserves_code(self):
        source = "def main():\n    var x = 42\n"
        stripped = _strip_comments_and_strings(source)
        assert "def main" in stripped
        assert "var x = 42" in stripped

    def test_hash_inside_string_not_treated_as_comment(self):
        source = 'var x = "foo#bar"\nvar y = 10\n'
        stripped = _strip_comments_and_strings(source)
        assert "var y = 10" in stripped

    def test_escaped_quotes_handled(self):
        source = 'var x = "say \\"hello\\""\nowned param: Int\n'
        stripped = _strip_comments_and_strings(source)
        assert "owned param: Int" in stripped


class TestFalsePositivePrevention:
    """Each test validates that a previously-identified false positive no longer fires."""

    def test_owned_in_docstring_not_flagged(self):
        code = '"""An owned I/O resource handle with RAII semantics."""\ndef main(): pass\n'
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "owned-keyword-removed" not in ids

    def test_owned_in_parameter_position_still_flagged(self):
        code = "def foo(owned x: Int):\n    pass\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "owned-keyword-removed" in ids

    def test_string_indexing_not_flagged_on_code_scan(self):
        code = "def main():\n    var arr = InlineArray[UInt8, 4](fill=0)\n    var x = arr[0]\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "string-indexing" not in ids

    def test_inline_array_fill_not_flagged(self):
        code = "def main():\n    var arr = InlineArray[UInt8, 4](fill=0)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "inline-array-init" not in ids

    def test_list_kwargs_not_flagged(self):
        code = "def main():\n    var x = List[Int](size=5, data=ptr)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "list-variadic-construction" not in ids

    def test_list_variadic_still_flagged(self):
        code = "def main():\n    var x = List[Int](1, 2, 3)\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "list-variadic-construction" in ids

    def test_unsafe_pointer_always_flagged(self):
        code = "def main():\n    var p = UnsafePointer[Int, MutAnyOrigin]\n"
        issues = validate_code(code, "0.26.2")
        ids = [i["id"] for i in issues]
        assert "sec-unsafe-pointer-app-code" in ids
