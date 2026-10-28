#!/usr/bin/env bash
# Hardware DataBase 统一备份/恢复（存储收敛第 0 期配套工具）。
#
# 数据端是两个物理对象，备份就是两件事，本脚本合成一条命令：
#   1) PostgreSQL 控制面（Docker 业务栈或 deploy/postgres，pg_dump --format=custom）
#   2) storage（auth.db、pipeline_documents.db、chat_attachments.db、
#      归档/导出字节等本地权威数据）
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
#   --deployment docker|host  显式选择部署；两种 PG 同时运行时必须指定。
#   create 会短暂停写并在退出时恢复原服务，保证 PG 与文件数据一致。
#
# restore 是破坏性操作：会先停应用、把现有 storage 挪到 storage.pre-restore-<ts>
# 再解包，并用 pg_restore --clean 覆盖 PG 库；除 --yes 外会交互确认。
set -euo pipefail
umask 077

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT" || exit 1

LEGACY_PG_COMPOSE="$APP_ROOT/deploy/postgres/docker-compose.yml"
LEGACY_PG_DEPLOY_ENV="$APP_ROOT/deploy/postgres/.env"
DOCKER_PG_COMPOSE="$APP_ROOT/deploy/docker/docker-compose.yml"
DOCKER_DEPLOY_ENV="$APP_ROOT/deploy/docker/.env"
DEFAULT_BACKUP_DIR="$APP_ROOT/backups"

BACKUP_DIR="$DEFAULT_BACKUP_DIR"
RETENTION=7
LEAN=0
SKIP_PG=0
DRY_RUN=0
ASSUME_YES=0
SUBCOMMAND="create"
RESTORE_INPUT=""
DEPLOYMENT="${HDB_BACKUP_DEPLOYMENT:-auto}"
STORAGE_PARENT="$APP_ROOT"
RESUME_SERVICES=()
RESUME_HOST=0
RESTORE_PREVIOUS_STORAGE=""
RESTORE_FILES_REPLACED=0
RESTORE_COMMITTED=0

