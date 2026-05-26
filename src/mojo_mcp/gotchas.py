"""Gotcha pattern loading, matching, and version filtering."""

import re
from functools import lru_cache
from pathlib import Path

import yaml


_NUMERIC_PREFIX_RE = re.compile(r"^(\d+)")

_STRING_OR_COMMENT_RE = re.compile(
    r'"""[\s\S]*?"""|'
    r"'''[\s\S]*?'''|"
    r'"(?:[^"\\]|\\.)*"|'
    r"'(?:[^'\\]|\\.)*'|"
    r"#[^\n]*"
)


def _strip_comments_and_strings(source: str) -> str:
    """Remove string literals and comments to prevent false-positive pattern matches.

    Handles triple-quoted docstrings, single/double-quoted strings (with escapes),
    and ``#`` line comments. Matched spans are replaced with the empty string so that
    prose inside docstrings (e.g. ``"An owned handle"``) no longer triggers
    code-pattern regexes.
    """
    return _STRING_OR_COMMENT_RE.sub("", source)


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse '0.26.2', '26.2', or '1.0.0b1' into a comparable tuple.

    Per-segment pre-release suffixes (`b1`, `a2`, `rc1`, …) are stripped, so
    `1.0.0b1` parses as `(1, 0, 0)` — gotchas keyed to `>=1.0.0` then apply to
    the beta. Non-numeric segments are skipped.
    """
    parts: list[int] = []
    for segment in v.strip().split("."):
        m = _NUMERIC_PREFIX_RE.match(segment)
        if m:
            parts.append(int(m.group(1)))
    return tuple(parts)


def _version_matches(mojo_version: str, ranges: list[str]) -> bool:
    """Check if mojo_version satisfies any of the given semver ranges.

    Supports: >=X.Y.Z, <=X.Y.Z, ==X.Y.Z, >X.Y.Z, <X.Y.Z
    """
    v = _parse_version(mojo_version)
    for r in ranges:
        r = r.strip()
        if r.startswith(">="):
            if v >= _parse_version(r[2:]):
                return True
        elif r.startswith("<="):
            if v <= _parse_version(r[2:]):
                return True
        elif r.startswith("=="):
            if v == _parse_version(r[2:]):
                return True
        elif r.startswith(">"):
            if v > _parse_version(r[1:]):
                return True
        elif r.startswith("<"):
            if v < _parse_version(r[1:]):
                return True
        else:
            if v == _parse_version(r):
                return True
    return False


def _gotcha_to_hint(g: dict) -> dict:
    """Extract the user-facing hint fields from a gotcha entry."""
    hint: dict = {
        "id": g["id"],
        "title": g["title"],
        "severity": g["severity"],
        "description": g["description"],
        "fix": g["fix"],
    }
    if g.get("category"):
        hint["category"] = g["category"]
    if g.get("link"):
        hint["link"] = g["link"]
    return hint


@lru_cache(maxsize=1)
def load_gotchas() -> list[dict]:
    """Load and parse gotchas.yaml. Cached after first call."""
    yaml_path = Path(__file__).parent / "gotchas.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    return data.get("gotchas", [])


def validate_code(
    source: str,
    mojo_version: str,
    category: str | None = None,
    path: str | None = None,
) -> list[dict]:
    """Run code_pattern regexes against source code.

    Returns a list of matched gotcha hints for patterns that:
    - have a code_pattern
    - match the given mojo_version
    - match the source code (after stripping comments/strings)
    - (optionally) belong to the specified category
    - are not excluded by path_exclude when a path is given
    """
    gotchas = load_gotchas()
    stripped = _strip_comments_and_strings(source)
    hits: list[dict] = []
    for g in gotchas:
        if not g.get("code_pattern"):
            continue
        if category is not None and g.get("category") != category:
            continue
        if not _version_matches(mojo_version, g.get("mojo_versions", [])):
            continue
        if path and g.get("path_exclude"):
            if re.search(g["path_exclude"], path):
                continue
        if re.search(g["code_pattern"], stripped, re.MULTILINE):
            hits.append(_gotcha_to_hint(g))
    return hits


def enrich_error(stderr: str, timed_out: bool, mojo_version: str) -> list[dict]:
    """Match error output and timeout status against gotcha patterns.

    Returns a list of matched gotcha hints for patterns that:
    - have an error_pattern matching stderr, OR
    - have timeout_pattern=True and timed_out is True
    - AND match the given mojo_version
    """
    gotchas = load_gotchas()
    hits: list[dict] = []
    seen_ids: set[str] = set()
    for g in gotchas:
        if not _version_matches(mojo_version, g.get("mojo_versions", [])):
            continue
        matched = False
        if timed_out and g.get("timeout_pattern"):
            matched = True
        if not matched and g.get("error_pattern") and stderr:
            if re.search(g["error_pattern"], stderr):
                matched = True
        if matched and g["id"] not in seen_ids:
            seen_ids.add(g["id"])
            hits.append(_gotcha_to_hint(g))
    return hits
