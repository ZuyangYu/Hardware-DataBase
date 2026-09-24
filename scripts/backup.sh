#!/usr/bin/env bash
# Hardware DataBase 统一备份/恢复（存储收敛第 0 期配套工具）。
#
# 数据端是两个物理对象，备份就是两件事，本脚本合成一条命令：
#   1) PostgreSQL 控制面（deploy/postgres，pg_dump --format=custom）
#   2) ./storage（auth.db、pipeline_documents.db、chat_attachments.db、
#      document_authoring.db、projects.db、归档/导出字节等本地权威数据）
#
# 用法：
#   bash scripts/backup.sh create [--dir DIR] [--retention N] [--lean] [--skip-pg] [--dry-run]
#   bash scripts/backup.sh restore <archive.tar.gz|解包目录> [--yes]
#   bash scripts/backup.sh list [--dir DIR]
#
#   --lean     排除"可重建"数据（表格/电路/外部对话索引、LangGraph checkpoint、
#              资源配额、记忆投影，见 wiki/02-architecture.md §4.1 权威性列）；
#              恢复后首次解析会按需重建。默认全量。
#   --skip-pg  不备份/不恢复 PostgreSQL（纯 SQLite 部署用）。
#   --retention N  每类归档保留最近 N 份（默认 7，0=不清理）。
#
# restore 是破坏性操作：会先停应用、把现有 ./storage 挪到 storage.pre-restore-<ts>
# 再解包，并用 pg_restore --clean 覆盖 PG 库；除 --yes 外会交互确认。
set -u

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT" || exit 1

PG_COMPOSE="$APP_ROOT/deploy/postgres/docker-compose.yml"
PG_DEPLOY_ENV="$APP_ROOT/deploy/postgres/.env"
DEFAULT_BACKUP_DIR="$APP_ROOT/backups"

BACKUP_DIR="$DEFAULT_BACKUP_DIR"
RETENTION=7
LEAN=0
SKIP_PG=0
DRY_RUN=0
ASSUME_YES=0
SUBCOMMAND="create"
RESTORE_INPUT=""

# 永远不属于"要备份数据"的部分：日志、pid、便携 Node、嵌套备份、测试产物。
ALWAYS_EXCLUDE=(storage/logs storage/run storage/nodeenv storage/backups storage/_test_output)
# --lean 额外排除：权威性为"可重建"的数据（wiki/02-architecture.md §4.1）。
LEAN_EXCLUDE=(
  storage/table_indexes
  storage/circuits
  storage/external_conversations
  storage/agent_checkpoints.db
  storage/document_authoring_checkpoints.sqlite
  storage/resource_quotas.db
  storage/memory.db
)

