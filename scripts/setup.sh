#!/usr/bin/env bash
set -euo pipefail

echo "==> Installing mojo-mcp Python dependencies..."
cd "$(dirname "$0")/.."
uv sync

echo ""
echo "==> Verifying mojo-mcp installation..."
uv run -- mojo-mcp --version 2>/dev/null || echo "(version check not available)"

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

echo ""
echo "==> For Mojo projects using mojox (recommended):"
echo "    uv add mojox          # In your Mojo project"
echo "    uv run -- mojox run my_app.mojo"
