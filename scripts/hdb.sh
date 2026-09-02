#!/usr/bin/env bash
# Hardware DataBase 本地服务管理: status / start / stop / logs
# 用法: bash scripts/hdb.sh [status|start|stop|logs]
set -u

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT"

API_HOST="${HDB_API_HOST:-127.0.0.1}"
API_PORT="${HDB_API_PORT:-8003}"
FRONT_PORT="${HDB_FRONT_PORT:-5175}"
LOG_DIR="${HDB_LOG_DIR:-$APP_ROOT/storage/logs}"
PID_DIR="${HDB_PID_DIR:-$APP_ROOT/storage/run}"
API_PID_FILE="$PID_DIR/api.pid"
FRONT_PID_FILE="$PID_DIR/frontend.pid"

api_ok()   { curl -sf -m 2 "http://${API_HOST}:${API_PORT}/health" >/dev/null 2>&1; }
front_ok() { curl -sf -m 2 -o /dev/null "http://127.0.0.1:${FRONT_PORT}/"; }

pid_matches_root() {
  local pid="$1" expected_root="$2" cwd
  cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
  [ "$cwd" = "$expected_root" ] || [[ "$cwd" == "$expected_root"/* ]]
}

managed_pid() {
  local pid_file="$1" expected_root="$2" pid
  [ -f "$pid_file" ] || return 1
  pid="$(sed -n '1p' "$pid_file" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  pid_matches_root "$pid" "$expected_root" || return 1
  printf '%s\n' "$pid"
}

start_process() {
  local name="$1" cwd="$2" pid_file="$3"
  shift 3
  mkdir -p "$LOG_DIR" "$PID_DIR"
  (
    cd "$cwd" || exit 1
    exec setsid nohup "$@" < /dev/null >> "$LOG_DIR/${name}.log" 2>&1
  ) &
  printf '%s\n' "$!" > "$pid_file"
}

stop_process() {
  local name="$1" cwd="$2" pid_file="$3" pid
  pid="$(managed_pid "$pid_file" "$cwd" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    echo "[${name}] 已停止"
  fi
  rm -f "$pid_file"
}

wait_for() {
  local check_fn="$1" attempts="$2" i
  for i in $(seq 1 "$attempts"); do
    if "$check_fn"; then return 0; fi
    sleep 1
  done
  return 1
}

do_start() {
  mkdir -p "$LOG_DIR" "$PID_DIR"
  if api_ok; then
    echo "[api]    已在运行 (:$API_PORT)"
  else
    echo "[api]    启动中 (:$API_PORT)..."
    start_process api "$APP_ROOT" "$API_PID_FILE" \
      env HDB_API_HOST="$API_HOST" HDB_API_PORT="$API_PORT" uv run hardware-database-server
    if ! wait_for api_ok 15; then
      echo "[api]    启动失败，查看 $LOG_DIR/api.log"
      return 1
    fi
  fi

  if front_ok; then
    echo "[front]  已在运行 (:$FRONT_PORT)"
  else
    if [ ! -x "$APP_ROOT/frontend/node_modules/.bin/vite" ]; then
      echo "[front]  缺少前端依赖，请先在 frontend 执行 npm ci"
      return 1
    fi
    echo "[front]  启动中 (:$FRONT_PORT)..."
    start_process frontend "$APP_ROOT/frontend" "$FRONT_PID_FILE" \
      env VITE_API_PROXY_TARGET="http://127.0.0.1:$API_PORT" \
      ./node_modules/.bin/vite --host 0.0.0.0 --port "$FRONT_PORT" --strictPort
    if ! wait_for front_ok 10; then
      echo "[front]  启动失败，查看 $LOG_DIR/frontend.log"
      return 1
    fi
  fi
  do_status
}

do_stop() {
  stop_process frontend "$APP_ROOT/frontend" "$FRONT_PID_FILE"
  stop_process api "$APP_ROOT" "$API_PID_FILE"
  for _ in $(seq 1 5); do
    if ! api_ok && ! front_ok; then break; fi
    sleep 1
  done
  do_status || true
  return 0
}

do_status() {
  local rc=0
  if api_ok; then
    echo "✅ [api]    http://$API_HOST:$API_PORT"
  else
    echo "❌ [api]    未启动      -> bash scripts/hdb.sh start"
    rc=1
  fi
  if front_ok; then
    echo "✅ [front]  http://127.0.0.1:$FRONT_PORT"
  else
    echo "❌ [front]  未启动      -> bash scripts/hdb.sh start"
    rc=1
  fi
  [ "$rc" = 0 ] && echo "—— 全部就绪（API 子进程按本副本配置管理）——" || echo "—— 有缺失,执行: bash scripts/hdb.sh start ——"
  return "$rc"
}

do_logs() {
  for log in "$LOG_DIR/api.log" "$LOG_DIR/frontend.log" "$LOG_DIR/worker.log"; do
    [ -f "$log" ] && tail -n 30 "$log"
  done
}

case "${1:-status}" in
  status) do_status ;;
  start)  do_start ;;
  stop)   do_stop ;;
  logs)   do_logs ;;
  *) echo "用法: bash scripts/hdb.sh [status|start|stop|logs]" ;;
esac
