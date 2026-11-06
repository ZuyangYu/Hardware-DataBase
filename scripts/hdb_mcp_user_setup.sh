#!/usr/bin/env bash
# Configure a per-user HDB MCP bridge for OpenCode.
#
# Usage:
#   scripts/hdb_mcp_user_setup.sh --username alice
#   HDB_SETUP_PASSWORD=... scripts/hdb_mcp_user_setup.sh --username alice
#
# Run this as the user who owns the account.  Each user gets their own token
# and their own stdio bridge process.  The token file is chmod 600 and never
# written into OpenCode configuration.  Do not run it for another user.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USERNAME=""
API_BASE="${HDB_API_BASE:-http://127.0.0.1:8002/api/v1}"
HOME_DIR="${HOME}"
SKILL_SOURCE="$REPO/.claude/hw-design-solution"
SKILL_ID="hw-design-solution"
PYTHON="$REPO/.venv/bin/python"

usage() {
  cat <<'EOF'
Usage: scripts/hdb_mcp_user_setup.sh --username <name> [options]

Run this yourself, with your own account.  Administrators must not run it on
behalf of another user.

Options:
  --username <name>   HDB employee username (required)
  --api-base <url>    HDB API base (default: http://127.0.0.1:8002/api/v1)
  --home <dir>        Home directory to configure (default: $HOME)
  --repo <dir>        Repository root (default: script parent)
  --help              Show this help

Password:
  Set HDB_SETUP_PASSWORD, or the script prompts without echo.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --username) USERNAME="${2:-}"; shift 2 ;;
    --api-base) API_BASE="${2:-}"; shift 2 ;;
    --home) HOME_DIR="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$USERNAME" ]]; then
  usage >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "missing Python venv: $PYTHON" >&2
  echo "run this script from the Hardware-DataBase repository or pass --repo" >&2
  exit 1
fi
if [[ ! -f "$SKILL_SOURCE/SKILL.md" ]]; then
  echo "missing skill: $SKILL_SOURCE/SKILL.md" >&2
  exit 1
fi

if [[ -z "${HDB_SETUP_PASSWORD:-}" ]]; then
  read -r -s -p "Password for $USERNAME: " HDB_SETUP_PASSWORD
  echo
fi
export HDB_SETUP_PASSWORD

TOKEN="$("$PYTHON" - "$API_BASE" "$USERNAME" <<'PY'
import os
import sys

import httpx

base, username = sys.argv[1], sys.argv[2]
response = httpx.post(
    f"{base.rstrip('/')}/login",
    json={"username": username, "password": os.environ["HDB_SETUP_PASSWORD"]},
    timeout=60,
)
if response.status_code != 200:
    print(response.text[:300], file=sys.stderr)
    raise SystemExit(1)
print(response.json()["token"])
PY
)"

CONFIG_DIR="$HOME_DIR/.config/hdb"
OPENCODE_DIR="${XDG_CONFIG_HOME:-$HOME_DIR/.config}/opencode"
TOKEN_FILE="$CONFIG_DIR/mcp_token"
WRAPPER="$CONFIG_DIR/hdb-mcp"

mkdir -p "$CONFIG_DIR" "$OPENCODE_DIR/skills"
umask 077
printf '%s' "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export HDB_API_BASE="\${HDB_API_BASE:-$API_BASE}"
export HDB_API_TOKEN="\$(cat "$TOKEN_FILE")"
export PYTHONPATH="$REPO\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PYTHON" -m src.mcp_bridge.server
EOF
chmod 700 "$WRAPPER"

# OpenCode discovers <source>/hw-design-solution/SKILL.md.  A symlink keeps
# references/ and scripts/ next to the skill without copying them.
ln -sfn "$SKILL_SOURCE" "$OPENCODE_DIR/skills/$SKILL_ID"

if command -v opencode >/dev/null 2>&1; then
  opencode mcp add hdb --global \
    --env "HDB_API_BASE=$API_BASE" \
    -- "$WRAPPER" >/dev/null 2>&1 || true
fi

cat <<EOF
Configured HDB MCP for $USERNAME.

  token:    $TOKEN_FILE (chmod 600)
  wrapper:  $WRAPPER (chmod 700)
  skill:    $OPENCODE_DIR/skills/$SKILL_ID -> $SKILL_SOURCE

If OpenCode is already running, run /reload or open a new session.
Verify with:
  opencode mcp list
  opencode run --auto "调用 hdb_list_kbs，列出知识库名称。"

To rotate the token, delete $TOKEN_FILE and run this script again.
EOF
