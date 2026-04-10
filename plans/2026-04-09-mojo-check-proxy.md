# mojo-check-proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use atelier:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a compiler-backed LSP proxy that replaces mojo-lsp-server diagnostics with real `mojo build` output, eliminating false positives that mislead Claude Code agents.
**Architecture:** Raw async JSON-RPC proxy over stdio. Spawns `mojo-lsp-server` as a child process for navigation (go-to-def, hover, references). Intercepts and drops `publishDiagnostics` from the child. On `didSave`, runs `mojo build -o /dev/null` and publishes real compiler diagnostics with gotcha enrichment. Import-related errors suppressed in v1 (single-file mode).
**Tech Stack:** Python 3.11+, asyncio, `pygls>=2.0` (for `lsprotocol` types), existing `mojo_mcp.sandbox` and `mojo_mcp.gotchas` modules.

**Architecture note:** The spec chose "pygls upstream, raw JSON-RPC downstream." After researching pygls v2, its `LanguageServer` class is designed for implementing servers with registered typed handlers — not for proxying arbitrary requests to a child process. Forwarding would require unstructuring typed params back to dicts for every method, and returning raw child responses through pygls's typed serialization. For a proxy, raw asyncio JSON-RPC on both sides is simpler, more maintainable, and avoids fighting the framework. We add `pygls>=2.0` as a dependency (which brings in `lsprotocol` for Diagnostic type constants) but implement the proxy transport directly with asyncio.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/mojo_mcp/lsp_proxy.py` | Create | Proxy server: JSON-RPC framing, child transport, message routing, diagnostic parsing, mojo build subprocess, gotcha enrichment |
| `tests/test_lsp_proxy.py` | Create | Unit tests for framing, parsing, suppression, enrichment, capability merging |
| `pyproject.toml` | Modify | Add `pygls>=2.0` dependency, add `mojo-check-proxy` entry point |
| `plugins/mojo-lsp/.claude-plugin/plugin.json` | Modify | Point command to `mojo-check-proxy`, bump version to 2.0.0 |

---

### Task 1: Add dependency, entry point, and module skeleton

**Files:**
- Modify: `pyproject.toml:1-35`
- Create: `src/mojo_mcp/lsp_proxy.py`

- [ ] **Step 1: Add pygls dependency and entry point to pyproject.toml**

In `pyproject.toml`, add `pygls>=2.0` to dependencies and `mojo-check-proxy` to scripts:

```python
# In [project] dependencies, add:
"pygls>=2.0",

# In [project.scripts], add:
mojo-check-proxy = "mojo_mcp.lsp_proxy:main"
```

- [ ] **Step 2: Create lsp_proxy.py with imports, constants, and main stub**

Create `src/mojo_mcp/lsp_proxy.py`:

```python
"""LSP proxy: compiler-backed diagnostics for Mojo.

Proxies navigation requests to mojo-lsp-server (child process).
Replaces all diagnostics with real `mojo build` output.
Enriches errors with gotcha hints from gotchas.yaml.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .gotchas import enrich_error
from .sandbox import _find_mojo_version_file, _mojo_cmd

# Logging goes to stderr — stdout is the LSP transport
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(name)s: %(message)s",
)
logger = logging.getLogger("mojo-check-proxy")

# ── Constants ──────────────────────────────────────────────────────────

DIAG_RE = re.compile(
    r"^(.+?):(\d+):(\d+): (error|warning|note): (.+)$", re.MULTILINE
)

SEVERITY_MAP = {
    "error": 1,    # DiagnosticSeverity.Error
    "warning": 2,  # DiagnosticSeverity.Warning
    "note": 3,     # DiagnosticSeverity.Information
}

IMPORT_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"cannot find module",
        r"unknown import",
        r"no module named",
        r"unable to locate module",
        r"failed to find",
    ]
]


def main():
    """Entry point for mojo-check-proxy."""
    asyncio.run(_run())


async def _run():
    proxy = MojoCheckProxy()
    await proxy.run()
```

