# 回滚手册（rollback-runbook）

> 基线：`feature/fix-module-matching` commit `01c807e`（2026-09-15，PR #49 合并）
> 适用：实施方案 §10.3 的逐任务回滚。任何回滚必须先停止新写入、处理在途执行，再切换版本。
> 原则：目标库/目标格式接收新写入后不允许直接切回旧库；回滚窗口内保留旧库与旧文件。

## 通用步骤

1. 记录当前 `baseline_id`、commit、Feature Flag 快照、队列深度与在途任务清单；
2. 停止准入（新 turn/新任务提交），保留已有任务状态可见；
3. 排空或显式撤销在途执行（等待 lease 过期或调用取消接口），确认无旧消费者抢领；
4. 执行该任务回滚步骤；
5. 运行 `uv run python scripts/production_hardening/regression_runner.py` + 任务相关测试；
6. 记录回滚时间点、丢失/重放范围、恢复点，归档到实施目录。

## 逐任务回滚

| 任务 | 回滚操作 | 关键约束 |
|---|---|---|
| P0-002 turn_sequence | 保留列与索引（兼容），关闭强制校验开关；不删除已回填数据 | 不重建会话顺序；不把 `turn_sequence` 降回 NULL 后再启用 |
| P0-002b Checkpoint 验证 | 仅验证任务，无生产影响；若产生孤儿 checkpoint，按保留期清理 | 不得删除被恢复指针引用的数据 |
| P0-003 claim/lease/fencing | 先停止新 turn 创建并排空在途，关闭 `CONVERSATION_SERIALIZE_SAME_THREAD`，回到经单消费者验证的兼容模式 | 禁止新旧 claim 规则混用；已推进的 fencing token 不回溯 |
| P0-004 waiting 状态机 | 保留新字段（可空），停止 parse 角色的协调控制线程并确认 `coordinator_leases` 无有效 owner，再由旧扫描入口推进等待中的 turn | 不遗留永久 `waiting_for_attachments`；回滚前先推进或失败化；不得两个协调者同时扫描 |
| P0-005 Worker 角色 | 恢复 `--role all` 单进程；先停新角色进程，等待 lease 过期/撤销后再启旧进程 | 禁止新旧消费者同时抢领同一队列 |
| P0-006 Resource Governor | 暂停准入与新 lease 申请，保留已有 lease 释放与 `uncertain` 处理，回滚到上一兼容版本后恢复准入；被升级隔离的 resource 必须用 `governor_recovery.py release`（证据 + 审计）恢复，禁止直接删行或清计数 | 禁止关闭 governor 后无限制放行；`uncertain` 额度按 deadline 或人工确认收口 |
| P0-007 Shared Retrieval Executor | 关闭 `SHARED_RETRIEVAL_EXECUTOR_ENABLED`，恢复每调用线程池路径 | 不关闭其他请求共用的进程级 executor 而不排空 |
| P1-001 Checkpoint provider | 切回 SQLite 前确认无仅写入 PostgreSQL 的新状态；否则保持 PG 前向修复 | attempt 指针与数据成对回滚 |
| P1-002 Memory | 切回旧投影时从权威账本重建，不复制可能过期的索引；权威账本按 AUTH_DB 原子组整体回滚 | 不一半 SQLite、一半 PostgreSQL |
| P1-003 状态/文件迁移 | 开放写入前可无损回滚；开放后优先回滚应用版本（兼容目标 schema）；必须回旧库时先停写、导出并校验新增状态 | 已接受任务 RPO=0；反向迁移须演练通过 |
| P2-001 Action DAG 并行 | 关闭 `ACTION_DAG_PARALLEL_ENABLED`，保留串行路径与 v1 契约 | 不接受并行期间产生的半合并 receipt 作为完成 |
| P2-002 指标 | 指标只读，无回滚动作；看板版本回退 | 不删除历史指标数据源 |
| P3-001/002 Sub-Agent | 关闭 `REVIEWER_AGENT_ENABLED`/`RESEARCH_AGENT_ENABLED`，回到 Supervisor-only 路径 | 审查结果不得污染权威 Memory；已标记未完成审查的发布保持未完成 |

## 数据窗口与备份

- 每次涉及 schema 的变更前：备份 `storage/auth.db`（含 WAL checkpoint）、`agent_checkpoints.db`、`document_authoring_checkpoints.sqlite`、`memory.db`、附件/归档目录；
- 备份使用 SQLite 官方 backup API 或 `VACUUM INTO`，禁止直接复制正在写入的 WAL 库；
- 备份与恢复点写入迁移记录；备份保留至回滚窗口结束（默认两个发布周期）。
