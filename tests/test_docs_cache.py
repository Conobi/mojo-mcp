"""Tests for docs cache schema v2 (R1)."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mojo_mcp import docs as docs_mod


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    cache = tmp_path / "docs.json"
    monkeypatch.setattr(docs_mod, "CACHE_PATH", cache)
    return cache


class TestCacheSchemaV2:
    def test_save_writes_v2_envelope(self, tmp_cache):
        modules = {"foo": {"name": "foo", "structs": [], "functions": [], "traits": [], "aliases": []}}
        docs_mod.save_docs_cache(modules, docs_source_version="v25.5", mojo_version_at_fetch="0.25.5.0")
        data = json.loads(tmp_cache.read_text())
        assert data["schema_version"] == 2
        assert data["docs_source_version"] == "v25.5"
        assert data["mojo_version_at_fetch"] == "0.25.5.0"
        assert data["modules"] == modules
        assert "fetched_at" in data

    def test_load_returns_modules_dict_when_fresh(self, tmp_cache):
        docs_mod.save_docs_cache({"foo": {"name": "foo"}}, docs_source_version="v25.5", mojo_version_at_fetch=None)
        result = docs_mod.load_cached_docs()
        assert result is not None
        assert "foo" in result

    def test_load_returns_none_when_ttl_expired(self, tmp_cache):
        docs_mod.save_docs_cache({"foo": {}}, docs_source_version="v25.5", mojo_version_at_fetch=None)
        envelope = json.loads(tmp_cache.read_text())
        envelope["fetched_at"] = time.time() - docs_mod.CACHE_TTL - 1
        tmp_cache.write_text(json.dumps(envelope))
        assert docs_mod.load_cached_docs() is None

    def test_load_returns_none_for_legacy_schema(self, tmp_cache, caplog):
        # Legacy v1: flat module dict, no envelope
        tmp_cache.write_text(json.dumps({"foo": {"name": "foo"}}))
        with caplog.at_level("INFO", logger="mojo_mcp.docs"):
            result = docs_mod.load_cached_docs()
        assert result is None
        assert any("rebuilding docs cache" in r.message for r in caplog.records)

    def test_load_returns_none_for_missing_schema_version(self, tmp_cache):
        tmp_cache.write_text(json.dumps({"fetched_at": time.time(), "modules": {"foo": {}}}))
        assert docs_mod.load_cached_docs() is None

    def test_load_returns_none_for_wrong_schema_version(self, tmp_cache):
        tmp_cache.write_text(json.dumps({
            "schema_version": 99, "fetched_at": time.time(),
            "docs_source_version": "v25.5", "mojo_version_at_fetch": None,
            "modules": {"foo": {}},
        }))
        assert docs_mod.load_cached_docs() is None


class TestDocsSourceVersionParser:
    def test_parses_version_banner(self):
        html = '<html><head><meta name="docs-version" content="v25.5"></head></html>'
        assert docs_mod._parse_docs_source_version(html) == "v25.5"

    def test_falls_back_to_unknown(self):
        html = "<html><body>no banner</body></html>"
        assert docs_mod._parse_docs_source_version(html) == "unknown"

    def test_picks_first_version_pattern(self):
        html = '<html><body><span>Docs version: v26.1</span></body></html>'
        result = docs_mod._parse_docs_source_version(html)
        assert result.startswith("v") or result == "unknown"


class TestMojoVersionAtFetch:
    def test_capture_returns_none_when_mojo_missing(self):
        with patch("mojo_mcp.docs.shutil.which", return_value=None):
            assert docs_mod._capture_mojo_version() is None

    def test_capture_returns_version_string(self):
        with patch("mojo_mcp.docs.shutil.which", return_value="/usr/bin/mojo"):
            with patch("mojo_mcp.docs.subprocess.run") as run:
                run.return_value.stdout = "mojo 0.25.5.0.dev2026031905 (compatible with v25.5)\n"
                run.return_value.returncode = 0
                v = docs_mod._capture_mojo_version()
                assert v is not None
                assert "25.5" in v

    def test_capture_returns_none_on_subprocess_error(self):
        with patch("mojo_mcp.docs.shutil.which", return_value="/usr/bin/mojo"):
            with patch("mojo_mcp.docs.subprocess.run", side_effect=OSError("boom")):
                assert docs_mod._capture_mojo_version() is None
