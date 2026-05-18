"""Mojo docs surfaces: stdlib search/lookup, changelog, manual/reference/cli.

The data sources are split per content kind, see `docs_backend.py` for shared
HTTP/auth helpers and version resolution:

- **Stdlib reference** (`search` + `lookup`): mojolang.org `llms-stdlib.txt`
  index + per-page `.md`, with version-pinned `.mojo` source from
  `modular/modular@<tag>` as the primary `lookup` payload (parsed by
  `mojo_source.extract_symbol`). The mojolang.org `.md` is used as the fallback
  when source lookup is not available.
- **Handwritten docs** (`changelog`, `manual`, `reference`, `cli`): raw markdown
  from `modular/modular@<tag>/mojo/docs/...`, version-pinned to the user's
  installed Mojo when available.

No HTML scraping anywhere — `lxml`/`beautifulsoup4` are no longer used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from . import docs_backend as db
from . import mojo_source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stdlib index — cache shape, IO, build
# ---------------------------------------------------------------------------

CACHE_PATH = Path.home() / ".cache" / "mojo-mcp" / "docs.json"
CACHE_TTL = 1209600  # 14 days
CACHE_SCHEMA_VERSION = 3

LLMS_STDLIB_URL = db.MOJOLANG_BASE + "/llms-stdlib.txt"


def _capture_mojo_version() -> str | None:
    """Best-effort `mojo --version` capture. Returns None on any failure."""
    binary = shutil.which("mojo")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    line = (result.stdout or "").splitlines()
    return line[0].strip() if line else None


def get_cached_mojo_version() -> str | None:
    """Return the mojo_version_at_fetch stored in the docs cache envelope, or None."""
    if not CACHE_PATH.exists():
        return None
    try:
        envelope = json.loads(CACHE_PATH.read_text())
    except Exception:
        return None
    if not isinstance(envelope, dict) or envelope.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    return envelope.get("mojo_version_at_fetch")


def load_cached_docs() -> dict | None:
    """Return cached module dict if envelope is valid and TTL is fresh."""
    if not CACHE_PATH.exists():
        return None
    try:
        envelope = json.loads(CACHE_PATH.read_text())
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("schema_version") != CACHE_SCHEMA_VERSION:
        logger.info(
            "rebuilding docs cache: schema_version=%r -> %d",
            envelope.get("schema_version"), CACHE_SCHEMA_VERSION,
        )
        return None
    fetched_at = envelope.get("fetched_at", 0)
    if time.time() - fetched_at > CACHE_TTL:
        return None
    modules = envelope.get("modules")
    return modules if isinstance(modules, dict) else None


def save_docs_cache(
    modules: dict,
    *,
    mojo_version_at_fetch: str | None = None,
) -> None:
    """Write the v3 envelope wrapping `modules`."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "fetched_at": time.time(),
        "mojo_version_at_fetch": mojo_version_at_fetch,
        "modules": modules,
    }
    CACHE_PATH.write_text(json.dumps(envelope, indent=2))


def _empty_module(name: str) -> dict:
    return {
        "name": name,
        "url": "",
        "description": "",
        "structs": [],
        "functions": [],
        "traits": [],
        "aliases": [],
    }


def _bucket_for_name(name: str) -> str:
    """Heuristic kind classifier for the shallow stdlib index.

    Without parsing source, we can only distinguish by naming convention:
    - All-caps with underscores → alias (a constant)
    - PascalCase (starts with uppercase) → struct (could also be trait; the
      true kind is resolved at `lookup` time via `mojo_source.extract_symbol`)
    - Anything else → function
    """
    if not name:
        return "functions"
    if name.isupper() and ("_" in name or name.isalpha()) and len(name) > 1:
        return "aliases"
    first = name[0]
    if first.isupper():
        return "structs"
    return "functions"


def _path_from_llms_url(url: str) -> str | None:
    """Extract the `<path>` portion from `https://mojolang.org/docs/std/<path>.md`."""
    marker = "/docs/std/"
    if marker not in url:
        return None
    path = url.split(marker, 1)[1]
    return path.removesuffix(".md")


