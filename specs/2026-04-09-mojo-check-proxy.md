# Spec: mojo-check-proxy — Compiler-Backed LSP Diagnostic Proxy

**Date:** 2026-04-09
**Status:** Approved
**Triggered by:** LSP false-positive feedback from mojo-net development (3 documented issues affecting agent accuracy)

---

## Problem

The official `mojo-lsp-server` produces persistent false-positive diagnostics:

1. **Circular import breaks method resolution** — reports `no matching function` on every save for code that compiles fine
2. **Stale symbol table after file writes** — reports missing attributes after new methods are added and committed
3. **`main` in package member** — 34 permanent false positives across all test files

These false positives cause Claude Code agents to:
- Waste tokens triaging phantom errors
- Attempt to "fix" working code (especially Issue 2 — the agent thinks *it* made a mistake)
- Learn to discount all LSP output, missing real errors

## Solution

An LSP proxy server (`mojo-check-proxy`) that:
- **Proxies navigation requests** (go-to-definition, hover, find-references, document-symbols) to `mojo-lsp-server`
- **Replaces diagnostics** with real `mojo build -o /dev/null` compiler output
- **Enriches errors** with gotcha hints from `gotchas.yaml`

### Architecture

```
Claude Code ←→ mojo-check-proxy ←→ mojo-lsp-server (child process)
                    │                    │
                    │  pygls upstream     │  raw JSON-RPC downstream
                    │                    │
                    │  forwards: all     │
                    │  navigation +      │
                    │  sync requests     │
                    │                    │
                    │  intercepts:       │
                    │  publishDiagnostics│
                    │  (discards)        │
                    │                    │
                    └── on didSave:
                        mojo build -o /dev/null <file>
                        parse stderr → Diagnostic[]
                        + gotcha enrichment
                        publishDiagnostics to Claude Code
```

## Design Decisions

### 1. Replace diagnostics, don't merge

All `textDocument/publishDiagnostics` notifications from `mojo-lsp-server` are silently discarded. Only diagnostics from `mojo build` are published to Claude Code.

**Rationale:** Merging reintroduces false positives. The compiler is the single source of truth for whether code is valid.

### 2. Hybrid transport: pygls upstream, raw JSON-RPC downstream

- **Upstream (Claude Code ↔ proxy):** pygls handles the server side — JSON-RPC framing, typed handlers for `didSave`, and diagnostic publishing.
- **Downstream (proxy ↔ mojo-lsp-server):** Raw async JSON-RPC over stdin/stdout pipes to the child process. All unhandled upstream requests are forwarded verbatim to the child via a catch-all mechanism. Responses are forwarded back to Claude Code.

**Rationale:** pygls is designed for implementing servers, not proxying. Using raw JSON-RPC on the child side gives full control over message forwarding without fighting the framework. pygls on the upstream side gives us typed handlers for the features we intercept (diagnostics, `didSave`).

### 3. Trigger on `didSave`, not `didChange`

Claude Code sends `textDocument/didSave` after Edit/Write operations. Compilation (0.14s–1.7s) is too slow for `didChange` (every keystroke). `didSave` is the right cadence for agent-driven development.

`didChange` notifications are still forwarded to `mojo-lsp-server` so its navigation features stay up-to-date.

### 4. Kill-and-restart on rapid saves

If a `mojo build` process is still running when a new `didSave` arrives for the same file, kill the old process and start a new one. Only the latest file state matters.

### 5. Gotcha enrichment

When `mojo build` reports an error, run `gotchas.enrich_error(stderr, timed_out=False, mojo_version=<resolved_version>)` against the error output. Append matching gotcha hints to the diagnostic message. The `mojo_version` is resolved via `sandbox._mojo_cmd()` logic at initialization time.

### 6. Single-file compilation with import error suppression (v1)

Run `mojo build -o /dev/null <file>` on the saved file only. Does not resolve cross-file imports in v1.

**Import error suppression:** Errors matching import-related patterns (e.g., `cannot find module`, `unknown import`, `no module named`) are suppressed in v1. Without this, single-file mode would replace LSP false positives with compiler false positives on every file that uses package imports — which is most files in a real project.

