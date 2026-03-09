# mojo-mcp

An MCP server that gives Claude Code deep access to the [Mojo](https://www.modular.com/mojo) programming language — stdlib reference, full symbol documentation, live changelogs, and your own project files.

## Tools

| Tool | What it does |
|---|---|
| `search` | Query the cached stdlib index with a Python snippet. Returns structs, functions, traits, aliases across all modules. |
| `execute` | Run a complete Mojo file. Returns stdout, stderr, and return code. |
| `lookup` | Fetch full documentation for one symbol: signature, parameters, methods, args/returns. |
| `changelog` | Get the Mojo changelog. Filter by version or get the latest two releases. |
| `read_file` | Read a file from your project (up to 100 KB). |
| `list_files` | List files in a directory by glob pattern. Defaults to `**/*.mojo`. |

### Why six tools?

`search` finds things by name; `lookup` gives you the full contract once you know what you're looking for. Together they let Claude audit a Mojo project for outdated APIs without ever leaving the conversation.

---

## Installation

**Prerequisites:** Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> && cd mojo-mcp
bash scripts/setup.sh
```

The setup script installs the Mojo compiler via `uv tool install modular` and syncs the Python dependencies. The `execute` tool requires a working `mojo` binary; the other five tools work without it.

---

## Configuration

Add this to your `~/.claude/mcp.json` (adjust the path):

```json
{
  "mcpServers": {
    "mojo": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/mojo-mcp", "mojo-mcp"]
    }
  }
}
```

On first start the server scrapes and indexes the full Mojo stdlib (~200 module pages). This takes 30–60 seconds and is then cached for 24 hours at `~/.cache/mojo-mcp/docs.json`. Subsequent starts are instant.

---

## Usage examples

### Discover symbols

```
search: return [f['name'] for f in docs['collections.dict']['structs']]
```

### Get the full signature

```
lookup: collections.dict.Dict
```

Returns the full struct signature, parameters, implemented traits, and all methods with their overloads and arg/return docs.

### Check what changed recently

```
changelog:              # latest 2 releases
changelog: nightly      # nightly only
changelog: v26.1        # specific version
```

### Audit a project

```
list_files: /path/to/my-project
read_file:  /path/to/my-project/src/main.mojo
```

Combine with `lookup` and `changelog` to find outdated APIs, renamed functions, and removed symbols.

---

## Cache

| File | TTL | Contents |
|---|---|---|
| `~/.cache/mojo-mcp/docs.json` | 24 h | Full stdlib index |
| `~/.cache/mojo-mcp/changelog.json` | 1 h | Changelog by version |

Delete a file to force a refresh on next use.

---

## Development

```bash
uv sync                        # install deps
uv run mojo-mcp                # run the server manually

# Quick smoke tests
uv run python -c "
import asyncio
from mojo_mcp.docs import fetch_symbol_page, fetch_changelog
from mojo_mcp.sandbox import run_read_file, run_list_files

print(asyncio.run(fetch_symbol_page('collections.dict.Dict'))[:300])
print(asyncio.run(fetch_changelog('nightly'))[:300])
print(run_read_file('pyproject.toml'))
print(run_list_files('.', '**/*.py'))
"
```

### Project structure

```
src/mojo_mcp/
  server.py    — MCP server, tool definitions, routing
  docs.py      — stdlib scraping/caching, lookup, changelog
  sandbox.py   — file I/O, Mojo execution
```

---

## License

MIT
