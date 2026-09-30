#!/usr/bin/env bash
# Hardware DataBase 内网离线一键部署。
#
# 用法（在离线包根目录或 docker/ 目录内执行）：
#   bash offline-install.sh                    # 导入镜像 → 生成配置 → 启动（含观测栈）→ 就绪检查
#   bash offline-install.sh --without-observability # 不启动观测栈（资源受限时）
#   bash offline-install.sh --port 9090        # 指定 Web 端口（默认 8080）
#   bash offline-install.sh --help
#
# 幂等：重复执行会重新校验/导入镜像、保留已有配置，可直接用于升级
# （替换 hdb-images.tar.gz 后重跑即可）。
# 说明：本脚本只服务于“离线包”分发形态；在线部署请使用 quickstart.sh。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---- 定位离线包结构（兼容“包根目录”与“docker/ 子目录”两种位置）----
if [ -f "$SCRIPT_DIR/docker-compose.yml" ]; then
  DOCKER_DIR="$SCRIPT_DIR"
  BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -f "$SCRIPT_DIR/docker/docker-compose.yml" ]; then
  BUNDLE_DIR="$SCRIPT_DIR"
  DOCKER_DIR="$SCRIPT_DIR/docker"
else
  echo "[install] 错误: 未找到 docker-compose.yml，请在离线包根目录或 docker/ 目录内运行。" >&2
  exit 1
fi
IMAGES_TAR="$BUNDLE_DIR/hdb-images.tar.gz"

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] 警告: %s\n' "$*" >&2; }
die()  { printf '[install] 错误: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Hardware DataBase 内网离线一键部署。

用法（在离线包根目录或 docker/ 目录内执行）：
  bash $(basename "$0")                          # 导入镜像 → 生成配置 → 启动（含观测栈）→ 就绪检查
  bash $(basename "$0") --without-observability  # 不启动观测栈（资源受限时）
  bash $(basename "$0") --port 9090              # 指定 Web 端口（默认 8080）
  bash $(basename "$0") --help

幂等：重复执行会重新校验/导入镜像、保留已有配置，可直接用于升级
（替换 hdb-images.tar.gz 后重跑即可）。
EOF
  exit 0
}

WEB_PORT=""
WITH_OBS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --port) WEB_PORT="${2:?--port 需要一个端口号}"; shift 2 ;;
    --without-observability) WITH_OBS=0; shift ;;
    -h|--help) usage ;;
    *) die "未知参数: $1（支持 --port N / --without-observability / --help）" ;;
  esac
done

# ---- 权限与依赖 ----
if [ "$(id -u)" -ne 0 ] && ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    log "当前用户无 Docker 权限，改用 sudo 重新执行..."
    exec sudo -E bash "$0" "$@"
  fi
  die "无法访问 Docker daemon：请以 root 运行，或将用户加入 docker 组。"
fi
docker info >/dev/null 2>&1 || die "无法访问 Docker daemon。"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件（docker-compose-plugin）。"

# ---- 1) 校验并导入镜像 ----
[ -f "$IMAGES_TAR" ] || die "缺少镜像包: $IMAGES_TAR"
if [ -f "$IMAGES_TAR.sha256" ]; then
  log "校验镜像包..."
  (cd "$BUNDLE_DIR" && sha256sum -c "$(basename "$IMAGES_TAR").sha256") || die "镜像包校验失败，请重新拷贝。"
fi
log "导入镜像（首次约 1~3 分钟，已存在时仅刷新标签）..."
docker load -i "$IMAGES_TAR" >/dev/null

# ---- 2) 生成配置（保留已有） ----
cd "$DOCKER_DIR"
if [ ! -f config/.env ]; then
  log "生成密钥与配置..."
  bash quickstart.sh --no-start
else
  log "检测到 config/.env，保留现有配置"
fi

if [ -n "$WEB_PORT" ]; then
  if grep -qE '^HDB_HTTP_PORT=' .env 2>/dev/null; then
    sed -i "s/^HDB_HTTP_PORT=.*/HDB_HTTP_PORT=${WEB_PORT}/" .env
  else
    printf 'HDB_HTTP_PORT=%s\n' "$WEB_PORT" >> .env
  fi
  log "Web 端口已设为 ${WEB_PORT}"