**Accepted tradeoff:** v1 catches syntax errors, type errors, and most common mistakes. Cross-file/project-aware compilation is a v2 enhancement.

### 7. Compiler resolution

Reuse `sandbox._mojo_cmd()` logic for finding the correct `mojo` binary (pinned version → uvx, project venv → mojox, fallback → system mojo).

**Workspace folder resolution for `cwd`:**
1. Use the first entry from `workspaceFolders` in the `initialize` request
2. Fall back to `rootUri` (deprecated but still sent by many clients)
3. Fall back to the directory of the first file opened via `didOpen`
4. Fall back to `None` (picks system `mojo`)

Per-file `cwd` changes (files outside the workspace) are out of scope for v1.

### 8. Capabilities negotiation

The proxy reads the child's `textDocumentSync` capability and advertises the same kind (or a compatible superset) to Claude Code, ensuring `didChange` is forwarded to the child. The proxy additionally ensures save notifications are enabled (needed for the `didSave` trigger).

Specifically: the proxy advertises `TextDocumentSyncOptions` with `change` set to whatever the child requested and `save` set to `True`.

## Proxy Behavior

### Initialization

1. Claude Code sends `initialize` to proxy
2. Proxy spawns `mojo-lsp-server` as child process (stdin/stdout pipes)
3. Proxy forwards `initialize` to child via raw JSON-RPC, awaits response
4. Proxy reads child capabilities and builds merged capabilities:
   - Navigation capabilities from child (go-to-def, hover, references, symbols, completion)
   - `textDocumentSync`: child's sync kind + `save: True`
   - Removes diagnostic-related capabilities from child (proxy owns diagnostics)
5. Proxy responds to Claude Code with merged capabilities
6. Proxy forwards `initialized` to child

**If child spawn fails:** Proxy logs the error and starts in diagnostics-only mode (navigation unavailable). Does NOT retry spawn — emits a single `window/showMessage` warning: "mojo-lsp-server not found; navigation features unavailable."

### Steady state — navigation

All requests/responses for navigation features are forwarded transparently via the raw JSON-RPC child channel:
- `textDocument/definition`
- `textDocument/hover`
- `textDocument/references`
- `textDocument/documentSymbol`
- `workspace/symbol`
- `textDocument/completion`
- Any other request/notification not explicitly intercepted

### Steady state — diagnostics

1. `textDocument/didSave` arrives from Claude Code
2. Proxy extracts the file URI → absolute path
3. Proxy kills any in-flight `mojo build` for this file
4. Proxy spawns `mojo build -o /dev/null <path>` (async subprocess)
5. On completion:
   - Parse stderr for diagnostic lines (see Error Format Parsing below)
   - Filter out import-related errors (v1 suppression)
   - Map remaining to LSP `Diagnostic` objects
   - Run `gotchas.enrich_error(stderr, timed_out=False, mojo_version=...)` for enrichment
   - Append gotcha hints to diagnostic messages where matched
   - Publish via `textDocument/publishDiagnostics`
6. If `mojo build` returns 0 (success): publish empty diagnostics (clears previous errors)

### Intercepted notifications (from child)

- `textDocument/publishDiagnostics` — **silently dropped**

### Shutdown

1. Claude Code sends `shutdown` to proxy
2. Proxy forwards `shutdown` to child
3. Proxy kills any in-flight `mojo build` processes
4. Proxy sends `exit` to child, waits for termination (5s timeout, then SIGKILL)
5. Proxy responds to Claude Code's `shutdown`

### Child crash recovery

If `mojo-lsp-server` crashes mid-session:
- Proxy restarts it automatically (up to 3 attempts, with 2s backoff)
- During restart, pending navigation requests receive immediate responses: `null` for single-result methods (`definition`, `hover`), `[]` for list methods (`references`, `documentSymbol`)
- If all 3 restart attempts fail, proxy logs the error and continues in diagnostics-only mode
- Diagnostics always continue working (independent of child)

## Error Format Parsing

Mojo compiler errors follow this pattern:
```
/path/to/file.mojo:3:5: error: cannot implicitly convert 'StringLiteral["wrong"]' value to 'Int'
    var x: Int = "wrong"
                 ^~~~~~~
```