def _llms_entries_to_docs(entries: list[dict]) -> dict[str, dict]:
    """Convert parsed llms.txt entries into the legacy module-keyed `docs` dict."""
    # First pass: collect all paths to identify which are directories
    parsed: list[tuple[str, dict]] = []
    for e in entries:
        path = _path_from_llms_url(e["url"])
        if path:
            parsed.append((path, e))
    dir_paths: set[str] = set()
    for path, _ in parsed:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dir_paths.add("/".join(parts[:i]))

    docs: dict[str, dict] = {}
    for path, entry in parsed:
        parts = path.split("/")
        name = entry["name"]
        is_module = path in dir_paths
        if is_module:
            module_name = ".".join(parts)
            mod = docs.setdefault(module_name, _empty_module(module_name))
            if not mod["description"]:
                mod["description"] = entry["description"]
            if not mod["url"]:
                mod["url"] = entry["url"]
        else:
            if len(parts) < 2:
                # Top-level leaf (no parent module); attach to the symbol's own
                # name as a degenerate module so we don't drop it.
                module_name = parts[0]
            else:
                module_name = ".".join(parts[:-1])
            mod = docs.setdefault(module_name, _empty_module(module_name))
            bucket = _bucket_for_name(name)
            mod[bucket].append({
                "name": name,
                "signature": name,
                "description": entry["description"],
            })
    return docs


async def fetch_stdlib_index() -> dict:
    """Fetch and parse `llms-stdlib.txt` into the module-keyed docs dict."""
    logger.info("Fetching Mojo stdlib index from %s", LLMS_STDLIB_URL)
    async with db.build_mojolang_client() as client:
        resp = await client.get(LLMS_STDLIB_URL)
        resp.raise_for_status()
    entries = db.parse_llms_txt(resp.text)
    return _llms_entries_to_docs(entries)


async def get_docs() -> dict:
    """Return docs from cache if fresh, otherwise refetch and persist."""
    cached = load_cached_docs()
    if cached:
        logger.info("Loaded Mojo stdlib docs from cache (%d modules)", len(cached))
        return cached
    mojo_version = _capture_mojo_version()
    docs = await fetch_stdlib_index()
    save_docs_cache(docs, mojo_version_at_fetch=mojo_version)
    logger.info("Indexed and cached %d Mojo stdlib modules", len(docs))
    return docs


# ---------------------------------------------------------------------------
# Stdlib lookup
# ---------------------------------------------------------------------------

_SYMBOL_SEG_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _split_symbol_query(query: str) -> list[str]:
    """Validate and return the dot-separated segments of a symbol query."""
    if not query:
        raise ValueError("Empty symbol query")
    parts = [p for p in query.split(".") if p]
    if parts and parts[0].lower() == "std":
        parts = parts[1:]
    if len(parts) < 2:
        raise ValueError(
            f"Need at least 2 components (module.Symbol), got: {query!r}. "
            "Example: 'collections.dict.Dict' or 'builtin.int.Int'"
        )
    for seg in parts:
        if not _SYMBOL_SEG_RE.match(seg):
            raise ValueError(f"Invalid segment {seg!r} in query {query!r}")
    return parts


def _candidate_source_paths(parts: list[str]) -> list[str]:
    """Possible `.mojo` file paths inside `mojo/stdlib/std/` for a query."""
    # E.g. ["collections", "dict", "Dict"] → try:
    #   - mojo/stdlib/std/collections/dict.mojo (symbol Dict lives in this file)
    #   - mojo/stdlib/std/collections/dict/Dict.mojo (symbol has its own file)
    cands: list[str] = []
    if len(parts) >= 2:
        cands.append("mojo/stdlib/std/" + "/".join(parts[:-1]) + ".mojo")
        cands.append("mojo/stdlib/std/" + "/".join(parts) + ".mojo")
    return cands


