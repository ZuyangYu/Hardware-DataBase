# 指标目录（metric-catalog）

> 基线：`feature/fix-module-matching` commit `01c807e`（2026-09-15，PR #49 合并）
> 采集实现：`src/observability/metrics.py`（OTel，经 OTLP 导出到 Grafana/Phoenix）
> 标签白名单：`src/observability/semantic.py::METRIC_LABEL_KEYS`；未列入白名单的标签会被 `metric_attributes` 静默丢弃

## 1. 已有指标（复用，禁止重复计数）

| 指标 | 类型 | 语义 | 采集位置 |
|---|---|---|---|
| `hdb.chat.turns` | Counter | 聊天 turn 计数 | `record_chat_turn` |
| `hdb.chat.turn.duration` | Histogram / s | turn 端到端时长 | `record_chat_turn` |
| `hdb.chat.queue.wait` | Histogram / s | 聊天排队等待 | `record_chat_turn` |
| `hdb.chat.ttft` | Histogram / s | 首 token 时间 | `record_chat_turn` |
| `hdb.agent.runs` / `hdb.agent.duration` | Counter / Histogram | Agent 运行 | `record_agent` |
| `hdb.agent.retrieval_rounds` | Histogram | 检索轮次 | `record_agent` |
| `hdb.agent.stage.completed` / `hdb.agent.stage.duration` | Counter / Histogram | 阶段耗时 | `record_agent_stage` |
| `hdb.llm.calls` / `hdb.llm.duration` / `hdb.llm.ttft` | Counter / Histogram | LLM 调用 | `record_llm` |
| `hdb.worker.task.processed` / `.failed` / `.duration` | Counter / Histogram | Worker 任务 | `record_worker` |
| `hdb.queue.depth` / `hdb.queue.oldest_age` | ObservableGauge | 持久队列深度与最老年龄（chat/document_authoring/export/memory_projection） | `set_queue_state` |
| `hdb.authoring.runs` / `.run.duration` | Counter / Histogram | 文档生成运行 | `record_authoring` |
| `hdb.authoring.unit.outcome` / `.duration` | Counter / Histogram | 文档生成单元 | `record_authoring_unit` |
| `hdb.document.authoring.compatibility.events` | Counter | 兼容边界事件 | `record_document_compatibility` |
| `hdb.authoring.agent.*` | Counter / Histogram | Authoring Agent 运行与工具 | `record_authoring_agent(_tool)` |
| `hdb.document.planning.shadow.*` | Counter / Histogram | 影子规划质量 | `record_planning_shadow` |
| `hdb.export.jobs` / `.job.duration` / `.queue.wait` | Counter / Histogram | 导出任务 | `record_export_job` |
| `hdb.export.render.duration` / `.artifact.bytes` / `.downloads` | Histogram / Counter | 渲染与产物 | `record_export_*` |
| `hdb.attachment.events` / `.duration` | Counter / Histogram | 附件事件 | `record_attachment` |
| `hdb.memory.index_health` / `.projection_outbox_pending` / `.projection_lag_seconds` | ObservableGauge | Memory 投影健康 | `set_memory_*` |
| `hdb.memory.*`（job/outbox/search/extract 等） | Counter / Histogram | Memory 域指标 | `src/memory/worker.py` 等 |
| `hdb.eval.*` / `hdb.evaluation.*` | Counter / Histogram | 评测运行 | `record_evaluation*` |

## 2. 计划新增指标（按任务交付）

| 指标 | 类型 / 单位 | 标签 | 采集位置 | 任务 |
|---|---|---|---|---|
| `hdb.conversation.lock.wait` | Histogram / s | status | 会话领取 | P0-003 |
| `hdb.conversation.stale_write_rejected` | Counter / `{writes}` | operation | 权威状态拒写 | P0-003 |
| `hdb.conversation.attachment_wait` | Histogram / s | status（completed/timed_out/failed/cancelled） | `waiting_for_attachments` 终态迁移 | P0-004 |
| `hdb.resource.active` | ObservableGauge / `{calls}` | resource, scope | 配额存储观测器 | P0-006 |
| `hdb.resource.queue.wait` | Histogram / s | resource, status | 额度申请结束 | P0-006 |
| `hdb.resource.rejected` | Counter / `{requests}` | resource, reason | 准入拒绝 | P0-006 |
| `hdb.chat.reserved_capacity` | ObservableGauge / `{slots}` | resource | 配额配置观测器 | P0-006 |
| `hdb.governor.lease_lost` | Counter / `{leases}` | resource, reason | 续约/接管 | P0-006 |
| `hdb.resource.uncertain` | ObservableGauge / `{calls}` | resource | 不确定额度隔离 | P0-006 |
| `hdb.retrieval.executor.*`（queue wait / rejected / fanout） | Histogram / Counter | resource, status | 共享检索执行器 | P0-007 |
| `hdb.action.parallel.*`（layer size / merge duration / conflict） | Histogram / Counter | status | Action DAG 并行 | P2-001 |

