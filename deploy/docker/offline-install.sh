#!/usr/bin/env bash
# Hardware DataBase 内网离线一键部署 / 一键升级。
#
# 首次部署（在离线包根目录或 docker/ 目录内执行）：
#   bash offline-install.sh                        # 导入镜像 → 生成配置 → 启动（含观测栈）→ 就绪检查
#   bash offline-install.sh --without-observability # 不启动观测栈（资源受限时）
#   bash offline-install.sh --port 9090            # 指定 Web 端口（默认 8080）
#
# 升级（解压新离线包后，在新包内执行，指向旧部署目录）：
#   bash offline-install.sh --upgrade --target <旧部署目录>
#   bash offline-install.sh --upgrade              # 若新包已覆盖到旧目录，可直接就地升级
#
# 升级流程自动完成：备份数据库(pg_dump) → 更新 compose/镜像文件 → 导入新镜像 → 重建容器 → 就绪检查。
# config/ 与 storage/ 不会被覆盖；数据卷不受影响；失败时不会修改目标目录文件。
#
# 幂等：重复执行安全。说明：本脚本只服务于“离线包”分发形态；在线部署请使用 quickstart.sh。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 识别目录布局：包根目录（含 docker/）或 docker/ 目录本身（含 docker-compose.yml）
read_layout() { # $1=dir；结果写入 L_BUNDLE / L_DOCKER，未识别则为空
  L_BUNDLE=""; L_DOCKER=""
  if [ -f "$1/docker/docker-compose.yml" ]; then
    L_BUNDLE="$(cd "$1" && pwd)"; L_DOCKER="$L_BUNDLE/docker"
  elif [ -f "$1/docker-compose.yml" ]; then
    L_DOCKER="$(cd "$1" && pwd)"; L_BUNDLE="$(cd "$1/.." && pwd)"
  fi
}

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] 警告: %s\n' "$*" >&2; }
die()  { printf '[install] 错误: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Hardware DataBase 内网离线一键部署 / 一键升级。

首次部署（在离线包根目录或 docker/ 目录内执行）：
  bash $(basename "$0")                            # 导入镜像 → 生成配置 → 启动（含观测栈）→ 就绪检查
  bash $(basename "$0") --without-observability    # 不启动观测栈（资源受限时）
  bash $(basename "$0") --port 9090                # 指定 Web 端口（默认 8080）

升级（解压新离线包后，在新包内执行，指向旧部署目录）：
  bash $(basename "$0") --upgrade --target <旧部署目录>
  bash $(basename "$0") --upgrade                  # 新包已覆盖旧目录时就地升级

说明：升级会自动备份数据库并保留 config/ 与 storage/；数据卷不受影响。
EOF
  exit 0
}

# ---- 参数 ----
WEB_PORT=""
WITH_OBS=1
UPGRADE=0
TARGET_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) WEB_PORT="${2:?--port 需要一个端口号}"; shift 2 ;;
    --without-observability) WITH_OBS=0; shift ;;
    --upgrade) UPGRADE=1; shift ;;
    --target) TARGET_ARG="${2:?--target 需要旧部署目录}"; shift 2 ;;
    -h|--help) usage ;;
    *) die "未知参数: $1（支持 --port N / --without-observability / --upgrade [--target DIR] / --help）" ;;
  esac
done

# ---- 定位源包与目标目录 ----
read_layout "$SCRIPT_DIR"
[ -n "$L_BUNDLE" ] || die "未找到 docker-compose.yml：请在离线包根目录或 docker/ 目录内运行。"
SOURCE_BUNDLE="$L_BUNDLE"; SOURCE_DOCKER="$L_DOCKER"

if [ -n "$TARGET_ARG" ]; then
  [ "$UPGRADE" -eq 1 ] || die "--target 只能与 --upgrade 一起使用。"
  [ -d "$TARGET_ARG" ] || die "目标目录不存在: $TARGET_ARG"
  read_layout "$TARGET_ARG"
  [ -n "$L_BUNDLE" ] || die "目标目录不是有效的离线部署（缺少 docker/docker-compose.yml）: $TARGET_ARG"
  TARGET_BUNDLE="$L_BUNDLE"; TARGET_DOCKER="$L_DOCKER"
else
  TARGET_BUNDLE="$SOURCE_BUNDLE"; TARGET_DOCKER="$SOURCE_DOCKER"
fi

BUNDLE_DIR="$TARGET_BUNDLE"
DOCKER_DIR="$TARGET_DOCKER"
IMAGES_TAR="$BUNDLE_DIR/hdb-images.tar.gz"

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

COMPOSE_ARGS=(-f docker-compose.yml)
if [ "$WITH_OBS" -eq 1 ]; then
  [ -f "$DOCKER_DIR/docker-compose.observability.yml" ] || die "缺少 docker-compose.observability.yml；请改用 --without-observability"
  COMPOSE_ARGS+=(-f docker-compose.observability.yml)