def _render_extracted_markdown(name: str, extracted: dict) -> str:
    """Render `mojo_source.extract_symbol` output as Markdown."""
    lines: list[str] = [f"# {name}", ""]
    for i, decl in enumerate(extracted["declarations"]):
        if len(extracted["declarations"]) > 1:
            lines.append(f"## Overload {i + 1}")
            lines.append("")
        if decl["decorators"]:
            lines.append("```mojo")
            lines.extend(decl["decorators"])
            lines.append(decl["signature"])
            lines.append("```")
        else:
            lines += ["```mojo", decl["signature"], "```"]
        lines.append("")
        if decl["docstring"]:
            lines.append(decl["docstring"])
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


async def _fetch_mojolang_md(parts: list[str]) -> str | None:
    """Fetch the mojolang.org `.md` page for `<path>` (joined from parts)."""
    url = db.MOJOLANG_BASE + "/docs/std/" + "/".join(parts) + ".md"
    async with db.build_mojolang_client() as client:
        try:
            resp = await client.get(url)
        except Exception as e:
            logger.warning("Failed mojolang fetch %s: %s", url, e)
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return db.strip_mojolang_preamble(resp.text)


async def fetch_symbol_page(
    query: str,
    mojo_version: str | None = None,
) -> str:
    """Fetch full Mojo symbol documentation as Markdown.

    Strategy: pull the matching `.mojo` source from `modular/modular@<ref>`
    and parse it with `mojo_source.extract_symbol`. If the source path can't
    be resolved (e.g. the symbol moved between versions), fall back to the
    mojolang.org `.md` page.
    """
    try:
        parts = _split_symbol_query(query)
    except ValueError as e:
        return f"Error: {e}"

    symbol_name = parts[-1]
    ref = await db.resolve_mojo_ref(
        mojo_version or _capture_mojo_version() or get_cached_mojo_version()
    )
    candidate_paths = _candidate_source_paths(parts)

    async with db.build_github_client() as client:
        for src_path in candidate_paths:
            url = f"{db.GITHUB_RAW_BASE}/{ref}/{src_path}"
            try:
                resp = await client.get(url)
            except Exception as e:
                logger.warning("Failed source fetch %s: %s", url, e)
                continue
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            extracted = mojo_source.extract_symbol(resp.text, symbol_name)
            if extracted:
                rendered = _render_extracted_markdown(symbol_name, extracted)
                return (
                    rendered.rstrip()
                    + f"\n\n_Source: `{src_path}` @ `{ref}`_\n"
                )
            # Source file exists but symbol wasn't found there — try next candidate
        # All source-paths failed: fall back to mojolang.org rendered page
        md = await _fetch_mojolang_md(parts)
        if md is not None:
            return md
    return (
        f"Symbol not found at any of:\n"
        + "\n".join(f"- {p}" for p in candidate_paths)
        + f"\n\nTried ref `{ref}` and the mojolang.org rendered page.\n"
        "Hint: symbol names are PascalCase, module names lowercase "
        "(e.g. 'collections.dict.Dict', 'builtin.int.Int')."
    )


# ---------------------------------------------------------------------------
# Manual / reference / cli — handwritten GitHub-sourced docs
# ---------------------------------------------------------------------------

# Directory paths inside modular/modular for each handwritten surface
_HANDWRITTEN_SURFACES = {
    "manual": "mojo/docs/manual",
    "reference": "mojo/docs/reference",
    "cli": "mojo/docs/tools",
}

# Cache filename pattern: <surface>-<sanitized_ref>.json
_HANDWRITTEN_CACHE_TTL = 604800  # 7 days
_HANDWRITTEN_SCHEMA_VERSION = 1


def _handwritten_cache_path(surface: str, ref: str) -> Path:
    safe_ref = ref.replace("/", "_")
    return Path.home() / ".cache" / "mojo-mcp" / f"{surface}-{safe_ref}.json"


def _load_handwritten_cache(surface: str, ref: str) -> dict | None:
    path = _handwritten_cache_path(surface, ref)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("_schema_version") != _HANDWRITTEN_SCHEMA_VERSION:
        return None
    if time.time() - data.get("_fetched_at", 0) > _HANDWRITTEN_CACHE_TTL:
        return None
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        return None
    return data


