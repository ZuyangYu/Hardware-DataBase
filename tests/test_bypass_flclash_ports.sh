#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../deploy/observability" && pwd)
script="$script_dir/bypass-flclash-ports.sh"

bash -n "$script"

ports=$(sed -n 's/.*--dports \([^ ]*\).*/\1/p' "$script" | head -n 1)
[[ ",$ports," == *,6080,* ]]
