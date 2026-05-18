"""Scrape and cache the Mojo stdlib reference from docs.modular.com."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

STDLIB_INDEX_URL = "https://docs.modular.com/mojo/std/"
CACHE_PATH = Path.home() / ".cache" / "mojo-mcp" / "docs.json"
CACHE_TTL = 1209600  # 14 days
CACHE_SCHEMA_VERSION = 2

# Unicode zero-width space that Docusaurus appends to headings
_ZWS = "\u200b"


def _text(el: Tag | None) -> str:
    return el.get_text(separator=" ", strip=True).replace(_ZWS, "").strip() if el else ""


def _parse_docs_source_version(html: str) -> str:
    """Best-effort extraction of the docs site version from a page's HTML.

    Returns 'unknown' if no version marker can be located.
    """
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"name": "docs-version"})
    if meta and meta.get("content"):
        return str(meta["content"]).strip()
    match = re.search(r"\bv\d+(?:\.\d+)+\b", html)
    if match:
        return match.group(0)
    return "unknown"


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


def _parse_module_page(html: str, url: str) -> dict:
    """Parse a single Mojo stdlib module page into structured data."""
    soup = BeautifulSoup(html, "lxml")

    # Module name from URL: .../std/collections/dict/ -> collections.dict
    parts = [p for p in url.rstrip("/").split("/") if p]
    try:
        idx = parts.index("std")
        module_parts = parts[idx + 1 :]
    except ValueError:
        module_parts = parts[-2:]
    module_name = ".".join(module_parts)

    article = soup.find("article") or soup.find("main") or soup

    # Description: first substantial paragraph (skip "Mojo module" boilerplate)
    description = ""
    for p in article.find_all("p", limit=8):  # type: ignore[union-attr]
        t = _text(p)
        if len(t) > 20 and t.lower() != "mojo module":
            description = t
            break

    structs: list[dict] = []
    functions: list[dict] = []
    traits: list[dict] = []
    aliases: list[dict] = []

    # New layout: <h2> section headers followed by <ul> item lists.
    # Section names: "Structs", "Functions", "Traits", "comptimevalues".
    _SECTION_MAP = {
        "structs": structs,
        "functions": functions,
        "traits": traits,
        "protocols": traits,
        "comptimevalues": aliases,
        "aliases": aliases,
        "type-aliases": aliases,
    }

    for h2 in article.find_all("h2"):  # type: ignore[union-attr]
        section_key = _text(h2).lower().replace(" ", "")
        target = _SECTION_MAP.get(section_key)
        if target is None:
            continue

        sib = h2.find_next_sibling()
        if sib and sib.name == "ul":
            for li in sib.find_all("li", recursive=False):
                code = li.find("code")
                name = _text(code) if code else ""
                if not name:
                    name = _text(li).split(":")[0].strip()
                full = _text(li)
                # Description follows the name and a colon separator
                desc = full[len(name):].lstrip(" :​") if name and name in full else full
                target.append({"name": name, "signature": name, "description": desc})
        elif sib and sib.name == "h3":
            # Inline items directly under the h2 (e.g. some alias sections)
            _collect_h3_items(h2, target)

    # Also collect h3 items that appear under comptimevalues/aliases h2
    # (the div-based detail format used for inline alias definitions)
    for h3 in article.find_all("h3"):  # type: ignore[union-attr]
        name = _text(h3)
        if not name:
            continue
        # Find the nearest preceding h2 to determine section
        prev_h2 = h3.find_previous("h2")
        if not prev_h2:
            continue
        section_key = _text(prev_h2).lower().replace(" ", "")
        target = _SECTION_MAP.get(section_key)
        if target is None:
            continue
        # Avoid duplicates from the <ul> pass
        if any(e["name"] == name for e in target):
            continue

        sig = name
        desc = ""
        detail_div = h3.find_next_sibling("div")
        if detail_div:
            sig_el = detail_div.find(class_=re.compile(r"sig"))
            if sig_el:
                sig = _text(sig_el)
            desc_p = detail_div.find("p")
            if desc_p:
                desc = _text(desc_p)

        target.append({"name": name, "signature": sig, "description": desc})

    return {
        "name": module_name,
        "url": url,
        "description": description,
        "structs": structs,
        "functions": functions,
        "traits": traits,
        "aliases": aliases,
    }


def _collect_h3_items(h2: Tag, target: list) -> None:
    """Collect <h3> items directly following an <h2> into target list."""
    sib = h2.find_next_sibling()
    while sib and sib.name != "h2":
        if sib.name == "h3":
            name = _text(sib)
            if name:
                desc = ""
                detail = sib.find_next_sibling("div")
                if detail:
                    p = detail.find("p")
                    desc = _text(p) if p else _text(detail)
                target.append({"name": name, "signature": name, "description": desc})
        sib = sib.find_next_sibling()


def _collect_urls_from_html(html: str, pattern: str) -> list[str]:
    """Extract unique absolute URLs matching `pattern` from HTML."""
    matches = re.findall(pattern, html)
    seen: set[str] = set()
    urls: list[str] = []
    for path in matches:
        url = "https://docs.modular.com" + path.rstrip("/") + "/"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def build_docs_index() -> dict:
    """Fetch and index the full Mojo stdlib. Returns a module-keyed dict."""
    logger.info("Fetching Mojo stdlib index from %s", STDLIB_INDEX_URL)

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "mojo-mcp/0.1 docs-indexer"},
    ) as client:
        # Level 1: index → package pages (e.g. /mojo/std/collections/)
        resp = await client.get(STDLIB_INDEX_URL)
        resp.raise_for_status()
        pkg_urls = _collect_urls_from_html(resp.text, r"/mojo/std/[a-zA-Z0-9_]+/")

        logger.info("Found %d package pages; fetching module lists...", len(pkg_urls))

        # Level 2: package pages → module pages (e.g. /mojo/std/collections/dict/)
        module_urls: list[str] = []
        for pkg_url in pkg_urls:
            try:
                pr = await client.get(pkg_url)
                pr.raise_for_status()
                pkg_path = "/" + pkg_url.split("docs.modular.com/")[1]
                # Match sub-pages: /mojo/std/{pkg}/{module}/
                escaped = re.escape(pkg_path.rstrip("/"))
                sub_urls = _collect_urls_from_html(
                    pr.text, escaped + r"/[a-zA-Z0-9_]+"
                )
                module_urls.extend(sub_urls)
            except Exception as e:
                logger.warning("Failed to fetch package page %s: %s", pkg_url, e)

    logger.info("Found %d module pages to scrape", len(module_urls))

    docs: dict[str, dict] = {}
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "mojo-mcp/0.1 docs-indexer"},
    ) as client:
        for i, url in enumerate(module_urls):
            try:
                r = await client.get(url)
                r.raise_for_status()
                parsed = _parse_module_page(r.text, url)
                docs[parsed["name"]] = parsed
                if (i + 1) % 10 == 0:
                    logger.info(
                        "Scraped %d/%d module pages", i + 1, len(module_urls)
                    )
            except Exception as e:
                logger.warning("Failed to scrape %s: %s", url, e)

    return docs


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
    schema_version = envelope.get("schema_version")
    if schema_version != CACHE_SCHEMA_VERSION:
        logger.info(
            "rebuilding docs cache: schema_version=%r -> %d",
            schema_version, CACHE_SCHEMA_VERSION,
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
    docs_source_version: str = "unknown",
    mojo_version_at_fetch: str | None = None,
) -> None:
    """Write the v2 envelope wrapping `modules`."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "fetched_at": time.time(),
        "docs_source_version": docs_source_version,
        "mojo_version_at_fetch": mojo_version_at_fetch,
        "modules": modules,
    }
    CACHE_PATH.write_text(json.dumps(envelope, indent=2))