def _save_handwritten_cache(surface: str, ref: str, files: dict[str, str]) -> None:
    path = _handwritten_cache_path(surface, ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_schema_version": _HANDWRITTEN_SCHEMA_VERSION,
        "_fetched_at": time.time(),
        "ref": ref,
        "files": files,
    }))


async def _fetch_github_dir_recursive(
    base_path: str,
    ref: str,
) -> tuple[dict[str, str], bool]:
    """Recursively fetch all `.md` / `.mdx` files under `base_path` at `ref`.

    Returns `(files dict, base_dir_existed)`. `base_dir_existed=False` when the
    initial listing of `base_path` 404s — useful for callers that want to
    retry at a fallback ref.
    """
    files: dict[str, str] = {}
    base_existed = False

    async with db.build_github_client() as client:
        # Walk the tree breadth-first using the Contents API
        to_list = [base_path]
        file_targets: list[tuple[str, str]] = []  # (rel_path, raw_url)

        first = True
        while to_list:
            path = to_list.pop(0)
            url = f"{db.GITHUB_API_BASE}/contents/{path}?ref={ref}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as e:
                logger.warning("Failed to list %s: %s", url, e)
                first = False
                continue
            if first:
                base_existed = True
                first = False
            entries = resp.json()
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                etype = entry.get("type")
                if etype == "dir":
                    to_list.append(entry["path"])
                elif etype == "file":
                    name = entry.get("name", "")
                    if name.endswith(".md") or name.endswith(".mdx"):
                        download = entry.get("download_url")
                        if download:
                            rel = entry["path"].removeprefix(base_path).lstrip("/")
                            file_targets.append((rel, download))

        # Concurrent raw fetches
        sem = asyncio.Semaphore(db.DIR_FETCH_CONCURRENCY)

        async def _fetch_one(rel: str, raw_url: str) -> tuple[str, str] | None:
            async with sem:
                try:
                    r = await client.get(raw_url)
                    r.raise_for_status()
                except Exception as e:
                    logger.warning("Failed to fetch %s: %s", raw_url, e)
                    return None
                return rel, r.text

        results = await asyncio.gather(
            *(_fetch_one(rel, url) for rel, url in file_targets)
        )

    for r in results:
        if r is not None:
            rel, content = r
            files[rel] = content
    return files, base_existed


async def _get_handwritten_surface(
    surface: str,
    mojo_version: str | None = None,
) -> tuple[dict[str, str], str]:
    """Return (files dict, resolved ref) for a handwritten surface."""
    base_path = _HANDWRITTEN_SURFACES[surface]
    effective_version = (
        mojo_version
        or _capture_mojo_version()
        or get_cached_mojo_version()
    )
    ref = await db.resolve_mojo_ref(effective_version)
    cached = _load_handwritten_cache(surface, ref)
    if cached and isinstance(cached.get("files"), dict):
        return cached["files"], ref
    files, base_existed = await _fetch_github_dir_recursive(base_path, ref)
    if not base_existed and ref != "main":
        logger.info(
            "%s missing at ref %s; falling back to main", base_path, ref,
        )
        cached_main = _load_handwritten_cache(surface, "main")
        if cached_main and isinstance(cached_main.get("files"), dict):
            return cached_main["files"], "main"
        files, _ = await _fetch_github_dir_recursive(base_path, "main")
        if files:
            _save_handwritten_cache(surface, "main", files)
        return files, "main"
    if files:
        _save_handwritten_cache(surface, ref, files)
    return files, ref


def _resolve_topic_match(topic: str, files: dict[str, str]) -> str | None:
    """Find a file in `files` matching `topic` (filename, stem, or path fragment)."""
    topic_norm = topic.strip().lstrip("/").lower()
    if not topic_norm:
        return None
    # Exact path match first
    for rel in files:
        if rel.lower() == topic_norm:
            return rel
    # Strip extension
    if not topic_norm.endswith((".md", ".mdx")):
        for ext in (".md", ".mdx"):
            target = topic_norm + ext
            for rel in files:
                if rel.lower() == target:
                    return rel
    # Stem match: e.g. topic="basics" → "basics.md", "manual/basics.md"
    for rel in files:
        rel_lower = rel.lower()
        stem = rel_lower.rsplit("/", 1)[-1]
        if stem in (topic_norm, topic_norm + ".md", topic_norm + ".mdx"):
            return rel
    # Substring fallback
    for rel in files:
        if topic_norm in rel.lower():
            return rel
    return None


