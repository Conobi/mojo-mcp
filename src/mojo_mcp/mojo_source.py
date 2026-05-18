"""Extract signatures and docstrings from raw Mojo source files.

This module exists so the MCP server can answer version-pinned stdlib lookups
without invoking the Mojo compiler (which can't introspect the compiled
`stdlib.mojopkg`). Inputs are raw `.mojo` source files (typically fetched from
`modular/modular` at a release tag); outputs are structured dicts with the
declaration signature and docstring.

The extractor is deliberately naïve — regex + a small bracket-counting state
machine. It handles:

- Module-level docstrings (first triple-quoted block at column 0).
- Top-level `struct`, `fn`, `def`, `trait`, `alias`, `comptime` declarations.
- Multi-line signatures with parameter lists, trait lists, and `where` clauses.
- Decorator chains (`@deco_a` / `@deco_b` immediately above the declaration).
- Overloads (multiple top-level declarations of the same name).
- PEP-257-style docstring dedenting.

It does NOT handle:

- Methods inside structs/traits (only top-level declarations).
- Triple-quotes nested inside docstrings (Mojo docs use backtick fences
  instead, so this is rare in practice).
- Single-line string boundary parsing for `#` characters (we may stop at a
  `#` inside a single-quoted literal while scanning a line, but that only
  affects which characters on that one line we look at — it never changes
  which lines count as "inside a multi-line string").
"""

from __future__ import annotations

import re
from typing import TypedDict

_DECL_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<kw>struct|fn|def|trait|alias|comptime)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_TRIPLE_QUOTES = ('"""', "'''")
_KEYWORDS_WITH_BODY = {"struct", "fn", "def", "trait"}


class Declaration(TypedDict):
    decorators: list[str]
    signature: str
    docstring: str
    line: int


class ExtractedSymbol(TypedDict):
    name: str
    kind: str
    declarations: list[Declaration]


def extract_module_docstring(source: str) -> str | None:
    """Return the module-level docstring at column 0, or None if absent."""
    lines = source.splitlines()
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if raw and raw[0] not in ('"', "'"):
            return None
        for q in _TRIPLE_QUOTES:
            if raw.startswith(q):
                return _read_triple_string(lines, i, q)[0]
        return None
    return None


def extract_symbol(source: str, name: str) -> ExtractedSymbol | None:
    """Find all top-level declarations of `name`. Returns None if not found."""
    lines = source.splitlines()
    in_string = _string_state_map(lines)

    declarations: list[Declaration] = []
    kind: str | None = None
    n = len(lines)
    i = 0
    while i < n:
        if in_string[i]:
            i += 1
            continue
        m = _DECL_RE.match(lines[i])
        if not m or m.group("name") != name or len(m.group("indent")) != 0:
            i += 1
            continue

        kw = m.group("kw")
        kind = kw
        decorators = _gather_decorators(lines, i, 0)
        signature, body_start = _extend_signature(lines, i, kw)
        doc_indent = 1 if kw in _KEYWORDS_WITH_BODY else 0
        docstring = _find_docstring_after(lines, body_start, doc_indent)
        declarations.append(
            Declaration(
                decorators=decorators,
                signature=signature,
                docstring=docstring,
                line=i + 1,
            )
        )
        i = body_start
    if not declarations:
        return None
    return ExtractedSymbol(name=name, kind=kind or "", declarations=declarations)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_triple_string(
    lines: list[str], start_line: int, quote: str
) -> tuple[str, int]:
    """Read a triple-quoted block starting at lines[start_line].

    Returns (dedented_content, end_line_idx_exclusive).
    """
    n = len(lines)
    first = lines[start_line]
    qpos = first.index(quote)
    rest = first[qpos + len(quote):]
    end_pos = rest.find(quote)
    if end_pos != -1:
        return rest[:end_pos].strip(), start_line + 1
    body = [rest]
    j = start_line + 1
    while j < n:
        line = lines[j]
        idx = line.find(quote)
        if idx != -1:
            body.append(line[:idx])
            return _dedent_docstring(body), j + 1
        body.append(line)
        j += 1
    return _dedent_docstring(body), n


