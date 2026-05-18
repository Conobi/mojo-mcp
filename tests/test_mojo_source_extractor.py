"""Tests for the Mojo source docstring extractor.

The extractor pulls signature + docstring text from raw `.mojo` source files,
without invoking the Mojo compiler. It is the engine behind a future
`stdlib_source` tool that returns version-pinned reference content from
`modular/modular`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mojo_mcp import mojo_source as ms


# ---------------------------------------------------------------------------
# Module docstring extraction
# ---------------------------------------------------------------------------


class TestExtractModuleDocstring:
    def test_returns_text_after_copyright_block(self):
        src = textwrap.dedent('''\
            # ===-----------=== #
            # Copyright 2026
            # ===-----------=== #
            """The module docstring.

            Multiple paragraphs are supported.
            """

            from foo import bar
            ''')
        assert ms.extract_module_docstring(src) == (
            "The module docstring.\n\nMultiple paragraphs are supported."
        )

    def test_returns_none_when_no_module_docstring(self):
        src = textwrap.dedent('''\
            # Copyright 2026
            from foo import bar

            fn main():
                """This is a function docstring, not module-level."""
                pass
            ''')
        assert ms.extract_module_docstring(src) is None

    def test_accepts_single_quote_triple(self):
        src = textwrap.dedent("""\
            \'\'\'A module with triple-single-quote docstring.\'\'\'

            fn main(): pass
            """)
        assert ms.extract_module_docstring(src) == (
            "A module with triple-single-quote docstring."
        )

    def test_single_line_docstring(self):
        src = '"""One-liner."""\n\nfn main(): pass\n'
        assert ms.extract_module_docstring(src) == "One-liner."

    def test_first_string_must_be_at_column_zero(self):
        # Triple-quote is indented (inside a struct/fn) → not the module docstring
        src = textwrap.dedent('''\
            fn main():
                """Function docstring, not module docstring."""
                pass
            ''')
        assert ms.extract_module_docstring(src) is None

    def test_skips_blank_lines_between_header_and_docstring(self):
        src = textwrap.dedent('''\
            # Copyright


            """The docstring."""
            ''')
        assert ms.extract_module_docstring(src) == "The docstring."


# ---------------------------------------------------------------------------
# Symbol extraction — basic cases
# ---------------------------------------------------------------------------


class TestExtractSymbolBasic:
    def test_simple_struct_one_line(self):
        src = textwrap.dedent('''\
            struct Foo:
                """A simple struct."""
                var x: Int
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert out["kind"] == "struct"
        assert len(out["declarations"]) == 1
        d = out["declarations"][0]
        assert d["signature"].rstrip() == "struct Foo:"
        assert d["docstring"] == "A simple struct."
        assert d["decorators"] == []
        assert d["line"] == 1

    def test_struct_with_trait_conformance(self):
        src = textwrap.dedent('''\
            struct Foo(Copyable, Movable):
                """A foo."""
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert "struct Foo(Copyable, Movable):" in out["declarations"][0]["signature"]
        assert out["declarations"][0]["docstring"] == "A foo."

    def test_struct_with_parameters(self):
        src = textwrap.dedent('''\
            struct Foo[T: AnyType, U: Copyable]:
                """Parameterized struct."""
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert "[T: AnyType, U: Copyable]" in out["declarations"][0]["signature"]
        assert out["declarations"][0]["docstring"] == "Parameterized struct."

    def test_returns_none_when_symbol_missing(self):
        src = 'struct Bar:\n    """A bar."""\n'
        assert ms.extract_symbol(src, "Foo") is None

    def test_does_not_match_substring(self):
        # `Dict` must NOT match `DictEntry`.
        src = textwrap.dedent('''\
            struct DictEntry:
                """Just an entry."""

            struct Dict:
                """The real Dict."""
            ''')
        out = ms.extract_symbol(src, "Dict")
        assert out is not None
        assert len(out["declarations"]) == 1
        assert out["declarations"][0]["docstring"] == "The real Dict."

    def test_does_not_match_inside_comments(self):
        src = textwrap.dedent('''\
            # struct Foo:
            #     This is a comment, not a declaration.
            struct Bar:
                """A bar."""
            ''')
        assert ms.extract_symbol(src, "Foo") is None

    def test_does_not_match_inside_strings(self):
        # `Foo` appears in a docstring of `Bar` but Bar has no such declaration.
        src = textwrap.dedent('''\
            struct Bar:
                """Mentions struct Foo: but doesn't declare it."""
            ''')
        assert ms.extract_symbol(src, "Foo") is None


# ---------------------------------------------------------------------------
# Multi-line declarations
# ---------------------------------------------------------------------------


class TestExtractSymbolMultiLine:
    def test_multi_line_struct_parameters(self):
        src = textwrap.dedent('''\
            struct Dict[
                K: KeyElement,
                V: Copyable,
                H: Hasher = default_hasher,
            ]:
                """A dictionary."""
            ''')
        out = ms.extract_symbol(src, "Dict")
        assert out is not None
        sig = out["declarations"][0]["signature"]
        assert sig.startswith("struct Dict[")
        assert "K: KeyElement," in sig
        assert sig.rstrip().endswith("]:")
        assert out["declarations"][0]["docstring"] == "A dictionary."

    def test_multi_line_params_and_traits(self):
        src = textwrap.dedent('''\
            struct Dict[
                K: KeyElement,
                V: Copyable,
            ](
                Boolable,
                Copyable,
                Sized,
            ):
                """Full Dict."""
            ''')
        out = ms.extract_symbol(src, "Dict")
        assert out is not None
        sig = out["declarations"][0]["signature"]
        assert "K: KeyElement," in sig
        assert "Boolable," in sig
        assert sig.rstrip().endswith("):")
        assert out["declarations"][0]["docstring"] == "Full Dict."

    def test_multi_line_fn_signature(self):
        src = textwrap.dedent('''\
            fn foo[
                T: AnyType,
            ](
                x: T,
                y: Int,
            ) -> T:
                """Multi-line fn."""
                return x
            ''')
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        assert out["kind"] == "fn"
        sig = out["declarations"][0]["signature"]
        assert "T: AnyType," in sig
        assert "x: T," in sig
        assert "-> T:" in sig


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


class TestExtractSymbolDecorators:
    def test_single_decorator(self):
        src = textwrap.dedent('''\
            @fieldwise_init
            struct Foo:
                """A foo."""
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert out["declarations"][0]["decorators"] == ["@fieldwise_init"]

    def test_multiple_decorators_preserve_order(self):
        src = textwrap.dedent('''\
            @register_passable
            @fieldwise_init
            struct Foo:
                """A foo."""
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert out["declarations"][0]["decorators"] == [
            "@register_passable",
            "@fieldwise_init",
        ]

    def test_decorator_with_arguments(self):
        src = textwrap.dedent('''\
            @parameter
            fn foo() -> Int:
                """An inline-evaluated function."""
                return 42
            ''')
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        assert out["declarations"][0]["decorators"] == ["@parameter"]

    def test_no_decorators_returns_empty_list(self):
        src = 'struct Foo:\n    """A foo."""\n'
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert out["declarations"][0]["decorators"] == []

    def test_blank_line_between_decorator_and_declaration_breaks_chain(self):
        # An empty line between a decorator and a declaration severs the link.
        src = textwrap.dedent('''\
            @fieldwise_init

            struct Foo:
                """A foo."""
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert out["declarations"][0]["decorators"] == []


# ---------------------------------------------------------------------------
# Function / def / alias / comptime / trait
# ---------------------------------------------------------------------------


class TestExtractKinds:
    def test_fn_with_return_type(self):
        src = 'fn foo() -> Int:\n    """Returns 42."""\n    return 42\n'
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        assert out["kind"] == "fn"
        assert out["declarations"][0]["signature"].rstrip() == "fn foo() -> Int:"
        assert out["declarations"][0]["docstring"] == "Returns 42."

    def test_fn_with_raises(self):
        src = 'fn foo() raises:\n    """Might raise."""\n    pass\n'
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        assert "raises" in out["declarations"][0]["signature"]

    def test_def_function(self):
        src = 'def foo(x: Int):\n    """A def."""\n    pass\n'
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        assert out["kind"] == "def"
        assert out["declarations"][0]["docstring"] == "A def."

    def test_alias_simple(self):
        src = 'alias MAX_SIZE = 1024\n"""The maximum size."""\n'
        out = ms.extract_symbol(src, "MAX_SIZE")
        assert out is not None
        assert out["kind"] == "alias"
        assert "alias MAX_SIZE = 1024" in out["declarations"][0]["signature"]
        assert out["declarations"][0]["docstring"] == "The maximum size."

    def test_alias_no_docstring(self):
        src = "alias MAX_SIZE = 1024\n\nfn main(): pass\n"
        out = ms.extract_symbol(src, "MAX_SIZE")
        assert out is not None
        assert out["kind"] == "alias"
        assert out["declarations"][0]["docstring"] == ""

    def test_comptime_alias(self):
        src = textwrap.dedent('''\
            comptime KeyElement = Copyable & Hashable & Equatable
            """A trait composition for dictionary keys."""
            ''')
        out = ms.extract_symbol(src, "KeyElement")
        assert out is not None
        assert out["kind"] == "comptime"
        assert "comptime KeyElement =" in out["declarations"][0]["signature"]
        assert out["declarations"][0]["docstring"].startswith("A trait composition")

    def test_trait_declaration(self):
        src = textwrap.dedent('''\
            trait Movable:
                """Can be moved."""

                fn __moveinit__(out self, deinit other: Self):
                    ...
            ''')
        out = ms.extract_symbol(src, "Movable")
        assert out is not None
        assert out["kind"] == "trait"
        assert out["declarations"][0]["docstring"] == "Can be moved."


# ---------------------------------------------------------------------------
# Overloads
# ---------------------------------------------------------------------------


class TestExtractOverloads:
    def test_two_fn_overloads(self):
        src = textwrap.dedent('''\
            fn foo(x: Int) -> Int:
                """Int overload."""
                return x

            fn foo(x: String) -> String:
                """String overload."""
                return x
            ''')
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        assert out["kind"] == "fn"
        assert len(out["declarations"]) == 2
        docstrings = [d["docstring"] for d in out["declarations"]]
        assert docstrings == ["Int overload.", "String overload."]

    def test_overloads_preserve_line_numbers(self):
        src = textwrap.dedent('''\
            fn foo(x: Int) -> Int:
                """A."""
                return x

            fn foo(x: String) -> String:
                """B."""
                return x
            ''')
        out = ms.extract_symbol(src, "foo")
        assert out is not None
        lines = [d["line"] for d in out["declarations"]]
        assert lines == [1, 5]


# ---------------------------------------------------------------------------
# Docstring edge cases
# ---------------------------------------------------------------------------


class TestDocstringEdgeCases:
    def test_docstring_with_code_block(self):
        src = textwrap.dedent('''\
            struct Foo:
                """A struct with code.

                Example:

                ```mojo
                var x = Foo()
                ```
                """
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert "Example:" in out["declarations"][0]["docstring"]
        assert "```mojo" in out["declarations"][0]["docstring"]

    def test_missing_docstring_yields_empty(self):
        src = "struct Foo:\n    var x: Int\n"
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        assert out["declarations"][0]["docstring"] == ""

    def test_docstring_preserves_relative_indentation(self):
        src = textwrap.dedent('''\
            struct Foo:
                """Title.

                Args:
                    x: an int.
                    y: a string.
                """
            ''')
        out = ms.extract_symbol(src, "Foo")
        assert out is not None
        doc = out["declarations"][0]["docstring"]
        # After dedenting from the source's struct-body indent, the structure
        # should keep its nested "Args:" plus 4-space bullets.
        assert "Args:" in doc
        assert "    x: an int." in doc


# ---------------------------------------------------------------------------
# Real-world: actual dict.mojo from modular/modular
# ---------------------------------------------------------------------------


@pytest.fixture
def real_dict_mojo() -> str:
    p = Path("/tmp/dict.mojo")
    if not p.exists():
        pytest.skip("dict.mojo fixture not pre-fetched; skipping integration check")
    return p.read_text()


class TestRealDictMojo:
    def test_module_docstring_extracted(self, real_dict_mojo):
        doc = ms.extract_module_docstring(real_dict_mojo)
        assert doc is not None
        assert doc.startswith("Defines `Dict`")

    def test_dict_struct_signature_captured(self, real_dict_mojo):
        out = ms.extract_symbol(real_dict_mojo, "Dict")
        assert out is not None
        assert out["kind"] == "struct"
        sig = out["declarations"][0]["signature"]
        assert "K: KeyElement," in sig
        assert "H: Hasher = default_hasher," in sig
        assert sig.rstrip().endswith("):")
        assert out["declarations"][0]["docstring"].startswith(
            "A container that stores key-value pairs."
        )

    def test_dict_does_not_match_dictkeyerror(self, real_dict_mojo):
        out = ms.extract_symbol(real_dict_mojo, "Dict")
        assert out is not None
        # Multiple decls would mean we matched DictKeyError too; we should not.
        assert len(out["declarations"]) == 1

    def test_dictkeyerror_extracted_separately(self, real_dict_mojo):
        out = ms.extract_symbol(real_dict_mojo, "DictKeyError")
        assert out is not None
        assert out["kind"] == "struct"
        assert "Parameters:" in out["declarations"][0]["docstring"]

    def test_emptydicterror_decorator_captured(self, real_dict_mojo):
        out = ms.extract_symbol(real_dict_mojo, "EmptyDictError")
        assert out is not None
        assert "@fieldwise_init" in out["declarations"][0]["decorators"]

    def test_keyelement_comptime_alias(self, real_dict_mojo):
        out = ms.extract_symbol(real_dict_mojo, "KeyElement")
        assert out is not None
        assert out["kind"] == "comptime"
        assert "Copyable" in out["declarations"][0]["signature"]
