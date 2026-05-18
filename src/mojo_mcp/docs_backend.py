"""Shared transport, version resolution, and parsing helpers for docs tools.

Centralizes the bits that every docs surface needs:

- GitHub auth header from env (`GITHUB_TOKEN` or `GITHUB_PERSONAL_ACCESS_TOKEN`).
- HTTP client builders for `api.github.com` / `raw.githubusercontent.com` /
  `mojolang.org`.
- YAML frontmatter and mojolang-preamble stripping.
- llms.txt entry parsing.
- Mojo version-string normalization, candidate tag generation, and
  `resolve_mojo_ref()` that maps a Mojo version to a `modular/modular` git ref.
- A disk-cached list of available tags so version resolution stays fast.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com/repos/modular/modular"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/modular/modular"
MOJOLANG_BASE = "https://mojolang.org"

CACHE_DIR = Path.home() / ".cache" / "mojo-mcp"
TAGS_CACHE_PATH = CACHE_DIR / "tags.json"
TAGS_CACHE_TTL = 86400  # 24h

DEFAULT_USER_AGENT = "mojo-mcp/0.1 docs-fetcher"

# Concurrent-fetch knobs
DIR_FETCH_CONCURRENCY = 8


# ---------------------------------------------------------------------------
# Auth + HTTP client builders
# ---------------------------------------------------------------------------


def github_auth_header() -> dict[str, str]:
    """Bearer header from env, if available. Empty dict otherwise."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get(
        "GITHUB_PERSONAL_ACCESS_TOKEN"
    )
    return {"Authorization": f"Bearer {token}"} if token else {}


def github_headers() -> dict[str, str]:
    """Standard headers for GitHub API + raw requests."""
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/vnd.github+json",
        **github_auth_header(),
    }


def build_github_client() -> httpx.AsyncClient:
    """Async client preconfigured for GitHub API / raw fetches."""
    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers=github_headers(),
    )


def build_mojolang_client() -> httpx.AsyncClient:
    """Async client preconfigured for mojolang.org (no auth needed).

    Sends no custom `Accept` header: the site's content negotiation returns 404
    on `text/markdown,...` for the `.txt`/`.md` paths we want.
    """
    return httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )


# ---------------------------------------------------------------------------
# Frontmatter + mojolang preamble
# ---------------------------------------------------------------------------


def strip_frontmatter(md: str) -> tuple[dict[str, str], str]:
    """Strip a leading YAML frontmatter block. Returns (front_dict, body)."""
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


_PREAMBLE_RE = re.compile(
    r"\A> [^\n]*llms\.txt[^\n]*\n> [^\n]*\.md to any URL[^\n]*\n+",
    re.IGNORECASE,
)


def strip_mojolang_preamble(md: str) -> str:
    """Remove the two-line `>` blockquote header mojolang.org prepends to `.md` pages."""
    return _PREAMBLE_RE.sub("", md)


# ---------------------------------------------------------------------------
# llms.txt parsing
# ---------------------------------------------------------------------------


_LLMS_LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<name>[^\]]+)\]\((?P<url>[^)]+)\)(?::\s*(?P<desc>.*))?$"
)


def parse_llms_txt(text: str) -> list[dict[str, str]]:
    """Parse a `llms.txt`/`llms-*.txt` body into `{name, url, description}` entries.

    Lines not matching the `- [name](url): description` shape (headers, blanks,
    blockquote intro text) are ignored.
    """
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        m = _LLMS_LINE_RE.match(line)
        if not m:
            continue
        entries.append({
            "name": m.group("name").strip(),
            "url": m.group("url").strip(),
            "description": (m.group("desc") or "").strip(),
        })
    return entries


# ---------------------------------------------------------------------------
# Mojo version normalization + candidate tags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MojoVersion:
    major: int
    minor: int
    patch: int | None
    pre: str | None  # "b1", "a2", etc. — pre-release suffix