def _render_surface_toc(surface: str, files: dict[str, str], ref: str) -> str:
    """Render a one-line-per-file table of contents for a surface."""
    if not files:
        return (
            f"No {surface} pages fetched (cache empty). "
            "Hint: GitHub may be rate-limiting; set GITHUB_TOKEN to raise the limit."
        )
    lines = [f"# Mojo {surface} (ref: `{ref}`)", ""]
    for rel in sorted(files.keys()):
        lines.append(f"- `{rel}`")
    lines.append("")
    lines.append(
        f"_Call `{surface}` with `topic=<filename>` to fetch a specific page._"
    )
    return "\n".join(lines)


async def fetch_handwritten(
    surface: str,
    topic: str | None = None,
    mojo_version: str | None = None,
) -> str:
    """Generic dispatcher for `manual` / `reference` / `cli` tools."""
    if surface not in _HANDWRITTEN_SURFACES:
        return f"Error: unknown handwritten surface {surface!r}"
    files, ref = await _get_handwritten_surface(surface, mojo_version)
    if not topic:
        return _render_surface_toc(surface, files, ref)
    rel = _resolve_topic_match(topic, files)
    if rel is None:
        return (
            f"No {surface} page matches topic={topic!r} at ref `{ref}`.\n\n"
            f"Available pages: {len(files)}. Use `{surface}` with no topic "
            "to list them."
        )
    content = files[rel]
    return f"# `{rel}` (ref: `{ref}`)\n\n{content.rstrip()}\n"


# ---------------------------------------------------------------------------
# Changelog — already shipped on 2026-05-17; refactored to use docs_backend
# ---------------------------------------------------------------------------

CHANGELOG_CACHE_PATH = Path.home() / ".cache" / "mojo-mcp" / "changelog.json"
CHANGELOG_CACHE_TTL = 604800  # 7 days
CHANGELOG_CACHE_SCHEMA_VERSION = 3
CHANGELOG_FETCH_CONCURRENCY = 8

GITHUB_RELEASES_LISTING_URL = (
    db.GITHUB_API_BASE + "/contents/mojo/docs/releases"
)
GITHUB_RAW_RELEASES_BASE = (
    db.GITHUB_RAW_BASE + "/main/mojo/docs/releases"
)
GITHUB_NIGHTLY_RAW_URL = (
    db.GITHUB_RAW_BASE + "/main/mojo/docs/nightly-changelog.md"
)

_VERSIONED_RELEASE_RE = re.compile(r"^v\d+(?:\.\d+)+(?:[abc]\d+)?$")
_VERSION_PARTS_RE = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?(?:([abc])(\d+))?$")
_CHANNEL_RANK = {None: 4, "c": 3, "b": 2, "a": 1}


def _version_key_from_filename(filename: str) -> str:
    stem = filename.removesuffix(".md")
    if stem == "nightly-changelog":
        return "nightly"
    return stem


def _is_versioned_release(key: str) -> bool:
    return bool(_VERSIONED_RELEASE_RE.match(key))


def _version_sort_key(key: str) -> tuple[int, int, int, int, int]:
    m = _VERSION_PARTS_RE.match(key)
    if not m:
        return (-1, 0, 0, 0, 0)
    return (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)) if m.group(3) else 0,
        _CHANNEL_RANK[m.group(4)],
        int(m.group(5)) if m.group(5) else 0,
    )


