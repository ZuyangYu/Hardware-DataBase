#!/usr/bin/env bash
# Hardware DataBase 项目自管 PostgreSQL 管理脚本。
#
# 用法:
#   bash scripts/postgres.sh init        # 生成 deploy/postgres/.env（随机密码，0600）
#   bash scripts/postgres.sh up          # 启动/更新容器并等待健康
#   bash scripts/postgres.sh down        # 停止容器（保留数据卷）
#   bash scripts/postgres.sh status      # 容器状态 + 连接检查
#   bash scripts/postgres.sh logs        # 最近日志
#   bash scripts/postgres.sh dsn         # 打印 5 个后端 DSN（默认打码，--show 显示明文）
#   bash scripts/postgres.sh psql [args] # docker exec 进 psql（默认 -d hdb）
#   bash scripts/postgres.sh write-env   # 把 5 个 PG 后端写入应用 .env（备份原文件）
#
# 实例定义在 deploy/postgres/（docker compose，pgvector 镜像，仅监听宿主 127.0.0.1）。
# 后端开关均为重启生效项：write-env 后需执行 `bash scripts/hdb.sh restart`。
# 当前用户不在 docker 组时会回退 `sudo docker`（交互式输入密码）。
set -u

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT" || exit 1

COMPOSE_FILE="$APP_ROOT/deploy/postgres/docker-compose.yml"
DEPLOY_ENV="$APP_ROOT/deploy/postgres/.env"
APP_ENV="$APP_ROOT/.env"
SERVICE="postgres"
DEFAULT_PORT=55433
DEFAULT_USER="hdb"
DEFAULT_DB="hdb"

read_env_value() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2-
}

deploy_port() {
  local port
  port="$(read_env_value "$DEPLOY_ENV" HDB_PG_PORT || true)"
  printf '%s\n' "${port:-$DEFAULT_PORT}"
}

pg_user() { printf '%s\n' "$(read_env_value "$DEPLOY_ENV" POSTGRES_USER || true)" | grep . || printf '%s\n' "$DEFAULT_USER"; }
pg_db()   { printf '%s\n' "$(read_env_value "$DEPLOY_ENV" POSTGRES_DB || true)" | grep . || printf '%s\n' "$DEFAULT_DB"; }
pg_password() { read_env_value "$DEPLOY_ENV" POSTGRES_PASSWORD || true; }

dsn_for() {
  local db="$1"
  printf 'postgresql://%s:%s@127.0.0.1:%s/%s\n' "$(pg_user)" "$(pg_password)" "$(deploy_port)" "$db"
}

DOCKER_CMD=()
detect_docker() {
  if [ "${#DOCKER_CMD[@]}" -gt 0 ]; then return 0; fi
  if docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
  elif command -v sudo >/dev/null 2>&1; then
    echo "[pg] 当前用户无 docker 权限，改用 sudo（可能需要输入密码）" >&2
    DOCKER_CMD=(sudo docker)
  else
    echo "[pg] 无法访问 docker daemon：请将用户加入 docker 组，或以 root 运行。" >&2
    return 1
  fi
}

run_docker() {
  detect_docker || return 1
  "${DOCKER_CMD[@]}" "$@"
}

compose() {
  if [ ! -f "$DEPLOY_ENV" ]; then
    echo "[pg] 缺少 deploy/postgres/.env：先运行 bash scripts/postgres.sh init" >&2
    return 1
  fi
  run_docker compose --env-file "$DEPLOY_ENV" -f "$COMPOSE_FILE" "$@"
}

do_init() {
  if [ -f "$DEPLOY_ENV" ]; then
    echo "[pg] deploy/postgres/.env 已存在（端口 $(deploy_port)）"
    return 0
  fi
  local password
  if command -v openssl >/dev/null 2>&1; then
    password="$(openssl rand -hex 16)"
  else
    password="$(uv run python -c 'import secrets; print(secrets.token_hex(16))')" || return 1
  fi
  umask 077
  {
    echo "# 由 scripts/postgres.sh init 生成（gitignored）。改端口/密码后需 up + 应用 restart。"
    echo "HDB_PG_PORT=$DEFAULT_PORT"
    echo "POSTGRES_USER=$DEFAULT_USER"
    echo "POSTGRES_DB=$DEFAULT_DB"
    echo "POSTGRES_PASSWORD=$password"
  } > "$DEPLOY_ENV"
  chmod 600 "$DEPLOY_ENV"
  echo "[pg] 已生成 deploy/postgres/.env（随机密码，端口 $DEFAULT_PORT）"
}

wait_ready() {
  local tries="${1:-30}" i out
  for i in $(seq 1 "$tries"); do
    out="$(compose exec -T "$SERVICE" pg_isready -U "$(pg_user)" -d "$(pg_db)" 2>/dev/null || true)"
    case "$out" in *"accepting connections"*) return 0 ;; esac
    sleep 1
  done
  return 1
}

