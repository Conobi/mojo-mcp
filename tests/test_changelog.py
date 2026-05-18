"""Tests for the GitHub-backed changelog backend (spec: 2026-05-17)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from mojo_mcp import docs as docs_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_changelog_cache(tmp_path, monkeypatch):
    cache = tmp_path / "changelog.json"
    monkeypatch.setattr(docs_mod, "CHANGELOG_CACHE_PATH", cache)
    return cache


@pytest.fixture
def clean_github_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)


# Sample listing API response shape (trimmed).
_LISTING = [
    {
        "name": "v0.26.2.md",
        "type": "file",
        "sha": "abc123",
        "download_url": "https://raw.example/releases/v0.26.2.md",
    },
    {
        "name": "v0.26.1.md",
        "type": "file",
        "sha": "def456",
        "download_url": "https://raw.example/releases/v0.26.1.md",
    },
    {
        "name": "v1.0.0b1.md",
        "type": "file",
        "sha": "beta789",
        "download_url": "https://raw.example/releases/v1.0.0b1.md",
    },
    {
        "name": "2023-08.md",
        "type": "file",
        "sha": "old001",
        "download_url": "https://raw.example/releases/2023-08.md",
    },
    {
        "name": "v0.4.0-mac.md",
        "type": "file",
        "sha": "mac002",
        "download_url": "https://raw.example/releases/v0.4.0-mac.md",
    },
    {
        "name": "subdir",
        "type": "dir",
    },
]


def _release_body(title: str) -> str:
    return f"""---
title: Mojo {title}
version: {title.lstrip("v")}
date: 2026-03-19
---

## Highlights

- Did a thing in {title}.
"""


_NIGHTLY_BODY = """---
title: Mojo nightly
---

## Highlights

- Nightly bullet.
"""


def _install_mock_transport(monkeypatch, *, captured: list[httpx.Request] | None = None,
                            empty_listing: bool = False):
    """Patch docs_mod._build_changelog_http_client to use a MockTransport."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        url = str(request.url)
        if url == docs_mod.GITHUB_RELEASES_LISTING_URL:
            payload = [] if empty_listing else _LISTING
            return httpx.Response(200, json=payload)
        if url == docs_mod.GITHUB_NIGHTLY_RAW_URL:
            if empty_listing:
                # Simulate full GitHub outage: listing empty AND nightly missing
                return httpx.Response(404)
            return httpx.Response(200, text=_NIGHTLY_BODY)
        # Per-release raw fetches: match by filename suffix.
        for entry in _LISTING:
            if entry.get("type") != "file":
                continue
            if url.endswith("/" + entry["name"]):
                title = entry["name"].removesuffix(".md")
                return httpx.Response(200, text=_release_body(title))
        return httpx.Response(404)

    def _factory():
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            headers=docs_mod._build_changelog_headers(),
            timeout=30,
        )

    monkeypatch.setattr(docs_mod, "_build_changelog_http_client", _factory)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestStripFrontmatter:
    def test_strips_yaml_block_at_start(self):
        md = "---\ntitle: Mojo v0.26.2\nversion: 0.26.2.0\n---\n\n## Highlights\n\n- thing\n"
        front, body = docs_mod._strip_frontmatter(md)
        assert front == {"title": "Mojo v0.26.2", "version": "0.26.2.0"}
        assert body.startswith("## Highlights")

    def test_no_frontmatter_returns_body_unchanged(self):
        md = "## Highlights\n\n- thing\n"
        front, body = docs_mod._strip_frontmatter(md)
        assert front == {}
        assert body == md

    def test_handles_empty_string(self):
        front, body = docs_mod._strip_frontmatter("")
        assert front == {}
        assert body == ""


class TestVersionKeyFromFilename:
    @pytest.mark.parametrize("filename,expected", [
        ("v0.26.2.md", "v0.26.2"),
        ("v1.0.0b1.md", "v1.0.0b1"),
        ("nightly-changelog.md", "nightly"),
        ("v0.4.0-mac.md", "v0.4.0-mac"),
        ("2023-08.md", "2023-08"),
    ])
    def test_maps_filename_to_version_key(self, filename, expected):
        assert docs_mod._version_key_from_filename(filename) == expected


