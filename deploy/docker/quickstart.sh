#!/usr/bin/env bash
# Hardware DataBase —— Docker 快速部署（开源用户首选路径）。
#
#   bash deploy/docker/quickstart.sh                         # 生产部署（含观测栈）
#   bash deploy/docker/quickstart.sh --no-start              # 只准备文件和密钥，不构建/启动
#   bash deploy/docker/quickstart.sh --without-observability # 资源受限环境的显式例外
#
# 前置：Docker Engine + compose 插件；外部服务 RAGFlow 与 LLM（Ollama 或
# OpenAI 兼容 API）由使用者自备——先起服务、登录后在「系统配置」里填也可以。
#
# 它做三件事（全部幂等，可重复执行）：
#   1. 生成 PostgreSQL 密码（写入 deploy/docker/.env）
#   2. 生成应用配置 config/.env；管理员口令为空时自动生成并打印
#   3. 构建镜像并启动生产栈（postgres -> migrate -> api -> web + 观测栈）
#      --without-observability 仅在资源受限环境下跳过观测组件。
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

log() { printf '[quickstart] %s\n' "$*"; }
die() { printf '[quickstart] 错误: %s\n' "$*" >&2; exit 1; }

START=1
# 生产部署默认启动观测栈；保留 --with-observability 作为旧命令的兼容参数。
WITH_OBS=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    --with-observability) WITH_OBS=1 ;;
    --without-observability) WITH_OBS=0 ;;
    *) die "未知参数: $arg（支持 --no-start / --without-observability）" ;;
  esac
done

command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker Engine + compose 插件。"
docker info >/dev/null 2>&1 || die "无法访问 docker daemon（当前用户需加入 docker 组，或由管理员运行：sudo usermod -aG docker $USER；重新登录后再试）。"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件（docker-compose-plugin）。"

if [ -e .env ] && [ ! -r .env ]; then
  die "deploy/docker/.env 当前不可读；请由管理员将其交给部署用户读取，或先执行 chown。"
fi
if [ -d config ] && [ ! -w config ]; then
  die "deploy/docker/config 目录不可写；请由管理员将 config/storage 交给部署用户。"
fi
if [ -d storage ] && [ ! -w storage ]; then
  die "deploy/docker/storage 目录不可写；请由管理员将 config/storage 交给部署用户。"
fi

compose_value() {
  local key="$1" fallback="$2" value=""
  if [ -r .env ]; then
    value="$(grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${value:-$fallback}"
}

check_host_port() {
  local port="$1" label="$2"
  if command -v ss >/dev/null 2>&1 && ss -H -ltn 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    die "$label 端口 ${port} 已被占用；请在 deploy/docker/.env 改端口后重试。"
  fi
}

if [ "$START" -eq 1 ]; then
  check_host_port "$(compose_value HDB_HTTP_PORT 8080)" "Web"
  check_host_port "$(compose_value HDB_API_PORT 8001)" "API"
  check_host_port "$(compose_value HDB_MCP_HOST_PORT 9100)" "MCP"
  if [ "$WITH_OBS" -eq 1 ]; then
    check_host_port "$(compose_value OBS_GRAFANA_PORT 3000)" "Grafana"
    check_host_port "$(compose_value OBS_PHOENIX_PORT 6006)" "Phoenix"
    check_host_port "$(compose_value OBS_PROMETHEUS_PORT 9090)" "Prometheus"
    check_host_port "$(compose_value OBS_ALERTMANAGER_PORT 9093)" "Alertmanager"
    check_host_port "$(compose_value OBS_OTLP_GRPC_PORT 4317)" "OTLP gRPC"
    check_host_port "$(compose_value OBS_OTLP_HTTP_PORT 4318)" "OTLP HTTP"
    check_host_port "$(compose_value OBS_METRICS_PORT 9464)" "OTel metrics"
  fi
fi

available_kb="$(df -Pk . | awk 'NR==2 {print $4}')"
if [ -n "$available_kb" ] && [ "$available_kb" -lt 10485760 ]; then
  log "警告：当前磁盘可用空间少于 10 GiB，首次构建应用/观测镜像可能失败；请先扩容或清理磁盘。"
fi

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16
  else
    tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16 || true
  fi
}

# ---- 1) PostgreSQL 密码（compose 变量）----
if [ ! -f .env ] || ! grep -qE '^HDB_PG_PASSWORD=.+' .env; then
  printf 'HDB_PG_PASSWORD=%s\n' "$(gen_secret)" >> .env
  chmod 600 .env
  log "已生成 PostgreSQL 密码 -> .env"
else
  log "沿用已有 PostgreSQL 密码（.env）"
fi

# The API and all workers use one unprivileged identity for shared bind mounts.
if ! grep -qE '^HDB_UID=' .env; then
  printf 'HDB_UID=%s\nHDB_GID=%s\n' "${SUDO_UID:-$(id -u)}" "${SUDO_GID:-$(id -g)}" >> .env
fi
# Direct root invocation uses the image's dedicated hdb user.
sed -i 's/^HDB_UID=0$/HDB_UID=10001/;s/^HDB_GID=0$/HDB_GID=10001/' .env
chmod 600 .env

