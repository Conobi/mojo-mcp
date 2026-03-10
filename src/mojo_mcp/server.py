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
"""

import asyncio
import logging
import sys

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .docs import fetch_changelog, fetch_symbol_page, get_docs
from .sandbox import run_execute, run_install_mojo, run_list_files, run_mojo_version, run_read_file, run_search, run_update_server

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

app = Server("mojo-mcp")

# Docs are loaded once at startup and reused across all requests.
_docs: dict = {}

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
            }
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
        "Pass `include_paths` for `-I` flags (e.g. `[\".\""]` to import local packages). "
        "Pass `defines` for `-D` compile-time defines (e.g. `{\"ASSERT\": \"all\"}`). "
        "Typical project test invocation: cwd=<project_root>, include_paths=[\".\"], "
        "defines={\"ASSERT\": \"all\"}. "
        "If mojo is not installed, call `install_mojo` first."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Complete Mojo source file contents.",
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
        },
        "required": ["code"],
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
            }
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
    inputSchema={"type": "object", "properties": {}, "required": []},
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
            "path": {"type": "string", "description": "Absolute or relative path to the file."}
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
        },
        "required": ["path"],
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
            }
        },
        "required": ["query"],
    },
)

CHANGELOG_TOOL = types.Tool(
    name="changelog",
    description=(
        "Get the Mojo changelog. Cached for 1 hour. "
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
            }
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
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    loop = asyncio.get_event_loop()

    if name == "search":
        result = await loop.run_in_executor(
            None, run_search, arguments.get("code", ""), _docs
        )

    elif name == "execute":
        result = await loop.run_in_executor(
            None,
            run_execute,
            arguments.get("code", ""),
            arguments.get("cwd"),
            arguments.get("include_paths"),
            arguments.get("defines"),
            arguments.get("timeout", 30),
        )

    elif name == "mojo_version":
        result = await loop.run_in_executor(
            None, run_mojo_version, arguments.get("path")
        )

    elif name == "update_server":
        result = await loop.run_in_executor(None, run_update_server)

    elif name == "install_mojo":
        result = await loop.run_in_executor(
            None, run_install_mojo, arguments.get("version"), arguments.get("project_path")
        )

    elif name == "read_file":
        result = await loop.run_in_executor(
            None, run_read_file, arguments.get("path", "")
        )

    elif name == "list_files":
        result = await loop.run_in_executor(
            None, run_list_files, arguments.get("path", "."), arguments.get("pattern", "**/*.mojo")
        )

    elif name == "lookup":
        result = await fetch_symbol_page(arguments.get("query", ""))

    elif name == "changelog":
        result = await fetch_changelog(arguments.get("version"))

    else:
        raise ValueError(f"Unknown tool: {name}")

    return [types.TextContent(type="text", text=result)]


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
    asyncio.run(_run())


if __name__ == "__main__":
    main()