class TestIsVersionedRelease:
    @pytest.mark.parametrize("key,expected", [
        ("v0.26.2", True),
        ("v1.0.0b1", True),
        ("v0.4.0", True),
        ("nightly", False),
        ("2023-08", False),
        ("v0.4.0-mac", False),
        ("", False),
    ])
    def test_classifies_keys(self, key, expected):
        assert docs_mod._is_versioned_release(key) is expected


class TestVersionSortKey:
    def test_orders_descending_by_semver(self):
        keys = ["v0.26.1", "v1.0.0b1", "v0.26.2"]
        keys.sort(key=docs_mod._version_sort_key, reverse=True)
        assert keys == ["v1.0.0b1", "v0.26.2", "v0.26.1"]

    def test_stable_beats_beta_at_same_xyz(self):
        # If a hypothetical v1.0.0 existed, it must outrank v1.0.0b1.
        keys = ["v1.0.0b1", "v1.0.0"]
        keys.sort(key=docs_mod._version_sort_key, reverse=True)
        assert keys == ["v1.0.0", "v1.0.0b1"]


# ---------------------------------------------------------------------------
# GitHub auth
# ---------------------------------------------------------------------------


class TestGithubAuthHeader:
    def test_returns_empty_when_no_token(self, clean_github_env):
        assert docs_mod._github_auth_header() == {}

    def test_uses_github_token(self, clean_github_env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok_a")
        assert docs_mod._github_auth_header() == {"Authorization": "Bearer tok_a"}

    def test_falls_back_to_personal_access_token(self, clean_github_env, monkeypatch):
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "tok_b")
        assert docs_mod._github_auth_header() == {"Authorization": "Bearer tok_b"}

    def test_github_token_wins_over_personal(self, clean_github_env, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok_a")
        monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "tok_b")
        assert docs_mod._github_auth_header() == {"Authorization": "Bearer tok_a"}


# ---------------------------------------------------------------------------
# Cache schema v3
# ---------------------------------------------------------------------------


