#!/usr/bin/env bash
# Run the Hermes MCP gateway integration tests against the pinned image.
# Usage: test/mcp-gateway/hermes-mcp/run.sh [-k <expr>]
set -euo pipefail

cd "$(dirname "$0")/../.."

python3 -m pytest -q test/mcp-gateway/hermes-mcp "$@"
