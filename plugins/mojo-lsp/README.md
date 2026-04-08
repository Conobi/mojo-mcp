# mojo-lsp

Mojo language server for Claude Code, providing code intelligence via `uvx --from mojo mojo-lsp-server`.

## Supported Extensions

`.mojo`

## Features

- Go to definition
- Find references
- Hover documentation
- Diagnostics (type errors, undefined names)
- Document symbols

## Installation

Install the Mojo compiler via the `mojo` package:

```bash
uv tool install mojo
```

Verify it works:

```bash
uvx --from mojo mojo-lsp-server --help
```

## More Information

- [Mojo Documentation](https://docs.modular.com/mojo/)
- [Modular CLI](https://docs.modular.com/magic/)
