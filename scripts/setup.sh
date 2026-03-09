#!/usr/bin/env bash
# Install the Mojo runtime and set up the mojo-mcp server.
set -euo pipefail

echo "==> Installing Mojo via uv..."
uv tool install modular

echo ""
echo "==> Verifying mojo installation..."
mojo --version

echo ""
echo "==> Installing mojo-mcp Python dependencies..."
cd "$(dirname "$0")/.."
uv sync

echo ""
echo "==> Done. Add this to ~/.claude/mcp.json to use with Claude Code:"
cat <<'EOF'
{
  "mcpServers": {
    "mojo": {
      "command": "uv",
      "args": ["run", "--project", "~/Projets/perso/mojo-mcp", "mojo-mcp"]
    }
  }
}
EOF