async def _probe_docs_source_version() -> str:
    """Fetch only the index page to detect docs version. Best-effort, 5s timeout."""
    try:
        async with httpx.AsyncClient(
            timeout=5,
            follow_redirects=True,
            headers={"User-Agent": "mojo-mcp/0.1 version-probe"},
        ) as client:
            resp = await client.get(STDLIB_INDEX_URL)
            resp.raise_for_status()
            return _parse_docs_source_version(resp.text)
    except Exception:
        return "unknown"


async def get_docs() -> dict:
    """Return docs from cache if fresh, otherwise refresh-in-place or rebuild."""
    cached = load_cached_docs()
    if cached:
        logger.info("Loaded Mojo stdlib docs from cache (%d modules)", len(cached))
        return cached

    # TTL miss with valid v2 envelope: maybe just refresh fetched_at if source hasn't changed.
    if CACHE_PATH.exists():
        try:
            envelope = json.loads(CACHE_PATH.read_text())
            if envelope.get("schema_version") == CACHE_SCHEMA_VERSION:
                cached_source = envelope.get("docs_source_version", "unknown")
                live_source = await _probe_docs_source_version()
                if live_source != "unknown" and live_source == cached_source:
                    envelope["fetched_at"] = time.time()
                    CACHE_PATH.write_text(json.dumps(envelope, indent=2))
                    modules = envelope.get("modules", {})
                    logger.info(
                        "Refreshed docs cache (source version %s unchanged)", live_source
                    )
                    return modules
        except Exception:
            pass

    # Full rebuild
    mojo_version = _capture_mojo_version()
    docs = await build_docs_index()
    source_version = await _probe_docs_source_version()
    save_docs_cache(docs, docs_source_version=source_version, mojo_version_at_fetch=mojo_version)
    logger.info("Indexed and cached %d Mojo stdlib modules", len(docs))
    return docs


