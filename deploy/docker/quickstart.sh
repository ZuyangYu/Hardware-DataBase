#!/usr/bin/env bash
# Hardware DataBase —— Docker 快速部署（开源用户首选路径）。
#
#   bash deploy/docker/quickstart.sh              # 生成密钥 + 配置 + 构建 + 启动
#   bash deploy/docker/quickstart.sh --no-start   # 只准备文件和密钥，不构建/启动
#
# 前置：Docker Engine + compose 插件；外部服务 RAGFlow 与 LLM（Ollama 或
# OpenAI 兼容 API）由使用者自备——先起服务、登录后在「系统配置」里填也可以。
#
# 它做三件事（全部幂等，可重复执行）：
#   1. 生成 PostgreSQL 密码（写入 deploy/docker/.env）
#   2. 生成应用配置 config/.env；管理员口令为空时自动生成并打印
#   3. 构建镜像并启动（postgres -> migrate -> api -> web）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

log() { printf '[quickstart] %s\n' "$*"; }
die() { printf '[quickstart] 错误: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--no-start" ]; then
  START=0
else
  START=1
fi

command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker Engine + compose 插件。"
docker info >/dev/null 2>&1 || die "无法访问 docker daemon（把用户加入 docker 组，或用 root 运行）。"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件（docker-compose-plugin）。"

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

# ---- 2) 应用配置 ----
mkdir -p storage config
if [ ! -f config/.env ]; then
  cp hdb.env.example config/.env
  log "已从模板生成 config/.env"
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
chmod 600 config/.env
log "RAGFlow / 模型端点可稍后在「系统配置」页面填写（config/.env 里也可直接改）"

if [ "$START" -eq 0 ]; then
  log "已准备就绪。启动：docker compose up -d（首次建议带构建文件，见 README）"
  exit 0
fi

# ---- 3) 构建 + 启动 ----
log "构建镜像（首次 5~15 分钟，可用 APT_MIRROR/PIP_INDEX_URL/NPM_REGISTRY 加速）..."
docker compose -f docker-compose.yml -f docker-compose.build.yml build
log "启动（postgres + redis -> migrate -> api -> web）..."
docker compose -f docker-compose.yml up -d

host_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
log "完成。访问 http://${host_ip:-<本机IP>}:8080 ，用 admin 登录"
log "首次登录后到「系统配置」填 RAGFlow 与模型端点；日志：docker compose logs -f api"
