# mojo-mcp — Claude Code Instructions

## Project Purpose

An MCP server that gives Claude Code deep, real-time access to the Mojo programming language:
the stdlib reference, full symbol documentation, live changelogs, and the user's own source files.
Designed for the **Code Mode** pattern: every tool returns structured text that fits in ~1–2K tokens.

---

## Repository Layout

```
src/mojo_mcp/
  __init__.py      — empty
  server.py        — MCP server, tool definitions, call_tool routing
  docs.py          — scraping, caching, symbol lookup, changelog
  sandbox.py       — file I/O helpers and Mojo code execution
  gotchas.py       — gotcha pattern matching engine (validate + error enrichment)
  gotchas.yaml     — known gotcha patterns database (version-filtered)
tests/
  conftest.py      — pytest fixtures, --run-mojo marker
  test_axi.py      — AXI ergonomic improvements (hints, compact JSON, empty states)
  test_gotchas.py  — gotcha loading and pattern matching tests
  test_validate.py — validate tool unit tests
  test_enrichment.py — execute error enrichment tests
  test_integration.py — live Mojo integration tests (requires mojo)
pyproject.toml     — package, deps, entry point
scripts/setup.sh   — install Mojo + sync deps
uv.lock
```

---

## Running the Server

```bash
uv run mojo-mcp          # start via stdio (normal MCP usage)
uv run pytest             # run unit tests
uv run pytest --run-mojo  # run all tests including live Mojo integration
```

---

## The Ten Tools

All tools are defined as `types.Tool` constants in `server.py` and routed in `call_tool`.

### 1. `search`
- **Input:** `code` — Python function body with access to `docs` dict
- **Backend:** `sandbox.run_search(code, docs)` — sandboxed `exec()` in a thread, 5 s timeout
- **Docs dict shape:** `{module_name: {name, description, structs, functions, traits, aliases}}`
  - Each item: `{name, signature, description}`
- **Note:** No imports available inside the snippet. Must use `return`.
- **Returns:** Wrapped metadata object (compact JSON):
  - Normal: `{result, hint}` — `result` is the Python return value, `hint` suggests `lookup`
  - Null: `{result: null, message, hint}` — when agent code returns `None`
  - Truncated (>8KB): `{result_raw, truncated: true, total_bytes, hint}` — `result_raw` is a truncated JSON string
  - Error/timeout: `{error}`
- **Breaking change (AXI):** Previously returned raw JSON; now always wrapped in metadata object

### 2. `execute`
- **Input:** `code` — complete Mojo source file (must have `def main()`)
- **Optional inputs:**
  - `cwd` — working directory for the subprocess (relative paths like `-I .` resolve from here); also used to locate `.mojo-version`
  - `include_paths` — list of strings passed as `-I` flags (e.g. `["."]` to import local packages)
  - `defines` — dict of compile-time defines passed as `-D KEY=VALUE` (e.g. `{"ASSERT": "all"}`)
  - `timeout` — process timeout in seconds (default 30)
- **Backend:** `sandbox.run_execute(code, cwd, include_paths, defines, timeout)` — writes to `mkdtemp`, runs `mojo run [flags] <file>` from `cwd`
- **Compiler resolution (`_mojo_cmd`):** pinned version → `uvx --from mojo-compiler==<ver> mojo`; no pin → `mojox` from project `.venv/bin/` if present; fallback → system `mojo`
- **mojox pipeline:** when `cwd` has a venv with `mojox`, it's used as the compiler frontend (auto-discovers installed Mojo packages). For version-pinned projects (bare `mojo` via uvx), `_find_mojo_packages()` manually injects `-I` for `mojo_packages/` and sets `LD_LIBRARY_PATH` for native libs.
- **Error enrichment:** failed executions and timeouts are automatically enriched with matching gotcha hints via `gotchas.enrich_error()`
- **Key fix:** subprocess `cwd` is set to the user's `cwd` (not `tmp_dir`) so that `-I .` resolves against the project root
- **Returns:** Compact JSON with AXI ergonomics:
  - Always: `{stdout, returncode, duration_s}`
  - On failure or non-empty stderr: `+ stderr`
  - On failure: `+ hint, error_summary` (first `error:` line extracted from stderr)
  - With pinned version: `+ mojo_version, version_file`
  - With gotcha matches: `+ gotcha_hints`
  - `stderr` is **omitted** on success when empty (saves tokens)
- **Typical project test:** `execute(code=..., cwd="/path/to/project", include_paths=["."], defines={"ASSERT": "all"})`