- [ ] **Step 3: Verify dependency installs**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv sync`
Expected: pygls installs successfully, no errors.

- [ ] **Step 4: Verify entry point resolves**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run python -c "from mojo_mcp.lsp_proxy import main; print('ok')"`
Expected: `ok` (will fail with NameError for MojoCheckProxy — that's expected, we create it next)

Actually the import will fail because `MojoCheckProxy` doesn't exist yet. Add a temporary stub at the bottom of the file:

```python
class MojoCheckProxy:
    async def run(self):
        pass
```

Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run python -c "from mojo_mcp.lsp_proxy import main; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: add lsp_proxy module skeleton with pygls dependency`

---

### Task 2: JSON-RPC message framing

**Files:**
- Modify: `src/mojo_mcp/lsp_proxy.py`
- Create: `tests/test_lsp_proxy.py`

- [ ] **Step 1: Write failing tests for JSON-RPC framing**

Create `tests/test_lsp_proxy.py`:

```python
"""Unit tests for lsp_proxy — JSON-RPC framing, diagnostic parsing, etc."""

import asyncio
import json
import pytest

from mojo_mcp.lsp_proxy import encode_message, read_message


# ── JSON-RPC framing ──────────────────────────────────────────────────


class TestEncodeMessage:
    def test_simple_request(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        encoded = encode_message(msg)
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        expected = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        assert encoded == expected

    def test_notification_no_id(self):
        msg = {"jsonrpc": "2.0", "method": "initialized"}
        encoded = encode_message(msg)
        assert b"Content-Length:" in encoded
        assert b'"method":"initialized"' in encoded

    def test_roundtrip(self):
        msg = {"jsonrpc": "2.0", "id": 42, "method": "test", "params": {"a": 1}}
        encoded = encode_message(msg)
        # Parse it back
        header, body = encoded.split(b"\r\n\r\n", 1)
        parsed = json.loads(body)
        assert parsed == msg


@pytest.mark.asyncio
class TestReadMessage:
    async def test_read_simple(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "test"}
        data = encode_message(msg)
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        result = await read_message(reader)
        assert result == msg

    async def test_read_eof(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        result = await read_message(reader)
        assert result is None

    async def test_read_multiple(self):
        msg1 = {"jsonrpc": "2.0", "id": 1, "method": "first"}
        msg2 = {"jsonrpc": "2.0", "id": 2, "method": "second"}
        reader = asyncio.StreamReader()
        reader.feed_data(encode_message(msg1) + encode_message(msg2))
        r1 = await read_message(reader)
        r2 = await read_message(reader)
        assert r1 == msg1
        assert r2 == msg2

    async def test_read_unicode(self):
        msg = {"jsonrpc": "2.0", "id": 1, "params": {"text": "héllo wörld"}}
        reader = asyncio.StreamReader()
        reader.feed_data(encode_message(msg))
        result = await read_message(reader)
        assert result["params"]["text"] == "héllo wörld"
```

- [ ] **Step 2: Verify tests fail**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v 2>&1 | head -30`
Expected: FAIL — `ImportError: cannot import name 'encode_message' from 'mojo_mcp.lsp_proxy'`

- [ ] **Step 3: Implement JSON-RPC framing functions**

Add to `src/mojo_mcp/lsp_proxy.py`, after the constants section:

```python
# ── JSON-RPC framing ──────────────────────────────────────────────────


def encode_message(msg: dict) -> bytes:
    """Encode a JSON-RPC message with Content-Length header."""
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def read_message(reader: asyncio.StreamReader) -> dict | None:
    """Read a Content-Length framed JSON-RPC message. Returns None on EOF."""
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            return None  # EOF
        text = line.decode("ascii").strip()
        if text == "":
            break  # End of headers
        if ":" in text:
            key, value = text.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length == 0:
        return None

    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))
```

- [ ] **Step 4: Verify tests pass**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: add JSON-RPC message framing for LSP proxy`

---

### Task 3: Diagnostic parsing and import error suppression

**Files:**
- Modify: `src/mojo_mcp/lsp_proxy.py`
- Modify: `tests/test_lsp_proxy.py`

- [ ] **Step 1: Write failing tests for diagnostic parsing**

Append to `tests/test_lsp_proxy.py`:

```python
from mojo_mcp.lsp_proxy import parse_diagnostics


# ── Diagnostic parsing ────────────────────────────────────────────────


class TestParseDiagnostics:
    def test_single_error(self):
        stderr = (
            "/tmp/main.mojo:2:18: error: cannot implicitly convert "
            "'StringLiteral[\"wrong\"]' value to 'Int'\n"
            "    var x: Int = \"wrong\"\n"
            "                 ^~~~~~~\n"
        )
        diags = parse_diagnostics(stderr)
        assert len(diags) == 1
        d = diags[0]
        assert d["severity"] == 1  # Error
        assert d["range"]["start"]["line"] == 1  # 0-indexed
        assert d["range"]["start"]["character"] == 17
        assert d["range"]["end"]["line"] == 1
        assert d["source"] == "mojo build"
        assert "cannot implicitly convert" in d["message"]

    def test_warning(self):
        stderr = "/tmp/main.mojo:5:1: warning: unused variable 'x'\n"
        diags = parse_diagnostics(stderr)
        assert len(diags) == 1
        assert diags[0]["severity"] == 2  # Warning

    def test_note(self):
        stderr = "/tmp/main.mojo:5:1: note: see declaration here\n"
        diags = parse_diagnostics(stderr)
        assert len(diags) == 1
        assert diags[0]["severity"] == 3  # Information

    def test_multiple_errors(self):
        stderr = (
            "/tmp/main.mojo:2:5: error: first error\n"
            "/tmp/main.mojo:5:10: error: second error\n"
        )
        diags = parse_diagnostics(stderr)
        assert len(diags) == 2
        assert diags[0]["range"]["start"]["line"] == 1
        assert diags[1]["range"]["start"]["line"] == 4

    def test_mojo_binary_error_skipped(self):
        """The mojo binary error line has no line:col and should not match."""
        stderr = (
            "/tmp/main.mojo:2:18: error: type mismatch\n"
            "/home/user/.local/bin/mojo: error: failed to parse the provided Mojo source module\n"
        )
        diags = parse_diagnostics(stderr)
        assert len(diags) == 1
        assert "type mismatch" in diags[0]["message"]

    def test_empty_stderr(self):
        assert parse_diagnostics("") == []
        assert parse_diagnostics("\n") == []

    def test_success_output_no_diagnostics(self):
        stderr = "some informational output\n"
        assert parse_diagnostics(stderr) == []

    def test_import_error_suppressed(self):
        stderr = "/tmp/main.mojo:1:1: error: cannot find module 'mypackage'\n"
        diags = parse_diagnostics(stderr, suppress_imports=True)
        assert len(diags) == 0

    def test_import_error_not_suppressed_when_disabled(self):
        stderr = "/tmp/main.mojo:1:1: error: cannot find module 'mypackage'\n"
        diags = parse_diagnostics(stderr, suppress_imports=False)
        assert len(diags) == 1

    def test_import_suppression_patterns(self):
        patterns = [
            "cannot find module 'foo'",
            "unknown import 'bar'",
            "no module named 'baz'",
            "unable to locate module 'qux'",
            "failed to find 'pkg'",
        ]
        for msg in patterns:
            stderr = f"/tmp/main.mojo:1:1: error: {msg}\n"
            diags = parse_diagnostics(stderr, suppress_imports=True)
            assert len(diags) == 0, f"Should suppress: {msg}"

    def test_non_import_error_not_suppressed(self):
        stderr = "/tmp/main.mojo:3:5: error: use of undefined variable 'x'\n"
        diags = parse_diagnostics(stderr, suppress_imports=True)
        assert len(diags) == 1
```

- [ ] **Step 2: Verify tests fail**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py::TestParseDiagnostics -x -v 2>&1 | head -15`
Expected: FAIL — `ImportError: cannot import name 'parse_diagnostics'`

- [ ] **Step 3: Implement parse_diagnostics**

Add to `src/mojo_mcp/lsp_proxy.py`, after the JSON-RPC section:

```python
# ── Diagnostic parsing ────────────────────────────────────────────────


def parse_diagnostics(stderr: str, suppress_imports: bool = True) -> list[dict]:
    """Parse mojo build stderr into LSP-compatible diagnostic dicts.

    Args:
        stderr: Raw stderr from `mojo build`.
        suppress_imports: If True, drop import-related errors (v1 single-file mode).
    """
    diagnostics = []
    for match in DIAG_RE.finditer(stderr):
        _path, line_s, col_s, severity, message = match.groups()
        if suppress_imports and any(p.search(message) for p in IMPORT_ERROR_PATTERNS):
            continue
        line = int(line_s) - 1  # LSP is 0-indexed
        col = int(col_s) - 1
        diagnostics.append({
            "range": {
                "start": {"line": line, "character": col},
                "end": {"line": line, "character": 999},
            },
            "severity": SEVERITY_MAP.get(severity, 1),
            "source": "mojo build",
            "message": message,
        })
    return diagnostics
```

- [ ] **Step 4: Verify tests pass**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: add compiler diagnostic parsing with import error suppression`

