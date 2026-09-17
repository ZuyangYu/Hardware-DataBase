# 实施记录（P0–P3，工作区未提交）

> 基线：`feature/fix-module-matching` commit `01c807e`（2026-09-15，PR #49 `feature/asset-center-and-llm-governance` 合并）
> 日期：2026-09-15
> 说明：改动保留在工作区，**未创建分支、未提交**；`docs/` 被 `.gitignore` 忽略。
> 参考备份：`Hardware-DataBase-observability_changes_backup_20260915`（基于 `7fd340a` 的早期实施）。本记录区分「复用备份并逐项验证」与「按方案修正的偏离」。

## 1. 交付清单与验证

| 任务 | 主要改动 | 验证 |
|---|---|---|
| P0-001 | `scripts/production_hardening/{regression_runner,state_fingerprint,load_probe}.py`；`docs/.../state-inventory.md`、`metric-catalog.md`、`rollback-runbook.md`、`regression-set.md`、`baseline-report.md` | 基线 `20260915T084230Z-01c807e-p0-baseline-01c807e` 全绿；指纹归档 `fingerprints/p0-baseline-01c807e.json` |
| P0-002 | `conversation.py`：`turn_sequence` 列、`next_turn_sequence` 计数（**存下一可用值**）、按 `(session_id, user_message_id)` 回填、唯一索引；迁移脚本 `migrate_chat_turn_sequence.py` | `test_chat_turn_sequence.py`（6）；`counter = MAX+1`、幂等重放不分配、清空不复用 || P0-002b | `src/agents/checkpoints/fork.py`；验证报告 `checkpoint-fork-verification.md` | `test_checkpoint_fork_restore.py`（5）：fork/恢复、pending writes、namespace 隔离 |
| P0-003 | `conversation_execution_state` 权威表（lease/fencing/generation/attempt/双 checkpoint 指针/revision）；head-of-line claim；结构化 `TurnClaimResult`；stale-owner 拒写；取消撤销提交；`query.py` 接线 | `test_chat_turn_claim_fencing.py`（11）、`test_agent_checkpoint_attempts.py`（7） |
| P0-004 | 等待上限 600s 扫描、`blocked_by_turn_id/queue_reason/wait_deadline`、`hdb.conversation.attachment_wait`；**新增 `coordinator_leases` 单所有者控制循环**（`src/attachments/coordinator_lease.py`） | `test_waiting_turn_timeout.py`（5）、`test_coordinator_lease.py`（3） |
| P0-005 | `workers/main.py` `--role chat|authoring|parse|all`；parse 角色专用协调线程（不解析/不清理/不发网络请求） | `test_worker_roles.py`（6） |
| P0-006 | `src/core/concurrency/{resource_governor,rate_limiter}.py`；配额的存储权威值 + 身份/版本校验；**uncertain 升级与恢复工具 `governor_recovery.py`**；维护循环；LLM/RAG/TOOL/CHAT/HEAVY/OCR/EXPORT/MEMORY 边界接入 | `test_resource_governor.py`（9）、`test_governor_recovery.py`（6） |
| P0-007 | `src/agents/tools/retrieval_executor.py` 共享有界执行器；batch 接线；**ToolRuntime call-epoch 迟到证据拒收**；进程生命周期接线（API lifespan / worker） | `test_shared_retrieval_executor.py`（9） |
| P0-008 | `settings.validate_provider_configuration`：postgres 缺 URL 在保存与启动阶段显式失败；`apply_settings`、API lifespan 接入 | `test_provider_config_guard.py`（6） |
| P1-001 | `src/agents/checkpoints/{base,factory,sqlite_provider,postgres_provider}.py`；`AGENT_CHECKPOINT_BACKEND/DATABASE_URL`；runner 接线；依赖 `langgraph-checkpoint-postgres`+`psycopg[binary]` | `test_checkpoint_provider.py`（无 DSN 时 skip）；Docker PG 集成脚本 `verify_checkpoint_postgres.py` |
| P1-002/003 | `migrate_sqlite_to_postgres.py` 分组迁移与哈希校验；`postgres-migration-runbook.md` | 备份阶段已在真实 `auth.db` 副本 41 表校验；动态列表自动包含 `conversation_execution_state` |
| P2-001 | `contracts.py` `resolve_action`/`ResolvedAction`/依赖条件；`executor.py` 受控并行层；**持久化 receipt repository `orchestration/receipts.py`（稳定键 + 复用/重放/待协调语义 + 计划持久化 + 领域结算唤醒）**；runner/query 透传 `execution_scope` | `test_action_parallel_executor.py`（7）、`test_action_receipt_store.py`（8） |
| P2-002 | `hdb.action.parallel.*`、`hdb.retrieval.executor.*`、`hdb.checkpoint.*`；Grafana `production-hardening.json`；Prometheus 告警（新增 `HdbResourceUncertainEscalated`） | 看板/规则解析校验 |
| P3-001/002 | `subagents/reviewer_agent.py`、`research_agent.py`（flag gated） | `test_subagents.py`（9） |

