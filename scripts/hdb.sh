#!/usr/bin/env bash
# Hardware DataBase 本地服务管理: status / start / stop / logs
# 用法: bash scripts/hdb.sh [status|start|stop|logs]
set -u

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT" || exit 1

# 允许 .env 中的 HDB_* 覆盖监听端口(只读取白名单键, 不 source 整个 .env,
# 避免把带 $ 的提示词等应用配置展开进 shell)。多 checkout 共存时端口互不冲突。
if [ -f "$APP_ROOT/.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      HDB_API_HOST|HDB_API_PORT|HDB_FRONT_PORT|HDB_WORKER_ROLES|HDB_DRAWIO_MCP_ENABLED|HDB_DRAWIO_MCP_PORT) export "$key=$value" ;;
    esac
  done < <(grep -E '^(HDB_API_HOST|HDB_API_PORT|HDB_FRONT_PORT|HDB_WORKER_ROLES|HDB_DRAWIO_MCP_ENABLED|HDB_DRAWIO_MCP_PORT)=' "$APP_ROOT/.env" 2>/dev/null || true)
fi

API_HOST="${HDB_API_HOST:-127.0.0.1}"
API_PORT="${HDB_API_PORT:-8001}"
FRONT_PORT="${HDB_FRONT_PORT:-5176}"
LOG_DIR="${HDB_LOG_DIR:-$APP_ROOT/storage/logs}"
PID_DIR="${HDB_PID_DIR:-$APP_ROOT/storage/run}"
API_PID_FILE="$PID_DIR/api.pid"
FRONT_PID_FILE="$PID_DIR/frontend.pid"
WORKER_PID_FILE="$PID_DIR/worker.pid"
MEMORY_WORKER_PID_FILE="$PID_DIR/memory-worker.pid"
EXPORT_WORKER_PID_FILE="$PID_DIR/export-worker.pid"
DRAWIO_MCP_ENABLED="${HDB_DRAWIO_MCP_ENABLED:-false}"
DRAWIO_MCP_PORT="${HDB_DRAWIO_MCP_PORT:-8091}"
DRAWIO_MCP_PID_FILE="$PID_DIR/drawio-mcp.pid"
# Worker 角色进程: 默认 chat/authoring/parse 三个独立进程(互不阻塞)。
# HDB_WORKER_ROLES=all 时回退为单进程 legacy 模式。
WORKER_ROLES="${HDB_WORKER_ROLES:-chat parse}"

# 后台 worker 命令名:聊天/文档轮次在 worker 进程内执行(agent 工具、
# 计划提交、记忆提炼),重复启动会留下携带旧代码的僵尸实例,因此
# start/stop 前先清理未纳管的本项目 worker。
WORKER_PATTERNS=(
  "hardware-database-worker"
  "hardware-database-memory-worker"
  "src.workers.main"
  "src.result_exports.worker"
)

api_ok()   { curl -sf -m 2 "http://${API_HOST}:${API_PORT}/health" >/dev/null 2>&1; }
front_ok() { curl -sf -m 2 -o /dev/null "http://127.0.0.1:${FRONT_PORT}/"; }
drawio_mcp_ok() { curl -sf -m 2 "http://127.0.0.1:${DRAWIO_MCP_PORT}/health" >/dev/null 2>&1; }
pid_alive() { kill -0 "$1" 2>/dev/null; }

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

# ---- worker 生命周期(纳管 + 清理未纳管旧实例) ----

worker_pid_file_for() {
  # $1 = role ("all" -> legacy single worker pid file)
  case "$1" in
    all) printf '%s\n' "$WORKER_PID_FILE" ;;
    *)   printf '%s\n' "$PID_DIR/worker-$1.pid" ;;
  esac
}

worker_pid_files() {
  printf '%s\n' "$WORKER_PID_FILE" "$MEMORY_WORKER_PID_FILE" "$EXPORT_WORKER_PID_FILE"
  local role
  for role in $WORKER_ROLES; do
    [ "$role" = "all" ] && continue
    printf '%s\n' "$PID_DIR/worker-$role.pid"
  done
}

is_managed_worker_pid() {
  local pid="$1" pid_file
  for pid_file in $(worker_pid_files); do
    if [ "$pid" = "$(sed -n '1p' "$pid_file" 2>/dev/null || true)" ]; then return 0; fi
  done
  return 1
}

cleanup_stale_workers() {
  local pattern pid killed=0
  for pattern in "${WORKER_PATTERNS[@]}"; do
    while read -r pid; do
      [ -n "$pid" ] || continue
      is_managed_worker_pid "$pid" && continue
      # 仅清理 cwd 属于本副本的进程,绝不误伤其它 checkout 的同名 worker。
      pid_matches_root "$pid" "$APP_ROOT" || continue
      if kill "$pid" 2>/dev/null; then
        echo "[cleanup] 停掉未纳管的旧 worker 进程 $pid (${pattern})"
        killed=$((killed + 1))
      fi
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
  done
  if [ "$killed" -gt 0 ]; then sleep 1; fi
  return 0
}

