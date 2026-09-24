#!/usr/bin/env bash
# Hardware DataBase 项目自管 Redis（实时平面）管理脚本。
#
# 用法:
#   bash scripts/redis.sh init           # 生成 deploy/redis/.env（随机密码，0600）
#   bash scripts/redis.sh up             # 启动/更新容器；健康后把 REDIS_URL 写入应用 .env
#   bash scripts/redis.sh down           # 停止容器（保留数据卷）
#   bash scripts/redis.sh status         # 容器状态 + 应用侧配置提示
#   bash scripts/redis.sh logs           # 最近日志
#   bash scripts/redis.sh url            # 打印 REDIS_URL（默认打码，--show 显示明文）
#   bash scripts/redis.sh test [pytest参数...]  # 跑 Redis 门控测试（用 15 号库）
#
# 实例定义在 deploy/redis/（docker compose，仅监听宿主 127.0.0.1）。
# REDIS_URL 是重启生效项：`up` 写入后需执行 `bash scripts/hdb.sh restart`。
# 当前用户不在 docker 组时会回退 `sudo docker`（交互式输入密码）。
set -u

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT" || exit 1

COMPOSE_FILE="$APP_ROOT/deploy/redis/docker-compose.yml"
DEPLOY_ENV="$APP_ROOT/deploy/redis/.env"
APP_ENV="$APP_ROOT/.env"
SERVICE="redis"
DEFAULT_PORT=6380

# 只读取白名单键，不 source 整个 .env（避免把提示词里的 $ 展开进 shell）。
read_env_value() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 1
  grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2-
}

deploy_port() {
  local port
  port="$(read_env_value "$DEPLOY_ENV" HDB_REDIS_PORT || true)"
  printf '%s\n' "${port:-$DEFAULT_PORT}"
}

deploy_password() {
  read_env_value "$DEPLOY_ENV" REDIS_PASSWORD || true
}

# ---- docker 探测：无权限时回退 sudo（仅在交互终端可用）----
DOCKER_CMD=()

detect_docker() {
  if [ "${#DOCKER_CMD[@]}" -gt 0 ]; then return 0; fi
  if docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
  elif command -v sudo >/dev/null 2>&1; then
    echo "[redis] 当前用户无 docker 权限，改用 sudo（可能需要输入密码）" >&2
    DOCKER_CMD=(sudo docker)
  else
    echo "[redis] 无法访问 docker daemon：请将用户加入 docker 组，或以 root 运行。" >&2
    return 1
  fi
}

run_docker() {
  detect_docker || return 1
  "${DOCKER_CMD[@]}" "$@"
}

compose() {
  if [ ! -f "$DEPLOY_ENV" ]; then
    echo "[redis] 缺少 deploy/redis/.env：先运行 bash scripts/redis.sh init" >&2
    return 1
  fi
  run_docker compose --env-file "$DEPLOY_ENV" -f "$COMPOSE_FILE" "$@"
}

# ---- 生命周期 ----
do_init() {
  if [ -f "$DEPLOY_ENV" ]; then
    echo "[redis] deploy/redis/.env 已存在（端口 $(deploy_port)）"
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
    echo "# 由 scripts/redis.sh init 生成（gitignored）。改端口/密码后需 up + 应用 restart。"
    echo "HDB_REDIS_PORT=$DEFAULT_PORT"
    echo "REDIS_PASSWORD=$password"
  } > "$DEPLOY_ENV"
  chmod 600 "$DEPLOY_ENV"
  echo "[redis] 已生成 deploy/redis/.env（随机密码，端口 $DEFAULT_PORT）"
}

wait_ready() {
  local password="$1" tries="${2:-30}" i out
  for i in $(seq 1 "$tries"); do
    out="$(compose exec -T "$SERVICE" redis-cli -a "$password" --no-auth-warning ping 2>/dev/null || true)"
    [ "$out" = "PONG" ] && return 0
    sleep 1
  done
  return 1
}