## 2. 相对备份的修正（验证发现）

1. **P0-003 存储位置**：备份把 lease/fencing/generation 放在 `chat_sessions` 列；方案 §5.2.1 明确要求每会话一行的 `conversation_execution_state`（含 `active_attempt_id`、`recoverable_checkpoint_ref`、`committed_checkpoint_ref`、`revision`）。已按方案重构：所有领取/续约/释放/拒写检查走该表，`chat_sessions` 不再维护第二份执行状态；完成 turn 时 committed 指针与答案在同一事务写入；`clear_session` 推进 generation 并清空指针，取消只推进 generation（不清 committed）。
2. **P0-002 计数语义**：备份的 `next_turn_sequence` 存「最后使用值」；方案 §5.1 明确存「下一可用值」。已改为 `MAX+1`，新 session 默认 1，迁移脚本校验 counter `<= MAX` 视为缺陷。
3. **结构化领取结果**：新增 `claim_turn_result()`（claimed/blocked/stale/cancelled/retry_exhausted/noop），`claim_turn()` 保留为兼容适配器。
4. **续约失败停止新增调用**：`_run_turn` 心跳线程在 `touch_turn_worker` 返回 False 时置 `lease_lost` 并触发取消信号，不再继续调度新工具调用。
5. **附件协调单所有者**：备份仅在 `AttachmentWorker.run_once()` 内扫描；方案 §5.3/§5.4 要求至少每 5 秒、独立于阻塞解析。已新增 `AttachmentCoordinatorLoop` + `coordinator_leases`（15s lease/5s 续约/token 递增），parse 角色启动专用线程；`AttachmentWorker` 与 `python -m src.attachments.worker` 复用同一协议。
6. **P0-006 缺口补齐**：新增 uncertain `uncertain_since/escalated_at/recovery_reason`、`resource_permit_audit`、`resource_governor_meta`（deployment_id/schema_version）、`resource_quotas`（存储权威 capacity）、`RESOURCE_UNCERTAIN_ESCALATE_SECONDS`、维护循环与 `governor_recovery.py`（list/show/reconcile/release；缺证据拒绝、重复 release 幂等、审计）。
7. **功能开关默认值**：按方案 §10.1 改为默认关闭（`CONVERSATION_SERIALIZE_SAME_THREAD` / `RESOURCE_GOVERNOR_ENABLED` / `SHARED_RETRIEVAL_EXECUTOR_ENABLED=false`），测试显式开启；备份原为默认开启。
8. **ToolRuntime 迟到证据**：新增 call epoch + 结束标记，`check_cancel` 撤销全部活动 epoch；`add_evidence(..., epoch=...)` 在同一把锁内检查并登记，迟到登记计入 `hdb.retrieval.late_evidence_rejected`。
9. **测试修正**：`test_chat_turn_claim_fencing.py` 改为查询 `conversation_execution_state`；`test_chat_turn_sequence.py` 按下一可用值断言；`test_shared_retrieval_executor.py` 显式启用开关。
10. **attempt 命名空间带 attempt_id**：`prepare_attempt` 接受服务器生成的 `attempt_id`（执行状态表记录同一 ID），checkpoint namespace 变为 `<visible>::attempt-<attempt_id>`，便于把权威 attempt 与物理 checkpoint 对应；legacy token-only 路径保持兼容。
11. **聊天排队 deadline**：新增 `ConversationService.expire_queued_turns()`（`CHAT_QUEUE_DEADLINE_SECONDS`，从接受时起计、包含附件等待/同会话阻塞，运行中 turn 不受影响），chat 角色每轮扫描；失败事件 `chat_queue_timeout` 与 `hdb.chat.queue_timeout` 计数。`tests/test_chat_queue_deadline.py`（3）。
12. **P2-001 持久化 receipt repository**：新增 `src/agents/orchestration/receipts.py`，键 `(tenant_id, session_generation, turn_id, plan_version, action_id)`；durable 结果（completed/accepted/blocked/rejected）复用不重发，failed/skipped 才重放；非幂等 handler 失败标记 `needs_reconciliation` 停止自动重试；`client_request_id` 沿用稳定键传入 revision/attachment 领域工具；runner/query 透传 `action_scope`。
13. **LLM 部署级 batch 标记**：`llm_governor.slot()` 通过 `CURRENT_LLM_PRIORITY` ContextVar 把优先级传播到 `chat_model_runtime` 的部署级 permit（batch 不再占用互动预留）；超时路径也恢复优先级。
14. **可恢复检查点指针逐写登记**：新增 `checkpoints/observer.py`（按 attempt thread 过滤的 saver put/aput 观察器）；`ConversationService.update_recoverable_checkpoint_ref` 在权威事务内 CAS 登记；claim 时读取 `recoverable_checkpoint_ref`，`prepare_attempt(resume_ref=...)` 从该精确检查点分叉（pending writes 一并复制）；取消推进 generation 后旧指针被拒。`tests/test_recoverable_checkpoint_pointer.py`（6）。