---

### Task 4: Child transport for mojo-lsp-server

**Files:**
- Modify: `src/mojo_mcp/lsp_proxy.py`

- [ ] **Step 1: Implement ChildTransport class**

Add to `src/mojo_mcp/lsp_proxy.py`, after the diagnostic parsing section:

```python
# ── Child transport ───────────────────────────────────────────────────


class ChildTransport:
    """Manages mojo-lsp-server as a child process with JSON-RPC over stdio."""

    def __init__(self, command: list[str]):
        self._command = command
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int | str, asyncio.Future[dict]] = {}
        self._next_id = 0
        self._reader_task: asyncio.Task[None] | None = None
        self.on_notification: Any = None  # async callback(msg: dict)

    async def start(self) -> None:
        """Spawn the child process and start reading its stdout."""
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Read JSON-RPC messages from child stdout."""
        assert self._process and self._process.stdout
        try:
            while True:
                msg = await read_message(self._process.stdout)
                if msg is None:
                    break  # Child exited or EOF
                if "id" in msg and "method" not in msg:
                    # Response to a request
                    future = self._pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                elif "method" in msg:
                    # Notification from child
                    if self.on_notification:
                        asyncio.create_task(self.on_notification(msg))
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            pass

    async def request(self, method: str, params: Any = None) -> dict:
        """Send a request with a proxy-prefixed ID and await the response."""
        self._next_id += 1
        msg_id = f"proxy-{self._next_id}"
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        await self._write(msg)
        return await asyncio.wait_for(future, timeout=30)

    async def forward_request(self, msg: dict) -> dict:
        """Forward a client request verbatim and await the child's response.

        Uses the original request ID, so the response maps back to the client.
        """
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[msg["id"]] = future
        await self._write(msg)
        return await asyncio.wait_for(future, timeout=30)

    async def notify(self, method: str, params: Any = None) -> None:
        """Send a notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def forward_notify(self, msg: dict) -> None:
        """Forward a client notification verbatim to the child."""
        await self._write(msg)

    async def _write(self, msg: dict) -> None:
        """Write a JSON-RPC message to the child's stdin."""
        assert self._process and self._process.stdin
        data = encode_message(msg)
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def stop(self) -> None:
        """Gracefully shut down the child process."""
        if self._process and self._process.returncode is None:
            try:
                await self.request("shutdown")
                await self.notify("exit")
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                self._process.kill()
                await self._process.wait()
        if self._reader_task:
            self._reader_task.cancel()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None
```

- [ ] **Step 2: Verify module still imports**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run python -c "from mojo_mcp.lsp_proxy import ChildTransport; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run existing tests still pass**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**
Use the `commit-smart` skill. Message: `feat: add ChildTransport for mojo-lsp-server subprocess`

---

### Task 5: Proxy server core — initialization, routing, and forwarding

**Files:**
- Modify: `src/mojo_mcp/lsp_proxy.py`
- Modify: `tests/test_lsp_proxy.py`

- [ ] **Step 1: Write failing tests for capability merging**

Append to `tests/test_lsp_proxy.py`:

```python
from mojo_mcp.lsp_proxy import merge_capabilities


# ── Capability merging ────────────────────────────────────────────────


class TestMergeCapabilities:
    def test_int_sync_kind(self):
        """When child advertises TextDocumentSyncKind as int, wrap in options."""
        child_caps = {"textDocumentSync": 2, "hoverProvider": True}
        merged = merge_capabilities(child_caps)
        sync = merged["textDocumentSync"]
        assert sync["change"] == 2
        assert sync["save"] is True
        assert sync["openClose"] is True
        assert merged["hoverProvider"] is True

    def test_dict_sync_kind(self):
        """When child advertises TextDocumentSyncOptions dict, add save."""
        child_caps = {
            "textDocumentSync": {"openClose": True, "change": 1},
            "definitionProvider": True,
        }
        merged = merge_capabilities(child_caps)
        assert merged["textDocumentSync"]["save"] is True
        assert merged["textDocumentSync"]["change"] == 1

    def test_no_sync_kind(self):
        """When child has no textDocumentSync, set defaults."""
        merged = merge_capabilities({})
        assert merged["textDocumentSync"]["change"] == 1
        assert merged["textDocumentSync"]["save"] is True

    def test_diagnostic_provider_removed(self):
        """Proxy owns diagnostics — remove child's diagnosticProvider."""
        child_caps = {"diagnosticProvider": {"interFileDependencies": True}}
        merged = merge_capabilities(child_caps)
        assert "diagnosticProvider" not in merged

    def test_child_caps_preserved(self):
        """Navigation capabilities pass through unchanged."""
        child_caps = {
            "hoverProvider": True,
            "definitionProvider": True,
            "referencesProvider": True,
            "documentSymbolProvider": True,
            "completionProvider": {"triggerCharacters": ["."]},
        }
        merged = merge_capabilities(child_caps)
        for key in child_caps:
            assert merged[key] == child_caps[key]
```