fi
WEB_PORT="$(grep -E '^HDB_HTTP_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"; WEB_PORT="${WEB_PORT:-8080}"
API_PORT="$(grep -E '^HDB_API_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"; API_PORT="${API_PORT:-8001}"

# 观测栈强制要求口令；老配置可能缺失，这里兜底生成
if [ "$WITH_OBS" -eq 1 ]; then
  gen_secret() {
    if command -v openssl >/dev/null 2>&1; then
      openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | cut -c1-16
    else
      tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 16 || true
    fi
  }
  if ! grep -qE '^GRAFANA_ADMIN_PASSWORD=.+' .env 2>/dev/null; then
    printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$(gen_secret)" >> .env
  fi
  if ! grep -qE '^PHOENIX_DB_PASSWORD=.+' .env 2>/dev/null; then
    printf 'PHOENIX_DB_PASSWORD=%s\n' "$(gen_secret)" >> .env
  fi
fi

# ---- 3) 启动 ----
COMPOSE_ARGS=(-f docker-compose.yml)
if [ "$WITH_OBS" -eq 1 ]; then
  [ -f docker-compose.observability.yml ] || die "缺少 docker-compose.observability.yml；请改用 --without-observability"
  COMPOSE_ARGS+=(-f docker-compose.observability.yml)
  log "启动服务（postgres/redis → migrate → api + worker → web + 观测栈）..."
else
  log "启动服务（postgres/redis → migrate → api + worker → web；已跳过观测栈）..."
fi
if ! docker compose "${COMPOSE_ARGS[@]}" up -d; then
  warn "启动失败。常见原因：端口被占用（改 .env 的 HDB_HTTP_PORT / HDB_API_PORT / OBS_*_PORT），或旧数据卷与 .env 里的 PG 密码不一致。"
  exit 1
fi

# ---- 4) 就绪检查 ----
if command -v curl >/dev/null 2>&1; then
  log "等待服务就绪（最多 180 秒，未就绪返回 503 属正常）..."
  ready=0
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_PORT}/health/ready" || true)"
    if [ "$code" = "200" ]; then ready=1; break; fi
    sleep 3
  done
  if [ "$ready" -eq 1 ]; then
    log "服务已就绪"
  else
    warn "180 秒内未就绪。排查: docker compose ps / docker compose logs api"
  fi
else
  warn "未找到 curl，跳过就绪检查；请执行 docker compose ps 查看状态"
fi

# ---- 5) 输出结果 ----
ADMIN_USER="$(grep -E '^AUTH_DEFAULT_ADMIN_USERNAME=' config/.env 2>/dev/null | tail -1 | cut -d= -f2-)"; ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PW="$(grep -E '^AUTH_DEFAULT_ADMIN_PASSWORD=' config/.env 2>/dev/null | tail -1 | cut -d= -f2-)"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; HOST_IP="${HOST_IP:-127.0.0.1}"

cat <<EOF

============================================================
  Hardware DataBase 部署完成
  访问地址   : http://${HOST_IP}:${WEB_PORT}
  管理员账号 : ${ADMIN_USER} / ${ADMIN_PW:-<见 config/.env>}
  服务状态   : cd ${DOCKER_DIR} && docker compose ps
  后端日志   : docker compose logs -f api
============================================================
EOF

if [ "$WITH_OBS" -eq 1 ]; then
  GRAFANA_PW="$(grep -E '^GRAFANA_ADMIN_PASSWORD=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"
  cat <<EOF
观测入口（默认仅本机可访问；远程访问可设 OBS_BIND_HOST=0.0.0.0 或做端口转发）:
  Grafana    http://127.0.0.1:3000   admin / ${GRAFANA_PW:-<见 .env>}
  Prometheus http://127.0.0.1:9090
  Phoenix    http://127.0.0.1:6006
============================================================
EOF
fi

cat <<EOF
RAGFlow 与模型服务如未配置，请登录后在「系统配置」填写，
或编辑 ${DOCKER_DIR}/config/.env 后执行 docker compose "${COMPOSE_ARGS[@]}" up -d。
EOF