# ---------------------------------------------------------------------------
# Phase 2 — lookup tool
# ---------------------------------------------------------------------------

_SYMBOL_KEYWORDS = {"struct", "fn", "alias", "trait"}
_BASE_DOC_URL = "https://docs.modular.com"


def _build_symbol_url(query: str) -> str:
    """Convert dot-notation path to docs URL.

    E.g. 'collections.dict.Dict' → '/mojo/std/collections/dict/Dict'
    """
    parts = [p for p in query.split(".") if p]
    if len(parts) < 2:
        raise ValueError(
            f"Need at least 2 components (module.Symbol), got: {query!r}. "
            "Example: 'collections.dict.Dict' or 'builtin.int.Int'"
        )
    # Strip leading 'std' if user included it
    if parts[0].lower() == "std":
        parts = parts[1:]
    for seg in parts:
        if not re.match(r"^[A-Za-z0-9_]+$", seg):
            raise ValueError(f"Invalid segment {seg!r} in query {query!r}")
    return "/mojo/std/" + "/".join(parts)


def _parse_symbol_page(html: str, url: str) -> str:
    """Parse a Mojo symbol page into Markdown."""
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find("main") or soup

    # Symbol name from URL
    symbol_name = url.rstrip("/").rsplit("/", 1)[-1]

    lines: list[str] = [f"# {symbol_name}", ""]

    # Signature: first <code> after <h1> whose text starts with a keyword or has ( or [
    signature = ""
    h1 = article.find("h1")  # type: ignore[union-attr]
    if h1:
        for el in h1.find_next_siblings():
            if el.name in ("h2", "h3"):
                break
            if el.name == "code":
                t = _text(el)
                first_word = t.split()[0] if t.split() else ""
                if first_word in _SYMBOL_KEYWORDS or "(" in t or "[" in t:
                    signature = t
                    break
            # also check inside divs
            code_el = el.find("code") if hasattr(el, "find") else None
            if code_el:
                t = _text(code_el)
                first_word = t.split()[0] if t.split() else ""
                if first_word in _SYMBOL_KEYWORDS or "(" in t or "[" in t:
                    signature = t
                    break

    if signature:
        lines += [f"```mojo\n{signature}\n```", ""]

    # Description: first <p> with len > 20 in article
    for p in article.find_all("p", limit=10):  # type: ignore[union-attr]
        t = _text(p)
        if len(t) > 20:
            lines += [t, ""]
            break

    # Walk H2 sections
    for h2 in article.find_all("h2"):  # type: ignore[union-attr]
        section = _text(h2).lower().replace(" ", "")

        if section == "parameters":
            lines.append("## Parameters")
            ul = h2.find_next_sibling("ul")
            if ul:
                for li in ul.find_all("li", recursive=False):
                    lines.append(f"- {_text(li)}")
            lines.append("")

        elif section == "implementedtraits":
            sib = h2.find_next_sibling()
            if sib:
                lines += [f"## Implemented Traits", _text(sib), ""]

        elif section in ("args", "arguments"):
            lines.append("## Args")
            ul = h2.find_next_sibling("ul")
            if ul:
                for li in ul.find_all("li", recursive=False):
                    lines.append(f"- {_text(li)}")
            lines.append("")

        elif section == "returns":
            sib = h2.find_next_sibling()
            if sib:
                lines += ["## Returns", _text(sib), ""]

        elif section == "methods":
            lines.append("## Methods")
            lines.append("")
            # Collect h3 method entries
            sib = h2.find_next_sibling()
            while sib and sib.name != "h2":
                if sib.name == "h3":
                    method_name = _text(sib)
                    if method_name:
                        lines.append(f"### {method_name}")
                        # Collect <code> overloads following this h3
                        inner = sib.find_next_sibling()
                        while inner and inner.name not in ("h2", "h3"):
                            if inner.name == "code":
                                lines.append(f"```mojo\n{_text(inner)}\n```")
                            elif hasattr(inner, "find"):
                                # Look for args/returns h4 subsections
                                for h4 in inner.find_all("h4"):
                                    h4_label = _text(h4).lower()
                                    if h4_label in ("args", "returns"):
                                        lines.append(f"**{_text(h4)}**")
                                        ul = h4.find_next_sibling("ul")
                                        if ul:
                                            for li in ul.find_all("li", recursive=False):
                                                lines.append(f"- {_text(li)}")
                            inner = inner.find_next_sibling()
                        lines.append("")
                sib = sib.find_next_sibling()

    return "\n".join(lines)


