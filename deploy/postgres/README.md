# Hardware DataBase 项目自管 PostgreSQL

控制面账本 + 记忆投影 + Agent 检查点 + 资源配额的唯一数据库。
**用 pgvector 镜像**：记忆投影的语义索引需要 `vector` 扩展，官方 alpine 镜像没有。

## 快速开始

```bash
bash scripts/postgres.sh up          # 生成密码 -> 起容器 -> 等健康
bash scripts/postgres.sh write-env   # 把 5 个 PG 后端写入应用 .env（原文件先备份）
bash scripts/hdb.sh restart          # 后端开关是重启生效项
```

`write-env` 会设置：`CONTROL_PLANE_*`、`MEMORY_STORE_*`、`AGENT_CHECKPOINT_*`、
`DOCUMENT_AUTHORING_CHECKPOINTER_*`、`RESOURCE_GOVERNOR_*`（含 `SCOPE=deployment`），
DSN 全部指向本实例（默认 `127.0.0.1:55433/hdb`）。

## 实例参数

| 项 | 值 | 说明 |
|---|---|---|
| 镜像 | `pgvector/pgvector:pg17` | 含 vector 扩展；升级时改 compose 并记录兼容性 |
| 监听 | `127.0.0.1:${HDB_PG_PORT:-55433}` | 仅宿主机回环；默认 55433 避开已有的 55432 演练实例 |
| 鉴权 | `POSTGRES_PASSWORD`（必填） | compose 缺失即报错；随机 32 位 hex，存 `deploy/postgres/.env`（0600，gitignored） |
| 库 | `hdb`（主）、`hdb_rehearsal`（预演） | `initdb/001-init.sql` 首次启动时建库 + 装 vector |
| 连接 | `max_connections=120`，`shared_buffers=512MB`，`timezone=UTC` | 连接预算用 `connection_budget.py` 复核 |
| 数据卷 | `hardware-database-postgres-data` | `down` 不删 |

## 首次部署 / 全新安装

生产机的空库初始化：当前唯一入口是迁移脚本（从 SQLite 快照生成 schema + 导数据）：

```bash
# 1. 预演（不碰生产，跑在 hdb_rehearsal 上）
uv run python scripts/production_hardening/cutover_rehearsal.py \
    --source storage/auth.db \
    --dsn "postgresql://hdb:<pass>@127.0.0.1:55433/hdb_rehearsal" --report /tmp/rehearsal.json

# 2. 停机窗口：停写 -> 冻结快照 -> 装载
bash scripts/hdb.sh stop
uv run python -c "import sqlite3; sqlite3.connect('storage/auth.db').execute('VACUUM INTO ?', ('storage/backups/auth-prepg.db',))"
uv run python scripts/production_hardening/load_sqlite_into_postgres.py \
    --source storage/backups/auth-prepg.db \
    --dsn "postgresql://hdb:<pass>@127.0.0.1:55433/hdb" --report /tmp/load.json

# 3. 切配置并重启
bash scripts/postgres.sh write-env && bash scripts/hdb.sh start
```

> 注意：控制面 schema 是**手写固定**的（`src/storage/control_plane/schema.py`），
> 应用不做隐式 DDL，schema 缺失/版本不符时 readiness 直接 not-ready（fail-closed）。
> 源库若新增列，必须同步更新固定 DDL —— 预演脚本就是用来发现这种漂移的
> （已发现并修复过：`chat_sessions.rolling_summary`、JSONB 空串默认值）。

## 命令

```bash
bash scripts/postgres.sh status        # 容器 + 连接 + 应用侧后端提示
bash scripts/postgres.sh dsn [--show]  # 打印 DSN（默认打码）
bash scripts/postgres.sh psql          # 进 psql（默认 -d hdb）
bash scripts/postgres.sh logs          # 最近日志
bash scripts/postgres.sh down          # 停止（保留数据卷）
```

## 排障

- `/health/ready` 的 `control_plane` 报 `down` + `schema_version missing`：
  没跑装载脚本，或 schema 版本不匹配；按上面的初始化流程处理。
- 记忆语义检索不可用：确认 `hdb` 库里有 `vector` 扩展（`\dx`）。
- 测试：**本机 `.env` 指向生产 PG 时，跑 pytest 前不要忘了 conftest 已强制 sqlite**；
  需要 PG 的套件显式传 `HDB_TEST_POSTGRES_DSN`（用 `hdb_rehearsal`，不要指向 `hdb`）。