15. **前端消费队列可见字段（P0-004）**：`TurnView` 增加 `blocked_by_turn_id/queue_reason/wait_deadline`；`useKbChat` 输出 `turnQueueNotice`（阻塞原因 + 截止时间），ChatPage 展示排队/等待附件横幅；`turnQueueNoticeFromTurn` 单元测试（3）。
16. **准入未终态校验（P0-006）**：`ConversationService.find_turn_by_client_request`/`count_unterminal_turns`；创建 turn 前先做幂等重放（不计数、不新建），再校验会话/用户未终态上限（默认 8/32，`session_backlog`/`user_backlog` 429 + Retry-After）；API 测试 3 例。
17. **停机回收（P0-007）**：`RetrievalExecutor.close(deadline_seconds)` 取消未启动项、限时等待运行中调用并回报 `hdb.retrieval.executor.unconfirmed`；未确认调用保留下游 permit（lease 过期后 uncertain），API lifespan / Worker 退出按 `RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS`（默认 10s）执行。
18. **计划持久化与领域唤醒（P2-001）**：`action_plans` 表 + `save_plan/get_plan/mark_plan_status/list_ready_plans`；receipt 增加 `idempotency_key/source_plan_id`；`settle_domain_job`/`settle_action_receipts_for_job` 幂等结算 accepted receipt 并把计划标记 ready；authoring worker 完成/死信时调用（best-effort）。恢复语义：计划重入时复用已结算 receipt，只执行缺失的 action。
19. **LLM 部署级 permit 移到真实请求边界（P0-006）**：`model_gateway._deployment_llm_permit` 在 `_generate/_stream/_agenerate/_astream`（每次底层请求）获取/释放，SDK 重试会重新获取额度；`chat_model_runtime._llm_governor` 只保留活跃 gauge，避免同一作用域双重申请；async 路径用 `asyncio.to_thread` 进入 permit。
20. **检查点时序指标（P2-002）**：`hdb.checkpoint.write_latency`（observer 包裹 put/aput）、`hdb.checkpoint.fork_latency`、`hdb.checkpoint.commit_latency`。
21. **文档与版本**：新增 `entry-inventory.md`（全部入口盘点 + EVAL_EMBEDDING 范围外标注）、`worker-deployment-manifest.md`（角色/启动命令/就绪检查/接管）；`langgraph==1.2.11`、`langgraph-checkpoint-postgres==3.1.2`、`psycopg[binary]==3.3.5` 精确锁定并更新锁文件。

