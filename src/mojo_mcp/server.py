"""Mojo MCP Server — Code Mode pattern.

Tools exposed:
  search(code)                             — query the Mojo stdlib docs programmatically
  execute(code, cwd, include_paths, ...)   — run a .mojo file with project flags
  mojo_version(path)                       — report global and project-pinned Mojo versions
  install_mojo(...)                        — install/upgrade Mojo globally or pin a project version
  read_file(path)                          — read a source file
  list_files(path)                         — list .mojo files in a directory
  lookup(query)                            — fetch full symbol docs
  changelog(version)                       — get Mojo changelog
  validate(code, path, mojo_version)       — check code against known gotcha patterns
"""

import asyncio
import logging
import sys

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .docs import fetch_changelog, fetch_symbol_page, get_docs
from .formatting import render
from .sandbox import run_execute, run_install_mojo, run_list_files, run_mojo_version, run_read_file, run_search, run_update_server, run_validate

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

app = Server("mojo-mcp")

# Docs are loaded once at startup and reused across all requests.
_docs: dict = {}


def _to_dict(s: str) -> dict:
    """Parse a sandbox-emitted JSON string back into a dict."""
    import json
    try:
        v = json.loads(s)
    except Exception:
        return {"error": "internal: invalid JSON from sandbox", "raw": s[:500]}
    return v if isinstance(v, dict) else {"value": v}

_FORMAT_PROP = {
    "type": "string",
    "enum": ["md", "json"],
    "default": "md",
    "description": "Response format. Default markdown for LLM readability; json for programmatic consumption.",
}


SEARCH_TOOL = types.Tool(
    name="search",
    description=(
        "Search the Mojo stdlib reference. "
        "You have access to a `docs` dict structured as: "
        "{module_name: {name, description, structs: [{name, signature, description}], "
        "functions: [{name, signature, description}], traits: [...], aliases: [...]}}. "
        "Write a Python function body (use `return`) that filters or transforms `docs` "
        "and returns what you need. No imports available. "
        "Use `lookup` instead when you need full signatures or method details."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python function body with access to `docs`. Must use `return`.",
            },
            "format": _FORMAT_PROP,
        },
        "required": ["code"],
    },
)

EXECUTE_TOOL = types.Tool(
    name="execute",
    description=(
        "Execute Mojo code. Write a complete .mojo file (include `fn main()`). "
        "Returns stdout, stderr, and return code. Default timeout: 30 seconds. "
        "Pass `cwd` to set the working directory; this also enables project-local "
        "version selection (nearest .mojo-version file is honoured). "
        "Pass `include_paths` for `-I` flags (e.g. `[\".\"]` to import local packages). "
        "Pass `defines` for `-D` compile-time defines (e.g. `{\"ASSERT\": \"all\"}`). "
        "Typical project test invocation: cwd=<project_root>, include_paths=[\".\"], "
        "defines={\"ASSERT\": \"all\"}. "
        "If mojo is not installed, call `install_mojo` first. "
        "Provide exactly one of `code` (inline source) or `path` (file to read). "
        "For code longer than ~20 lines, prefer `path` to avoid Claude Code's "
        "multiline parameter rendering issue (anthropics/claude-code#13359)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Complete Mojo source file contents.",
            },
            "path": {
                "type": "string",
                "description": (
                    "Path to a .mojo source file. Resolved against `cwd` if relative, "
                    "or absolute. Use this for code longer than ~20 lines to avoid "
                    "Claude Code's multiline parameter rendering issue "
                    "(upstream: anthropics/claude-code#13359)."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory for the Mojo process. Relative include paths "
                    "are resolved from here. Also used to locate a .mojo-version file."
                ),
            },
            "include_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of paths passed as -I flags. "
                    "Use [\".\"] to import packages from the project root (cwd)."
                ),
            },
            "defines": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Compile-time -D defines. Each key-value pair becomes "
                    "`-D KEY=VALUE`; an empty string value becomes `-D KEY`. "
                    "Example: {\"ASSERT\": \"all\"}."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Process timeout in seconds (default 30).",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
)

