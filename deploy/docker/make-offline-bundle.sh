#!/usr/bin/env bash
# Hardware DataBase —— 内网离线部署包打包脚本。
#
#   bash deploy/docker/make-offline-bundle.sh <输出目录> [--no-build|--skip-images|--tag <版本>]
#
#   <输出目录> 必填，必须在仓库之外（如 ~/hdb-offline）。离线包是构建产物，
#   放进仓库会让 git status 变脏、还得每次更新代码都顺带刷新它。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DOCKER_SRC="$REPO_ROOT/deploy/docker"
OBS_SRC="$REPO_ROOT/deploy/observability"

OUT_DIR=""
DO_BUILD=1
WITH_IMAGES=1
TAG=""

log() { printf '[bundle] %s\n' "$*"; }
die() { printf '[bundle] 错误: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Hardware DataBase —— 内网离线部署包打包脚本。

用法：
  bash deploy/docker/make-offline-bundle.sh <输出目录> [选项]

  <输出目录>   必填，且必须在仓库之外（例如 ~/hdb-offline 或 /data/hdb-offline）。
               离线包是构建产物，放在仓库里会污染 git status、占用仓库空间，
               并且每次更新代码都得同步刷新它。

选项：
  --no-build      不重新构建 app/web 镜像（镜像已在本地时使用）
  --skip-images   只组装源码/配置包，不保存镜像（快速验证包结构）
  --tag <版本>    写入 VERSION 的版本号（默认取 git describe）
  -h, --help      帮助

产物结构（与 offline-install.sh 的识别逻辑一致）：
  <输出目录>/
    offline-install.sh          一键安装 / 升级
    INSTALL.md                  安装说明
    VERSION                     tag=<版本>
    hdb-images.tar.gz           全部镜像（docker save | gzip）
    hdb-images.tar.gz.sha256    校验和
    docker/                     deploy/docker（不含 config/、storage/、.env）
    observability/              deploy/observability（观测栈配置）

设计约束：
  - **config/.env 与 storage/ 绝不进包**：前者是密钥，后者是本地业务数据；
  - **compose 引用的每个配置文件都必须在包内**：缺一个就会让容器拿到空目录
    占位并反复重启（离线部署最常见的故障），脚本对此做校验；
  - 镜像清单由 compose 自身解析（`config --images`），不会与实际编排漂移。
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) DO_BUILD=0; shift ;;
    --skip-images) WITH_IMAGES=0; shift ;;
    --tag) TAG="${2:?--tag 需要一个版本号}"; shift 2 ;;
    -h|--help) usage ;;
    -*) die "未知参数: $1" ;;
    *) [ -z "$OUT_DIR" ] || die "只能指定一个输出目录"; OUT_DIR="$1"; shift ;;
  esac
done

OUT_DIR="${OUT_DIR:-}"
[ -n "$OUT_DIR" ] || { usage; exit 2; }

# 拒绝把构建产物写进仓库：既污染 git status，又让"更新代码"变成"还要顺手更新包"。
# 先规范化路径再判断，避免"先建目录后拒绝"留下空目录；realpath -m 允许路径尚不存在。
if command -v realpath >/dev/null 2>&1; then
  OUT_ABS="$(realpath -m "$OUT_DIR")"
else
  OUT_ABS="$(cd "$(dirname "$OUT_DIR")" 2>/dev/null && pwd)/$(basename "$OUT_DIR")"