# 仅当应用 .env 的 REDIS_URL 为空时写入；已指向其它实例则只提示（FORCE_ENV=1 覆盖）。
set_app_redis_url() {
  local url="$1" current
  if [ ! -f "$APP_ENV" ]; then
    echo "[redis] 未找到应用 .env，请手动设置 REDIS_URL=$url"
    return 0
  fi
  current="$(read_env_value "$APP_ENV" REDIS_URL || true)"
  if [ "$current" = "$url" ]; then
    echo "[redis] 应用 .env 的 REDIS_URL 已是最新"
    return 0
  fi
  if [ -n "$current" ] && [ "${FORCE_ENV:-0}" != "1" ]; then
    echo "[redis] 应用 .env 已有 REDIS_URL（指向其它实例），保持不变；如需切换："
    echo "         FORCE_ENV=1 bash scripts/redis.sh up"
    return 0
  fi
  local tmp="${APP_ENV}.tmp.$$"
  if grep -qE '^REDIS_URL=' "$APP_ENV"; then
    awk -v v="$url" '/^REDIS_URL=/{print "REDIS_URL=" v; next} {print}' "$APP_ENV" > "$tmp"
  else
    cp "$APP_ENV" "$tmp"
    printf '\n# Redis 实时平面（由 scripts/redis.sh 写入）\nREDIS_URL=%s\n' "$url" >> "$tmp"
  fi
  chmod --reference="$APP_ENV" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  # 以 root 运行（sudo bash scripts/redis.sh up）时不能让 .env 变成 root 属主，
  # 否则应用/系统配置页后续无法写回。
  chown --reference="$APP_ENV" "$tmp" 2>/dev/null || true
  mv "$tmp" "$APP_ENV"
  echo "[redis] 已写入应用 .env：REDIS_URL=redis://:***@127.0.0.1:$(deploy_port)/0"
}

do_up() {
  do_init || return 1
  local password port url
  password="$(deploy_password)"
  port="$(deploy_port)"
  if [ -z "$password" ]; then
    echo "[redis] deploy/redis/.env 缺少 REDIS_PASSWORD" >&2
    return 1
  fi
  url="redis://:${password}@127.0.0.1:${port}/0"
  echo "[redis] 启动容器（127.0.0.1:${port} -> 6379）..."
  compose up -d || return 1
  if wait_ready "$password"; then
    echo "[redis] 容器健康"
    set_app_redis_url "$url" || true
    echo "[redis] 下一步：bash scripts/hdb.sh restart   # 应用侧重启生效（REDIS_URL 为重启项）"
    echo "[redis] 自检：  bash scripts/redis.sh test"
  else
    echo "[redis] 等待健康超时，查看日志：bash scripts/redis.sh logs" >&2
    return 1
  fi
}

do_down() {
  compose down || return 1
  echo "[redis] 已停止（数据卷保留）。应用侧建议同时置空 REDIS_URL 并 restart；"
  echo "         若保持配置，Redis 不可用时实时面按 fail-soft 退化到 DB 轮询。"
}

do_status() {
  local password port current
  password="$(deploy_password)"
  port="$(deploy_port)"
  compose ps || true
  if [ -n "$password" ] && wait_ready "$password" 3; then
    echo "[redis] PING 正常：127.0.0.1:${port}"
  else
    echo "[redis] PING 失败（容器未起或密码不匹配）-> bash scripts/redis.sh up"
  fi
  current="$(read_env_value "$APP_ENV" REDIS_URL || true)"
  if [ -n "$current" ]; then
    echo "[redis] 应用 .env：REDIS_URL 已配置（redis://:***@$(printf '%s' "$current" | sed -E 's#.*@##')）"
  else
    echo "[redis] 应用 .env：REDIS_URL 未配置 -> bash scripts/redis.sh up 自动写入"
  fi
}

do_logs() {
  compose logs --tail 80 "$SERVICE"
}

do_url() {
  local password port
  password="$(deploy_password)"
  port="$(deploy_port)"
  if [ -z "$password" ]; then
    echo "[redis] 尚未初始化：先跑 bash scripts/redis.sh init" >&2
    return 1
  fi
  if [ "${1:-}" = "--show" ]; then
    printf 'redis://:%s@127.0.0.1:%s/0\n' "$password" "$port"
  else
    printf 'redis://:***@127.0.0.1:%s/0 （--show 显示明文）\n' "$port"
  fi
}

do_test() {
  local password port url
  password="$(deploy_password)"
  port="$(deploy_port)"
  if [ -z "$password" ]; then
    echo "[redis] 尚未初始化：先跑 bash scripts/redis.sh up" >&2
    return 1
  fi
  url="redis://:${password}@127.0.0.1:${port}/15"
  echo "[redis] 运行 Redis 门控测试（15 号库，不影响实时数据）..."
  HDB_TEST_REDIS_URL="$url" uv run python -m pytest \
    tests/test_redis_events.py \
    tests/test_redis_cancel.py \
    tests/test_distributed_admission.py \
    tests/test_provider_quota.py \
    "$@"
}

case "${1:-status}" in
  init)   do_init ;;
  up)     do_up ;;
  down)   do_down ;;
  status) do_status ;;
  logs)   do_logs ;;
  url)    shift; do_url "$@" ;;
  test)   shift; do_test "$@" ;;
  *) echo "用法: bash scripts/redis.sh [init|up|down|status|logs|url|test]" ;;
esac
