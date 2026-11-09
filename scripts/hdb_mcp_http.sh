#!/usr/bin/env bash
# Run the HDB knowledge MCP bridge as a remote streamable-http server.
#
# Default endpoint: http://127.0.0.1:9100/mcp (env: HDB_MCP_HOST / HDB_MCP_PORT).
# Tool auth to the HDB API still comes from HDB_MCP_TOKEN / HDB_API_TOKEN
# (same sourcing rules as scripts/hdb_mcp_stdio.sh).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # Strip a UTF-8 BOM from the first line before sourcing.
  # shellcheck disable=SC1090
  . <(sed '1s/^\xEF\xBB\xBF//' "$ROOT/.env")
  set +a
fi

if [[ -z "${HDB_MCP_TOKEN:-}" && -z "${HDB_API_TOKEN:-}" ]]; then
  TOKEN_FILE="$HOME/.config/hdb/mcp_token"
  if [[ -f "$TOKEN_FILE" ]]; then
    TOKEN_VALUE="$(cat "$TOKEN_FILE")"
    if [[ "$TOKEN_VALUE" == hdbmcp_* ]]; then
      HDB_MCP_TOKEN="$TOKEN_VALUE"
    else
      HDB_API_TOKEN="$TOKEN_VALUE"
    fi
  fi
fi

export HDB_MCP_TOKEN HDB_API_TOKEN
export HDB_API_BASE="${HDB_API_BASE:-http://127.0.0.1:8002/api/v1}"
export HDB_MCP_TRANSPORT="${HDB_MCP_TRANSPORT:-streamable-http}"
export HDB_MCP_HOST="${HDB_MCP_HOST:-127.0.0.1}"
export HDB_MCP_PORT="${HDB_MCP_PORT:-9100}"
# Remote MCP always runs the per-user hdbmcp_* token gate.  Acknowledging
# an unauthenticated intranet listen requires the explicit
# HDB_MCP_HTTP_ALLOW_UNAUTHED=1 escape hatch.
export HDB_MCP_HTTP_ALLOW_UNAUTHED
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -m src.mcp_bridge.server
fi
exec uv run python -m src.mcp_bridge.server
