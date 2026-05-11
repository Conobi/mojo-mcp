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