# 永远不属于"要备份数据"的部分：日志、pid、便携 Node、嵌套备份、测试产物。
ALWAYS_EXCLUDE=(storage/logs storage/run storage/nodeenv storage/backups storage/_test_output)
# --lean 额外排除：权威性为"可重建"的数据（wiki/02-architecture.md §4.1）。
LEAN_EXCLUDE=(
  storage/table_indexes
  storage/circuits
  storage/external_conversations
  storage/agent_checkpoints.db
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
PG_MODE=""
PG_COMPOSE=""
PG_DEPLOY_ENV=""
COMPOSE_ARGS=()
detect_docker() {
  if [ "${#DOCKER_CMD[@]}" -gt 0 ]; then return 0; fi
  if docker info >/dev/null 2>&1; then
    DOCKER_CMD=(docker)
  elif command -v sudo >/dev/null 2>&1; then
    warn "当前用户无 docker 权限，改用 sudo（可能需要输入密码）"
    DOCKER_CMD=(sudo docker)
  else
    die "无法访问 docker daemon：请把用户加入 docker 组，或以 root 运行。"
  fi
}

select_postgres_deployment() {
  local requested="$DEPLOYMENT" docker_cid="" host_cid=""
  if [ "$requested" = "auto" ] && [ "$SKIP_PG" -eq 0 ]; then
    detect_docker
    docker_cid="$("${DOCKER_CMD[@]}" compose --env-file "$DOCKER_DEPLOY_ENV" -f "$DOCKER_PG_COMPOSE" ps -q postgres 2>/dev/null || true)"
    host_cid="$("${DOCKER_CMD[@]}" compose --env-file "$LEGACY_PG_DEPLOY_ENV" -f "$LEGACY_PG_COMPOSE" ps -q postgres 2>/dev/null || true)"
    if [ -n "$docker_cid" ] && [ -n "$host_cid" ]; then
      die "两种 PostgreSQL 同时运行，请指定 --deployment docker 或 --deployment host。"
    elif [ -n "$docker_cid" ]; then
      requested="docker"
    elif [ -n "$host_cid" ]; then
      requested="host"
    fi
  fi
  if [ "$requested" = "auto" ]; then
    if [ -f "$DOCKER_DEPLOY_ENV" ] && [ -f "$APP_ROOT/deploy/docker/config/.env" ]; then
      requested="docker"
    else
      requested="host"
    fi
  fi
  if [ "$requested" = "docker" ]; then
    PG_MODE="docker"
    PG_COMPOSE="$DOCKER_PG_COMPOSE"
    PG_DEPLOY_ENV="$DOCKER_DEPLOY_ENV"
    COMPOSE_ARGS=(--env-file "$DOCKER_DEPLOY_ENV" -f "$DOCKER_PG_COMPOSE")
    STORAGE_PARENT="$APP_ROOT/deploy/docker"
    return 0
  fi
  [ "$requested" = "host" ] || die "部署模式必须为 docker、host 或 auto。"
  PG_MODE="legacy"
  PG_COMPOSE="$LEGACY_PG_COMPOSE"
  PG_DEPLOY_ENV="$LEGACY_PG_DEPLOY_ENV"
  COMPOSE_ARGS=(--env-file "$LEGACY_PG_DEPLOY_ENV" -f "$LEGACY_PG_COMPOSE")
}

resume_writers() {
  if [ "${#RESUME_SERVICES[@]}" -gt 0 ]; then
    compose_pg start "${RESUME_SERVICES[@]}" || warn "请手动启动应用服务。"
  fi
  if [ "$RESUME_HOST" -eq 1 ]; then
    bash "$APP_ROOT/scripts/hdb.sh" start || warn "请手动启动宿主机服务。"
  fi
}

finish_restore() {
  local status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ] || [ "$RESTORE_COMMITTED" -ne 1 ]; then
    if [ "$RESTORE_FILES_REPLACED" -eq 1 ] && [ "$RESTORE_COMMITTED" -eq 0 ]; then
      rm -rf "$STORAGE_PARENT/storage"
      if [ -n "$RESTORE_PREVIOUS_STORAGE" ] && [ -d "$RESTORE_PREVIOUS_STORAGE" ]; then
        mv "$RESTORE_PREVIOUS_STORAGE" "$STORAGE_PARENT/storage" \
          || warn "文件回滚失败，请从 $RESTORE_PREVIOUS_STORAGE 手动恢复。"
      fi
    fi
    # A signal or lost pg_restore connection can leave the DB outcome unknown.
    # Keep writers stopped even after a successful file rollback.
    warn "恢复未成功，应用保持停止。请确认 PostgreSQL 与 storage 属于同一批次后再启动。"
    [ "$status" -ne 0 ] || status=1
  else
    resume_writers
  fi
  exit "$status"
}