fi
case "$OUT_ABS/" in
  "$REPO_ROOT"/*|"$REPO_ROOT")
    die "输出目录在仓库内（$OUT_ABS）。离线包是构建产物，请放到仓库外，例如 ~/hdb-offline 或 /data/hdb-offline"
    ;;
esac
mkdir -p "$OUT_ABS" || die "无法创建输出目录: $OUT_ABS"

# ---------------------------------------------------------------- 前置检查
if [ "$WITH_IMAGES" -eq 1 ]; then
  if [ "$(id -u)" -ne 0 ] && ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1; then
      log "当前用户无 Docker 权限，改用 sudo 重新执行..."
      exec sudo -E bash "$0" "$@"
    fi
  fi
  docker info >/dev/null 2>&1 || die "无法访问 docker daemon"
  docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"
fi
[ -d "$DOCKER_SRC" ] || die "缺少 $DOCKER_SRC"
[ -d "$OBS_SRC" ] || die "缺少 $OBS_SRC（观测栈配置，必须一起打包）"

# compose 的必填变量（GRAFANA_ADMIN_PASSWORD 等）只用于解析镜像清单/构建，
# 不会写入包内：真正的口令由 quickstart / offline-install 在目标机生成。
TMP_ENV="$(mktemp /tmp/hdb-bundle-env.XXXXXX)"
trap 'rm -f "$TMP_ENV"' EXIT
cat > "$TMP_ENV" <<'EOF'
HDB_PG_PASSWORD=bundle-placeholder
GRAFANA_ADMIN_PASSWORD=bundle-placeholder
PHOENIX_DB_PASSWORD=bundle-placeholder
EOF

COMPOSE_FILES=(-f "$DOCKER_SRC/docker-compose.yml" -f "$DOCKER_SRC/docker-compose.observability.yml")

# ---------------------------------------------------------------- 1. 构建
if [ "$WITH_IMAGES" -eq 1 ] && [ "$DO_BUILD" -eq 1 ]; then
  log "构建 app / web 镜像（首次 5~15 分钟）"
  docker compose --env-file "$TMP_ENV" \
    -f "$DOCKER_SRC/docker-compose.yml" \
    -f "$DOCKER_SRC/docker-compose.build.yml" build api web
fi

# ---------------------------------------------------------------- 2. 镜像清单
IMAGES=()
if [ "$WITH_IMAGES" -eq 1 ]; then
  mapfile -t IMAGES < <(docker compose --env-file "$TMP_ENV" "${COMPOSE_FILES[@]}" config --images | sort -u)
  [ "${#IMAGES[@]}" -gt 0 ] || die "未能从 compose 解析出镜像清单"
  missing=()
  for image in "${IMAGES[@]}"; do
    docker image inspect "$image" >/dev/null 2>&1 || missing+=("$image")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    printf '[bundle] 错误: 以下镜像在本地不存在：\n' >&2
    printf '  - %s\n' "${missing[@]}" >&2
    die "请先构建（去掉 --no-build）或 docker pull 这些镜像"
  fi
  log "镜像清单（${#IMAGES[@]} 个）由 compose 解析："
  printf '  %s\n' "${IMAGES[@]}"
fi

# ---------------------------------------------------------------- 3. 组装源码/配置
log "组装包到 $OUT_DIR"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/docker"
# 显式排除：config/（密钥）、storage/（本地数据）、.env、bundle/、__pycache__、离线包自身。
# offline-install.sh 在包根目录，不在 docker/ 内。
(cd "$REPO_ROOT/deploy" && tar -cf - \
  --exclude=config --exclude=storage --exclude=bundle --exclude=build \
  --exclude=.env --exclude='*.tar.gz' --exclude='*.tar' --exclude=__pycache__ \
  --exclude=offline-install.sh --exclude=make-offline-bundle.sh \
  docker observability) | (cd "$OUT_DIR" && tar -xf -)

# ---------------------------------------------------------------- 4. 校验包内容
log "校验包内容"
# 4.1 密钥与本地数据不得进包
for bad in config storage .env; do
  [ ! -e "$OUT_DIR/docker/$bad" ] || die "包内含 $bad/（密钥或本地数据）"
done
[ -z "$(find "$OUT_DIR" -name '.env' -not -name hdb.env.example -print -quit 2>/dev/null)" ] \
  || die "包内存在 .env 文件（密钥）"
# 4.2 compose 引用的每个挂载源都必须在包内（防"空目录占位"故障复现）
mount_source() {  # 从 compose 文件提取相对挂载源（含 ${VAR:-../path} 形式的默认值）
  grep -hoE '^[[:space:]]*- \.\.?/[^:]+' "$DOCKER_SRC"/*.yml | sed -E 's/^[[:space:]]*- //'
  grep -hoE '\$\{[A-Z_]+:-\.\.?/[^:}]+' "$DOCKER_SRC"/*.yml | sed -E 's/^.*:-//'
}
while IFS= read -r source; do
  [ -n "$source" ] || continue
  case "$source" in
    ./config|./storage) continue ;;   # 运行期数据，目标机生成
    ../*) pkg_path="$OUT_DIR/${source#../}" ;;
    ./*)  pkg_path="$OUT_DIR/docker/${source#./}" ;;
    *) continue ;;
  esac
  [ -e "$pkg_path" ] || die "compose 引用的 $source 不在包内（离线机会拿到空目录占位）"
  if [ -d "$pkg_path" ]; then
    [ -n "$(ls -A "$pkg_path" 2>/dev/null)" ] || die "compose 引用的 $source 是空目录（离线机会拿到空目录占位）"
  fi
done < <(mount_source)
# 4.3 运行必需文件
for required in docker/docker-compose.yml docker/docker-compose.observability.yml \
                docker/docker-compose.build.yml docker/hdb.env.example docker/initdb \
                docker/nginx.conf docker/entrypoint.sh docker/quickstart.sh; do
  [ -e "$OUT_DIR/$required" ] || die "包内缺少 $required"
done
[ -f "$DOCKER_SRC/offline-install.sh" ] || die "缺少 offline-install.sh"
cp -f "$DOCKER_SRC/offline-install.sh" "$OUT_DIR/offline-install.sh"
chmod +x "$OUT_DIR/offline-install.sh"

# ---------------------------------------------------------------- 5. 保存镜像
if [ "$WITH_IMAGES" -eq 1 ]; then
  log "保存 ${#IMAGES[@]} 个镜像（约数分钟，输出约 GB 级）"
  docker save "${IMAGES[@]}" | gzip -1 > "$OUT_DIR/hdb-images.tar.gz"
  (cd "$OUT_DIR" && sha256sum "$(basename "$OUT_DIR/hdb-images.tar.gz")" > hdb-images.tar.gz.sha256)
  log "镜像包: $(du -h "$OUT_DIR/hdb-images.tar.gz" | cut -f1)"
fi

# ---------------------------------------------------------------- 6. 版本与说明
[ -n "$TAG" ] || TAG="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)"
printf 'tag=%s\nbuilt=%s\n' "$TAG" "$(date +%Y-%m-%dT%H:%M:%S)" > "$OUT_DIR/VERSION"

cat > "$OUT_DIR/INSTALL.md" <<EOF
# Hardware DataBase 离线部署（版本 $TAG）

## 安装

把本目录整体拷到目标机，然后执行：

\`\`\`bash
bash offline-install.sh                 # 导入镜像 → 生成配置 → 启动（含观测栈）→ 就绪检查
bash offline-install.sh --port 9090     # 指定 Web 端口（默认 8080）
bash offline-install.sh --without-observability   # 资源受限时跳过观测栈
\`\`\`

脚本会生成本机随机密码并打印在终端，也会写入 \`docker/config/.env\`。

## 安装后必做

1. 访问 \`http://<目标机IP>:8080\`，用 \`admin\` + 终端打印的密码登录；
2. 进入「系统配置」填写 **RAGFlow**（地址 / API Key / 数据集）与**模型端点**
   （内网地址；指向本机服务用 \`host.docker.internal\`）；
3. 重启 api 使配置生效：\`cd docker && docker compose up -d\`。

## 升级

\`\`\`bash
bash offline-install.sh --upgrade --target <旧部署目录>
\`\`\`

升级前自动 pg_dump 备份；\`config/\` 与 \`storage/\` 不被覆盖。

## 排障

\`\`\`bash
cd docker
docker compose ps
docker compose logs -f api
cat docker-compose.yml   # 配置项见 hdb.env.example
\`\`\`
EOF

# ---------------------------------------------------------------- 汇总
# 以 sudo 运行时把产物还给调用者，避免后续拷贝/解压遇到权限问题。
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_UID:-}" ]; then
  chown -R "${SUDO_UID}:${SUDO_GID:-$SUDO_UID}" "$OUT_DIR" 2>/dev/null || true
fi
log "完成：$OUT_DIR"
log "包大小合计 $(du -sh "$OUT_DIR" | cut -f1)"
find "$OUT_DIR" -maxdepth 2 -mindepth 1 | sort | sed 's/^/  /'
cat <<EOF

[bundle] 下一步（内网目标机）：
  tar -czf hdb-offline.tar.gz -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"
  # 拷贝到目标机后：
  tar -xzf hdb-offline.tar.gz && cd "$(basename "$OUT_DIR")"
  bash offline-install.sh --port <端口>
EOF
