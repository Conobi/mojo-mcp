"""Tests for shared docs backend helpers (auth, version resolution, llms.txt)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from mojo_mcp import docs_backend as db


@pytest.fixture
def clean_github_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)


@pytest.fixture
def tmp_tags_cache(tmp_path, monkeypatch):
    cache = tmp_path / "tags.json"
    monkeypatch.setattr(db, "TAGS_CACHE_PATH", cache)
    return cache


# ---------------------------------------------------------------------------
# Auth header
# ---------------------------------------------------------------------------


class TestGithubAuthHeader:
    def test_empty_when_no_token(self, clean_github_env):
        assert db.github_auth_header() == {}

    def test_uses_github_token(self, clean_github_env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok_a")
        assert db.github_auth_header() == {"Authorization": "Bearer tok_a"}

    def test_falls_back_to_personal_access_token(self, clean_github_env, monkeypatch):
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "tok_b")
        assert db.github_auth_header() == {"Authorization": "Bearer tok_b"}

    def test_github_token_wins(self, clean_github_env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok_a")
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "tok_b")
        assert db.github_auth_header() == {"Authorization": "Bearer tok_a"}


# ---------------------------------------------------------------------------
# Frontmatter + mojolang preamble
# ---------------------------------------------------------------------------


class TestStripFrontmatter:
    def test_strips_block(self):
        md = "---\ntitle: Hi\nversion: 1.0\n---\n\n## Body\n"
        front, body = db.strip_frontmatter(md)
        assert front == {"title": "Hi", "version": "1.0"}
        assert body.startswith("## Body")

    def test_no_frontmatter_passes_through(self):
        md = "## Hello\n"
        front, body = db.strip_frontmatter(md)
        assert front == {}
        assert body == md


class TestStripMojolangPreamble:
    def test_strips_two_line_blockquote_header(self):
        md = (
            "> For the complete Mojo documentation index, see [llms.txt](/llms.txt).\n"
            "> Markdown versions of all pages are available by appending .md to any URL.\n"
            "\n"
            "# Dict\n\nReal content.\n"
        )
        out = db.strip_mojolang_preamble(md)
        assert out.startswith("# Dict")
        assert "llms.txt" not in out

    def test_idempotent_when_no_preamble(self):
        md = "# Already clean\n\nContent.\n"
        assert db.strip_mojolang_preamble(md) == md

    def test_leaves_other_blockquotes_alone(self):
        md = "# Title\n\n> A real blockquote, not a preamble.\n"
        assert db.strip_mojolang_preamble(md) == md


# ---------------------------------------------------------------------------
# llms.txt parsing
# ---------------------------------------------------------------------------


class TestParseLlmsTxt:
    def test_parses_standard_entries(self):
        text = (
            "# Title\n"
            "\n"
            "## Table of Contents\n"
            "\n"
            "- [foo](https://example/foo.md): A foo function.\n"
            "- [Bar](https://example/Bar.md): A struct.\n"
        )
        entries = db.parse_llms_txt(text)
        assert len(entries) == 2
        assert entries[0] == {
            "name": "foo",
            "url": "https://example/foo.md",
            "description": "A foo function.",
        }
        assert entries[1]["name"] == "Bar"

    def test_skips_headers_and_blanks(self):
        text = "# H1\n\n## H2\n\n- [a](u): d\n\n"
        assert len(db.parse_llms_txt(text)) == 1

    def test_handles_missing_description(self):
        # Some entries may lack a description (no `: ...`)
        text = "- [a](https://example/a.md)\n- [b](https://example/b.md): with desc\n"
        entries = db.parse_llms_txt(text)
        assert len(entries) == 2
        assert entries[0]["description"] == ""
        assert entries[1]["description"] == "with desc"


# ---------------------------------------------------------------------------
# Version normalization + candidate tags
# ---------------------------------------------------------------------------


class TestNormalizeMojoVersion:
    @pytest.mark.parametrize("inp,expected", [
        ("0.26.2.0", (26, 2, 0, None)),
        ("v0.26.2", (26, 2, None, None)),
        ("26.2", (26, 2, None, None)),
        ("v26.2.1", (26, 2, 1, None)),
        ("mojo 0.26.2.0.dev2026031905 (compatible with v26.2)", (26, 2, None, None)),
        ("1.0.0b1", (1, 0, 0, "b1")),
        ("v1.0.0b1", (1, 0, 0, "b1")),
    ])
    def test_extracts_version_components(self, inp, expected):
        v = db.normalize_mojo_version(inp)
        assert v is not None
        assert (v.major, v.minor, v.patch, v.pre) == expected

    def test_returns_none_on_garbage(self):
        assert db.normalize_mojo_version("") is None
        assert db.normalize_mojo_version("not a version") is None

    def test_returns_none_on_none(self):
        assert db.normalize_mojo_version(None) is None  # type: ignore[arg-type]


class TestCandidateTags:
    def test_with_patch_includes_both_exact_and_zero(self):
        v = db.MojoVersion(major=25, minor=6, patch=1, pre=None)
        cands = db.candidate_tags(v)
        # Exact patch first
        assert cands[0] == "modular/v25.6.1"
        assert cands[1] == "max/v25.6.1"
        assert cands[2] == "mojo/v25.6.1"
        # Then patch=0 fallback
        assert "modular/v25.6.0" in cands
        assert cands.index("modular/v25.6.0") > cands.index("modular/v25.6.1")

    def test_no_patch_defaults_to_zero(self):
        v = db.MojoVersion(major=26, minor=2, patch=None, pre=None)
        cands = db.candidate_tags(v)
        assert cands[0] == "modular/v26.2.0"
        assert cands[1] == "max/v26.2.0"
        assert cands[2] == "mojo/v26.2.0"

    def test_preserves_prerelease_suffix(self):
        v = db.MojoVersion(major=1, minor=0, patch=0, pre="b1")
        cands = db.candidate_tags(v)
        assert "mojo/v1.0.0b1" in cands
        assert "modular/v1.0.0b1" in cands


# ---------------------------------------------------------------------------
# Tag list caching + ref resolution
# ---------------------------------------------------------------------------


_TAGS_FIXTURE = [
    "modular/v26.2.0",
    "modular/v26.1.0",
    "modular/v25.7.0",
    "modular/v25.6.1",
    "modular/v25.6.0",
    "modular/v25.5.0",
    "modular/v25.4.0",
    "modular/v25.3.0",
    "max/v26.3.0",
    "max/v25.2.0",
    "max/v25.1.0",
    "max/v24.6.0",
    "mojo/v24.5.0",
    "mojo/v1.0.0b1",
    "stable",
    "SAFE-DOCS-65-indexing",
]


def _install_tags_mock(monkeypatch, captured=None, *, payload=None):
    def _handler(request):
        if captured is not None:
            captured.append(request)
        if str(request.url).startswith(db.GITHUB_API_BASE + "/tags"):
            data = payload if payload is not None else [
                {"name": t} for t in _TAGS_FIXTURE
            ]
            return httpx.Response(200, json=data)
        return httpx.Response(404)

    def _factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            headers=db.github_headers(),
            timeout=30,
        )

    monkeypatch.setattr(db, "build_github_client", _factory)


@pytest.mark.asyncio
class TestListModularTags:
    async def test_fetches_and_caches(self, monkeypatch, tmp_tags_cache, clean_github_env):
        captured: list[httpx.Request] = []
        _install_tags_mock(monkeypatch, captured)
        tags = await db.list_modular_tags()
        assert "modular/v26.2.0" in tags
        assert "mojo/v1.0.0b1" in tags
        # Cache should be written
        assert tmp_tags_cache.exists()
        # Second call should not hit network
        before = len(captured)
        tags2 = await db.list_modular_tags()
        assert tags2 == tags
        assert len(captured) == before

    async def test_refetches_when_cache_stale(self, monkeypatch, tmp_tags_cache, clean_github_env):
        # Write a stale cache
        tmp_tags_cache.write_text(json.dumps({
            "fetched_at": time.time() - db.TAGS_CACHE_TTL - 1,
            "tags": ["modular/v999.0.0"],
        }))
        captured: list[httpx.Request] = []
        _install_tags_mock(monkeypatch, captured)
        tags = await db.list_modular_tags()
        # Should have refetched and replaced the stale fixture
        assert "modular/v999.0.0" not in tags
        assert "modular/v26.2.0" in tags
        assert len(captured) >= 1


@pytest.mark.asyncio
class TestResolveMojoRef:
    async def test_picks_modular_tag(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("0.26.2.0")
        assert ref == "modular/v26.2.0"

    async def test_picks_max_tag_when_modular_missing(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("0.24.6")
        assert ref == "max/v24.6.0"

    async def test_picks_mojo_tag_for_oldest(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("0.24.5")
        assert ref == "mojo/v24.5.0"

    async def test_picks_prerelease_tag(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("1.0.0b1")
        assert ref == "mojo/v1.0.0b1"

    async def test_prefers_exact_patch(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("0.25.6.1")
        assert ref == "modular/v25.6.1"

    async def test_falls_back_to_patch_zero(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        # 25.7.5 doesn't exist as a tag, but 25.7.0 does
        ref = await db.resolve_mojo_ref("0.25.7.5")
        assert ref == "modular/v25.7.0"

    async def test_returns_main_for_none(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref(None)
        assert ref == "main"

    async def test_returns_main_for_garbage(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("not a version")
        assert ref == "main"

    async def test_returns_main_for_unknown_version(self, monkeypatch, tmp_tags_cache, clean_github_env):
        _install_tags_mock(monkeypatch)
        ref = await db.resolve_mojo_ref("0.99.0")
        assert ref == "main"