22. **多进程 SQLite 竞争验收（§13.2）**：`tests/test_multiprocess_sqlite_contention.py` 用真实子进程 + 共享临时库验证：同会话 turn 只有最早一个可领取（token=1、后序 blocked）、`coordinator_leases` 恰好一个 owner、容量 1 的配额 permit 跨进程互斥（start/end 日志无重叠）。
23. **本机负载基线（P0-001 部分）**：`load_probe.py` 增加 SSE 首 token 测量（`--ttft`）；归档 health（75 探测 0 错误，P50 108ms）、同会话（未串行/串行）、跨会话、TTFT 四个探测报告（见 baseline-report §4）。容量/绝对 SLO 仍待目标环境。
24. **P3 确定性评测**：新增 `scripts/production_hardening/reviewer_eval.py` + `evaluation/fixtures/reviewer_cases.jsonl`（10 例），status accuracy 1.0、4 类 code precision/recall 1.0；语义（unsupported claim）对比需模型预算，报告显式标记未评测（`reviewer-eval-20260915.json`）。
25. **LLM 准入归位（路线图 A.3）**：部署级共享 permit 从 `model_gateway` 移入 `llm_governor.slot()`——进程槽后取部署 permit，`llm_governor` 成为唯一 LLM 准入权威；适配器不再叠加同作用域包装；拒绝/装配失败会释放进程槽并向上传播（不静默绕过）。
26. **Governor 一等接口（§6.1.1）**：`GovernorPermit.finish(completed/cancelled/uncertain, evidence)`（证据白名单、重复确认幂等、审计）、`reconcile(evidence, operator)`（CAS 释放 uncertain）、`cancel_wait(request_id)`（本进程排队取消）、`snapshot()` 按资源计数。 同时补 SQLite 忙错误处理：`try_acquire` 遇写锁忙返回“未准入”（由 governor 有界循环重试），`release/renew/finish/recover` 短事务忙错误在 2s 内退避重试（多进程压测暴露后修复）。
27. **准入维度（§4.5）与队列优先级（§6.2）**：新增 deep/document/export/ocr/evaluation 资源桶（空值继承 chat/heavy）与可选 per-model 桶（`ADMISSION_MODEL_RPM_*`）；创建 turn 按 fast/deep 选桶，导出/文档计划/评测分别用 export/document/evaluation；P0–P3 队列优先级映射写入 `entry-inventory.md` §3.1。
28. **评测治理显式状态（§6.2）**：`model_gateway.governance_status()` / `reset_governance_status()` 记录 judge wrapper 装配结果；`govern_ragas_llm` 失败时标记 bypassed 并计数 `hdb.eval.governance_bypassed`；评测 summary metadata 增加 `judge_governance`（bypassed 结果不得用于生产容量/质量验收）。测试 3 例。
29. **双副本 HTTP 接管演练**：新增 `scripts/production_hardening/multi_instance_drill.py`，两个 API 进程共享存储，验证 session 跨副本可见、并发 `/start` 单所有者执行、恰好一条 `turn_end`；产物 `multi-instance-http-drill-20260915.json`（ok=true）。
30. **全组迁移演练与脚本加固**：authority 45 表 / attachments 22 表 / pipeline 4 表 / projection 4 表计数与内容哈希全部一致（`postgres-migration-drill-20260915.json`）。修复演练暴露的脚本缺陷：行序敏感哈希→多重集哈希、类型归一（TEXT 列 BLOB、布尔/实数表示）、FTS/trigram 影子表 `derived_skipped`、`ok` 纳入哈希/计数不匹配；补 `tests/test_migration_validation.py`（6）。
31. **回归数据缺口修复**：`evaluation/datasets/hardware_qa_v1.jsonl` 从既有上传副本还原（25 条原始样本，非生成数据），`hardware-database-eval validate` 通过；回归集 143 条。
32. **P3 语义冒烟**：`reviewer_eval.py --model` 用真实模型跑语义通道（10 例，6 例给出 unsupported 判断）；确定性门禁与语义通道分层评分，冒烟不改变确定性结果（status accuracy 1.0）。
33. **可观测栈实测**：Prometheus 目标 `otel-collector` up、`hdb_chat_turns_total` 等 `hdb.*` 指标端到端可查（应用→OTel→Prometheus）；执行 `POST /-/reload` 后 15 条 Hdb 告警全部生效（含 `HdbResourceUncertainEscalated`、`HdbStaleAuthorityWrites`、`HdbAttachmentWaitTimeouts`）；Grafana 已加载 `Hardware DataBase / Production Hardening` 看板。
34. **迁移回滚演练**：新增 `scripts/production_hardening/migration_rollback_drill.py`（按 runbook 在一致性快照副本上演练，不触碰在线服务），authority 组双跑：第二遍 copied=0、计数/哈希一致、源库校验和不变、派生表跳过；同时把心跳表 `worker_instances` 标记为 `transient_skipped`（瞬态状态，切换后重建）。
35. **附件编辑 e2e 修复与基线**：`e2e_runner` 身份创建适配两级角色模型（仅系统管理员注册员工、去 per-KB 授权、清理用管理员令牌）；全量 50 例实测 **50/50=100%**（gate≥90%），产物 `attachment-e2e-baseline-20260916.json`。
36. **RAGAS 质量基线数据缺口定位**：本机 RAGFlow 有 `ADAS_new`（5 文档/2234 chunks）但无电路索引、`AAA` 不存在，评测集引用的证据面不完整；已用 1 例冒烟验证 collection+scoring+judge 治理装配（assembled=true）链路可用，结论标记为“数据前置未满足，不构成质量基线”。
37. **ADAS_new RAGAS 前置修复（小样本可跑）**：新增 `scripts/production_hardening/prepare_adas_new_eval.py`（幂等）——因本机缺少 ADAS_new 的 5 个远端文档的本地部门映射（部门 96），导致 chunk 全被 `document` 过滤；脚本登记映射并把本机 EDF 索引进 ADAS_new 电路索引（20 模块）。修复后电路/文档样本均返回真实答案与证据。
38. **RAGAS 小样本测试（用户要求只做小样本）**：ADAS_new 上 n=3（文档/电路/联合）× `faithfulness`+`answer_correctness`，6/6 成功：faithfulness 0.905、correctness 0.538、evidence_consistency 1.0，`judge_governance.assembled=true`；产物 `ragas-small-sample-adas-new-20260916.json`。并发判分需 `LLM_BATCH_MAX_CONCURRENCY≥2` + `LLM_QUEUE_TIMEOUT_SECONDS=900`，否则 batch 槽排队会触发 `LLMQueueTimeoutError`；全量 25×6 判分约 3.6 分钟/指标项，留待专门预算窗口。
39. **故障隔离演练**：新增 `scripts/production_hardening/fault_isolation_drill.py`——作者/解析角色 Worker 被杀后通用聊天仍完成且健康检查 200；`RAGFLOW_BASE_URL` 不可达的副本仍可服务且通用聊天完成（检索故障 ≠ 主链路不可用）；产物 `fault-isolation-drill-20260916.json`（ok=true）。共享 RAGFlow 未被触碰。
40. **PG Checkpoint 连接池（路线图 §5.3）**：`PostgresCheckpointerProvider` 改用 `psycopg_pool.ConnectionPool`（`AGENT_CHECKPOINT_POOL_SIZE`，默认 4），16 并发线程读写实测通过；单连接回退保留。连接清单与复用现状写入 `entry-inventory.md §3.3`。
41. **回归样本扩充结论**：源用例文档 `docs/diagrams/joint_retrieval_test_cases.md` 共 17 个案例（A1–D3），`hardware_qa_v1` 的 25 条已覆盖其全部案例与变体，无未纳入的已核实用例；距 200 条目标需业务新标注，不做生成式填充。
42. **修复 governor 维护线程自死锁（演练发现）**：`start_governor_maintenance()` 在持有 `_GOVERNOR_LOCK` 时调用 `get_governor()`（同锁不可重入）→ 只要 `RESOURCE_GOVERNOR_ENABLED=true` 且 deployment 作用域，API/Worker 启动即在 lifespan 挂死。修复为先建 governor 再入锁；新增回归测试 `test_start_maintenance_does_not_deadlock` / `test_start_is_idempotent`（带 join 超时，防回归时挂死测试）。
43. **多用户多并发 A/B 实测**：新增 `scripts/production_hardening/concurrency_compare_drill.py`（隔离临时库，不影响在线服务）——`off`（legacy）vs `on`（serialize+deployment governor+共享执行器+低 RPM）：
44. **多实例维护选主（§6.3.1）**：`SqliteQuotaStore` 新增 `resource_maintenance_lease`（owner/token/expiry + CAS 接管递增 token），`start_governor_maintenance` 仅由 lease 持有者执行 `maintain`，退出时释放；补 3 个测试（选主/接管/释放、启动不挂死、幂等），避免多进程重复扫描与重复升级。
45. **测试稳健性**：`test_chat_attachment_scopes` 客户端超时 30s→60s，消除满载时的一次性偶发失败（后续全量多次通过）。
46. **G0 交付固化**：在 `feature/production-hardening-p0` 分支按主题提交 10 个 commit（配置/工具、P0a×2、P0b×2、P1、P2、P3、评测修复、文档纳管）；`/docs/` 工件用 `git add -f` 显式纳管；生成 `workspace-manifest.json`（base commit、untracked、lock/manifest hash、数据集 hash、复现命令）；修正样本数口径为 **143**（25+47+50+21）。
47. **G1 初始化竞争修复（后续方案 §6.1）**：复现 `test_multiprocess_sqlite_contention` 20 次 5 失败（失败轮 31.3s，ready 超时），根因是配额/租约库构造期 `BEGIN IMMEDIATE` 无有界重试且 busy timeout 1s，4 进程同时启动时子进程在建 ready 前退出导致父进程等 30s。修复：`SqliteQuotaStore`/`CoordinatorLeaseStore` 初始化包在有界忙重试；`ConversationService` 增加进程内初始化缓存、`_init_db` 有界重试，并把部门/`turn_sequence` 回填改为“先探测再写”。验证：多进程专项 **20/20 通过、耗时 3.3s 稳定**；G1 验收套件 34 passed；全量 2657 passed / 0 failed。
48. **G1 验收**：G1 套件 34 passed；多进程专项 20/20（耗时稳定 3.3s）；stale-owner/重复终态/重复 Artifact 由测试覆盖为 0；同会话顺序与附件超时由测试与 A/B 演练验证；干净提交基线 `20260916T114508Z-d7898b8-g1-clean-baseline`（commit d7898b8）。测试环境 P0a 开关由 `concurrency_compare_drill` 以 ON 配置实测通过。
49. **G2 验收（同机部分）**：新增多进程“LLM 部署总量不放大”用例（容量 2、4 进程、并发水位 ≤2），10/10 稳定；G2 套件 53 passed；uncertain 维护选主、证据恢复、停机未确认回收、入口 429 背压均已覆盖。跨主机 PostgreSQL 配额按修订方案归入 G3；EVAL_EMBEDDING 与供应商 RPM/TPM 保持范围外标注。
50. **G3/G4 执行准备（本机可交付部分）**：迁移回滚演练新增 `--all-groups`（快照副本逐组双跑，authority 45/attachments 22/pipeline 4/projection 4 全部计数+哈希一致、源库不变，产物 `g3-all-groups-rollback-drill.json`）；新增 `g3-cutover-checklist.md`（切换单元 A/B/C/D、同事务规则、停写/校验/回滚与演练命令）与 `provider-compatibility-matrix.md`（三类 provider 配置/实现/验证/版本与配额存储作用域）；新增 `g4-execution-pack.md`（容量配方 5/30/3 + 2× 过载、发布门槛、200 条样本模板、预算与责任）。生产切换与压测仍待环境/预算。
51. **G3 本机验收补齐 + 全量复验**：`multi_instance_drill.py` 增加 `--checkpoint-backend postgres`（两个 API 副本共用 PostgreSQL agent/authoring checkpoint + memory projection），新增“副本 B 同会话续跑”检查；SQLite 与 PostgreSQL 两种模式均通过（session 跨副本可见、恰好一条 `turn_end`、副本 B 续跑 completed、消息 4 条；PG 实测写入 checkpoints/checkpoint_writes/store），产物 `multi-instance-pg-provider-drill-20260916.json`。`migration_rollback_drill.py` 增加 `--kill-after`（第一遍 SIGKILL，窗口不足自动缩小），authority/attachments/pipeline/projection 四组从半迁移状态恢复收敛（末遍 copied=0、计数+哈希一致、源库不变），产物 `g3-migration-failure-recovery-drill.json`。G4 确定性层：对 25 条历史快照执行 `score --metric none`，evidence_consistency=1.0、forbidden_claims=1.0、gate passed，产物 `g4-deterministic-rules-20260916.*`。复验：回归套件 `20260916T124053Z-d9b55d6-g3-g4-local` 36.8s 通过；全量 `2658 passed, 7 skipped, 0 failed`（9 分 40 秒）。
52. **跨主机配额存储（PG）与全栈预演**：新增 `PostgresQuotaStore`（短事务 + 每资源 advisory xact lock、租约/uncertain/证据恢复/维护选主与 SQLite 同契约），新增配置 `RESOURCE_GOVERNOR_BACKEND=sqlite|postgres` + `RESOURCE_GOVERNOR_DATABASE_URL`（postgres 必须配 URL 且 SCOPE=deployment，保存/启动拒绝静默回退）；`tests/test_resource_governor_postgres.py` 11 例（含 4 子进程容量 2 高水位 ≤2、双副本共享预算、部署预算不被进程数放大）。`multi_instance_drill.py` 增加跨副本附件上传/下载（元数据 + 字节 hash 校验）与 `--governor-backend postgres`；全栈 PG 预演（四组迁移 + PG checkpoint/memory + PG 配额）通过，产物 `g3-fullstack-pg-drill-20260916.json`（checkpoints/checkpoint_writes/store/resource_permits 实测写入，终态 permits 清零）。
53. **50 用户多会话并发 A/B 实测（开发机口径）**：`concurrency_compare_drill.py` 支持 `--wave-size/--wait-timeout/--user-rpm/--tenant-rpm` 并修正隔离校验（用首个用户的 turn 而非完成顺序首条）。50 用户（5 波×10，峰值在途 10）各 1 turn：两模式 **50/50 完成、0 错误**；off（legacy）P50 70.2s / P95 119.5s / 队列 P95 95.7s，同会话突发 2 次重叠；on（序列化+部署 governor+共享执行器+低 RPM）P50 69.7s / P95 115.4s / 队列 P95 92.8s，同会话突发 **0 重叠、后序 queue wait 17.0s/38.9s 严格串行**，快速提交 2/5 429 且带 `Retry-After`；跨用户越权两模式均 404。产物 `multi-user-50-ab-20260916.{json,md}`。注：本轮曾暴露并修复压测脚本自身两个缺陷（on 模式开关被 queue-deadline 分支覆盖、隔离校验取错 turn），首轮无效数据已作废重跑。
   - 跨用户 3/3 完成；e2e P50 47.4s→50.1s、P95 80.4s→73.3s（同量级，串行不拖慢跨会话）；
   - 同会话突发：`off` 出现 1 次执行重叠；`on` 0 重叠、后序 queue wait 39.3s/66.8s，严格按 `turn_sequence` 串行；
   - 跨用户隔离两模式均 404；准入过载 `off` 0/4、`on` 1/4 且带 `Retry-After`。
   报告：`multi-user-concurrency-ab-20260916.{json,md}`。