async def _fetch_and_parse_changelog() -> dict:
    """Fetch the changelog from modular/modular as raw markdown."""
    async with db.build_github_client() as client:
        listing_resp = await client.get(GITHUB_RELEASES_LISTING_URL)
        listing_resp.raise_for_status()
        listing = listing_resp.json()

        targets: list[tuple[str, str, str | None]] = []
        for entry in listing:
            if not isinstance(entry, dict) or entry.get("type") != "file":
                continue
            name = entry.get("name", "")
            if not isinstance(name, str) or not name.endswith(".md"):
                continue
            key = _version_key_from_filename(name)
            url = entry.get("download_url") or f"{GITHUB_RAW_RELEASES_BASE}/{name}"
            targets.append((key, url, entry.get("sha")))

        targets.append(("nightly", GITHUB_NIGHTLY_RAW_URL, None))

        sem = asyncio.Semaphore(CHANGELOG_FETCH_CONCURRENCY)

        async def _fetch_one(key, url, sha):
            async with sem:
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                except Exception as e:
                    logger.warning("Failed to fetch %s: %s", url, e)
                    return None
                front, body = db.strip_frontmatter(r.text)
                heading = front.get("title") or key
                entry: dict = {"heading": heading, "markdown": body}
                if sha:
                    entry["sha"] = sha
                return key, entry

        results = await asyncio.gather(
            *(_fetch_one(k, u, s) for k, u, s in targets)
        )

    data: dict = {
        "_schema_version": CHANGELOG_CACHE_SCHEMA_VERSION,
        "_fetched_at": time.time(),
    }
    for r in results:
        if r is None:
            continue
        key, entry = r
        data[key] = entry
    return data


def _load_changelog_cache() -> dict | None:
    if not CHANGELOG_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CHANGELOG_CACHE_PATH.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("_schema_version") != CHANGELOG_CACHE_SCHEMA_VERSION:
        logger.info(
            "rebuilding changelog cache: schema_version=%r -> %d",
            data.get("_schema_version"), CHANGELOG_CACHE_SCHEMA_VERSION,
        )
        return None
    if time.time() - data.get("_fetched_at", 0) > CHANGELOG_CACHE_TTL:
        return None
    return data


def _save_changelog_cache(data: dict) -> None:
    CHANGELOG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANGELOG_CACHE_PATH.write_text(json.dumps(data, indent=2))


def _has_version_entries(data: dict) -> bool:
    return any(not k.startswith("_") for k in data)


def _match_version(user_input: str | None, keys: list[str]) -> list[str]:
    version_keys = [k for k in keys if not k.startswith("_")]
    if not user_input or user_input.lower() in ("latest", ""):
        sortable = [k for k in version_keys if _is_versioned_release(k)]
        sortable.sort(key=_version_sort_key, reverse=True)
        return sortable[:2]
    q = user_input.lower().strip()
    if q == "nightly":
        return ["nightly"] if "nightly" in version_keys else []
    matches: list[str] = []
    for k in version_keys:
        k_stripped = k.lstrip("v").lstrip("0").lstrip(".")
        q_stripped = q.lstrip("v").lstrip("0").lstrip(".")
        if k_stripped == q_stripped or k == q or k.endswith(q) or q.endswith(k_stripped):
            matches.append(k)
    return matches


async def fetch_changelog(version: str | None = None) -> str:
    """Fetch the Mojo changelog as Markdown, optionally filtered by version."""
    data = _load_changelog_cache()
    if not data:
        data = await _fetch_and_parse_changelog()
        if not _has_version_entries(data):
            return (
                "Error: changelog fetch returned no versions — GitHub may be "
                "unreachable, rate-limiting, or the modular/modular repo layout "
                "changed. Cache was not written; retry shortly or set GITHUB_TOKEN "
                "to raise the rate limit."
            )
        _save_changelog_cache(data)

    keys = list(data.keys())
    matched = _match_version(version, keys)

    if not matched:
        version_keys = [k for k in keys if not k.startswith("_")]
        return (
            f"No changelog entry found for version {version!r}.\n"
            f"Available versions: {', '.join(version_keys)}"
        )

    sections = []
    for k in matched:
        entry = data.get(k, {})
        sections.append(entry.get("markdown", f"## {k}\n\n(no content)"))

    return "\n\n".join(sections)