MOJO_VERSION_TOOL = types.Tool(
    name="mojo_version",
    description=(
        "Report the active Mojo version(s). "
        "Returns the globally installed mojo binary version and, if a .mojo-version "
        "file is found by walking up from `path`, the project-pinned version and its file location. "
        "Call this before executing to understand which version will be used."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Optional path to start searching for a .mojo-version file.",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
)

UPDATE_SERVER_TOOL = types.Tool(
    name="update_server",
    description=(
        "Pull the latest mojo-mcp from GitHub and refresh the uvx cache. "
        "The running server is NOT replaced — the user must restart Claude Code "
        "after this completes to load the new version."
    ),
    inputSchema={
        "type": "object",
        "properties": {"format": _FORMAT_PROP},
        "required": [],
    },
)

INSTALL_MOJO_TOOL = types.Tool(
    name="install_mojo",
    description=(
        "Install, upgrade, or pin the Mojo version used by a project. "
        "All operations require uv to be installed. "
        "\n\nBehaviours (by argument combination):"
        "\n- version + project_path → write .mojo-version in project_path and warm the uvx cache"
        "\n- project_path only      → remove .mojo-version (revert project to global mojo)"
        "\n- version only           → uv tool install modular==<version> globally"
        "\n- neither                → uv tool install modular (latest) or upgrade if already installed"
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "version": {
                "type": "string",
                "description": "Modular package version, e.g. '26.1.0' or '25.1.0'.",
            },
            "project_path": {
                "type": "string",
                "description": "Project root directory in which to write (or remove) .mojo-version.",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
)

READ_FILE_TOOL = types.Tool(
    name="read_file",
    description=(
        "Read a file from the user's project. "
        "Returns file path and contents (capped at 100 KB). "
        "Use this to inspect .mojo source files during audits."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative path to the file."},
            "format": _FORMAT_PROP,
        },
        "required": ["path"],
    },
)

LIST_FILES_TOOL = types.Tool(
    name="list_files",
    description=(
        "List files in a directory matching a glob pattern. "
        "Defaults to **/*.mojo. Returns up to 200 sorted paths. "
        "Use this to discover the structure of a Mojo project."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to search."},
            "pattern": {"type": "string", "description": "Glob pattern (default: **/*.mojo)."},
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
)

LOOKUP_TOOL = types.Tool(
    name="lookup",
    description=(
        "Fetch full documentation for a specific Mojo symbol. "
        "Input: dot-notation path like 'collections.dict.Dict' or 'math.math.abs'. "
        "Returns Markdown with full signature, parameters, methods (structs), args/returns (functions). "
        "Use `search` to discover names, then `lookup` for full details."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Dot-notation symbol path. E.g. 'collections.dict.Dict', 'builtin.int.Int'.",
            },
            "format": _FORMAT_PROP,
        },
        "required": ["query"],
    },
)

CHANGELOG_TOOL = types.Tool(
    name="changelog",
    description=(
        "Get the Mojo changelog. Cached for 7 days. "
        "No version → latest 2 releases. "
        "Pass version to filter: 'nightly', 'v26.1', 'v0.26.1', 'v25.5'. "
        "Returns Markdown with language and stdlib changes."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "version": {
                "type": "string",
                "description": "Optional version. Examples: 'nightly', 'v26.1', 'v0.25.7'.",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
)


VALIDATE_TOOL = types.Tool(
    name="validate",
    description=(
        "Validate Mojo source code against known gotcha patterns before compilation. "
        "Checks for common pitfalls: Variant in loops (compile hang), module-level vars, "
        "deprecated APIs, missing initializers, and more. "
        "Pass `code` for in-memory validation or `path` to check a file on disk. "
        "Returns a list of issues with severity, description, and fix suggestions."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Mojo source code to validate.",
            },
            "path": {
                "type": "string",
                "description": "Path to a .mojo file to validate. Ignored if code is provided.",
            },
            "mojo_version": {
                "type": "string",
                "description": "Mojo version for filtering patterns (e.g. '0.26.2'). Auto-detected if omitted.",
            },
            "format": _FORMAT_PROP,
        },
        "required": [],
    },
)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        SEARCH_TOOL,
        EXECUTE_TOOL,
        MOJO_VERSION_TOOL,
        INSTALL_MOJO_TOOL,
        UPDATE_SERVER_TOOL,
        READ_FILE_TOOL,
        LIST_FILES_TOOL,
        LOOKUP_TOOL,
        CHANGELOG_TOOL,
        VALIDATE_TOOL,
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    loop = asyncio.get_event_loop()
    fmt = arguments.get("format", "md")
    if fmt not in ("md", "json"):
        return [types.TextContent(type="text", text=render(
            {"error": f"Invalid format {fmt!r}. Use 'md' or 'json'.",
             "hint": "Omit `format` to default to markdown."},
            "md", tool="search",
        ))]

    if name == "search":
        raw = await loop.run_in_executor(None, run_search, arguments.get("code", ""), _docs)
        result_dict = _to_dict(raw)

    elif name == "execute":
        code = arguments.get("code")
        path = arguments.get("path")
        cwd = arguments.get("cwd")
        if code and path:
            result_dict = {
                "error": "Provide either 'code' or 'path', not both.",
                "hint": "Use 'code' for inline snippets; 'path' for files on disk.",
            }
            return [types.TextContent(type="text", text=render(result_dict, fmt, tool=name))]
        if not code and not path:
            result_dict = {
                "error": "Provide 'code' (inline source) or 'path' (file to read).",
                "hint": "execute(code='def main(): print(42)') or execute(path='./main.mojo').",
            }
            return [types.TextContent(type="text", text=render(result_dict, fmt, tool=name))]
        if path:
            from pathlib import Path as _P
            p = _P(path)
            if not p.is_absolute() and cwd:
                p = _P(cwd) / p
            try:
                source = p.read_text()
            except FileNotFoundError:
                result_dict = {
                    "error": f"File not found: {p}",
                    "hint": "Check the path or use list_files to discover files.",
                }
                return [types.TextContent(type="text", text=render(result_dict, fmt, tool=name))]
            except OSError as e:
                result_dict = {"error": f"Could not read {p}: {e}"}
                return [types.TextContent(type="text", text=render(result_dict, fmt, tool=name))]
        else:
            source = code

        assert source is not None  # narrowed by both-XOR / neither-XOR early returns above
        raw = await loop.run_in_executor(
            None, run_execute,
            source,
            cwd,
            arguments.get("include_paths"),
            arguments.get("defines"),
            arguments.get("timeout", 30),
        )
        result_dict = _to_dict(raw)

    elif name == "mojo_version":
        raw = await loop.run_in_executor(None, run_mojo_version, arguments.get("path"))
        result_dict = _to_dict(raw)

    elif name == "update_server":
        raw = await loop.run_in_executor(None, run_update_server)
        result_dict = _to_dict(raw)

    elif name == "install_mojo":
        raw = await loop.run_in_executor(
            None, run_install_mojo,
            arguments.get("version"), arguments.get("project_path"),
        )
        result_dict = _to_dict(raw)

    elif name == "read_file":
        raw = await loop.run_in_executor(None, run_read_file, arguments.get("path", ""))
        result_dict = _to_dict(raw)

    elif name == "list_files":
        raw = await loop.run_in_executor(
            None, run_list_files,
            arguments.get("path", "."), arguments.get("pattern", "**/*.mojo"),
        )
        result_dict = _to_dict(raw)

    elif name == "lookup":
        md = await fetch_symbol_page(arguments.get("query", ""))
        result_dict = {"content": md, "url": ""}

    elif name == "changelog":
        md = await fetch_changelog(arguments.get("version"))
        result_dict = {"content": md, "version": arguments.get("version") or "latest"}

    elif name == "validate":
        raw = await loop.run_in_executor(
            None, run_validate,
            arguments.get("code"), arguments.get("path"), arguments.get("mojo_version"),
        )
        result_dict = _to_dict(raw)

    else:
        result_dict = {
            "error": f"Unknown tool: {name}",
            "hint": "Available tools: search, execute, lookup, changelog, validate, read_file, list_files, mojo_version, install_mojo, update_server",
        }
        return [types.TextContent(type="text", text=render(result_dict, fmt, tool="search"))]

    return [types.TextContent(type="text", text=render(result_dict, fmt, tool=name))]


async def _run() -> None:
    global _docs

    logger.info("mojo-mcp: loading Mojo stdlib docs (may take a minute on first run)...")
    _docs = await get_docs()
    logger.info("mojo-mcp: ready. %d modules indexed.", len(_docs))

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def main() -> None:
    import sys
    if "--version" in sys.argv:
        import json
        from importlib.metadata import Distribution
        dist = Distribution.from_name("mojo-mcp")
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            info = json.loads(direct_url)
            commit = info.get("vcs_info", {}).get("commit_id", "")
            if commit:
                print(commit[:12])
                return
        # Fallback for editable / non-git installs
        print(dist.metadata["Version"])
        return
    asyncio.run(_run())


if __name__ == "__main__":
    main()
