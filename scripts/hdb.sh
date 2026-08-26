#!/usr/bin/env bash
# Hardware DataBase 本地服务管理:status / start / stop / logs
# 用法: bash scripts/hdb.sh [status|start|stop|logs]
set -u
cd "$(dirname "$0")/.."

API_PORT="${HDB_API_PORT:-8010}"
FRONT_PORT=5174
NODEENV="/tmp/opencode/nodeenv"

api_ok()    { curl -sf -m 2 "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; }
worker_pid(){ pgrep -u "$USER" -f "hardware-database-worker" | head -1; }
front_ok()  { curl -sf -m 2 -o /dev/null "http://127.0.0.1:${FRONT_PORT}/"; }

ensure_node() {
  if [ -x "${NODEENV}/lib/python3.12/site-packages/nodejs_wheel/bin/node" ]; then return 0; fi
  echo "[node] 便携 Node 不存在(/tmp 已清),重新安装..."
  rm -rf "${NODEENV}"
  uv venv "${NODEENV}" -q && uv pip install -q --python "${NODEENV}/bin/python" nodejs-wheel-binaries
}

start_one() {
  local name="$1"; shift
  setsid nohup "$@" < /dev/null >> "/tmp/opencode/hdb-${name}.log" 2>&1 &
  disown
}

do_start() {
  mkdir -p /tmp/opencode
  if api_ok; then echo "[api]    已在运行 (:${API_PORT})"
  else
    echo "[api]    启动中 (:${API_PORT})..."
    HDB_API_PORT="$API_PORT" start_one server uv run hardware-database-server
    sleep 6
  fi
  if [ -n "$(worker_pid)" ]; then echo "[worker] 已在运行 (pid $(worker_pid))"
  else
    echo "[worker] 启动中..."
    start_one worker uv run hardware-database-worker
    sleep 3
  fi
  if front_ok; then echo "[front]  已在运行 (:${FRONT_PORT})"
  else
    echo "[front]  启动中 (:${FRONT_PORT})..."
    ensure_node
    export PATH="${NODEENV}/lib/python3.12/site-packages/nodejs_wheel/bin:$PATH"
    (cd frontend && VITE_API_PROXY_TARGET="http://127.0.0.1:${API_PORT}" \
      start_one frontend ./node_modules/.bin/vite --host 0.0.0.0 --port "$FRONT_PORT")
    sleep 5
  fi
  do_status
}

do_stop() {
  pkill -u "$USER" -f "hardware-database-worker" 2>/dev/null && echo "[worker] 已停止"
  pkill -u "$USER" -f "hardware-database-server.*" 2>/dev/null
  fuser -k "${API_PORT}/tcp" 2>/dev/null
  pkill -u "$USER" -f "vite --host 0.0.0.0 --port ${FRONT_PORT}" 2>/dev/null && echo "[front]  已停止"
  sleep 1
  do_status
}

do_status() {
  local rc=0
  if api_ok; then echo "✅ [api]    http://127.0.0.1:${API_PORT}"; else echo "❌ [api]    未启动      -> bash scripts/hdb.sh start"; rc=1; fi
  if [ -n "$(worker_pid)" ]; then echo "✅ [worker] pid $(worker_pid)"; else echo "❌ [worker] 未启动(对话不会执行!) -> bash scripts/hdb.sh start"; rc=1; fi
  if front_ok; then echo "✅ [front]  http://127.0.0.1:${FRONT_PORT}"; else echo "❌ [front]  未启动      -> bash scripts/hdb.sh start"; rc=1; fi
  [ "$rc" = 0 ] && echo "—— 全部就绪 ——" || echo "—— 有缺失,执行: bash scripts/hdb.sh start ——"
  return $rc
}

case "${1:-status}" in
  status) do_status ;;
  start)  do_start ;;
  stop)   do_stop ;;
  logs)   tail -n 30 /tmp/opencode/hdb-server.log /tmp/opencode/hdb-worker.log /tmp/opencode/hdb-frontend.log ;;
  *) echo "用法: bash scripts/hdb.sh [status|start|stop|logs]" ;;
esac
