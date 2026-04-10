# Project Context — mojo-mcp

## Phase
done

## Why
The official `mojo-lsp-server` produces persistent false-positive diagnostics that mislead Claude Code agents during development. Three documented issues (circular import resolution, stale symbol tables, `main`-in-package errors) erode trust in LSP output and cause agents to "fix" working code. A compiler-backed diagnostic system would provide ground-truth error reporting.

## Scope
A compiler-backed diagnostic system for Mojo that replaces or supplements `mojo-lsp-server` diagnostics with real `mojo build` output. Ships as a Claude Code plugin within the mojo-mcp project.

## Non-goals
- Project-aware compilation (v2 — `-I`, `-D` flags from config)
- Real-time `didChange` diagnostics (requires faster compilation)
- Windows support (Mojo is Linux/macOS only)
- Replacing mojo-lsp-server for navigation (we proxy to it)

## Key Decisions
- (Previous AXI decisions remain in effect for the MCP server)
- **Critical constraint:** Claude Code supports only ONE LSP server per file extension — cannot run two servers for `.mojo`
- **Raw async JSON-RPC proxy** — pygls's LanguageServer class doesn't fit proxying; raw asyncio is cleaner. pygls added for lsprotocol types.
- **Replace diagnostics, don't merge** — all child publishDiagnostics are dropped; only mojo build output is published
- **Trigger on didSave, not didChange** — compilation too slow (0.14s–1.7s) for keystrokes
- **Import error suppression in v1** — single-file compilation produces false import errors; suppressed by pattern matching
- **Kill-and-restart on rapid saves** — only latest file state matters

## Active Specs and Plans
- `specs/2026-04-09-axi-ergonomics.md` — Done
- `plans/2026-04-09-axi-ergonomics.md` — Done
- `specs/2026-04-09-mojo-check-proxy.md` — Shelved (opted for CLAUDE.md guidance instead)
- `plans/2026-04-09-mojo-check-proxy.md` — Shelved

## Constraints
- Must remain a valid MCP server (stdio transport, `types.Tool` definitions)
- One LSP server per file extension in Claude Code
- Python codebase (no TypeScript/Go rewrite)
- `mojo build` takes ~1.7s for valid files, ~0.14s for files with errors (no `mojo check` command exists)

## Session History
| Date | Session | Summary |
|------|---------|---------|
| 2026-04-09 | acd17b59 | AXI ergonomics: brainstorming → spec → plan → implementation (10 tasks, 10 commits). 71 tests pass. |
| 2026-04-09 | 601653ac | Investigated LSP false positives from mojo-net. Designed proxy spec+plan, then opted for simpler CLAUDE.md guidance: trust `execute` over LSP diagnostics. Proxy spec/plan shelved for future if needed. |
