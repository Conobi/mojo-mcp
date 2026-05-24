"""Markdown and JSON rendering for tool responses (R3).

`render(result, fmt, *, tool)` is the single entry point. `fmt="json"` returns
the existing compact JSON shape; `fmt="md"` dispatches to a per-tool renderer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

_RENDERERS: dict[str, Callable[[dict], str]] = {}


def _fence(content: str) -> str:
    """Return a backtick fence one longer than the longest run in `content`.

    Minimum length is three backticks. This guarantees the fence can wrap any
    content without ambiguity, including content that itself contains fences.
    """
    longest = 0
    for match in re.finditer(r"`+", content):
        longest = max(longest, len(match.group(0)))
    return "`" * max(3, longest + 1)


def _compact_json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)


def render(result: dict, fmt: str, *, tool: str) -> str:
    """Render `result` as markdown or json.

    Raises KeyError if `fmt="md"` is requested for an unknown tool name.
    """
    if fmt == "json":
        return _compact_json(result)
    if fmt != "md":
        raise ValueError(f"Unsupported format: {fmt!r}. Use 'md' or 'json'.")
    renderer = _RENDERERS[tool]
    return renderer(result)


def _register(tool: str):
    """Decorator: register a per-tool markdown renderer."""
    def deco(fn: Callable[[dict], str]) -> Callable[[dict], str]:
        _RENDERERS[tool] = fn
        return fn
    return deco


def _kv_lines(d: dict, keys: list[str]) -> list[str]:
    """Render selected keys as `**key:** value` bullets, skipping missing/empty."""
    out: list[str] = []
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        out.append(f"- **{k}:** {v}")
    return out


@_register("search")
def _render_search(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}" + (f"\n\n_{r['hint']}_" if r.get("hint") else "")
    parts: list[str] = []
    if r.get("truncated"):
        parts.append(f"**Truncated result** (total_bytes={r.get('total_bytes')})")
        raw = r.get("result_raw", "")
        fence = _fence(raw)
        parts.append(f"{fence}\n{raw}\n{fence}")
    elif r.get("result") is None:
        parts.append(r.get("message") or "Search returned no results.")
    else:
        data = r["result"]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "name" in item:
                    name = item["name"]
                    desc = item.get("description") or item.get("signature") or ""
                    parts.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")
                else:
                    parts.append(f"- {item}")
        elif isinstance(data, dict):
            for k, v in data.items():
                parts.append(f"- **{k}:** {v}")
        else:
            fence = _fence(str(data))
            parts.append(f"{fence}\n{data}\n{fence}")
    if r.get("mojo_version"):
        parts.append(f"\n_mojo_version: {r['mojo_version']}_")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)


_SEVERITY_GLYPH = {"critical": "⛔", "warning": "⚠️", "info": "ℹ️"}
_CATEGORY_TAG = {"security": "🔒"}


@_register("validate")
def _render_validate(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}" + (f"\n\n_{r['hint']}_" if r.get("hint") else "")
    parts: list[str] = []
    issues = r.get("issues", [])
    if not issues:
        msg = r.get("message", "No known gotcha patterns matched.")
        parts.append(f"✓ {msg}")
    else:
        parts.append(f"**Found {len(issues)} issue{'s' if len(issues) != 1 else ''}:**")
        for issue in issues:
            sev = issue.get("severity", "info")
            glyph = _SEVERITY_GLYPH.get(sev, "•")
            title = issue.get("title", issue.get("id", "?"))
            iid = issue.get("id", "?")
            desc = issue.get("description", "")
            fix = issue.get("fix", "")
            cat = issue.get("category", "")
            cat_tag = _CATEGORY_TAG.get(cat, "")
            prefix = f"{cat_tag} " if cat_tag else ""
            parts.append(f"- {glyph} {prefix}**[{sev}] {iid}** — {title}")
            if desc:
                parts.append(f"  {desc}")
            if fix:
                parts.append(f"  _Fix:_ {fix}")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)


_LANG_MAP = {".mojo": "mojo", ".py": "python", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown", ".json": "json"}


def _lang_for(path: str) -> str:
    for ext, lang in _LANG_MAP.items():
        if path.endswith(ext):
            return lang
    return "text"


@_register("read_file")
def _render_read_file(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}"
    path = r.get("path", "")
    content = r.get("content", "")
    fence = _fence(content)
    lang = _lang_for(path)
    parts = [f"### {path}", f"{fence}{lang}", content, fence]
    if r.get("truncated"):
        parts.append(f"\n_truncated at 100KB; total_bytes={r.get('total_bytes')}_")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)


@_register("list_files")
def _render_list_files(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}"
    path = r.get("path", "")
    pattern = r.get("pattern", "")
    files = r.get("files", [])
    count = r.get("count", len(files))
    parts = [f"### {path}", f"_pattern:_ `{pattern}`"]
    if not files:
        parts.append(r.get("message", "(no matches)"))
    else:
        for f in files:
            parts.append(f"- {f}")
        parts.append(f"\n**Count:** {count}")
    if r.get("truncated"):
        parts.append("_(result truncated)_")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)


@_register("lookup")
def _render_lookup(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}" + (f"\n\n_{r['hint']}_" if r.get("hint") else "")
    content = r.get("content", "")
    url = r.get("url", "")
    parts = [content]
    if url:
        parts.append(f"\n_source:_ <{url}>")
    return "\n".join(parts)


@_register("changelog")
def _render_changelog(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}"
    content = r.get("content", "")
    version = r.get("version")
    if version:
        return f"{content}\n\n_version:_ `{version}`"
    return content


def _render_handwritten(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}" + (f"\n\n_{r['hint']}_" if r.get("hint") else "")
    content = r.get("content", "")
    ref = r.get("ref")
    topic = r.get("topic")
    footer_parts = []
    if topic:
        footer_parts.append(f"topic: `{topic}`")
    if ref:
        footer_parts.append(f"ref: `{ref}`")
    if footer_parts:
        return f"{content}\n\n_{' · '.join(footer_parts)}_"
    return content


_RENDERERS["manual"] = _render_handwritten
_RENDERERS["reference"] = _render_handwritten
_RENDERERS["cli"] = _render_handwritten


@_register("mojo_version")
def _render_mojo_version(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}" + (f"\n\n_{r['hint']}_" if r.get("hint") else "")
    parts: list[str] = []
    # Accept both the production shape ({global_version, pinned_version, ...})
    # and the legacy test shape ({active, pinned, ...}).
    pinned = r.get("pinned_version") or r.get("pinned")
    active = r.get("global_version") or r.get("active")
    if pinned:
        parts.append(f"**Pinned:** {pinned}")
    if active:
        parts.append(f"**Active:** {active}")
    if r.get("version_file"):
        parts.append(f"**Source:** `{r['version_file']}`")
    if r.get("global_binary"):
        parts.append(f"**Global binary:** `{r['global_binary']}`")
    if r.get("docs_ref"):
        parts.append(f"**Docs ref:** `{r['docs_ref']}`")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts) or "(no version info)"


@_register("install_mojo")
def _render_install_mojo(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}" + (f"\n\n_{r['hint']}_" if r.get("hint") else "")
    status = r.get("status", "ok")
    parts = [f"**Status:** `{status}`"]
    if r.get("version"):
        parts.append(f"**Version:** {r['version']}")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)


@_register("update_server")
def _render_update_server(r: dict) -> str:
    if "error" in r:
        return f"**Error:** {r['error']}"
    status = r.get("status", "ok")
    parts = [f"**Status:** `{status}`"]
    if r.get("commit"):
        parts.append(f"**Commit:** `{r['commit']}`")
    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)


@_register("execute")
def _render_execute(r: dict) -> str:
    if "error" in r:
        out = [f"**Error:** {r['error']}"]
        if r.get("hint"):
            out.append(f"\n_{r['hint']}_")
        return "\n".join(out)

    parts: list[str] = []
    stdout = r.get("stdout", "")
    if stdout:
        fence = _fence(stdout)
        parts.append(f"### stdout\n{fence}\n{stdout}\n{fence}")
    stderr = r.get("stderr", "")
    if stderr:
        fence = _fence(stderr)
        parts.append(f"### stderr\n{fence}\n{stderr}\n{fence}")

    tail = _kv_lines(r, ["returncode", "duration_s", "mojo_version", "version_file", "error_summary"])
    if tail:
        parts.append("\n".join(tail))

    hints = r.get("gotcha_hints") or []
    if hints:
        parts.append("### Gotcha hints")
        for h in hints:
            title = h.get("title", h.get("id", "?"))
            fix = h.get("fix", "")
            parts.append(f"- **{title}** — {fix}" if fix else f"- **{title}**")

    if r.get("hint"):
        parts.append(f"\n_{r['hint']}_")
    return "\n".join(parts)