class TestChangelogCacheSchemaV3:
    def test_load_returns_none_for_legacy_schema(self, tmp_changelog_cache, caplog):
        tmp_changelog_cache.write_text(json.dumps({
            "_fetched_at": time.time(),
            "v0.26.1": {"heading": "v0.26.1", "markdown": "old"},
        }))
        with caplog.at_level("INFO", logger="mojo_mcp.docs"):
            assert docs_mod._load_changelog_cache() is None
        assert any("rebuilding changelog cache" in r.message for r in caplog.records)

    def test_load_returns_none_when_expired(self, tmp_changelog_cache):
        tmp_changelog_cache.write_text(json.dumps({
            "_schema_version": docs_mod.CHANGELOG_CACHE_SCHEMA_VERSION,
            "_fetched_at": time.time() - docs_mod.CHANGELOG_CACHE_TTL - 1,
            "v0.26.2": {"heading": "Mojo v0.26.2", "markdown": "fresh"},
        }))
        assert docs_mod._load_changelog_cache() is None

    def test_load_returns_data_when_fresh(self, tmp_changelog_cache):
        payload = {
            "_schema_version": docs_mod.CHANGELOG_CACHE_SCHEMA_VERSION,
            "_fetched_at": time.time(),
            "v0.26.2": {"heading": "Mojo v0.26.2", "markdown": "fresh"},
        }
        tmp_changelog_cache.write_text(json.dumps(payload))
        loaded = docs_mod._load_changelog_cache()
        assert loaded is not None
        assert loaded["v0.26.2"]["markdown"] == "fresh"

    def test_save_writes_schema_version(self, tmp_changelog_cache):
        docs_mod._save_changelog_cache({
            "_schema_version": docs_mod.CHANGELOG_CACHE_SCHEMA_VERSION,
            "_fetched_at": time.time(),
            "v0.26.2": {"heading": "Mojo v0.26.2", "markdown": "x"},
        })
        on_disk = json.loads(tmp_changelog_cache.read_text())
        assert on_disk["_schema_version"] == docs_mod.CHANGELOG_CACHE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# End-to-end via httpx.MockTransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFetchAndParseChangelog:
    async def test_returns_version_keys_with_markdown(self, monkeypatch, clean_github_env):
        _install_mock_transport(monkeypatch)
        data = await docs_mod._fetch_and_parse_changelog()
        # Expect: every file from _LISTING + nightly + metadata keys
        for key in ("v0.26.2", "v0.26.1", "v1.0.0b1", "2023-08", "v0.4.0-mac", "nightly"):
            assert key in data, f"missing {key}"
            assert "markdown" in data[key]
            assert data[key]["markdown"].startswith("## Highlights") or data[key]["markdown"].lstrip().startswith("## Highlights")
        assert data["_schema_version"] == docs_mod.CHANGELOG_CACHE_SCHEMA_VERSION

    async def test_skips_directories_in_listing(self, monkeypatch, clean_github_env):
        _install_mock_transport(monkeypatch)
        data = await docs_mod._fetch_and_parse_changelog()
        assert "subdir" not in data

    async def test_heading_comes_from_frontmatter_title(self, monkeypatch, clean_github_env):
        _install_mock_transport(monkeypatch)
        data = await docs_mod._fetch_and_parse_changelog()
        assert data["v0.26.2"]["heading"] == "Mojo v0.26.2"
        assert data["nightly"]["heading"] == "Mojo nightly"

    async def test_includes_bearer_header_when_token_set(self, monkeypatch, clean_github_env):
        monkeypatch.setenv("GITHUB_TOKEN", "secret_token")
        captured: list[httpx.Request] = []
        _install_mock_transport(monkeypatch, captured=captured)
        await docs_mod._fetch_and_parse_changelog()
        assert captured, "no requests were captured"
        for req in captured:
            assert req.headers.get("authorization") == "Bearer secret_token"

    async def test_no_bearer_header_when_no_token(self, monkeypatch, clean_github_env):
        captured: list[httpx.Request] = []
        _install_mock_transport(monkeypatch, captured=captured)
        await docs_mod._fetch_and_parse_changelog()
        assert captured
        for req in captured:
            assert "authorization" not in {k.lower() for k in req.headers.keys()}


@pytest.mark.asyncio
class TestFetchChangelogEmptyParseGuard:
    async def test_empty_listing_does_not_write_cache(
        self, monkeypatch, clean_github_env, tmp_changelog_cache,
    ):
        _install_mock_transport(monkeypatch, empty_listing=True)
        result = await docs_mod.fetch_changelog(None)
        assert "no versions" in result.lower() or "unreachable" in result.lower() or "no entries" in result.lower()
        assert not tmp_changelog_cache.exists(), "cache must NOT be written on empty parse"


@pytest.mark.asyncio
class TestFetchChangelogIntegration:
    async def test_latest_returns_top_two_versioned_stable_releases(
        self, monkeypatch, clean_github_env, tmp_changelog_cache,
    ):
        _install_mock_transport(monkeypatch)
        out = await docs_mod.fetch_changelog(None)
        # Latest two by semver desc — among the versioned set:
        #   v1.0.0b1, v0.26.2, v0.26.1, v0.4.0(-mac excluded)
        # Top two: v1.0.0b1, v0.26.2.
        assert "v1.0.0b1" in out
        assert "v0.26.2" in out
        assert "v0.26.1" not in out  # only top 2
        assert "2023-08" not in out  # monthly drop excluded
        assert "v0.4.0-mac" not in out  # oddball excluded

    async def test_explicit_version_returns_single_entry(
        self, monkeypatch, clean_github_env, tmp_changelog_cache,
    ):
        _install_mock_transport(monkeypatch)
        out = await docs_mod.fetch_changelog("v0.26.1")
        assert "v0.26.1" in out
        assert "v0.26.2" not in out

    async def test_nightly_keyword_returns_nightly(
        self, monkeypatch, clean_github_env, tmp_changelog_cache,
    ):
        _install_mock_transport(monkeypatch)
        out = await docs_mod.fetch_changelog("nightly")
        assert "Nightly bullet" in out
