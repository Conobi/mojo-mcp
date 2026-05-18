"""Tests for the GitHub/llms.txt-backed stdlib indexer."""

from __future__ import annotations

import httpx
import pytest

from mojo_mcp import docs as docs_mod
from mojo_mcp import docs_backend as db


# A trimmed but realistic llms-stdlib.txt fixture.
_LLMS_STDLIB = """\
# Mojo standard library

> The Mojo standard library, covering all the APIs included with the language.

## Table of Contents

- [collections](https://mojolang.org/docs/std/collections.md): Container types.
- [dict](https://mojolang.org/docs/std/collections/dict.md): Defines Dict.
- [Dict](https://mojolang.org/docs/std/collections/dict/Dict.md): A container that stores key-value pairs.
- [KeyElement](https://mojolang.org/docs/std/collections/dict/KeyElement.md): A trait composition.
- [list](https://mojolang.org/docs/std/collections/list.md): Defines List.
- [List](https://mojolang.org/docs/std/collections/list/List.md): A dynamic-length list.
- [algorithm](https://mojolang.org/docs/std/algorithm.md): Algorithm utilities.
- [backend](https://mojolang.org/docs/std/algorithm/backend.md): Backend impls.
- [cpu](https://mojolang.org/docs/std/algorithm/backend/cpu.md): CPU algorithm backend.
- [elementwise](https://mojolang.org/docs/std/algorithm/backend/cpu/elementwise.md): CPU elementwise.
- [parallelize](https://mojolang.org/docs/std/algorithm/backend/cpu/parallelize.md): Parallelization.
- [func_unified](https://mojolang.org/docs/std/algorithm/backend/cpu/parallelize/func_unified.md): Unified function.
- [MAX_SIZE](https://mojolang.org/docs/std/utils/constants/MAX_SIZE.md): Maximum size constant.
"""


def _install_llms_mock(monkeypatch, *, payload=None, status=200):
    def _handler(request):
        url = str(request.url)
        if url == db.MOJOLANG_BASE + "/llms-stdlib.txt":
            return httpx.Response(status, text=payload if payload is not None else _LLMS_STDLIB)
        return httpx.Response(404)
    monkeypatch.setattr(
        db, "build_mojolang_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_handler), timeout=30),
    )


@pytest.mark.asyncio
class TestFetchStdlibIndex:
    async def test_returns_module_keyed_dict(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        assert isinstance(docs, dict)
        # Modules detected (have children in URL space)
        assert "collections" in docs
        assert "collections.dict" in docs
        assert "collections.list" in docs
        assert "algorithm" in docs
        assert "algorithm.backend" in docs
        assert "algorithm.backend.cpu" in docs
        assert "algorithm.backend.cpu.parallelize" in docs

    async def test_module_description_preserved(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        assert docs["collections.dict"]["description"].startswith("Defines Dict")
        assert docs["algorithm.backend.cpu"]["description"].startswith("CPU algorithm backend")

    async def test_pascal_case_symbols_go_to_structs(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        mod = docs["collections.dict"]
        names = [s["name"] for s in mod["structs"]]
        assert "Dict" in names
        # KeyElement is also PascalCase → structs bucket (heuristic limitation;
        # the actual `lookup` call resolves the true kind via mojo_source).
        assert "KeyElement" in names

    async def test_snake_case_symbols_go_to_functions(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        cpu = docs["algorithm.backend.cpu"]
        names = [f["name"] for f in cpu["functions"]]
        assert "elementwise" in names

    async def test_all_caps_symbols_go_to_aliases(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        mod = docs["utils.constants"]
        names = [a["name"] for a in mod["aliases"]]
        assert "MAX_SIZE" in names

    async def test_symbol_description_preserved(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        dict_struct = next(
            s for s in docs["collections.dict"]["structs"] if s["name"] == "Dict"
        )
        assert dict_struct["description"].startswith("A container that stores key-value pairs.")

    async def test_module_pages_not_added_as_symbols(self, monkeypatch):
        _install_llms_mock(monkeypatch)
        docs = await docs_mod.fetch_stdlib_index()
        # `dict` is a module (has children), so it should NOT appear as a function
        # inside `collections`
        collections = docs["collections"]
        names = [f["name"] for f in collections["functions"]]
        assert "dict" not in names
        assert "list" not in names


class TestBucketForName:
    @pytest.mark.parametrize("name,expected", [
        ("Dict", "structs"),
        ("List", "structs"),
        ("KeyElement", "structs"),
        ("elementwise", "functions"),
        ("parallelize", "functions"),
        ("MAX_SIZE", "aliases"),
        ("PI", "aliases"),
        ("_private", "functions"),
        ("__init__", "functions"),
    ])
    def test_classifies_by_naming(self, name, expected):
        assert docs_mod._bucket_for_name(name) == expected