### 3. `read_file`
- **Input:** `path` — absolute or relative path
- **Backend:** `sandbox.run_read_file(path)` — sync I/O in executor
- **Blocked paths:** `/etc`, `/proc`, `/sys`, `/dev` (raises `Access denied` in result)
- **Cap:** 100 KB
- **Returns:** Compact JSON:
  - Normal: `{path, content}` — for non-`.mojo` files
  - `.mojo` file: `{path, content, hint}` — hint suggests `validate`
  - Truncated: `{path, content, truncated: true, total_bytes, hint}` — combined hint with size info + validate suggestion for `.mojo` files
  - Error: `{error}`

### 4. `list_files`
- **Input:** `path` (dir, optional — defaults to `"."`), `pattern` (glob, default `**/*.mojo`)
- **Backend:** `sandbox.run_list_files(path, pattern)` — sync I/O in executor
- **Cap:** 200 entries
- **Returns:** Compact JSON:
  - Always: `{path, pattern, files, count}`
  - Truncated: `+ truncated: true, hint`
  - Empty: `+ message, hint` (e.g. "0 files matching **/*.mojo in /path")
  - Non-empty: `+ hint` (suggests `read_file` / `validate`)

### 5. `lookup`
- **Input:** `query` — dot-notation symbol path, e.g. `collections.dict.Dict`
- **Backend:** `docs.fetch_symbol_page(query)` — async HTTP fetch + BeautifulSoup parse
- **URL construction:** `collections.dict.Dict` → `https://docs.modular.com/mojo/std/collections/dict/Dict`
  - Strips leading `std.` if present
  - Validates each segment matches `^[A-Za-z0-9_]+$`
  - Requires ≥ 2 components; returns helpful error on single token
- **Returns:** Markdown with: signature (code fence), description, Parameters, Implemented Traits, Methods (with per-method overloads + Args/Returns)
- **On 404:** Returns casing hint: "Symbol names are PascalCase, module names lowercase"

### 6. `changelog`
- **Input:** `version` (optional) — `"nightly"`, `"v26.1"`, `"v0.26.1"`, `"v25.5"`, or omit for latest 2
- **Backend:** `docs.fetch_changelog(version)` — async HTTP fetch + cache
- **Cache:** `~/.cache/mojo-mcp/changelog.json`, 7 day TTL
- **Version matching (`_match_version`):**
  - `None` / `"latest"` → first 2 non-`_fetched_at` keys
  - `"nightly"` → exact key `"nightly"`
  - `"v26.1"` fuzzy-matches `"v0.26.1"` by stripping leading zeros
- **Returns:** Markdown with H2 version heading + H3 subsections + bullet lists

### 7. `validate`
- **Input:** `code` (Mojo source string), `path` (file path, ignored if code provided), `mojo_version` (optional, auto-detected)
- **Backend:** `sandbox.run_validate(code, path, mojo_version)` → `gotchas.validate_code()`
- **Pattern database:** `gotchas.yaml` — each entry has `id`, `severity`, `mojo_versions` (semver filter), `code_pattern` (regex), `error_pattern`, `timeout_pattern`, `description`, `fix`
- **Returns:** Compact JSON:
  - Clean: `{issues: [], count: 0, message, hint}` — confirms check ran, suggests `execute`
  - Issues: `{issues: [{id, title, severity, description, fix, link?}], count, hint}` — suggests fixing then `execute`
  - Error: `{error, hint}` — includes usage example
- **Also used by:** `execute` tool — failed executions and timeouts are automatically enriched with matching gotcha hints via `gotchas.enrich_error()`

---

## Docs Cache

- **Path:** `~/.cache/mojo-mcp/docs.json`
- **TTL:** 14 days (`CACHE_TTL = 1209600`)
- **Built by:** `docs.build_docs_index()` — 3-level scrape of `docs.modular.com/mojo/std/`
  1. Index page → package URLs (`/mojo/std/{pkg}/`)
  2. Package pages → module URLs (`/mojo/std/{pkg}/{module}/`)
  3. Module pages → `_parse_module_page()` → structured dict
- **Loaded at startup** by `server._run()` into the global `_docs` dict
- **Parsed with:** `lxml` backend via BeautifulSoup; `_ZWS` (Unicode zero-width space) stripped from all text

---

## Key Conventions

### Async vs sync
- `lookup` and `changelog` are `async def` → called with direct `await` in `call_tool`
- `search`, `execute`, `read_file`, `list_files` are sync → wrapped in `run_in_executor`

### Adding a new tool
1. Define a `types.Tool` constant in `server.py` (name, description, inputSchema)
2. Add to the `list_tools()` return list
3. Add an `if name == "..."` branch in `call_tool` — use `await` for async backends, `run_in_executor` for sync
4. Implement the backend in `docs.py` (network/parsing) or `sandbox.py` (I/O/execution)

