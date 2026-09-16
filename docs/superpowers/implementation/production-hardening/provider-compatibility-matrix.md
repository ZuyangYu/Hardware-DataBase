# Provider 兼容性矩阵（G3 §8.1）

> 目的：证明“配置与实现一致、未交付 provider 不会被静默接受”，并冻结版本与连接池约定。
> 基线：commit `01c807e` + production-hardening 分支实现。

## 1. Agent Checkpoint

| 后端 | 配置 | 实现 | 验证 |
|---|---|---|---|
| sqlite（默认） | `AGENT_CHECKPOINT_BACKEND=sqlite`、`AGENT_CHECKPOINT_DB_PATH` | `SqliteCheckpointerProvider`（共享连接，saver 内部串行） | `tests/test_checkpoint_provider.py`、`test_checkpoint_fork_restore.py` |
| postgres | `AGENT_CHECKPOINT_BACKEND=postgres` + `AGENT_CHECKPOINT_DATABASE_URL` | `PostgresCheckpointerProvider`：`psycopg_pool.ConnectionPool`（`AGENT_CHECKPOINT_POOL_SIZE`，默认 4；`min_size=1`、autocommit），单连接回退保留 | PG 集成测试 10 passed；16 线程并发 put/get；`verify_checkpoint_postgres.py` 全项通过；双副本 HTTP 演练跨副本续跑同一会话（`multi-instance-pg-provider-drill-20260916.json`，checkpoints/checkpoint_writes/store 表实测写入） |
| 其他值 | — | 配置保存/启动阶段拒绝（`validate_provider_configuration`） | `tests/test_provider_config_guard.py` |

版本：`langgraph==1.2.11`、`langgraph-checkpoint-postgres==3.1.2`、`langgraph-checkpoint-sqlite==3.1.1`、`psycopg[binary]==3.3.5`（已锁定并写入 `uv.lock`）。

## 2. Authoring Checkpoint

| 后端 | 配置 | 实现 | 验证 |
|---|---|---|---|
| sqlite | `DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=sqlite` | `FencedCheckpointer` + SQLite saver | `test_checkpoint_provider.py` |
| memory | `=memory` | 内存 saver（测试用） | 同上 |
| postgres | `=postgres` + `DOCUMENT_AUTHORING_CHECKPOINTER_DATABASE_URL` | `create_postgres_checkpointer`，**保留 FencedCheckpointer fencing** | PG smoke + 配置守卫测试 |
| 缺 URL | — | 保存/启动显式报错（不再“配置成功、运行失败”） | `test_provider_config_guard.py` |

## 3. Memory

| 后端 | 配置 | 实现 | 验证 |
|---|---|---|---|
| sqlite（默认） | `MEMORY_STORE_BACKEND=sqlite`、`MEMORY_SQLITE_PATH` | SQLite 权威账本 + 可重建投影 | 全套 Memory 测试 |
| postgres | `=postgres` + `MEMORY_DATABASE_URL` | `PostgresMemoryStoreRuntime`（官方 PostgresStore；无 pgvector 时关键词降级） | `test_memory_postgres_runtime.py`（真实 PG 往返） |
| 缺 URL/未知值 | — | 保存/启动拒绝 | `test_provider_config_guard.py` |

**权威账本迁移**：`storage/auth.db` 的 Memory ledger/jobs/outbox 与会话权威组同事务切换（单元 A），投影在切换后重建追平；不使用跨库双写。

## 4. 配额存储

| 作用域 | 存储 | 说明 |
|---|---|---|
| process | 进程内 semaphore | 开发/单实例 |
| deployment（同机） | `RESOURCE_GOVERNOR_DB_PATH` SQLite 配额库 | 身份行（deployment_id/schema_version/config_revision）、WAL、短事务忙重试、uncertain 隔离与维护选主 |
| deployment（跨主机） | PostgreSQL 配额存储（G3 范围） | 未实施；接口与 lease 契约保持一致，P1 切换只替换存储后端 |

## 5. 已知限制

- Authoring/Agent postgres provider 已锁版本并通过 PG 集成，但生产切换仍需 §G3 演练；
- Memory postgres 为投影后端；权威账本迁移未执行；
- `EVAL_EMBEDDING_*` 与供应商 RPM/TPM 为显式范围外（见 `entry-inventory.md`）。