> 状态更新（P0–P3 补齐后）：上表除以下项外均已实现并接线（2026-09-15）。
> 已实现：`hdb.conversation.lock.wait`、`hdb.conversation.concurrent_claim_blocked`（映射 `same_thread_concurrent_requests`）、`hdb.conversation.attachment_wait`、`hdb.conversation.stale_write_rejected`、`hdb.resource.active/queue.waiting/capacity/reserved/uncertain`、`hdb.chat.reserved_capacity`、`hdb.governor.lease_lost`、`hdb.agent.inflight_requests/step_count/cancel_count/tool_call_count`、`hdb.llm.active_requests/timeout_total/tokens`、`hdb.rag.active_requests/zero_hit/batch_fanout`、`hdb.retrieval.executor.*`、`hdb.memory.write_latency`、`hdb.job.execution_time/dead_letter_count`、`hdb.action.parallel.*`。
> 2026-09-15 实施补充：`hdb.resource.uncertain_escalated`（Counter，uncertain 超过升级窗口；对应 Prometheus 告警 `HdbResourceUncertainEscalated`）、`hdb.retrieval.late_evidence_rejected`（Counter，工具调用结束后到达的证据登记被拒，P0-007 说明而非重复上报）、`hdb.chat.queue_timeout`（Counter，`CHAT_QUEUE_DEADLINE_SECONDS` 超时失败，事件 `chat_queue_timeout`）、`hdb.action.receipt.reused`（Counter，durable receipt 复用）、`hdb.action.parallel.contract_missing`（Counter，并行执行未接线 receipt store 的提示）、`hdb.eval.governance_bypassed`（Counter，judge LLM 治理装配失败，评测 summary 同时记录 `judge_governance`）。会话执行状态现落在 `conversation_execution_state` 表（`src/core/conversation.py`），claim 指标口径不变。
> 实施补充（2026-09-15 第二轮）：`hdb.action.receipt.reused`、`hdb.action.parallel.contract_missing`、`hdb.checkpoint.write_latency`（observer 包裹 put/aput）、`hdb.checkpoint.fork_latency`、`hdb.checkpoint.commit_latency`、`hdb.retrieval.executor.unconfirmed`（停机 deadline 未确认调用）。
> 剩余（不可在本工作区实现，已标注原因）：
> - `checkpoint_lock_wait`：LangGraph saver 不暴露锁等待时长，`SqliteSaver` 内部串行访问；需官方 API 支持后才能采集；
> - `db_pool_active/wait`：当前为 SQLite 直连（无连接池）；池化属 P1 部署项；
> - `llm_retry_total`：LangChain SDK 内部重试不产生回调；已在 `_generate/_stream` 边界保证每次重试重新获取进程/部署 permit（`entry-inventory.md` §1），但重试次数仍无法从现成指标区分。
> - `job_wait_time`：由 `hdb.queue.oldest_age` 与 `hdb.export.queue.wait` 映射，未逐任务计时；
> - `llm_queue_wait_ms` / `rag_queue_wait_ms`：由 `hdb.resource.queue.wait{resource}` 覆盖；
> - `rag_query_latency_ms` / `rag_retry_rate`：由现有 trace/span 承担，未转指标；
> - `agent_queue_depth`：由 `hdb.queue.depth{queue="chat"}` 覆盖。

## 3. 标签规则

- 允许标签以 `METRIC_LABEL_KEYS` 为准；新增 `resource`、`reason` 等枚举标签必须先更新 `semantic.py` 白名单，否则数据被丢弃；
- 固定枚举：`resource`（llm/rag/tool/chat/heavy_job/ocr/export/memory）、`reason`（queue_full/deadline/disabled/uncertain/lease_lost）；
- user/session/turn/tenant ID、查询文本、异常原文禁止进入指标标签；
- 部署级 gauge（如 `hdb.resource.active`）只能由单一观测角色上报或由仪表盘去重，禁止对每个 Worker 上报值求和。

## 4. 看板与告警（P2-002 交付于 `deploy/observability/`）

- Chat SLO：`hdb.chat.queue.wait`、`hdb.chat.turn.duration`、`hdb.chat.ttft`、`hdb.queue.depth{queue="chat"}`；
- Heavy Job：`hdb.export.*`、`hdb.authoring.*`、`hdb.queue.*{queue=~"document_authoring|export"}`；
- 资源治理：`hdb.resource.*`、`hdb.governor.lease_lost`、`hdb.resource.uncertain`；
- 会话正确性：`hdb.conversation.stale_write_rejected`、`hdb.conversation.lock.wait`、`hdb.conversation.attachment_wait`；
- Worker 接管：`hdb.worker.task.*`、`hdb.queue.oldest_age`。
