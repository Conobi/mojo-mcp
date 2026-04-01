# mojo-mcp

An MCP server that gives Claude Code deep access to the [Mojo](https://www.modular.com/mojo) programming language — stdlib reference, full symbol documentation, live changelogs, and your own project files.

## Tools

| Tool | What it does |
|---|---|
| `search` | Query the cached stdlib index with a Python snippet. Returns structs, functions, traits, aliases across all modules. |
| `execute` | Run a complete Mojo file. Returns stdout, stderr, and return code. Pass `cwd` to pick up a project-pinned version from `.mojo-version`. Auto-detects `mojox` in the project venv and discovers installed Mojo packages. Failed runs are auto-enriched with gotcha hints. |
| `validate` | Check Mojo source against known gotcha patterns before compilation. Catches deprecated APIs, compile hangs, and common pitfalls with fix suggestions. |
| `lookup` | Fetch full documentation for one symbol: signature, parameters, methods, args/returns. |
| `changelog` | Get the Mojo changelog. Filter by version or get the latest two releases. |
| `read_file` | Read a file from your project (up to 100 KB). |
| `list_files` | List files in a directory by glob pattern. Defaults to `**/*.mojo`. |
| `mojo_version` | Report the globally installed Mojo version and any project-pinned version from `.mojo-version`. |
| `install_mojo` | Install, upgrade, or pin the Mojo version (globally or per-project via `.mojo-version`). |
| `update_server` | Pull the latest mojo-mcp from GitHub into the uvx cache. Restart Claude Code after to apply. |

`search` finds things by name; `lookup` gives you the full contract once you know what you're looking for. `validate` catches common pitfalls before you even compile. Together they let Claude audit a Mojo project for outdated APIs without ever leaving the conversation.

---

## Installation

**Prerequisite:** [uv](https://docs.astral.sh/uv/).

```bash
# 1. MCP server — stdlib docs, search, lookup, changelog, execute
claude mcp add mojo-mcp --scope user -- uvx --from git+https://github.com/Conobi/mojo-mcp mojo-mcp

# 2. LSP plugin — go-to-definition, hover, diagnostics in .mojo files
claude plugin marketplace add Conobi/mojo-mcp
claude plugin install mojo-lsp
```

`uvx` fetches the MCP package directly from GitHub into an isolated environment. No clone, no manual config.

| Component | What it gives Claude |
|---|---|
| MCP server | Stdlib search, full symbol docs, changelog, file access, code execution |
| LSP plugin | Go-to-definition, find-references, hover, diagnostics in your `.mojo` files |

Use `--scope project` on the `mcp add` command to commit the server into `.mcp.json` and share it with your team.

On first start the server scrapes and indexes the full Mojo stdlib (~200 module pages). This takes 30–60 seconds and is then cached for 14 days at `~/.cache/mojo-mcp/docs.json`. Subsequent starts are instant.

> **Note:** Both `execute` and the LSP plugin require a Mojo compiler. For projects using [`mojox`](https://github.com/Conobi/mojox), the `execute` tool auto-detects it in your project's `.venv` and automatically discovers installed Mojo packages — no global install or manual `-I` flags needed. Otherwise, use the `install_mojo` tool or run `uv tool install mojo-compiler` manually. The other tools work without a compiler.

---

## Updating

Ask Claude to run the `update_server` tool, or run this directly:

```bash
uvx --refresh --from git+https://github.com/Conobi/mojo-mcp mojo-mcp --version
```

Then restart Claude Code to load the new version. The `--refresh` flag forces uvx to re-fetch the latest commit from GitHub regardless of what's cached.

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

### Validate before compiling

```
validate: code="def main():\n    var s = 'hi'\n    print(s[0])"
validate: path=/path/to/my-project/src/main.mojo
```

Returns a list of issues with severity, description, and suggested fixes. The `execute` tool also automatically enriches compiler errors and timeouts with matching gotcha hints.

### Audit a project

```
list_files: /path/to/my-project
read_file:  /path/to/my-project/src/main.mojo
```

Combine with `validate`, `lookup`, and `changelog` to find outdated APIs, renamed functions, and removed symbols.

---

## Cache

| File | TTL | Contents |
|---|---|---|
| `~/.cache/mojo-mcp/docs.json` | 14 days | Full stdlib index |
| `~/.cache/mojo-mcp/changelog.json` | 7 days | Changelog by version |

Delete a file to force a refresh on next use.

---

## Development

```bash
uv sync --all-extras           # install deps + dev extras
uv run mojo-mcp                # run the server manually

# Unit tests (no Mojo binary required)
uv run pytest

# Full test suite including live Mojo integration
uv run pytest --run-mojo
```

### Project structure

```
src/mojo_mcp/
  server.py      — MCP server, tool definitions, routing
  docs.py        — stdlib scraping/caching, lookup, changelog
  sandbox.py     — file I/O, Mojo execution, validate
  gotchas.py     — gotcha pattern matching engine
  gotchas.yaml   — known gotcha patterns (version-filtered)
tests/
  conftest.py    — pytest config, --run-mojo marker
  test_gotchas.py, test_validate.py, test_enrichment.py, test_integration.py
```

---

## License

MIT