def _dedent_docstring(parts: list[str]) -> str:
    """PEP 257 dedent: keep first line as-is (stripped), strip common indent
    from rest, drop trailing blank lines.
    """
    if not parts:
        return ""
    first = parts[0]
    rest = parts[1:]
    indents = [
        len(line) - len(line.lstrip()) for line in rest if line.strip()
    ]
    common = min(indents) if indents else 0
    out = [first.strip()]
    for line in rest:
        if line.strip():
            out.append(line[common:].rstrip())
        else:
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _gather_decorators(lines: list[str], at_idx: int, indent: int) -> list[str]:
    """Walk backwards from at_idx-1 collecting `@decorator` lines at the given indent."""
    decorators: list[str] = []
    i = at_idx - 1
    while i >= 0:
        line = lines[i]
        stripped = line.lstrip()
        if not stripped.startswith("@"):
            break
        line_indent = len(line) - len(stripped)
        if line_indent != indent:
            break
        decorators.insert(0, line.strip())
        i -= 1
    return decorators


def _extend_signature(
    lines: list[str], start_idx: int, kw: str
) -> tuple[str, int]:
    """Walk forward, joining lines into one signature until brackets balance.

    For struct/fn/def/trait the declaration must also end with `:`. For
    alias/comptime a balanced first-line is enough.
    """
    parts: list[str] = []
    paren = bracket = 0
    n = len(lines)
    i = start_idx
    while i < n:
        line = lines[i]
        code = _strip_inline_comment(line)
        parts.append(line)
        for ch in code:
            if ch == "[":
                bracket += 1
            elif ch == "]":
                bracket -= 1
            elif ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
        if paren == 0 and bracket == 0:
            if kw in _KEYWORDS_WITH_BODY:
                if code.rstrip().endswith(":"):
                    return "\n".join(parts), i + 1
            else:
                return "\n".join(parts), i + 1
        i += 1
    return "\n".join(parts), i


def _strip_inline_comment(line: str) -> str:
    """Remove `# ...` from `line`, ignoring `#` inside single-line strings."""
    in_str = False
    quote = ""
    for i, ch in enumerate(line):
        if in_str:
            if ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            continue
        if ch == "#":
            return line[:i]
    return line


def _find_docstring_after(
    lines: list[str], start_idx: int, indent: int
) -> str:
    """Look for a docstring on or after `start_idx` at indent >= `indent`."""
    n = len(lines)
    i = start_idx
    while i < n and lines[i].strip() == "":
        i += 1
    if i >= n:
        return ""
    line = lines[i]
    stripped = line.lstrip()
    line_indent = len(line) - len(stripped)
    if line_indent < indent:
        return ""
    for q in _TRIPLE_QUOTES:
        if stripped.startswith(q):
            content, _ = _read_triple_string(lines, i, q)
            return content
    return ""


def _string_state_map(lines: list[str]) -> list[bool]:
    """Return a list where state[i] is True iff line `i` starts inside an
    unclosed triple-quoted string opened on a previous line.

    Single-line strings (`'...'`, `"..."`) and code outside triple-strings
    don't affect the state we care about — only the multi-line-string spans.
    """
    state: list[bool] = []
    in_string = False
    quote = ""
    for line in lines:
        state.append(in_string)
        i = 0
        n = len(line)
        while i < n:
            if not in_string:
                if line[i] == "#":
                    break
                matched = False
                for q in _TRIPLE_QUOTES:
                    if line.startswith(q, i):
                        in_string = True
                        quote = q
                        i += 3
                        matched = True
                        break
                if not matched:
                    i += 1
            else:
                if line.startswith(quote, i):
                    in_string = False
                    i += 3
                else:
                    i += 1
    return state