fi

cd "$DOCKER_DIR"

# ---- 0) 升级：备份数据库并更新目标目录文件 ----
BACKUP_FILE=""
if [ "$UPGRADE" -eq 1 ]; then
  OLD_TAG="$(grep -m1 '^tag=' "$BUNDLE_DIR/VERSION" 2>/dev/null | cut -d= -f2- || true)"
  NEW_TAG="$(grep -m1 '^tag=' "$SOURCE_BUNDLE/VERSION" 2>/dev/null | cut -d= -f2- || true)"
  log "升级模式：${OLD_TAG:-未知版本} -> ${NEW_TAG:-未知版本}"

  if docker compose "${COMPOSE_ARGS[@]}" ps -q postgres 2>/dev/null | grep -q .; then
    mkdir -p "$BUNDLE_DIR/backups"
    BACKUP_FILE="$BUNDLE_DIR/backups/pg-$(date +%Y%m%d-%H%M%S).dump"
    log "备份数据库 -> ${BACKUP_FILE}"
    if docker compose "${COMPOSE_ARGS[@]}" exec -T postgres pg_dump -U hdb -Fc hdb > "$BACKUP_FILE"; then
      log "备份完成（$(du -h "$BACKUP_FILE" | cut -f1)）"
    else
      rm -f "$BACKUP_FILE"; BACKUP_FILE=""
      die "数据库备份失败，已中止升级（未做任何修改）。"
    fi
  else
    warn "旧服务未在运行，跳过数据库备份"
  fi

  if [ "$SOURCE_BUNDLE" != "$BUNDLE_DIR" ]; then
    [ -f "$SOURCE_BUNDLE/hdb-images.tar.gz" ] || die "新包缺少 hdb-images.tar.gz: $SOURCE_BUNDLE"
    log "更新文件：$SOURCE_BUNDLE -> $BUNDLE_DIR（保留 config/ 与 storage/）"
    cp -f "$SOURCE_BUNDLE/hdb-images.tar.gz" "$BUNDLE_DIR/hdb-images.tar.gz"
    [ -f "$SOURCE_BUNDLE/hdb-images.tar.gz.sha256" ] && cp -f "$SOURCE_BUNDLE/hdb-images.tar.gz.sha256" "$BUNDLE_DIR/hdb-images.tar.gz.sha256"
    [ -f "$SOURCE_BUNDLE/VERSION" ] && cp -f "$SOURCE_BUNDLE/VERSION" "$BUNDLE_DIR/VERSION"
    [ -f "$SOURCE_BUNDLE/INSTALL.md" ] && cp -f "$SOURCE_BUNDLE/INSTALL.md" "$BUNDLE_DIR/INSTALL.md"
    if [ -n "$SOURCE_DOCKER" ] && [ "$SOURCE_DOCKER" != "$DOCKER_DIR" ]; then
      (cd "$SOURCE_DOCKER" && tar -cf - --exclude=config --exclude=storage --exclude=offline-install.sh .) | (cd "$DOCKER_DIR" && tar -xf -)
    fi
    if [ -d "$SOURCE_BUNDLE/observability" ] && [ "$SOURCE_BUNDLE" != "$BUNDLE_DIR" ]; then
      rm -rf "$BUNDLE_DIR/observability.new" && cp -a "$SOURCE_BUNDLE/observability" "$BUNDLE_DIR/observability.new" \
        && rm -rf "$BUNDLE_DIR/observability" && mv "$BUNDLE_DIR/observability.new" "$BUNDLE_DIR/observability"
    fi
    for dst in "$BUNDLE_DIR/offline-install.sh" "$DOCKER_DIR/offline-install.sh"; do
      if [ -f "$SOURCE_BUNDLE/offline-install.sh" ]; then
        cp -f "$SOURCE_BUNDLE/offline-install.sh" "${dst}.new" && mv -f "${dst}.new" "$dst" && chmod +x "$dst"
      fi
    done
    log "文件更新完成"
  else
    log "就地升级（使用当前目录的镜像包）"
  fi
fi

