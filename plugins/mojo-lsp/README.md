# mojo-lsp

Mojo language server for Claude Code, providing code intelligence via `uvx --from mojo-compiler mojo lsp`.

## Supported Extensions

`.mojo`

## Features

- Go to definition
- Find references
- Hover documentation
- Diagnostics (type errors, undefined names)
- Document symbols

## Installation

Install the Mojo compiler:

```bash
uv tool install mojo-compiler
```

Verify it works:

```bash
uvx --from mojo-compiler mojo --version
```

## More Information

- [Mojo Documentation](https://docs.modular.com/mojo/)
- [Modular CLI](https://docs.modular.com/magic/)