async def fetch_symbol_page(query: str) -> str:
    """Fetch full Mojo symbol documentation as Markdown."""
    try:
        path = _build_symbol_url(query)
    except ValueError as e:
        return f"Error: {e}"

    url = _BASE_DOC_URL + path
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return (
                    f"Symbol not found: {url}\n"
                    "Hint: Symbol names are PascalCase, module names lowercase "
                    "(e.g. 'collections.dict.Dict', 'builtin.int.Int')."
                )
            resp.raise_for_status()
            return _parse_symbol_page(resp.text, url)
    except Exception as e:
        return f"Error fetching {url}: {e}"


# ---------------------------------------------------------------------------
# Phase 3 — changelog tool (GitHub-backed)
# ---------------------------------------------------------------------------

CHANGELOG_CACHE_PATH = Path.home() / ".cache" / "mojo-mcp" / "changelog.json"
CHANGELOG_CACHE_TTL = 604800  # 7 days
CHANGELOG_CACHE_SCHEMA_VERSION = 3

GITHUB_RELEASES_LISTING_URL = (
    "https://api.github.com/repos/modular/modular/contents/mojo/docs/releases"
)
GITHUB_RAW_RELEASES_BASE = (
    "https://raw.githubusercontent.com/modular/modular/main/mojo/docs/releases"
)
GITHUB_NIGHTLY_RAW_URL = (
    "https://raw.githubusercontent.com/modular/modular/main/mojo/docs/nightly-changelog.md"
)
CHANGELOG_FETCH_CONCURRENCY = 8

