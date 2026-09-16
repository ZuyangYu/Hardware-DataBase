# PostgreSQL 状态迁移 Runbook（P1-002 / P1-003）

> 基线：`feature/fix-module-matching` commit `01c807e`（2026-09-15，PR #49 合并）
> 工具：
> - `scripts/production_hardening/state_fingerprint.py`（迁移前后指纹）
> - `scripts/production_hardening/migrate_sqlite_to_postgres.py`（分组复制 + 校验）
> - `scripts/production_hardening/migrate_chat_turn_sequence.py`（schema 迁移）
> - `scripts/production_hardening/verify_checkpoint_postgres.py`（Checkpoint 分叉/删除验证）
> 关联：route map v1.4 §5.4，实施方案 v1.1 §7/§13.6

## 1. 迁移组（同一组必须整组切换）

| 组 | 源库 | 内容 | 权威性 |
|---|---|---|---|
| `authority` | `storage/auth.db` | 身份/权限/会话/turn/事件/审计、文档生成任务与计划、Memory 权威账本/jobs/outbox、Export 任务 | 权威，**原子组** |
| `projection` | `storage/memory.db` | Memory 检索投影 | 可重建（权威组之后重建） |
| `attachments` | `storage/chat_attachments.db` | 附件元数据（文件另迁共享存储） | 权威元数据 |
| `pipeline` | `storage/pipeline_documents.db` | Pipeline 文档/解析任务台账 | 权威 |
| `compatibility` | `storage/document_authoring_compatibility.sqlite` | 文档兼容性审计账本 | 权威 |
| Checkpoint | `agent_checkpoints.db` / `document_authoring_checkpoints.sqlite` | Agent/Authoring Checkpoint | 恢复所需，attempt 指针与数据成对迁移 |

`memory.db` 只承载可重建投影；权威账本在 `authority` 组内，**不允许**只切投影后端。

## 2. 前置条件

1. 目标 PostgreSQL 可用，已完成 `verify_checkpoint_postgres.py` 验证（若使用 PG Checkpoint）；
2. 备份源库（SQLite backup API 或 `VACUUM INTO`，禁止复制写入中的 WAL）与文件目录；
3. 生成迁移前指纹：`uv run python scripts/production_hardening/state_fingerprint.py --label p1-pre --output .../fingerprints/p1-pre.json`；
4. 记录当前 commit、Feature Flag 快照与队列深度；确认无进行中的迁移/切换。

## 3. 切换步骤（每个组重复）

1. **演练**：把源库副本复制到备份目录，对副本执行迁移与校验；
2. **停写**：停止该组所有 API/Worker/维护任务写入；排空或显式撤销在途执行（等待 lease 过期或取消）；
3. **schema**：如涉及 `turn_sequence` 等仍为旧 schema 的库，先在该组停写窗口执行 `migrate_chat_turn_sequence.py`；
4. **复制**：
   ```bash
   uv run python scripts/production_hardening/migrate_sqlite_to_postgres.py \
       --group authority --dsn "$HDB_POSTGRES_DSN" --report .../reports/p1-authority.json
   ```
   `--dry-run` 可先查看行数；脚本只做只读源 + 幂等 upsert，校验每张表的行数与内容哈希；
5. **修序列**：`BIGINT GENERATED`/自增列在复制后需要 `setval` 校准（否则新插入冲突）：
   ```sql
   SELECT setval(pg_get_serial_sequence('chat_turns', 'rowid'), COALESCE((SELECT MAX(rowid) FROM chat_turns), 1));
   ```
   SQLite `rowid` 无对应列时，按业务主键（如 `chat_sessions.id`）校准；
6. **切换写入**：应用指向 PostgreSQL（同一权威组不逐请求混用新旧库）；
7. **灰度**：单 Worker → 内部用户 → 10% → 50% → 全量；每一步确认 queue wait、错误率、stale write、重复 Artifact、token usage 正常；
8. **事后校验**：再次生成指纹，与迁移前对比；记录未解释差异（应为 0）。

## 4. 校验标准（每张表）

- 行数一致；
- 内容哈希一致（`source_hash == target_hash`，脚本自动比较）；
- 外键/权限/租户隔离抽查通过；
- 已接受任务不丢失（RPO=0）；
- Checkpoint：历史可恢复、`delete_thread` 行为一致、fork 指针可回读。

## 5. 回滚

- 目标库尚未接受写入：直接切回旧库；
- 目标库已接受写入：优先回滚应用版本（兼容目标 schema）；必须回旧库时先停写、导出并校验目标新增状态，再按反向脚本切回；反向迁移未演练通过则保持目标库前向修复；
- Memory 投影回滚：从权威账本重建，不复制可能过期的索引；
- Checkpoint 回滚：切回 SQLite 前确认无仅写入 PostgreSQL 的新状态。

## 6. 文件与 Artifact（P1-003）

- 数据库只保存逻辑 storage key，禁止节点绝对路径；
- 所有 API/Worker 挂载同一共享根目录；临时文件与目标文件同文件系统原子改名；
- 迁移验证：断连、权限、大小/hash、跨节点下载、孤儿清理；
- 共享文件服务不可用时任务重试/失败，不发布缺失文件的成功记录；
- 本地向量索引不放入共享可写目录：只读快照 + 单写者发布，或独立索引服务。

## 7. 当前状态（本仓库实施记录）

- ✅ `turn_sequence` 迁移脚本（expand/backfill/enforce）已在真实 `auth.db` 副本上演练通过（154 行回填、NOT NULL 重建、校验 0 差异）；
- ✅ PostgreSQL Checkpoint provider + `verify_checkpoint_postgres.py` 已在 Docker `postgres:16-alpine` 上验证（put/get、fork pending writes、隔离、删除、清理全部通过）；
- ✅ `migrate_sqlite_to_postgres.py` 已在真实 `auth.db` 副本上完成 authority 组 41 张表的复制与哈希校验；
- ⏳ PostgreSQL Memory 投影后端、跨主机配额、共享文件服务属于部署环境事项，切换前必须按本 runbook 停写演练。