Parsing regex:
```python
r'^(.+?):(\d+):(\d+): (error|warning|note): (.+)$'
```

Map to LSP `Diagnostic`:
- `range.start.line` = line - 1 (LSP is 0-indexed)
- `range.start.character` = col - 1
- `range.end.line` = start.line
- `range.end.character` = end of line (set to a large value like 999 to highlight to EOL, or parse the `^~~~` underline if present for exact span)
- `severity` = `DiagnosticSeverity.Error` (1) | `Warning` (2) | `Information` (3) for notes
- `message` = the error text (+ gotcha hint if matched)
- `source` = `"mojo build"`

**Import error suppression patterns (v1):**
```python
IMPORT_ERROR_PATTERNS = [
    r"cannot find module",
    r"unknown import",
    r"no module named",
    r"unable to locate module",
    r"failed to find",
]
```
Diagnostics matching any of these patterns are silently dropped in single-file mode.

## File Layout

```
src/mojo_mcp/
  lsp_proxy.py     — proxy server: pygls upstream, raw JSON-RPC child management,
                      diagnostic parsing, mojo build subprocess, gotcha enrichment
  gotchas.py       — (existing) enrich_error() reused for enrichment
  sandbox.py       — (existing) _mojo_cmd() reused for compiler resolution
plugins/
  mojo-lsp/
    .claude-plugin/
      plugin.json  — updated: command points to mojo-check-proxy
tests/
  test_lsp_proxy.py — unit tests for stderr parsing, diagnostic mapping,
                       import error suppression, gotcha enrichment
```

### Entry point (pyproject.toml)

```toml
[project.scripts]
mojo-mcp = "mojo_mcp.server:main"
mojo-check-proxy = "mojo_mcp.lsp_proxy:main"
```

### Plugin config update

```json
{
  "name": "mojo-lsp",
  "description": "Mojo language server proxy. Provides navigation via mojo-lsp-server and compiler-backed diagnostics via mojo build.",
  "version": "2.0.0",
  "lspServers": {
    "mojo": {
      "command": "uvx",
      "args": ["--from", "mojo-mcp", "mojo-check-proxy"],
      "extensionToLanguage": { ".mojo": "mojo" }
    }
  }
}
```

Version bumped to 2.0.0 to reflect the architectural change. Users who want the vanilla `mojo-lsp-server` can pin version 1.0.0.

## New dependency

| Package | Why |
|---|---|
| `pygls>=2.0` | LSP server framework — JSON-RPC framing, message types, async transport (upstream side) |

## Implementation Phases

**Phase A — Proxy transport:** Forward everything to child, suppress child diagnostics, publish empty diagnostics. Testable with a mock child server. Validates the proxy architecture works end-to-end.

**Phase B — Compiler diagnostics:** Add `didSave` trigger, `mojo build` subprocess with kill-and-restart, stderr parsing, import error suppression, gotcha enrichment.

## Risks

1. **pygls catch-all forwarding** — pygls may not support a default handler for unregistered methods. If not, the proxy must register explicit pass-through handlers for all navigation methods, or intercept messages at a lower level in pygls's transport. This is the main implementation risk.

2. **Initialization sequencing** — The child's `initialize` must complete before the proxy can respond to Claude Code. The proxy must handle the case where the child is slow to start (use `startupTimeout` from plugin config if available).

3. **Import suppression false negatives** — The suppression patterns may not cover all import error formats. New Mojo versions may change error wording. The patterns should be conservative (suppress known formats) rather than aggressive (suppress anything that mentions "import").

## Platform

Mojo only runs on Linux and macOS. `/dev/null` is valid on both platforms. Windows is out of scope.

## Future (v2)

- **Project-aware compilation:** detect workspace root, pass `-I .` and `-D` flags from a config file
- **Debounced multi-file builds:** when multiple files are saved in quick succession, batch them into a single project build
- **Diagnostic caching:** cache "clean" results to avoid re-running `mojo build` on unchanged files
- **`didChange` with debounce:** for faster feedback on typing (if compilation speed improves)
