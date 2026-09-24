#!/usr/bin/env bash
# Hardware DataBase 应用容器入口。
#
# 职责只有两件事：把配置缺失变成可执行的报错、确保数据目录存在，然后交给 CMD。
# API 与各 worker 角色共用本脚本（worker 由镜像内置的 console script 启动）。
set -euo pipefail

ENV_PATH="${HDB_ENV_PATH:-/app/config/.env}"

if [ ! -f "$ENV_PATH" ]; then
  cat >&2 <<EOF
[hdb] 找不到配置文件：$ENV_PATH

      Docker 部署的配置来自宿主机挂载目录，请先在宿主机执行：
        cp hdb.env.example config/.env
        vi config/.env        # 至少填写 AUTH_DEFAULT_ADMIN_PASSWORD 与 RAGFLOW_*
      然后重启： docker compose up -d --force-recreate
EOF
  exit 78 # EX_CONFIG
fi

# 首次启动时卷挂载目录可能还不存在（compose 会自动建，裸 docker run 不会）。
mkdir -p /app/storage 2>/dev/null || true

# 管理员口令为空或仍是默认值时，AuthService 会在 lifespan 里抛错。这里提前给出
# 人话提示，避免只看到一坨 Python traceback。
if ! grep -qE '^AUTH_DEFAULT_ADMIN_PASSWORD=.+' "$ENV_PATH"; then
  echo "[hdb] 提示：$ENV_PATH 中 AUTH_DEFAULT_ADMIN_PASSWORD 为空，服务将以默认管理员口令失败启动。" >&2
  echo "[hdb]       请设置一个非空且不为 admin123 的强口令。" >&2
fi

exec "$@"