## 2.1 P1 外部集成验证（2026-09-15，Docker `postgres:16-alpine`）

- `verify_checkpoint_postgres.py`：setup、put/get、fork（含 pending writes）、命名空间隔离、delete 隔离、清理全部通过（报告 `checkpoint-postgres-verification-20260915.json`）；
- `HDB_TEST_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:55432/hdb uv run python -m pytest -q tests/test_checkpoint_provider.py tests/test_memory_postgres_runtime.py` → 14 passed（无 DSN 时这些用例 skip）；
- `migrate_sqlite_to_postgres.py --group authority`：真实 `storage/auth.db` 43 张表行数/内容哈希校验通过（含新表 `conversation_execution_state`），源库只读、目标幂等 upsert。

## 3. 未关闭项与移交

- **回归集样本 ≥200**：当前 118 条；缺口不得用模型生成答案填充，需要业务标注。
- **Action 并行默认开关**：`ACTION_DAG_PARALLEL_ENABLED=false`；收据/计划/唤醒契约已交付，开启前仍需 Phase 0 基线证明串行是瓶颈（方案 §8.3/§13.3）。
- **供应商 RPM/TPM 与 embedding 治理**：见 `entry-inventory.md` §4，属显式范围外。
- **P3 模型对比评测与灰度**：确定性层已评测（全绿）；语义/质量对比与灰度设计需模型预算与业务样本。
- **Heavy Job queue wait / 30 分钟固定到达率压测 / 过载恢复 / 故障注入**：需目标环境，未执行。
- **共享文件服务、多实例接管、跨主机配额、容量/质量/SLO 采集**：按方案 §7/§9/§10 属部署环境事项，本工作区未执行。
- 既有 6 个失败已处理：4 例附件权限用例更新为两级角色模型断言，2 例过期 RAGFlow 覆盖测试删除（`deploy/ragflow/embedding_model.py` 已在 PR #49 删除）。

## 4. 复现命令

```bash
uv sync
uv run python -m pytest -q                                   # 全量（本次 2653 passed, 0 failed，eval 组安装后 7 skipped）
uv run python -m pytest -q tests/test_chat_turn_sequence.py tests/test_chat_turn_claim_fencing.py \
  tests/test_waiting_turn_timeout.py tests/test_coordinator_lease.py tests/test_worker_roles.py \
  tests/test_resource_governor.py tests/test_governor_recovery.py tests/test_shared_retrieval_executor.py \
  tests/test_provider_config_guard.py
uv run python scripts/production_hardening/regression_runner.py --label <阶段>
uv run python scripts/production_hardening/state_fingerprint.py --label <阶段> --output ...
uv run python scripts/production_hardening/governor_recovery.py --db storage/resource_quotas.db list
uv run ruff check .
```