### `_parse_symbol_page` heuristic
The signature is extracted by walking siblings after `<h1>`, looking for the first `<code>` whose first word is in `{"struct", "fn", "alias", "trait"}` or contains `(` or `[`. This is fragile against layout changes; adjust if Modular redesigns their docs.

### Changelog structure
Each Mojo version is a `<section>` element whose **direct** first child is an `<h2>`. Nested `<section>` children hold subsections (H3 + UL). The `_fetch_and_parse_changelog` function relies on this structure.

### Output conventions (AXI-inspired)
All tool responses use compact JSON (`_json()` helper — no indentation, `separators=(",",":")`) to minimize token cost. Key conventions:

- **`"hint"` key:** Contextual next-step suggestion. Present on errors, empty states, and list outputs. Omitted on self-contained detail views (`lookup`, `changelog`). Always a single string, agent-agnostic (no "Claude Code" references).
- **Empty states:** When the result is "nothing", include an explicit `"message"` (e.g. `"0 files matching..."`, `"No known gotcha patterns matched."`) plus a `"hint"`.
- **Truncation:** Use `"truncated": true` + `"total_bytes": N` metadata fields instead of inline text in content. Combined hints cover both the truncation and next-step suggestion.
- **Omit empty fields:** `stderr` omitted from `execute` on success when empty. `duration_s` always included.
- **Error summaries:** `execute` failures include `"error_summary"` — the first `error:` line extracted from stderr (handles file-prefixed Mojo errors like `/path:3:5: error: ...`).
- **Idempotent mutations:** `install_mojo` returns `"already_pinned"` instead of overwriting when the version matches.

---

## Dependencies

| Package | Why |
|---|---|
| `mcp>=1.0` | MCP protocol (stdio server, types) |
| `httpx>=0.27` | Async HTTP for docs/changelog fetches |
| `beautifulsoup4>=4.12` | HTML parsing |
| `lxml>=5.0` | Fast BS4 backend (must be installed separately) |
| `pyyaml>=6.0` | Gotchas YAML parsing |

Dev dependencies (`[project.optional-dependencies] dev`): `pytest>=8.0`, `pytest-asyncio>=0.23`.

---

## Security Constraints

- `run_read_file`: blocks `/etc`, `/proc`, `/sys`, `/dev` by prefix. Root `/` is intentionally NOT blocked (would prevent reading any file). Absolute and relative paths both work via `Path.resolve()`.
- `run_execute`: runs in a fresh `mkdtemp` directory, cleaned up in `finally`. Relies on OS-level sandboxing — no seccomp/namespaces.
- `run_search`: uses a restricted `__builtins__` dict — no `open`, `import`, `exec`, `eval`, or `__import__` exposed.

---

## Cache Files

```
~/.cache/mojo-mcp/
  docs.json        # stdlib module index (14d TTL)
  changelog.json   # changelog by version (7d TTL)
```

To force a refresh, delete the relevant file.

---

## LSP Plugin Status

The `mojo-lsp` plugin (in `plugins/mojo-lsp/`) ships a Claude Code plugin that configures `mojo-lsp-server` for `.mojo` files. **It is currently disabled** in user settings due to persistent false-positive diagnostics in `mojo-lsp-server`:

1. **Circular import breaks method resolution** — reports `no matching function` on every save for code that compiles fine
2. **Stale symbol table after file writes** — reports missing attributes after new methods are added
3. **`main` in package member** — permanent false positives on all test files defining `main()`

These false positives cause agents to waste tokens triaging phantom errors and to "fix" working code. Until `mojo-lsp-server` improves, agents should use `execute` as the ground truth for code validity and the MCP tools (`search`, `lookup`) for navigation.

The plugin remains in the repo for when `mojo-lsp-server` diagnostics improve. A compiler-backed proxy design is shelved in `specs/2026-04-09-mojo-check-proxy.md`.

---

## Known Limitations / Future Work

- `search` returns shallow index data (name + short description). Use `lookup` for full signatures.
- `_parse_symbol_page` only parses the first-level method list; deeply nested overload detail is partial.
- No authentication — all fetches are unauthenticated public docs.
- `is_x86()` detection moved to `sys.info.CompilationTarget` in Mojo v25.5; the docs index does not yet expose struct methods, so `lookup` on `CompilationTarget` may be incomplete.
- The `execute` tool requires a real `mojo` binary or `mojox`. If not installed: returns a helpful error pointing to `uv add mojox` or `uv tool install mojo`.
- `execute` runs the subprocess from `cwd` (not `tmp_dir`) so `-I .` resolves against the project root; the temp file path is absolute so this is safe.
- `execute` default timeout raised to 30 s (was 10 s) to accommodate project compilation.