_COMPATIBLE_RE = re.compile(r"compatible with v(\d+)\.(\d+)")
_OLD_STYLE_RE = re.compile(
    r"(?:v)?0\.(\d+)\.(\d+)(?:\.(\d+))?([ab]\d+)?"
)
_MODERN_RE = re.compile(
    r"(?:v)?(\d+)\.(\d+)(?:\.(\d+))?([ab]\d+)?"
)


def normalize_mojo_version(s: str | None) -> MojoVersion | None:
    """Extract a `MojoVersion` from a free-form version string."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None

    # Prefer "compatible with vX.Y" — it's the canonical form Mojo emits itself
    m = _COMPATIBLE_RE.search(s)
    if m:
        return MojoVersion(
            major=int(m.group(1)),
            minor=int(m.group(2)),
            patch=None,
            pre=None,
        )

    # Old-style: leading "0." (e.g. "0.26.2.0", "v0.26.2")
    m = _OLD_STYLE_RE.search(s)
    if m:
        return MojoVersion(
            major=int(m.group(1)),
            minor=int(m.group(2)),
            patch=int(m.group(3)) if m.group(3) else None,
            pre=m.group(4),
        )

    # Modern: "1.0.0b1", "26.2", "v26.2.0"
    m = _MODERN_RE.search(s)
    if m:
        return MojoVersion(
            major=int(m.group(1)),
            minor=int(m.group(2)),
            patch=int(m.group(3)) if m.group(3) else None,
            pre=m.group(4),
        )
    return None


def candidate_tags(v: MojoVersion) -> list[str]:
    """Ordered list of `modular/modular` tag candidates to try for `v`.

    Naming-scheme drift handled: `modular/vX.Y.Z` (current) → `max/vX.Y.Z`
    (24.6–25.2) → `mojo/vX.Y.Z` (≤24.5, also Mojo 1.0 beta).

    If a specific patch is given, exact-patch candidates come first, then
    patch=0 candidates as a fallback.
    """
    pre = v.pre or ""
    patches: list[int] = []
    if v.patch is not None:
        patches.append(v.patch)
        if v.patch != 0:
            patches.append(0)
    else:
        patches.append(0)
    cands: list[str] = []
    for patch in patches:
        base = f"v{v.major}.{v.minor}.{patch}{pre}"
        cands.extend([
            f"modular/{base}",
            f"max/{base}",
            f"mojo/{base}",
        ])
    return cands


# ---------------------------------------------------------------------------
# Tag list cache + ref resolution
# ---------------------------------------------------------------------------


def _load_tags_cache() -> list[str] | None:
    if not TAGS_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TAGS_CACHE_PATH.read_text())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if time.time() - data.get("fetched_at", 0) > TAGS_CACHE_TTL:
        return None
    tags = data.get("tags")
    return tags if isinstance(tags, list) else None


def _save_tags_cache(tags: list[str]) -> None:
    TAGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TAGS_CACHE_PATH.write_text(json.dumps({
        "fetched_at": time.time(),
        "tags": tags,
    }))


async def list_modular_tags(*, force_refresh: bool = False) -> list[str]:
    """Return the list of tag names from `modular/modular`. Cached for 24h."""
    if not force_refresh:
        cached = _load_tags_cache()
        if cached is not None:
            return cached

    all_tags: list[str] = []
    page = 1
    async with build_github_client() as client:
        while True:
            url = f"{GITHUB_API_BASE}/tags?per_page=100&page={page}"
            resp = await client.get(url)
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            all_tags.extend(item["name"] for item in batch if isinstance(item, dict) and "name" in item)
            if len(batch) < 100:
                break
            page += 1

    _save_tags_cache(all_tags)
    return all_tags


async def resolve_mojo_ref(
    version: str | None,
    *,
    force_refresh: bool = False,
) -> str:
    """Resolve a Mojo version string to a `modular/modular` git ref.

    Returns the matching tag (e.g. `"modular/v26.2.0"`) or `"main"` on
    None/garbage input / no match.
    """
    if not version:
        return "main"
    v = normalize_mojo_version(version)
    if v is None:
        return "main"
    tags = set(await list_modular_tags(force_refresh=force_refresh))
    for cand in candidate_tags(v):
        if cand in tags:
            return cand
    return "main"
