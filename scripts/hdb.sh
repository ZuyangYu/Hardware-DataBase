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
WORKER_PID_FILE="$PID_DIR/worker.pid"
MEMORY_WORKER_PID_FILE="$PID_DIR/memory-worker.pid"

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

NODEENV="${NODEENV:-/tmp/opencode/nodeenv}"

ensure_node() {
  if [ -x "${NODEENV}/lib/python3.12/site-packages/nodejs_wheel/bin/node" ]; then return 0; fi
  echo "[node] 便携 Node 不存在(${NODEENV} 已清)，重新安装..."
  rm -rf "${NODEENV}"
  uv venv "${NODEENV}" -q && uv pip install -q --python "${NODEENV}/bin/python" nodejs-wheel-binaries
}

start_front() {
  ensure_node
  export PATH="${NODEENV}/lib/python3.12/site-packages/nodejs_wheel/bin:$PATH"
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
}

do_start() {
  mkdir -p "$LOG_DIR" "$PID_DIR"
  if api_ok; then
    if managed_pid "$API_PID_FILE" "$APP_ROOT" >/dev/null; then
      echo "[api]    已在运行 (:$API_PORT, pid $(cat "$API_PID_FILE"))"
    else
      # 端口被没有 pid 文件的进程占用（多为手动启动的旧进程）：自动清理，
      # 否则 stop 永远杀不到它，新代码也永远加载不上。
      echo "[api]    检测到未知进程占用 :$API_PORT，自动清理..."
      fuser -k "${API_PORT}/tcp" 2>/dev/null || true
      sleep 2
      if api_ok; then
        echo "[api]    未知进程清理失败，请手动处理: fuser -k ${API_PORT}/tcp"
        return 1
      fi
      echo "[api]    启动中 (:$API_PORT)..."
      start_process api "$APP_ROOT" "$API_PID_FILE" \
        env HDB_API_HOST="$API_HOST" HDB_API_PORT="$API_PORT" uv run hardware-database-server
      if ! wait_for api_ok 15; then
        echo "[api]    启动失败，查看 $LOG_DIR/api.log"
        return 1
      fi
    fi
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
    if managed_pid "$FRONT_PID_FILE" "$APP_ROOT/frontend" >/dev/null; then
      echo "[front]  已在运行 (:$FRONT_PORT, pid $(cat "$FRONT_PID_FILE"))"
    else
      echo "[front]  检测到未知进程占用 :$FRONT_PORT，自动清理..."
      fuser -k "${FRONT_PORT}/tcp" 2>/dev/null || true
      sleep 2
      start_front
    fi
  else
    start_front
  fi

  # 对话 turns 执行器：没有它，前端对话会一直停留或报错。
  if managed_pid "$WORKER_PID_FILE" "$APP_ROOT" >/dev/null; then
    echo "[worker] 已在运行 (pid $(cat "$WORKER_PID_FILE"))"
  else
    echo "[worker] 启动中..."
    start_process worker "$APP_ROOT" "$WORKER_PID_FILE" \
      uv run hardware-database-worker
  fi

  # 长期记忆提炼器：没有它，"创建个人记忆/自动提炼"会永远停在 pending。
  if managed_pid "$MEMORY_WORKER_PID_FILE" "$APP_ROOT" >/dev/null; then
    echo "[memory] 已在运行 (pid $(cat "$MEMORY_WORKER_PID_FILE"))"
  else
    echo "[memory] 启动中..."
    start_process memory-worker "$APP_ROOT" "$MEMORY_WORKER_PID_FILE" \
      uv run hardware-database-memory-worker
  fi
  do_status
}

do_stop() {
  stop_process frontend "$APP_ROOT/frontend" "$FRONT_PID_FILE"
  stop_process api "$APP_ROOT" "$API_PID_FILE"
  stop_process worker "$APP_ROOT" "$WORKER_PID_FILE"
  stop_process memory-worker "$APP_ROOT" "$MEMORY_WORKER_PID_FILE"
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
  if managed_pid "$WORKER_PID_FILE" "$APP_ROOT" >/dev/null; then
    echo "✅ [worker] pid $(cat "$WORKER_PID_FILE")"
  else
    echo "❌ [worker] 未启动(对话不会执行!) -> bash scripts/hdb.sh start"
    rc=1
  fi
  if managed_pid "$MEMORY_WORKER_PID_FILE" "$APP_ROOT" >/dev/null; then
    echo "✅ [memory] pid $(cat "$MEMORY_WORKER_PID_FILE")"
  else
    echo "❌ [memory] 未启动(长期记忆不会提炼!) -> bash scripts/hdb.sh start"
    rc=1
  fi
  [ "$rc" = 0 ] && echo "—— 全部就绪（API 子进程按本副本配置管理）——" || echo "—— 有缺失,执行: bash scripts/hdb.sh start ——"
  return "$rc"
}

do_logs() {
  for log in "$LOG_DIR/api.log" "$LOG_DIR/frontend.log" "$LOG_DIR/worker.log" "$LOG_DIR/memory-worker.log"; do
    [ -f "$log" ] && tail -n 30 "$log"
  done
}

case "${1:-status}" in
  status)  do_status ;;
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  logs)    do_logs ;;
  *) echo "用法: bash scripts/hdb.sh [status|start|stop|restart|logs]" ;;
esac
