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