- [ ] **Step 2: Verify tests fail**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py::TestMergeCapabilities -x -v 2>&1 | head -10`
Expected: FAIL — `ImportError: cannot import name 'merge_capabilities'`

- [ ] **Step 3: Implement merge_capabilities and MojoCheckProxy**

Add `merge_capabilities` after the `ChildTransport` class:

```python
# ── Capability merging ────────────────────────────────────────────────


def merge_capabilities(child_caps: dict) -> dict:
    """Merge child LSP capabilities with proxy overrides.

    Ensures textDocumentSync includes save notifications.
    Removes diagnosticProvider (proxy owns diagnostics).
    """
    caps = dict(child_caps)
    text_doc_sync = child_caps.get("textDocumentSync")
    if isinstance(text_doc_sync, int):
        caps["textDocumentSync"] = {
            "openClose": True,
            "change": text_doc_sync,
            "save": True,
        }
    elif isinstance(text_doc_sync, dict):
        caps["textDocumentSync"] = {**text_doc_sync, "save": True}
    else:
        caps["textDocumentSync"] = {"openClose": True, "change": 1, "save": True}
    caps.pop("diagnosticProvider", None)
    return caps
```

Replace the temporary `MojoCheckProxy` stub with the full implementation:

```python
# ── Proxy server ──────────────────────────────────────────────────────