report_worker() {
  local name="$1" pid_file="$2" pid
  pid="$(sed -n '1p' "$pid_file" 2>/dev/null || true)"
  if [ -n "$pid" ] && pid_alive "$pid"; then
    echo "[${name}] 已启动 (pid ${pid})"
  else
    echo "[${name}] 未能启动,查看 $LOG_DIR/${name}.log"
  fi
}

start_worker_processes() {
  # 幂等:重复 start 先停掉已纳管实例,避免越积越多的 worker 抢同一队列。
  stop_worker_processes
  echo "[worker] 启动中..."
  local role
  if [ "$WORKER_ROLES" = "all" ]; then
    start_process worker "$APP_ROOT" "$WORKER_PID_FILE" \
      uv run hardware-database-worker
  else
    for role in $WORKER_ROLES; do
      start_process "worker-$role" "$APP_ROOT" "$(worker_pid_file_for "$role")" \
        uv run hardware-database-worker --role "$role"
    done
  fi
  start_process memory-worker "$APP_ROOT" "$MEMORY_WORKER_PID_FILE" \
    uv run hardware-database-memory-worker
  start_process export-worker "$APP_ROOT" "$EXPORT_WORKER_PID_FILE" \
    uv run python -m src.result_exports.worker
  sleep 1
  if [ "$WORKER_ROLES" = "all" ]; then
    report_worker worker "$WORKER_PID_FILE"
  else
    for role in $WORKER_ROLES; do
      report_worker "worker-$role" "$(worker_pid_file_for "$role")"
    done
  fi
  report_worker memory-worker "$MEMORY_WORKER_PID_FILE"
  report_worker export-worker "$EXPORT_WORKER_PID_FILE"
}

stop_worker_processes() {
  local role
  # legacy 单进程 pid 文件在任何模式下都清理，避免切换模式后残留双消费。
  stop_process worker "$APP_ROOT" "$WORKER_PID_FILE"
  if [ "$WORKER_ROLES" != "all" ]; then
    for role in $WORKER_ROLES; do
      stop_process "worker-$role" "$APP_ROOT" "$(worker_pid_file_for "$role")"
    done
  fi
  stop_process memory-worker "$APP_ROOT" "$MEMORY_WORKER_PID_FILE"
  stop_process export-worker "$APP_ROOT" "$EXPORT_WORKER_PID_FILE"
}

wait_for() {
  local check_fn="$1" attempts="$2"
  for _ in $(seq 1 "$attempts"); do
    if "$check_fn"; then return 0; fi
    sleep 1
  done
  return 1
}

start_drawio_mcp() {
  [ "$DRAWIO_MCP_ENABLED" = "true" ] || return 0
  local mcp_root="$APP_ROOT/deploy/drawio-mcp" node_bin
  if [ ! -d "$mcp_root/node_modules/@drawio/mcp" ]; then
    echo "[drawio-mcp] 依赖缺失，跳过（cd deploy/drawio-mcp && npm ci --ignore-scripts）"
    return 0
  fi
  if drawio_mcp_ok; then
    if managed_pid "$DRAWIO_MCP_PID_FILE" "$mcp_root" >/dev/null; then
      echo "[drawio-mcp] 已在运行 (:$DRAWIO_MCP_PORT)"
      return 0
    fi
    echo "[drawio-mcp] 检测到未纳管实例，自动重启..."
    fuser -k "${DRAWIO_MCP_PORT}/tcp" 2>/dev/null || true
    sleep 1
  fi
  node_bin="$(command -v node 2>/dev/null || true)"
  if [ -z "$node_bin" ]; then
    echo "[drawio-mcp] 未找到 node，跳过"
    return 0
  fi
  echo "[drawio-mcp] 启动中 (:$DRAWIO_MCP_PORT)..."
  start_process drawio-mcp "$mcp_root" "$DRAWIO_MCP_PID_FILE" \
    env DRAWIO_MCP_HOST=127.0.0.1 DRAWIO_MCP_PORT="$DRAWIO_MCP_PORT" \
    "$node_bin" server.mjs
  if ! wait_for drawio_mcp_ok 10; then
    echo "[drawio-mcp] 启动失败，查看 $LOG_DIR/drawio-mcp.log"
    return 1
  fi
}

stop_drawio_mcp() {
  stop_process drawio-mcp "$APP_ROOT/deploy/drawio-mcp" "$DRAWIO_MCP_PID_FILE"
}