stop_writers() {
  if [ "$PG_MODE" = "docker" ]; then
    detect_docker
    local service running
    running="$(compose_pg ps --services --status running)"
    for service in api web worker-chat worker-parse worker-export worker-memory; do
      if grep -qx "$service" <<< "$running"; then RESUME_SERVICES+=("$service"); fi
    done
    if [ "${#RESUME_SERVICES[@]}" -gt 0 ]; then
      compose_pg stop "${RESUME_SERVICES[@]}" || die "无法停止应用写入进程，取消备份/恢复。"
    fi
  else
    local pid_file pid
    for pid_file in "$APP_ROOT"/storage/run/*.pid; do
      [ -f "$pid_file" ] || continue
      pid="$(cat "$pid_file")"
      if kill -0 "$pid" 2>/dev/null; then RESUME_HOST=1; break; fi
    done
    if [ "$RESUME_HOST" -eq 1 ]; then
      bash "$APP_ROOT/scripts/hdb.sh" stop || die "无法停止宿主机应用写入进程。"
    fi
  fi
}

compose_pg() {
  detect_docker
  "${DOCKER_CMD[@]}" compose "${COMPOSE_ARGS[@]}" "$@"
}

read_env_value() { # file key
  [ -f "$1" ] || return 1
  grep -E "^$2=" "$1" 2>/dev/null | tail -n 1 | cut -d= -f2-
}

pg_container() {
  local cid
  cid="$(compose_pg ps -q postgres 2>/dev/null || true)"
  [ -n "$cid" ] || die "未找到 $PG_MODE PostgreSQL 容器（请先启动对应 Docker Compose 栈）。"
  printf '%s' "$cid"
}

pg_db_name() {
  if [ "$PG_MODE" = "docker" ]; then printf 'hdb'; return; fi
  local db
  db="$(read_env_value "$PG_DEPLOY_ENV" POSTGRES_DB || true)"
  printf '%s' "${db:-hdb}"
}

pg_user_name() {
  if [ "$PG_MODE" = "docker" ]; then
    printf 'hdb'
    return
  fi
  local user
  user="$(read_env_value "$PG_DEPLOY_ENV" POSTGRES_USER || true)"
  printf '%s' "${user:-hdb}"
}

stamp() { date +%Y%m%d-%H%M%S; }

prune_old() { # dir prefix keep
  local dir="$1" prefix="$2" keep="$3" count file
  [ "$keep" -gt 0 ] 2>/dev/null || return 0
  local suffix="tar.gz"
  [ "$prefix" = "pg-" ] && suffix="dump"
  count="$(find "$dir" -maxdepth 1 -name "${prefix}*.${suffix}" | wc -l)"
  [ "$count" -gt "$keep" ] || return 0
  ls -1t "$dir"/"$prefix"*."$suffix" | tail -n "$((count - keep))" | while read -r file; do
    rm -f "$file" "$file.sha256"
    log "保留策略：删除旧备份 $(basename "$file")"
  done
}

create_backup() {
  local ts pg_db pg_dump_file archive
  ts="$(stamp)"
  mkdir -p "$BACKUP_DIR"
  log "目标目录: $BACKUP_DIR"
  log "部署: $PG_MODE；文件数据: $STORAGE_PARENT/storage"
  if [ "$DRY_RUN" -eq 0 ]; then
    trap resume_writers EXIT
    stop_writers
  fi

  # ---- 1) PostgreSQL 控制面 ----
  pg_dump_file=""
  if [ "$SKIP_PG" -eq 1 ]; then
    warn "--skip-pg：本次不备份 PostgreSQL"
  else
    detect_docker
    local container
    container="$(pg_container)" || exit 1
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
  ( cd "$STORAGE_PARENT" && tar -czf "$archive" "${tar_args[@]}" storage ) || tar_rc=$?
  if [ "$tar_rc" -ne 0 ]; then
    rm -f "$archive"
    [ -z "$pg_dump_file" ] || rm -f "$pg_dump_file"
    die "storage 打包失败（tar 退出码 $tar_rc）。"
  fi
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
  elif [[ "$input" == *storage-*.tar.gz ]]; then
    dir="$(dirname "$(realpath "$input")")"
  else
    [ -f "$input" ] || die "找不到备份文件: $input"
    dir="$(mktemp -d)"
    tar -xzf "$input" -C "$dir" || die "无法解包备份。"
  fi

  local pg_dump_file tar_file
  tar_file="$(ls -1t "$dir"/storage-*.tar.gz 2>/dev/null | head -n 1 || true)"
  if [ -f "$input" ] && [[ "$input" == *storage-*.tar.gz ]]; then tar_file="$(realpath "$input")"; fi
  [ -n "$tar_file" ] || die "归档里没有 storage-*.tar.gz。"
  local restore_ts
  restore_ts="$(basename "$tar_file" .tar.gz)"
  restore_ts="${restore_ts#storage-}"
  pg_dump_file="$dir/pg-$(pg_db_name)-${restore_ts}.dump"
  if [ "$SKIP_PG" -eq 0 ]; then
    [ -s "$pg_dump_file" ] || die "缺少同批次 PostgreSQL dump；纯 SQLite 恢复请显式 --skip-pg。"
    detect_docker
    pg_container >/dev/null || exit 1
  fi
  local verify_file
  for verify_file in "$tar_file" "$pg_dump_file"; do
    [ -f "$verify_file" ] || continue
    [ -f "$verify_file.sha256" ] || die "缺少校验和: $verify_file.sha256"
    (cd "$(dirname "$verify_file")" && sha256sum -c "$(basename "$verify_file").sha256") || die "备份校验失败。"
  done

  if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "恢复将覆盖 PostgreSQL 的 $(pg_db_name) 库并替换 ./storage（会先停应用）。输入 yes 继续: " answer || true
    [ "$answer" = "yes" ] || die "已取消。"
  fi

  log "停止应用服务（数据服务保留）..."
  trap finish_restore EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  stop_writers

  # ---- 1) ./storage：先把当前数据挪走（可回滚），再解包 ----
  local pre_restore="$STORAGE_PARENT/storage.pre-restore-$(stamp)"
  if [ -d "$STORAGE_PARENT/storage" ]; then
    log "现有 storage 挪到 $(basename "$pre_restore")（确认无误后可删除）"
    mv "$STORAGE_PARENT/storage" "$pre_restore"
    RESTORE_PREVIOUS_STORAGE="$pre_restore"
  fi
  RESTORE_FILES_REPLACED=1
  if ! tar -xzf "$tar_file" -C "$STORAGE_PARENT"; then
    die "storage 解包失败。"
  fi
  mkdir -p "$STORAGE_PARENT/storage/logs" "$STORAGE_PARENT/storage/run"

  # ---- 2) PostgreSQL ----
  if [ "$SKIP_PG" -eq 1 ]; then
    warn "--skip-pg 的备份不含 PostgreSQL，跳过数据库恢复。"
  elif [ -n "$pg_dump_file" ]; then
    local container pg_db
    container="$(pg_container)" || exit 1
    pg_db="$(pg_db_name)"
    log "恢复 PostgreSQL（pg_restore --clean, 库 $pg_db）..."
    "${DOCKER_CMD[@]}" cp "$pg_dump_file" "$container:/tmp/hdb-restore.dump" \
      || die "拷贝 dump 到容器失败。"
    if ! "${DOCKER_CMD[@]}" exec "$container" pg_restore \
        --clean --if-exists --no-owner --no-privileges \
        --exit-on-error --single-transaction \
        -U "$(pg_user_name)" -d "$pg_db" /tmp/hdb-restore.dump; then
      "${DOCKER_CMD[@]}" exec "$container" rm -f /tmp/hdb-restore.dump || true
      die "pg_restore 失败（确认没有活跃连接：应用已停？）。"
    fi
    RESTORE_COMMITTED=1
    "${DOCKER_CMD[@]}" exec "$container" rm -f /tmp/hdb-restore.dump || true
  else
    warn "归档里没有 pg-*.dump，跳过 PostgreSQL 恢复。"
  fi

  RESTORE_COMMITTED=1

  log "恢复完成，将恢复备份前运行的应用服务。"
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
    --deployment) DEPLOYMENT="${2:?--deployment 需要 docker 或 host}"; shift 2 ;;
    --retention) RETENTION="${2:-7}"; shift 2 ;;
    --lean) LEAN=1; shift ;;
    --skip-pg) SKIP_PG=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "未知参数: $1" ;;
  esac
done

[[ "$RETENTION" =~ ^[0-9]+$ ]] || die "--retention 必须为非负整数。"
BACKUP_DIR="$(realpath -m "$BACKUP_DIR")"

case "$SUBCOMMAND" in
  list) list_backups ;;
  restore|create)
    select_postgres_deployment
    [ -f "$PG_COMPOSE" ] || die "缺少 $PG_COMPOSE（无法确定 PostgreSQL 编排）。"
    [ "$SUBCOMMAND" != "create" ] || [ -d "$STORAGE_PARENT/storage" ] || die "缺少 $STORAGE_PARENT/storage 目录。"
    if [ "$SUBCOMMAND" = "restore" ]; then
      restore_backup "${RESTORE_INPUT:-}"
    else
      create_backup
    fi
    ;;
  *) die "未知操作: $SUBCOMMAND" ;;
esac
