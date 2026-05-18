"""Tests for the manual / reference / cli tools."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from mojo_mcp import docs as docs_mod
from mojo_mcp import docs_backend as db


# Sample directory listing for mojo/docs/manual at a fake ref.
_MANUAL_LISTING_ROOT = [
    {"name": "basics.md", "type": "file", "path": "mojo/docs/manual/basics.md",
     "download_url": "https://raw.example/manual/basics.md"},
    {"name": "lifecycle", "type": "dir", "path": "mojo/docs/manual/lifecycle"},
]

_MANUAL_LISTING_LIFECYCLE = [
    {"name": "death.md", "type": "file", "path": "mojo/docs/manual/lifecycle/death.md",
     "download_url": "https://raw.example/manual/lifecycle/death.md"},
]

_MANUAL_FILES = {
    "https://raw.example/manual/basics.md": "# Basics\n\nMojo basics content.\n",
    "https://raw.example/manual/lifecycle/death.md": "# Death\n\nValue destruction.\n",
}

_TAGS_FIXTURE = [
    {"name": "modular/v26.2.0"},
    {"name": "main-but-not-really"},
]


@pytest.fixture
def clean_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "TAGS_CACHE_PATH", tmp_path / "tags.json")
    # Use a tmp cache dir for handwritten surfaces too
    def _hw_path(surface, ref):
        return tmp_path / f"{surface}-{ref.replace('/', '_')}.json"
    monkeypatch.setattr(docs_mod, "_handwritten_cache_path", _hw_path)
    yield


def _install_mock(monkeypatch, captured=None):
    def _handler(request):
        if captured is not None:
            captured.append(request)
        url = str(request.url)
        # Tag list
        if url.startswith(db.GITHUB_API_BASE + "/tags"):
            return httpx.Response(200, json=_TAGS_FIXTURE)
        # Contents listings
        if "/contents/mojo/docs/manual?" in url:
            return httpx.Response(200, json=_MANUAL_LISTING_ROOT)
        if "/contents/mojo/docs/manual/lifecycle?" in url:
            return httpx.Response(200, json=_MANUAL_LISTING_LIFECYCLE)
        # Other surfaces — return empty for this test
        if "/contents/mojo/docs/" in url and "?" in url:
            return httpx.Response(200, json=[])
        # Raw file fetches
        if url in _MANUAL_FILES:
            return httpx.Response(200, text=_MANUAL_FILES[url])
        return httpx.Response(404)

    def _factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            headers=db.github_headers(),
            timeout=30,
        )
    monkeypatch.setattr(db, "build_github_client", _factory)


@pytest.mark.asyncio
class TestFetchHandwrittenManual:
    async def test_toc_when_no_topic(self, monkeypatch, clean_caches):
        _install_mock(monkeypatch)
        out = await docs_mod.fetch_handwritten("manual")
        assert "basics.md" in out
        assert "lifecycle/death.md" in out
        assert "ref:" in out  # ref appears in heading (whatever it resolved to)

    async def test_topic_returns_page_content(self, monkeypatch, clean_caches):
        _install_mock(monkeypatch)
        out = await docs_mod.fetch_handwritten("manual", topic="basics")
        assert "Mojo basics content." in out

    async def test_topic_with_subpath(self, monkeypatch, clean_caches):
        _install_mock(monkeypatch)
        out = await docs_mod.fetch_handwritten("manual", topic="lifecycle/death")
        assert "Value destruction." in out

    async def test_unknown_topic_returns_helpful_error(self, monkeypatch, clean_caches):
        _install_mock(monkeypatch)
        out = await docs_mod.fetch_handwritten("manual", topic="does-not-exist")
        assert "does-not-exist" in out
        assert "with no topic" in out.lower() or "list them" in out.lower()

    async def test_version_pinning_resolves_ref(self, monkeypatch, clean_caches):
        _install_mock(monkeypatch)
        out = await docs_mod.fetch_handwritten("manual", mojo_version="0.26.2")
        assert "modular/v26.2.0" in out  # resolved tag


class TestUnknownSurface:
    @pytest.mark.asyncio
    async def test_returns_error_for_unknown_surface(self, monkeypatch, clean_caches):
        out = await docs_mod.fetch_handwritten("bogus")
        assert "unknown" in out.lower()


@pytest.mark.asyncio
class TestHandwrittenCache:
    async def test_second_call_uses_cache(self, monkeypatch, clean_caches):
        captured: list[httpx.Request] = []
        _install_mock(monkeypatch, captured)
        # First call hits the network
        out1 = await docs_mod.fetch_handwritten("manual", topic="basics")
        n_after_first = len(captured)
        assert n_after_first > 0
        # Second call should hit the cache only
        out2 = await docs_mod.fetch_handwritten("manual", topic="basics")
        assert out1 == out2
        # No new HTTP requests should have been issued
        assert len(captured) == n_after_first