# ---- 0.5) 预检：观测配置必须是真实文件/目录 ----
# 缺失的 bind mount 源会被 Docker 自动建成空目录，容器随之反复重启且安装仍显示成功。
if [ "$WITH_OBS" -eq 1 ]; then
  OBS_MISSING=()
  for f in \
    ../observability/otel-collector/config.yaml \
    ../observability/prometheus/prometheus.yml \
    ../observability/tempo/tempo.yaml \
    ../observability/loki/loki.yaml; do
    [ -f "$f" ] || OBS_MISSING+=("$f（应为文件）")
  done
  [ -n "${ALERTMANAGER_CONFIG:-}" ] || [ -f ../observability/alertmanager/alertmanager.yml ] \
    || OBS_MISSING+=("../observability/alertmanager/alertmanager.yml（应为文件）")
  for d in ../observability/prometheus/rules ../observability/grafana/provisioning ../observability/grafana/dashboards; do
    [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ] || OBS_MISSING+=("$d（应为非空目录）")
  done
  if [ "${#OBS_MISSING[@]}" -gt 0 ]; then
    printf '[install] 错误: 观测配置缺失或为空占位目录：\n' >&2
    printf '  - %s\n' "${OBS_MISSING[@]}" >&2
    die "离线包必须包含 observability/ 目录（见 deploy/docker/README.md §2 打包步骤）；或改用 --without-observability"
  fi
  log "观测配置预检通过"
fi

# ---- 1) 校验并导入镜像 ----
[ -f "$IMAGES_TAR" ] || die "缺少镜像包: $IMAGES_TAR"
if [ -f "$IMAGES_TAR.sha256" ]; then
  log "校验镜像包..."
  (cd "$BUNDLE_DIR" && sha256sum -c "$(basename "$IMAGES_TAR").sha256") || die "镜像包校验失败，请重新拷贝。"
fi
log "导入镜像（首次约 2~5 分钟，已存在时仅刷新标签）..."
docker load -i "$IMAGES_TAR" >/dev/null || die "镜像导入失败。"

# ---- 2) 生成配置（保留已有） ----
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
WEB_PORT="$( { grep -E '^HDB_HTTP_PORT=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; WEB_PORT="${WEB_PORT:-8080}"
API_PORT="$( { grep -E '^HDB_API_PORT=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; API_PORT="${API_PORT:-8001}"
MCP_PORT="$( { grep -E '^HDB_MCP_HOST_PORT=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; MCP_PORT="${MCP_PORT:-9100}"
MCP_BIND="$( { grep -E '^HDB_MCP_BIND=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; MCP_BIND="${MCP_BIND:-0.0.0.0}"

# 旧模板可能在 config/.env 里留下 RESOURCE_GOVERNOR_SCOPE=process；它与本编排
# （后端 postgres）冲突——保存「系统配置」时文件会覆盖环境变量并校验失败。就地纠正。
if grep -qE '^RESOURCE_GOVERNOR_SCOPE=process$' config/.env 2>/dev/null; then
  sed -i 's/^RESOURCE_GOVERNOR_SCOPE=process$/RESOURCE_GOVERNOR_SCOPE=deployment/' config/.env
  log "已纠正 config/.env: RESOURCE_GOVERNOR_SCOPE=deployment（容器部署要求）"
fi

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
if [ "$WITH_OBS" -eq 1 ]; then
  log "启动服务（postgres/redis → migrate → api + mcp + worker → web + 观测栈）..."
else
  log "启动服务（postgres/redis → migrate → api + mcp + worker → web；已跳过观测栈）..."
fi
if ! docker compose "${COMPOSE_ARGS[@]}" up -d; then
  warn "启动失败。常见原因：端口被占用（改 .env 的 HDB_HTTP_PORT / HDB_API_PORT / HDB_MCP_HOST_PORT / OBS_*_PORT），或旧数据卷与 .env 里的 PG 密码不一致。"
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
ADMIN_USER="$( { grep -E '^AUTH_DEFAULT_ADMIN_USERNAME=' config/.env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PW="$( { grep -E '^AUTH_DEFAULT_ADMIN_PASSWORD=' config/.env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"; HOST_IP="${HOST_IP:-127.0.0.1}"

if [ "$UPGRADE" -eq 1 ]; then
  ACTION="升级完成"
else
  ACTION="部署完成"
fi

cat <<EOF

============================================================
  Hardware DataBase ${ACTION}
  访问地址   : http://${HOST_IP}:${WEB_PORT}
  管理员账号 : ${ADMIN_USER} / ${ADMIN_PW:-<见 config/.env>}
  MCP 服务   : http://${MCP_BIND/0.0.0.0/${HOST_IP}}:${MCP_PORT}/mcp（需在 config/.env 设置 HDB_MCP_HTTP_HOST，并使用个人 hdbmcp_* 令牌）
  部署目录   : ${BUNDLE_DIR}
  服务状态   : cd ${DOCKER_DIR} && docker compose ps
  后端日志   : docker compose logs -f api / mcp
============================================================
EOF

if [ -n "$BACKUP_FILE" ]; then
  cat <<EOF
升级前备份 : ${BACKUP_FILE}
回滚方法   : 恢复该 dump + 用旧离线包执行同一条升级命令
============================================================
EOF
fi

if [ "$WITH_OBS" -eq 1 ]; then
  GRAFANA_PW="$( { grep -E '^GRAFANA_ADMIN_PASSWORD=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"
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