NODEENV="${NODEENV:-$APP_ROOT/storage/nodeenv}"

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

run_migrate() {
  # 与容器编排一致：api / worker 启动前先串行建表（含 LangGraph 检查点表），
  # 避免运行期并发 setup() 在表缺失时撞唯一约束。
  mkdir -p "$LOG_DIR"
  echo "[migrate] 初始化存储（幂等，失败则中止启动）..."
  if (cd "$APP_ROOT" && uv run python -m src.storage.bootstrap > "$LOG_DIR/migrate.log" 2>&1); then
    echo "[migrate] 完成"
    return 0
  fi
  echo "[migrate] 失败，完整日志: $LOG_DIR/migrate.log"
  tail -n 15 "$LOG_DIR/migrate.log" 2>/dev/null
  return 1
}

do_start() {
  mkdir -p "$LOG_DIR" "$PID_DIR"
  # hdb.sh 自己管理各类 worker；API 内仍管理 Sandbox dispatcher/reconciler。
  export HDB_API_SPAWN_WORKER=0
  export HDB_API_SPAWN_EXPORT_WORKER=0
  export HDB_API_SPAWN_MEMORY_WORKER=0
  # 先清理历史残留的未纳管 worker,避免旧代码实例继续消费队列。
  cleanup_stale_workers
  run_migrate || return 1
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

  # chat/authoring/parse、memory、export 各启动一次，避免与 API 自启或
  # 本脚本的 legacy all worker 重复消费同一队列。
  start_worker_processes
  start_drawio_mcp || true
  do_status
}

do_stop() {
  stop_process frontend "$APP_ROOT/frontend" "$FRONT_PID_FILE"
  stop_process api "$APP_ROOT" "$API_PID_FILE"
  stop_worker_processes
  stop_drawio_mcp
  # 兜底清理未纳管的本项目 worker 实例。
  cleanup_stale_workers
  for _ in $(seq 1 5); do
    if ! api_ok && ! front_ok; then break; fi
    sleep 1
  done
  do_status || true
  return 0
}

do_status() {
  local rc=0 unmanaged=0
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
  local role pid_file name pid spec
  local worker_specs=""
  if [ "$WORKER_ROLES" = "all" ]; then
    worker_specs="worker:$WORKER_PID_FILE"
  else
    for role in $WORKER_ROLES; do
      worker_specs="$worker_specs worker-$role:$(worker_pid_file_for "$role")"
    done
  fi
  worker_specs="$worker_specs memory-worker:$MEMORY_WORKER_PID_FILE export-worker:$EXPORT_WORKER_PID_FILE"
  for spec in $worker_specs; do
    name="${spec%%:*}"
    pid_file="${spec#*:}"
    pid="$(managed_pid "$pid_file" "$APP_ROOT" 2>/dev/null || true)"
    if [ -n "$pid" ] && pid_alive "$pid"; then
      echo "✅ [${name}]  pid ${pid}"
    else
      echo "⚠️  [${name}]  未由 hdb.sh 纳管（手动启动时请检查 API 日志或进程）"
      unmanaged=1
    fi
  done
  if [ "$DRAWIO_MCP_ENABLED" = "true" ]; then
    local drawio_pid
    drawio_pid="$(managed_pid "$DRAWIO_MCP_PID_FILE" "$APP_ROOT/deploy/drawio-mcp" 2>/dev/null || true)"
    if [ -n "$drawio_pid" ] && pid_alive "$drawio_pid" && drawio_mcp_ok; then
      echo "✅ [drawio-mcp] http://127.0.0.1:$DRAWIO_MCP_PORT/mcp"
    else
      echo "⚠️  [drawio-mcp] 未启动（HDB_DRAWIO_MCP_ENABLED=true）"
      unmanaged=1
    fi
  fi
  if [ "$rc" = 0 ] && [ "$unmanaged" = 0 ]; then
    echo "—— API、前端和脚本纳管的 Worker 均已就绪 ——"
  elif [ "$rc" = 0 ]; then
    echo "—— API/前端可达；Worker 未纳管，需按所选启动模式核实 ——"
  else
    echo "—— API/前端有缺失，检查当前启动模式 ——"
  fi
  return "$rc"
}

do_logs() {
  local role logs log
  logs="$LOG_DIR/api.log $LOG_DIR/frontend.log $LOG_DIR/memory-worker.log $LOG_DIR/export-worker.log"
  [ "$DRAWIO_MCP_ENABLED" = "true" ] && logs="$logs $LOG_DIR/drawio-mcp.log"
  if [ "$WORKER_ROLES" = "all" ]; then
    logs="$logs $LOG_DIR/worker.log"
  else
    for role in $WORKER_ROLES; do
      logs="$logs $LOG_DIR/worker-$role.log"
    done
  fi
  for log in $logs; do
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