class MojoCheckProxy:
    """LSP proxy: navigation from mojo-lsp-server, diagnostics from mojo build."""

    def __init__(self):
        self._child: ChildTransport | None = None
        self._child_command = ["uvx", "--from", "mojo", "mojo-lsp-server"]
        self._stdin_reader: asyncio.StreamReader | None = None
        self._stdout_transport: Any = None
        self._workspace_dir: str | None = None
        self._mojo_version: str | None = None
        self._build_processes: dict[str, asyncio.subprocess.Process] = {}
        self._child_restarts = 0
        self._max_restarts = 3
        self._restart_backoff = 2  # seconds

    async def run(self) -> None:
        """Main loop: read from stdin, dispatch messages."""
        loop = asyncio.get_running_loop()

        # Async stdin reader
        self._stdin_reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(self._stdin_reader),
            sys.stdin.buffer,
        )

        # Stdout writer (transport.write is non-blocking)
        self._stdout_transport, _ = await loop.connect_write_pipe(
            lambda: asyncio.Protocol(), sys.stdout.buffer
        )

        while True:
            msg = await read_message(self._stdin_reader)
            if msg is None:
                break
            # Handle each message concurrently (don't block on child responses)
            asyncio.create_task(self._dispatch(msg))

    async def _dispatch(self, msg: dict) -> None:
        """Route an incoming JSON-RPC message."""
        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            await self._handle_initialize(msg)
        elif method == "initialized":
            if self._child and self._child.alive:
                await self._child.forward_notify(msg)
        elif method == "shutdown":
            await self._handle_shutdown(msg)
        elif method == "exit":
            sys.exit(0)
        elif method == "textDocument/didSave":
            # Forward to child so it knows about the save
            if self._child and self._child.alive:
                await self._child.forward_notify(msg)
            # Trigger compiler diagnostic
            await self._handle_did_save(msg)
        elif method and msg_id is not None:
            # Request — forward to child and relay response
            await self._forward_request(msg)
        elif method:
            # Notification — forward to child
            if self._child and self._child.alive:
                await self._child.forward_notify(msg)

    async def _handle_initialize(self, msg: dict) -> None:
        """Spawn child, forward initialize, merge capabilities."""
        params = msg.get("params", {})
        msg_id = msg["id"]

        # Resolve workspace directory
        wf = params.get("workspaceFolders")
        if wf and len(wf) > 0:
            uri = wf[0].get("uri", "")
            self._workspace_dir = unquote(urlparse(uri).path)
        elif params.get("rootUri"):
            self._workspace_dir = unquote(urlparse(params["rootUri"]).path)

        # Resolve mojo version for gotcha enrichment
        if self._workspace_dir:
            _, version = _find_mojo_version_file(self._workspace_dir)
            self._mojo_version = version

        # Spawn child
        child_caps: dict = {}
        try:
            self._child = ChildTransport(self._child_command)
            self._child.on_notification = self._handle_child_notification
            await self._child.start()
            child_response = await self._child.request("initialize", params)
            child_caps = child_response.get("result", {}).get("capabilities", {})
            logger.info("mojo-lsp-server started, capabilities received")
        except Exception as e:
            logger.warning("Failed to start mojo-lsp-server: %s", e)
            self._child = None
            # Send window/showMessage warning
            asyncio.create_task(self._send_notification(
                "window/showMessage",
                {"type": 2, "message": "mojo-lsp-server not found; navigation features unavailable."},
            ))

        capabilities = merge_capabilities(child_caps)
        await self._send_response(msg_id, {"capabilities": capabilities})

    async def _handle_shutdown(self, msg: dict) -> None:
        """Kill builds, stop child, respond."""
        for proc in self._build_processes.values():
            if proc.returncode is None:
                proc.kill()
        self._build_processes.clear()
        if self._child:
            await self._child.stop()
        await self._send_response(msg["id"], None)

    async def _forward_request(self, msg: dict) -> None:
        """Forward a request to child and relay response to Claude Code."""
        if not self._child or not self._child.alive:
            await self._send_response(msg["id"], None)
            return
        try:
            response = await self._child.forward_request(msg)
            if "result" in response:
                await self._send_response(msg["id"], response["result"])
            elif "error" in response:
                await self._send_error(msg["id"], response["error"])
            else:
                await self._send_response(msg["id"], None)
        except asyncio.TimeoutError:
            await self._send_response(msg["id"], None)
        except Exception:
            await self._send_response(msg["id"], None)

    async def _handle_child_notification(self, msg: dict) -> None:
        """Handle notifications from child. Drop publishDiagnostics."""
        method = msg.get("method")
        if method == "textDocument/publishDiagnostics":
            return  # Silently drop — we own diagnostics
        # Forward other notifications (window/logMessage, etc.)
        await self._send_notification(method, msg.get("params"))

    # ── didSave + mojo build (Task 6 will implement) ──────────────────

    async def _handle_did_save(self, msg: dict) -> None:
        """Placeholder — implemented in Task 6."""
        pass

    # ── Output helpers ────────────────────────────────────────────────

    def _write_stdout(self, data: bytes) -> None:
        """Write raw bytes to stdout transport."""
        if self._stdout_transport:
            self._stdout_transport.write(data)

    async def _send_response(self, msg_id: int | str, result: Any) -> None:
        self._write_stdout(encode_message({"jsonrpc": "2.0", "id": msg_id, "result": result}))

    async def _send_error(self, msg_id: int | str, error: dict) -> None:
        self._write_stdout(encode_message({"jsonrpc": "2.0", "id": msg_id, "error": error}))

    async def _send_notification(self, method: str, params: Any = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write_stdout(encode_message(msg))

    async def _publish_diagnostics(self, uri: str, diagnostics: list[dict]) -> None:
        await self._send_notification(
            "textDocument/publishDiagnostics",
            {"uri": uri, "diagnostics": diagnostics},
        )
```

- [ ] **Step 4: Verify tests pass**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: add proxy core with init, routing, and capability merging`

---

### Task 6: didSave handler with mojo build and gotcha enrichment

**Files:**
- Modify: `src/mojo_mcp/lsp_proxy.py`
- Modify: `tests/test_lsp_proxy.py`

- [ ] **Step 1: Write failing tests for gotcha-enriched diagnostics**

Append to `tests/test_lsp_proxy.py`:

```python
from mojo_mcp.lsp_proxy import enrich_diagnostics


# ── Gotcha enrichment ─────────────────────────────────────────────────


class TestEnrichDiagnostics:
    def test_no_enrichment_on_empty_stderr(self):
        diags = [{"message": "some error", "severity": 1, "source": "mojo build",
                  "range": {"start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 999}}}]
        result = enrich_diagnostics(diags, "", mojo_version="0.26.0")
        assert result[0]["message"] == "some error"

    def test_enrichment_appends_to_first_diagnostic(self):
        """When gotcha hints match, they are appended to the first diagnostic."""
        diags = [{"message": "type error", "severity": 1, "source": "mojo build",
                  "range": {"start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 999}}}]
        # Use a stderr that won't match any gotcha — just verify the function runs
        result = enrich_diagnostics(diags, "some random stderr", mojo_version="0.26.0")
        # No gotcha should match random text, so message unchanged
        assert result[0]["message"] == "type error"

    def test_no_crash_on_empty_diagnostics(self):
        result = enrich_diagnostics([], "some error text", mojo_version="0.26.0")
        assert result == []
```

- [ ] **Step 2: Verify tests fail**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py::TestEnrichDiagnostics -x -v 2>&1 | head -10`
Expected: FAIL — `ImportError: cannot import name 'enrich_diagnostics'`

- [ ] **Step 3: Implement enrich_diagnostics and _handle_did_save**

Add `enrich_diagnostics` after `parse_diagnostics`:

```python
def enrich_diagnostics(
    diagnostics: list[dict], stderr: str, mojo_version: str
) -> list[dict]:
    """Append matching gotcha hints to diagnostics.

    Gotcha hints are appended to the first diagnostic's message.
    """
    if not diagnostics or not stderr.strip():
        return diagnostics
    parts = mojo_version.split(".")
    version = ".".join(parts[:3])
    hints = enrich_error(stderr, timed_out=False, mojo_version=version)
    if hints:
        hint_lines = []
        for h in hints:
            hint_lines.append(f"[gotcha: {h['title']}] {h['description']}")
            hint_lines.append(f"Fix: {h['fix']}")
        diagnostics[0]["message"] += "\n\n" + "\n".join(hint_lines)
    return diagnostics
```

Replace the `_handle_did_save` placeholder in `MojoCheckProxy`:

```python
    async def _handle_did_save(self, msg: dict) -> None:
        """Trigger mojo build on file save, publish compiler diagnostics."""
        params = msg.get("params", {})
        uri = params.get("textDocument", {}).get("uri", "")
        if not uri.startswith("file://"):
            return
        path = unquote(urlparse(uri).path)
        if not path.endswith(".mojo"):
            return

        # Kill any in-flight build for this file
        old_proc = self._build_processes.pop(uri, None)
        if old_proc and old_proc.returncode is None:
            old_proc.kill()

        asyncio.create_task(self._run_build(uri, path))

    async def _run_build(self, uri: str, path: str) -> None:
        """Run mojo build and publish diagnostics from stderr."""
        mojo_cmd = _mojo_cmd(self._mojo_version, self._workspace_dir)
        cmd = [*mojo_cmd, "build", "-o", "/dev/null", path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workspace_dir,
            )
            self._build_processes[uri] = proc
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            stderr = ""
        except FileNotFoundError:
            logger.warning("mojo binary not found — cannot provide diagnostics")
            return
        except Exception as e:
            logger.error("Build failed: %s", e)
            return
        finally:
            self._build_processes.pop(uri, None)

        diagnostics = parse_diagnostics(stderr, suppress_imports=True)
        mojo_version = self._mojo_version or "0.26.0"
        diagnostics = enrich_diagnostics(diagnostics, stderr, mojo_version)
        await self._publish_diagnostics(uri, diagnostics)
```

- [ ] **Step 4: Verify tests pass**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: add didSave handler with mojo build and gotcha enrichment`

---

### Task 7: Child crash recovery

**Files:**
- Modify: `src/mojo_mcp/lsp_proxy.py`

- [ ] **Step 1: Add crash detection and restart to the child reader loop**

In `ChildTransport`, modify the `_read_loop` to detect child exit and notify the proxy. Add at the end of the `_read_loop` method, after the while loop breaks:

```python
        # After the while loop in _read_loop:
        # Child exited — notify proxy for potential restart
        if self.on_exit:
            asyncio.create_task(self.on_exit())
```

Add `on_exit` to `ChildTransport.__init__`:

```python
        self.on_exit: Any = None  # async callback on child exit
```

- [ ] **Step 2: Add restart logic to MojoCheckProxy**

Add to `MojoCheckProxy`, after `_handle_child_notification`:

```python
    async def _handle_child_exit(self) -> None:
        """Attempt to restart the child if it crashed."""
        if self._child_restarts >= self._max_restarts:
            logger.error(
                "mojo-lsp-server crashed %d times, giving up. "
                "Diagnostics will continue but navigation is unavailable.",
                self._max_restarts,
            )
            self._child = None
            return

        self._child_restarts += 1
        logger.warning(
            "mojo-lsp-server crashed, restarting (attempt %d/%d)...",
            self._child_restarts, self._max_restarts,
        )
        await asyncio.sleep(self._restart_backoff)

        try:
            self._child = ChildTransport(self._child_command)
            self._child.on_notification = self._handle_child_notification
            self._child.on_exit = self._handle_child_exit
            await self._child.start()

            # Re-initialize the child with cached workspace info
            init_params: dict[str, Any] = {}
            if self._workspace_dir:
                init_params["rootUri"] = f"file://{self._workspace_dir}"
            await self._child.request("initialize", init_params)
            await self._child.notify("initialized", {})
            logger.info("mojo-lsp-server restarted successfully")
        except Exception as e:
            logger.error("Failed to restart mojo-lsp-server: %s", e)
            self._child = None
```

- [ ] **Step 3: Wire on_exit in _handle_initialize**

In `_handle_initialize`, after the child starts successfully, add:

```python
            self._child.on_exit = self._handle_child_exit
```

(Add this line right after `self._child.on_notification = self._handle_child_notification`)

- [ ] **Step 4: Verify module imports and all tests pass**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest tests/test_lsp_proxy.py -x -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: add child crash detection and auto-restart with backoff`

---

### Task 8: Plugin config update and workspace resolution fallback

**Files:**
- Modify: `plugins/mojo-lsp/.claude-plugin/plugin.json`
- Modify: `src/mojo_mcp/lsp_proxy.py`

- [ ] **Step 1: Add didOpen fallback for workspace resolution**

In `MojoCheckProxy._dispatch`, add workspace resolution from the first `didOpen` URI. Replace the existing `elif method:` block (the generic notification forwarding) with:

```python
        elif method:
            # Notification — forward to child
            # Capture first didOpen URI as workspace fallback
            if method == "textDocument/didOpen" and not self._workspace_dir:
                td = msg.get("params", {}).get("textDocument", {})
                uri = td.get("uri", "")
                if uri.startswith("file://"):
                    self._workspace_dir = str(Path(unquote(urlparse(uri).path)).parent)
                    _, version = _find_mojo_version_file(self._workspace_dir)
                    self._mojo_version = version
            if self._child and self._child.alive:
                await self._child.forward_notify(msg)
```

- [ ] **Step 2: Update plugin.json**

Replace the contents of `plugins/mojo-lsp/.claude-plugin/plugin.json`:

```json
{
  "name": "mojo-lsp",
  "description": "Mojo language server proxy. Provides navigation via mojo-lsp-server and compiler-backed diagnostics via mojo build.",
  "version": "2.0.0",
  "author": {
    "name": "Conobi",
    "url": "https://github.com/Conobi"
  },
  "homepage": "https://github.com/Conobi/mojo-mcp",
  "repository": "https://github.com/Conobi/mojo-mcp",
  "license": "MIT",
  "keywords": ["mojo", "lsp", "language-server", "modular", "diagnostics"],
  "lspServers": {
    "mojo": {
      "command": "uvx",
      "args": ["--from", "mojo-mcp", "mojo-check-proxy"],
      "extensionToLanguage": {
        ".mojo": "mojo"
      }
    }
  }
}
```

- [ ] **Step 3: Run all tests (project-wide)**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && uv run pytest -x -v`
Expected: All tests PASS (both existing tests and new lsp_proxy tests)

- [ ] **Step 4: Verify entry point works**
Run: `cd /home/donokami/Projets/perso/mojo-mcp && echo '{}' | timeout 2 uv run mojo-check-proxy 2>&1; true`
Expected: Process starts, reads from stdin, exits on EOF or timeout. No crash. May log warnings about malformed JSON-RPC (expected — we sent `{}` not a valid Content-Length framed message).

- [ ] **Step 5: Commit**
Use the `commit-smart` skill. Message: `feat: update plugin to v2.0.0 with compiler-backed diagnostic proxy`

---

## Out-of-scope items

- **What:** Project-aware compilation (`-I .`, `-D` flags from config)
  **Severity:** required-later
  **Trigger:** When agents report false import errors from single-file mode

- **What:** Debounced multi-file builds
  **Severity:** optional
  **Trigger:** When multiple files are saved in rapid succession during refactors

- **What:** `didChange` with debounce (real-time diagnostics)
  **Severity:** optional
  **Trigger:** When mojo compilation speed improves or `mojo check` is added