log()  { printf '[backup] %s\n' "$*"; }
warn() { printf '[backup] 警告: %s\n' "$*" >&2; }
die()  { printf '[backup] 错误: %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ---- docker 探测（与 scripts/postgres.sh 相同：无权限回退 sudo）----
DOCKER_CMD=()
detect_docker() {
  if [ "${#DOCKER_CMD[@]}" -gt 0 ]; then return 0; fi
  if docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
  elif command -v sudo >/dev/null 2>&1; then
    log "当前用户无 docker 权限，改用 sudo（可能需要输入密码）"
    DOCKER_CMD=(sudo docker)
  else
    die "无法访问 docker daemon：请把用户加入 docker 组，或以 root 运行。"
  fi
}

read_env_value() { # file key
  [ -f "$1" ] || return 1
  grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 | cut -d= -f2-
}

pg_container() {
  detect_docker
  local cid
  cid="$("${DOCKER_CMD[@]}" compose -f "$PG_COMPOSE" ps -q postgres 2>/dev/null || true)"
  [ -n "$cid" ] || die "未找到 deploy/postgres 的 postgres 容器（先 bash scripts/postgres.sh up）。"
  printf '%s' "$cid"
}

pg_db_name() {
  local db
  db="$(read_env_value "$PG_DEPLOY_ENV" POSTGRES_DB || true)"
  printf '%s' "${db:-hdb}"
}

pg_user_name() {
  local user
  user="$(read_env_value "$PG_DEPLOY_ENV" POSTGRES_USER || true)"
  printf '%s' "${user:-hdb}"
}

stamp() { date +%Y%m%d-%H%M%S; }

prune_old() { # dir prefix keep
  local dir="$1" prefix="$2" keep="$3" count file
  [ "$keep" -gt 0 ] 2>/dev/null || return 0
  count="$(ls -1t "$dir"/"$prefix"* 2>/dev/null | wc -l)"
  [ "$count" -gt "$keep" ] || return 0
  ls -1t "$dir"/"$prefix"* 2>/dev/null | tail -n "$((count - keep))" | while read -r file; do
    rm -rf "$file"
    log "保留策略：删除旧备份 $(basename "$file")"
  done
}

create_backup() {
  local ts pg_db pg_dump_file archive
  ts="$(stamp)"
  mkdir -p "$BACKUP_DIR"
  log "目标目录: $BACKUP_DIR"

  # ---- 1) PostgreSQL 控制面 ----
  pg_dump_file=""
  if [ "$SKIP_PG" -eq 1 ]; then
    warn "--skip-pg：本次不备份 PostgreSQL"
  else
    local container
    container="$(pg_container)"
    pg_dump_file="$BACKUP_DIR/pg-$(pg_db_name)-${ts}.dump"
    if [ "$DRY_RUN" -eq 1 ]; then
      log "将执行: docker exec <postgres> pg_dump -U $(pg_user_name) -Fc $(pg_db_name) -> $(basename "$pg_dump_file")"
    else
      log "导出 PostgreSQL（pg_dump -Fc, 库 $(pg_db_name)）..."
      if ! "${DOCKER_CMD[@]}" exec "$container" pg_dump -U "$(pg_user_name)" -Fc "$(pg_db_name)" > "$pg_dump_file"; then
        rm -f "$pg_dump_file"
        die "pg_dump 失败（容器 $container）。"
      fi
      log "PG 完成: $(basename "$pg_dump_file") ($(du -h "$pg_dump_file" | cut -f1))"
    fi
  fi

  # ---- 2) ./storage ----
  archive="$BACKUP_DIR/storage-${ts}.tar.gz"
  local excludes=("${ALWAYS_EXCLUDE[@]}")
  if [ "$LEAN" -eq 1 ]; then excludes+=("${LEAN_EXCLUDE[@]}"); fi
  local tar_args=() item
  for item in "${excludes[@]}"; do
    tar_args+=(--exclude="$item")
  done

  if [ "$DRY_RUN" -eq 1 ]; then
    log "将执行: tar -czf $(basename "$archive") ${tar_args[*]} ./storage"
    log "（--dry-run）结束，未写入任何文件"
    return 0
  fi

  log "打包 storage（$([ "$LEAN" -eq 1 ] && echo lean || echo 全量)）..."
  # 运行中的服务会持续写日志/台账：tar 对"读的过程中文件变化"只给警告
  # （退出码 1，归档仍完整），只有 2+ 才是真正失败。
  local tar_rc=0
  ( cd "$APP_ROOT" && tar --warning=no-file-changed -czf "$archive" "${tar_args[@]}" ./storage ) || tar_rc=$?
  if [ "$tar_rc" -ge 2 ]; then
    rm -f "$archive"
    die "storage 打包失败（tar 退出码 $tar_rc）。"
  fi
  [ "$tar_rc" -eq 1 ] && warn "storage 打包期间有文件变化（运行中服务），已按警告处理。"
  log "storage 完成: $(basename "$archive") ($(du -h "$archive" | cut -f1))"

  ( cd "$BACKUP_DIR" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256" )
  if [ -n "$pg_dump_file" ] && [ -s "$pg_dump_file" ]; then
    ( cd "$BACKUP_DIR" && sha256sum "$(basename "$pg_dump_file")" > "$(basename "$pg_dump_file").sha256" )
  fi
  prune_old "$BACKUP_DIR" "storage-" "$RETENTION"
  [ "$SKIP_PG" -eq 1 ] || prune_old "$BACKUP_DIR" "pg-" "$RETENTION"

  log "备份完成：$BACKUP_DIR"
  ls -lh "$BACKUP_DIR" 2>/dev/null | tail -n +2 | head -10
}

restore_backup() {
  local input="$1"
  [ -n "$input" ] || die "restore 需要一个备份归档路径（或解包目录）。"
  local dir
  if [ -d "$input" ]; then
    dir="$input"
  else
    [ -f "$input" ] || die "找不到备份文件: $input"
    dir="$(mktemp -d)"
    tar -xzf "$input" -C "$dir"
  fi

  local pg_dump_file tar_file
  pg_dump_file="$(ls -1t "$dir"/pg-*.dump 2>/dev/null | head -n 1 || true)"
  tar_file="$(ls -1t "$dir"/storage-*.tar.gz 2>/dev/null | head -n 1 || true)"
  [ -n "$tar_file" ] || die "归档里没有 storage-*.tar.gz。"

  if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "恢复将覆盖 PostgreSQL 的 $(pg_db_name) 库并替换 ./storage（会先停应用）。输入 yes 继续: " answer || true
    [ "$answer" = "yes" ] || die "已取消。"
  fi

  log "停止应用服务（数据服务保留）..."
  bash "$APP_ROOT/scripts/hdb.sh" stop >/dev/null 2>&1 || true
  if [ -f "$APP_ROOT/deploy/docker/docker-compose.yml" ]; then
    if docker compose -f "$APP_ROOT/deploy/docker/docker-compose.yml" ps >/dev/null 2>&1; then
      docker compose -f "$APP_ROOT/deploy/docker/docker-compose.yml" stop api >/dev/null 2>&1 || true
    fi
  fi

  # ---- 1) ./storage：先把当前数据挪走（可回滚），再解包 ----
  local pre_restore="$APP_ROOT/storage.pre-restore-$(stamp)"
  if [ -d "$APP_ROOT/storage" ]; then
    log "现有 storage 挪到 $(basename "$pre_restore")（确认无误后可删除）"
    mv "$APP_ROOT/storage" "$pre_restore"
  fi
  if ! tar -xzf "$tar_file" -C "$APP_ROOT"; then
    [ -d "$pre_restore" ] && mv "$pre_restore" "$APP_ROOT/storage"
    die "storage 解包失败（已还原原目录）。"
  fi
  mkdir -p "$APP_ROOT/storage/logs" "$APP_ROOT/storage/run"

  # ---- 2) PostgreSQL ----
  if [ "$SKIP_PG" -eq 1 ]; then
    warn "--skip-pg 的备份不含 PostgreSQL，跳过数据库恢复。"
  elif [ -n "$pg_dump_file" ]; then
    local container pg_db
    container="$(pg_container)"
    pg_db="$(pg_db_name)"
    log "恢复 PostgreSQL（pg_restore --clean, 库 $pg_db）..."
    "${DOCKER_CMD[@]}" cp "$pg_dump_file" "$container:/tmp/hdb-restore.dump" \
      || die "拷贝 dump 到容器失败。"
    if ! "${DOCKER_CMD[@]}" exec "$container" pg_restore \
        --clean --if-exists --no-owner --no-privileges \
        -U "$(pg_user_name)" -d "$pg_db" /tmp/hdb-restore.dump; then
      "${DOCKER_CMD[@]}" exec "$container" rm -f /tmp/hdb-restore.dump || true
      die "pg_restore 失败（确认没有活跃连接：应用已停？）。"
    fi
    "${DOCKER_CMD[@]}" exec "$container" rm -f /tmp/hdb-restore.dump || true
  else
    warn "归档里没有 pg-*.dump，跳过 PostgreSQL 恢复。"
  fi

  log "恢复完成。启动：bash scripts/hdb.sh start（或 docker compose up -d）"
  log "验收: curl http://127.0.0.1:${HDB_API_PORT:-8001}/health/ready"
}

list_backups() {
  log "备份目录: $BACKUP_DIR"
  ls -lh "$BACKUP_DIR" 2>/dev/null | tail -n +2 || echo "（空）"
}

while [ $# -gt 0 ]; do
  case "$1" in
    create|list) SUBCOMMAND="$1"; shift ;;
    restore)
      SUBCOMMAND="restore"
      shift
      [ $# -gt 0 ] || die "restore 需要一个备份路径。"
      RESTORE_INPUT="$1"
      shift
      ;;
    --dir) BACKUP_DIR="${2:?--dir 需要目录}"; shift 2 ;;
    --retention) RETENTION="${2:-7}"; shift 2 ;;
    --lean) LEAN=1; shift ;;
    --skip-pg) SKIP_PG=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

[ -f "$PG_COMPOSE" ] || die "缺少 $PG_COMPOSE（本脚本绑定项目自管 PostgreSQL）。"
[ -d "$APP_ROOT/storage" ] || die "缺少 ./storage 目录（请在仓库根目录执行）。"

case "$SUBCOMMAND" in
  list) list_backups ;;
  restore) restore_backup "${RESTORE_INPUT:-}" ;;
  *) create_backup ;;
esac