_VERSIONED_RELEASE_RE = re.compile(r"^v\d+(?:\.\d+)+(?:[abc]\d+)?$")
_VERSION_PARTS_RE = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?(?:([abc])(\d+))?$")
# Channel rank: stable highest, then rc, beta, alpha. Higher = closer to stable.
_CHANNEL_RANK = {None: 4, "c": 3, "b": 2, "a": 1}


def _github_auth_header() -> dict[str, str]:
    """Return Authorization header dict if a GitHub token is present in env."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
        "GITHUB_PERSONAL_ACCESS_TOKEN"
    )
    return {"Authorization": f"Bearer {token}"} if token else {}


def _build_changelog_headers() -> dict[str, str]:
    return {
        "User-Agent": "mojo-mcp/0.1 changelog-fetcher",
        "Accept": "application/vnd.github+json",
        **_github_auth_header(),
    }


def _build_changelog_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=_build_changelog_headers(),
    )


def _strip_frontmatter(md: str) -> tuple[dict[str, str], str]:
    """Strip a leading YAML frontmatter block. Returns (front_dict, body).

    Only handles the simple `key: value` lines used in the modular/modular
    release files. Anything more elaborate falls through as part of the body.
    """
    if not md.startswith("---\n"):
        return {}, md
    end = md.find("\n---", 4)
    if end == -1:
        return {}, md
    front_block = md[4:end]
    rest_start = end + len("\n---")
    if md[rest_start:rest_start + 1] == "\n":
        rest_start += 1
    body = md[rest_start:].lstrip("\n")
    front: dict[str, str] = {}
    for line in front_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            front[k.strip()] = v.strip()
    return front, body


def _version_key_from_filename(filename: str) -> str:
    """Map a release-file name to its cache key."""
    stem = filename.removesuffix(".md")
    if stem == "nightly-changelog":
        return "nightly"
    return stem


def _is_versioned_release(key: str) -> bool:
    """True for `vX.Y[.Z][bN]` style keys; False for nightly/monthly/oddballs."""
    return bool(_VERSIONED_RELEASE_RE.match(key))


def _version_sort_key(key: str) -> tuple[int, int, int, int, int]:
    """Sortable tuple for semver-like keys. Use with `reverse=True` for desc."""
    m = _VERSION_PARTS_RE.match(key)
    if not m:
        return (-1, 0, 0, 0, 0)
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3)) if m.group(3) else 0
    channel = _CHANNEL_RANK[m.group(4)]
    channel_num = int(m.group(5)) if m.group(5) else 0
    return (major, minor, patch, channel, channel_num)


async def _fetch_and_parse_changelog() -> dict:
    """Fetch the changelog from the modular/modular GitHub repo.

    Pulls the release-files listing via the GitHub Contents API, then fetches
    each `vX.Y.Z.md` plus `nightly-changelog.md` as raw markdown. No HTML.
    """
    async with _build_changelog_http_client() as client:
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

        async def _fetch_one(
            key: str, url: str, sha: str | None
        ) -> tuple[str, dict] | None:
            async with sem:
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                except Exception as e:
                    logger.warning("Failed to fetch %s: %s", url, e)
                    return None
                front, body = _strip_frontmatter(r.text)
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
    schema = data.get("_schema_version")
    if schema != CHANGELOG_CACHE_SCHEMA_VERSION:
        logger.info(
            "rebuilding changelog cache: schema_version=%r -> %d",
            schema, CHANGELOG_CACHE_SCHEMA_VERSION,
        )
        return None
    fetched_at = data.get("_fetched_at", 0)
    if time.time() - fetched_at > CHANGELOG_CACHE_TTL:
        return None
    return data


def _save_changelog_cache(data: dict) -> None:
    CHANGELOG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANGELOG_CACHE_PATH.write_text(json.dumps(data, indent=2))


def _has_version_entries(data: dict) -> bool:
    return any(not k.startswith("_") for k in data)


def _match_version(user_input: str | None, keys: list[str]) -> list[str]:
    """Return matching version keys for `user_input`."""
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