do_up() {
  do_init || return 1
  local password port
  password="$(pg_password)"
  port="$(deploy_port)"
  if [ -z "$password" ]; then
    echo "[pg] deploy/postgres/.env 缺少 POSTGRES_PASSWORD" >&2
    return 1
  fi
  echo "[pg] 启动容器（127.0.0.1:${port} -> 5432）..."
  compose up -d || return 1
  if wait_ready; then
    echo "[pg] 容器健康（库 $(pg_db)，用户 $(pg_user)）"
    echo "[pg] 下一步：bash scripts/postgres.sh write-env && bash scripts/hdb.sh restart"
  else
    echo "[pg] 等待健康超时，查看日志：bash scripts/postgres.sh logs" >&2
    return 1
  fi
}

do_down() {
  compose down || return 1
  echo "[pg] 已停止（数据卷保留）。应用侧建议同时把 PG 后端开关写回 sqlite 并 restart。"
}

do_status() {
  compose ps || true
  if wait_ready 3; then
    echo "[pg] 连接正常：127.0.0.1:$(deploy_port)/$(pg_db)"
  else
    echo "[pg] 连接失败（容器未起或密码不匹配）-> bash scripts/postgres.sh up"
  fi
  local backend
  backend="$(read_env_value "$APP_ENV" CONTROL_PLANE_BACKEND || true)"
  echo "[pg] 应用 .env：CONTROL_PLANE_BACKEND=${backend:-<未设置，默认 sqlite>}"
}

do_logs() { compose logs --tail 80 "$SERVICE"; }

do_dsn() {
  local db
  if [ -z "$(pg_password)" ]; then
    echo "[pg] 尚未初始化：先跑 bash scripts/postgres.sh init" >&2
    return 1
  fi
  db="$(pg_db)"
  if [ "${1:-}" = "--show" ]; then
    printf '%s\n' "$(dsn_for "$db")"
  else
    printf 'postgresql://%s:***@127.0.0.1:%s/%s （--show 显示明文）\n' "$(pg_user)" "$(deploy_port)" "$db"
  fi
}

do_psql() {
  compose exec -T "$SERVICE" psql -U "$(pg_user)" "${@:--d "$(pg_db)"}"
}

# 把 5 个 PG 后端写入应用 .env（保留注释；原文件先备份）。
do_write_env() {
  local db dsn backend="$APP_ENV"
  if [ -z "$(pg_password)" ]; then
    echo "[pg] 尚未初始化：先跑 bash scripts/postgres.sh init" >&2
    return 1
  fi
  db="$(pg_db)"
  dsn="$(dsn_for "$db")"
  if [ ! -f "$backend" ]; then
    echo "[pg] 未找到应用 .env" >&2
    return 1
  fi

  local backup="${backend}.bak-pg-$(date +%Y%m%d%H%M%S)"
  cp -p "$backend" "$backup"
  echo "[pg] 已备份原 .env：$backup"

  local -a pairs=(
    "CONTROL_PLANE_BACKEND=postgres"
    "CONTROL_PLANE_DATABASE_URL=$dsn"
    "MEMORY_STORE_BACKEND=postgres"
    "MEMORY_DATABASE_URL=$dsn"
    "AGENT_CHECKPOINT_BACKEND=postgres"
    "AGENT_CHECKPOINT_DATABASE_URL=$dsn"
    "DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=postgres"
    "DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL=$dsn"
    "RESOURCE_GOVERNOR_BACKEND=postgres"
    "RESOURCE_GOVERNOR_DATABASE_URL=$dsn"
    "RESOURCE_GOVERNOR_SCOPE=deployment"
  )
  local pair key tmp="${backend}.tmp.$$"
  cp "$backend" "$tmp"
  for pair in "${pairs[@]}"; do
    key="${pair%%=*}"
    if grep -qE "^${key}=" "$tmp"; then
      awk -v k="$key" -v v="${pair#*=}" -F= 'BEGIN{FS=OFS="="} $1==k {print k, v; next} {print}' "$tmp" > "${tmp}.2" && mv "${tmp}.2" "$tmp"
    else
      printf '%s\n' "$pair" >> "$tmp"
    fi
  done
  chmod --reference="$backend" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  chown --reference="$backend" "$tmp" 2>/dev/null || true
  mv "$tmp" "$backend"
  echo "[pg] 已写入 5 个 PG 后端（DSN 指向 127.0.0.1:$(deploy_port)/${db}）"
  echo "[pg] 下一步：bash scripts/hdb.sh restart（并盯 /health/ready 的 control_plane）"
}

case "${1:-status}" in
  init)      do_init ;;
  up)        do_up ;;
  down)      do_down ;;
  status)    do_status ;;
  logs)      do_logs ;;
  dsn)       shift; do_dsn "$@" ;;
  psql)      shift; do_psql "$@" ;;
  write-env) do_write_env ;;
  *) echo "用法: bash scripts/postgres.sh [init|up|down|status|logs|dsn|psql|write-env]" ;;
esac
