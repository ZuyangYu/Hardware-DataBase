# G3 迁移切换清单（cutover checklist）

> 依据：后续优化实施方案 v1.0 §8/§13.6、完整实施方案 §7.4/§7.5
> 状态：**准备完成、未执行生产切换**。本清单用于在临时 PostgreSQL + 共享文件环境演练并留档。

## 1. 切换单元（按事务依赖，禁止跨库原子写）

| 单元 | 范围 | 同事务要求 | 切换条件 |
|---|---|---|---|
| A 会话权威组 | 身份/权限/会话/messages/turn/事件/`conversation_execution_state`/`action_receipts` + Memory ledger/jobs/outbox + 与 `complete_turn` 同事务的领域表 | **同一数据库连接、同一事务提交** | 停写排空后一次性切换；单元内不得一半 SQLite 一半 PG |
| B 领域权威组 | Authoring 任务/计划/工单/Checkpoint、Attachment、Export、Wiki、资产中心元数据 | 与 A 共享事务的表并入 A；跨库引用保留幂等协调 | 按 Phase 0 事务清单细分；逐组演练 §7.5 |
| C 文件存储 | Artifact、附件、归档文件 | 文件 hash 与元数据 storage key 成对校验 | 引用它们的写入者跨主机运行前完成 |
| D 检索投影 | Memory/Circuit/Spreadsheet/Document 可重建部分 | 无（可重建） | A/B/C 完成后重建并追平 outbox |

## 2. 切换前（演练环境）检查

1. `state_fingerprint.py --label g3-pre` 记录源库行数/hash 与文件目录指纹；
2. `migration_rollback_drill.py --all-groups --dsn <pg>`：快照副本 + 双跑幂等 + 源库不变；
3. `migrate_sqlite_to_postgres.py --group <g> --dsn <pg>`：逐组迁移；
4. 判定门槛：**任意 `hash_mismatches` 或 `count_mismatches` 非空即阻止切换**（不能只看 `ok=true`）；
5. 外键/权限/租约/恢复指针抽查；附件/Artifact 文件 hash 与元数据一致。

## 3. 切换步骤（生产，需发布窗口）

1. 记录 `baseline_id`、commit、配置快照、在途任务与队列深度；
2. 停止所有 API/Worker/维护任务写入（含附件协调线程、导出、Memory）；
3. 排空或封存在途执行：等待 lease 过期或显式取消，确认无旧消费者；
4. 对 A/B 组执行最终增量同步或可验证补齐，再次运行 §2 校验；
5. 旧消费者全部停止后切换写入与 Worker（禁止新旧并写同一权威组）;
6. 灰度放量：按租户/固定 session 分组，同一 session 不混用新旧 claim 协议；
7. 观测：`hdb.conversation.stale_write_rejected`、队列等待、错误率、重复终态事件/Artifact、token usage。

## 4. 回滚

- 目标库**未开放写入**：可直接切回旧库（无损）；
- 已开放写入：优先回滚应用版本（兼容目标 schema）；必须回旧库时，先停写、导出并校验目标新增状态，执行反向同步/操作重放；未演练通过的反向迁移不允许执行，保持目标库前向修复；
- 已接受任务 RPO=0；旧库与旧文件保留至回滚窗口结束。

## 5. 本机演练命令（不切换生产）

```bash
# 临时 PostgreSQL
docker run -d --name hdb-pg-test -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=hdb -p 55432:5432 postgres:16-alpine

# 全组回滚/幂等演练（一致性快照副本，零影响在线服务）
uv run python scripts/production_hardening/migration_rollback_drill.py --all-groups \
  --dsn postgresql://postgres:postgres@127.0.0.1:55432/hdb \
  --output docs/superpowers/implementation/production-hardening/g3-all-groups-rollback-drill.json

# 单组迁移（含 hash/count 判定）
uv run python scripts/production_hardening/migrate_sqlite_to_postgres.py --group authority \
  --dsn postgresql://postgres:postgres@127.0.0.1:55432/hdb --report <report.json>

# 状态指纹前后对比
uv run python scripts/production_hardening/state_fingerprint.py --label g3-post --output <fp.json>
```

## 6. 验收与留档

- 每单元：源/目标映射、行数与 hash、外键与权限、切换步骤、回滚说明、演练报告；
- 责任人：存储负责人（切换执行）、后端负责人（应用兼容）、测试负责人（校验）、运维负责人（窗口与回滚）；
- 当前就绪度：provider/迁移/回滚脚本与演练通过（见 `postgres-migration-drill-20260915.json`、`migration-rollback-drill-20260915.json`、`g3-all-groups-rollback-drill.json`）；共享文件与多主机待环境。