# ---- 1b) 生产观测栈 Grafana 口令（默认启用）----
if [ "$WITH_OBS" -eq 1 ]; then
  if [ ! -f .env ] || ! grep -qE '^GRAFANA_ADMIN_PASSWORD=.+' .env; then
    printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$(gen_secret)" >> .env
    chmod 600 .env
    log "已生成 Grafana 管理员口令（deploy/docker/.env）"
  fi
  if ! grep -qE '^PHOENIX_DB_PASSWORD=.+' .env; then
    printf 'PHOENIX_DB_PASSWORD=%s\n' "$(gen_secret)" >> .env
  fi
fi

# ---- 2) 应用配置 ----
mkdir -p storage config
if [ ! -f config/.env ]; then
  cp hdb.env.example config/.env
  log "已从模板生成 config/.env"
fi
# 旧模板写死的 RESOURCE_GOVERNOR_SCOPE=process 与本编排（后端 postgres）冲突：
# 保存「系统配置」时以本文件为准覆盖环境变量，会直接校验失败。就地纠正。
if grep -qE '^RESOURCE_GOVERNOR_SCOPE=process$' config/.env; then
  sed -i 's/^RESOURCE_GOVERNOR_SCOPE=process$/RESOURCE_GOVERNOR_SCOPE=deployment/' config/.env
  log "已纠正 config/.env: RESOURCE_GOVERNOR_SCOPE=deployment（容器部署要求）"
fi
if ! grep -qE '^AUTH_DEFAULT_ADMIN_PASSWORD=.+' config/.env; then
  ADMIN_PW="$(gen_secret)"
  awk -v v="$ADMIN_PW" '
    /^AUTH_DEFAULT_ADMIN_PASSWORD=/ { print "AUTH_DEFAULT_ADMIN_PASSWORD=" v; found=1; next }
    { print }
    END { if (!found) print "AUTH_DEFAULT_ADMIN_PASSWORD=" v }
  ' config/.env > config/.env.tmp
  mv config/.env.tmp config/.env
  log "已生成管理员口令：admin / ${ADMIN_PW}（保存在 config/.env）"
fi
# 局域网 MCP：首次生成配置时把对外 Host 设为当前主机 IP，避免客户端收到
# 421 Invalid Host header；使用域名/反向代理时可在 config/.env 改掉。
mcp_host_ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)"
if [ -n "$mcp_host_ip" ] && ! grep -qE '^HDB_MCP_HTTP_HOST=.+' config/.env; then
  awk -v v="$mcp_host_ip" '
    /^HDB_MCP_HTTP_HOST=/ { print "HDB_MCP_HTTP_HOST=" v; found=1; next }
    { print }
    END { if (!found) print "HDB_MCP_HTTP_HOST=" v }
  ' config/.env > config/.env.tmp
  mv config/.env.tmp config/.env
  log "已设置 MCP 局域网地址：HDB_MCP_HTTP_HOST=${mcp_host_ip}"
fi
chmod 600 config/.env
if [ "$(id -u)" -eq 0 ]; then
  runtime_uid="$(grep '^HDB_UID=' .env | tail -1 | cut -d= -f2-)"
  runtime_gid="$(grep '^HDB_GID=' .env | tail -1 | cut -d= -f2-)"
  chown -R "${runtime_uid}:${runtime_gid}" config storage
fi
log "RAGFlow / 模型端点可稍后在「系统配置」页面填写（config/.env 里也可直接改）"

COMPOSE_FILES=(-f docker-compose.yml)
if [ "$WITH_OBS" -eq 1 ]; then
  COMPOSE_FILES+=(-f docker-compose.observability.yml)
fi

if [ "$START" -eq 0 ]; then
  log "已准备就绪。启动：docker compose ${COMPOSE_FILES[*]} up -d（首次先带 docker-compose.build.yml 构建）"
  exit 0
fi

# ---- 3) 构建 + 启动 ----
log "构建镜像（首次 5~15 分钟，可用 APT_MIRROR/PIP_INDEX_URL/NPM_REGISTRY 加速）..."
docker compose -f docker-compose.yml -f docker-compose.build.yml build
if [ "$WITH_OBS" -eq 1 ]; then
  log "启动生产栈（postgres + redis -> migrate -> api -> web + 观测栈）..."
else
  log "启动生产栈（postgres + redis -> migrate -> api -> web；已显式跳过观测栈）..."
fi
docker compose "${COMPOSE_FILES[@]}" up -d --wait --wait-timeout 300

host_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
log "完成。访问 http://${host_ip:-<本机IP>}:8080 ，用 admin 登录"
log "首次登录后到「系统配置」填 RAGFlow 与模型端点；日志：docker compose logs -f api"
mcp_bind="$(compose_value HDB_MCP_BIND 0.0.0.0)"
mcp_port="$(compose_value HDB_MCP_HOST_PORT 9100)"
case "$mcp_bind" in
  0.0.0.0) mcp_host="${host_ip:-<本机IP>}" ;;
  *) mcp_host="$mcp_bind" ;;
esac
log "MCP：http://${mcp_host}:${mcp_port}/mcp（局域网；需在 config/.env 设置 HDB_MCP_HTTP_HOST）"
if [ "$WITH_OBS" -eq 1 ]; then
  GRAFANA_PW="$(grep -E '^GRAFANA_ADMIN_PASSWORD=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  log "观测栈：Grafana http://${host_ip:-127.0.0.1}:3000（admin / ${GRAFANA_PW:-change-me}）"
  log "        Phoenix http://${host_ip:-127.0.0.1}:6006 ；Prometheus http://${host_ip:-127.0.0.1}:9090"
else
  log "已显式跳过观测栈；如需恢复：quickstart.sh（默认含观测）"
fi
