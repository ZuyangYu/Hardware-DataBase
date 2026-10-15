# 05 · Agent 查询运行时

对应代码：`src/agents/runner.py`（3646 行，核心）、`src/agents/schemas.py`、
`src/agents/orchestration/`、`src/agents/checkpoints/`、`src/agents/subagents/`、
`src/agents/claim_evidence.py`、`src/agents/scopes/`。

---

## 1. 设计定位

`MultiSourceAgentRunner` 包装 `deepagents.create_deep_agent`——底层是编译后的 LangGraph，
但**没有手写节点**：就是一个通用 ReAct 工具循环。模型自己决定：

- 调用哪些检索工具（文档 / 表格 / 电路 / 外部对话 / Wiki / 记忆）；
- 调用顺序与跳数（多跳检索是 agentic 的，不是写死的 planner）；
- 何时认为证据已足够并给出回答。

护栏只有三类，全部是**观测型**而非规划型：

1. 系统提示词里的自检规则（「证据是否覆盖全部要点，不足先补证」）；
2. 多跳深度统计进 footer（`rt.diagnostics` / `rt.queries`）；
3. `document_search` 在全库零命中时的一次 **fail-open** `balanced_route` 重试
   （丢掉朴素 source_group 硬过滤，保留冻结的 `source_names` 作用域）。

循环预算：`AGENT_MAX_RETRIEVAL_ROUNDS`（默认 3）→ `recursion_limit`（`deep` 模式 `rounds*12`，
`fast` 模式 `rounds*6`）。**不是规划器的硬上限**，只是递归预算。

---

## 2. `stream()` 主流程（分阶段）

入口（由 `AppPipeline.query` 调用）：

```python
MultiSourceAgentRunner.stream(
    query, kb_name, history=..., ctx=None, agent_thread_id="", event_callback=None,
    query_mode="deep", should_cancel=None, persist_thread=False, wiki_assist=False,
    document_context=None, document_flow=None, attachments=None, source_scope="auto",
    attachment_user_id=None, action_scope=None,
)
```

阶段与行号锚点：

| 阶段 | 位置 | 做什么 |
|---|---|---|
| ① 作用域 | `runner.py:2021-2210` | 解析 KB/附件作用域、`KbScope`、权限、附件 refs、`source_scope` 归一化 |
| ② 运行时 | `2222-2248` | 构造 `ToolRuntime`（一次性，绑定 kb/ctx/取消/事件回调/预算） |
| ③ 工具装配 | `2261-2557` | 按 `allowed_tools`/capability 过滤、注册工具、构造 action plan 模式 |
| ④ 计划/执行器 | `2559-2675` | `ConversationOrchestrator.plan` → action plan 解析/校验/执行（可选） |
| ⑤ 快捷通道 | `2687-3358` | 无模型直答路径：导出协议、文档澄清回答、附件提案审批/取消、模板分析等确定性响应 |
| ⑥ Agent 流 | `3360-3523` | `agent.stream(...)`：模型 + 工具循环；事件驱动前端轨迹 |
| ⑦ 汇总 | `3547-3646` | footer / retrieval_summary / diagnostics / token usage / claim_coverage |

### 2.1 消息构造与 checkpoint

```python
config["configurable"]["thread_id"] = attempt_thread      # 由 checkpoints 模块派生
has_state = checkpointer.get_tuple(config) is not None
messages = [{"role":"user","content":query}] if has_state else _build_messages(query, history)
```

- `persist_thread=True`（持久化 turn worker）时才挂 checkpointer；
- 无状态时由 `history` 播种最近 6 轮（`_build_messages`）；
- 有状态时**只喂当前问题**，历史来自 checkpoint——这是多轮上下文复用的关键，
  但要求 view 层不要把历史重复塞进 prompt。

### 2.2 系统提示词装配顺序（安全相关，不能乱）

```
1. 作用域行（KB 行 + 附件行：可写时附完整 attachment_edit/release/revision_history 操作规则）
2. 基础提示词（settings.SYSTEM_PROMPT 覆盖，否则内置 _SYSTEM_PROMPT），
   含 {kb_name} / {workflow} 占位（_Fast/_Deep workflow）
3. 导出协议 _EXPORT_PROMPT（仅当服务端判定导出意图时）
4. 文档流提示 _DOCUMENT_FLOW_PROMPT（document_flow=True 时）
5. 记忆上下文（<untrusted_memory>，在固定规则之后 → 不能改变证据优先级）
6. 附件预检（<untrusted_attachment>，≤12000 字符）
7. action 收据摘要（enabled 模式下执行过的动作摘要行）
8. 统一工具模式下的受控动作说明句
```

> 顺序即安全边界：**固定规则在前，不可信内容在后**；`settings._PromptDict` 机制保证
> 自定义 `SYSTEM_PROMPT` 缺少占位符时也不会被格式化炸掉（占位符原样保留）。

### 2.3 上下文自动压缩

`AGENT_MODEL_MAX_INPUT_TOKENS`（默认 65536）会在 model profile 上写入
`{"max_input_tokens": ...}`，激活 deepagents 的 `SummarizationMiddleware`
（85% 触发 / 保留 10%）。设为 0 关闭。

---

## 3. 工具装配与可见性

### 3.1 注册的工具（LLM 可见名）

| 工具 | 工厂 | 连接的服务 | 默认可见性 |
|---|---|---|---|
| `document_search` | `make_document_search` | `RAGBackend.retrieve`（RAGFlow） | 常开 |
| `circuit_search` | `make_circuit_search` | `CircuitIndexService.query` | KB 有电路数据时可用 |
| `spreadsheet_schema_lookup` / `spreadsheet_sql_query` / `spreadsheet_row_search` / `spreadsheet_cell_lookup` | `make_spreadsheet_tools` | 每 KB 表索引 SQLite | 常开 |
| `conversation_search` | `make_conversation_search` | 外部对话索引 | KB 有外部对话时可用 |
| `wiki_search` | `make_wiki_search` | `WikiService.search_pages` | 仅 `wiki_assist=True` 且 capability 过滤通过 |
| `memory_search` | `make_memory_tool` | `MemoryService.search` | **`_ALWAYS_AVAILABLE`**（通用对话也挂） |
| `list_kb_sources` | `make_catalog_tool` | `scan_kb_sources`（非检索工具） | 常开 |
| 附件工具族 | `build_attachment_tools` | `AttachmentService` / 附件检索 / 修订 | 仅当作用域允许且有 refs |
| 文档生成工具族 | `document_authoring_tools.as_tools` | `DocumentGenerationService` | 仅 `AGENT_DOCUMENT_TOOLS_ENABLED=true` |
| `attachment_*` 的 capability 映射 | `tool_assembly.TOOL_CAPABILITIES` | — | `attachment.search` / `attachment.revise` |

`ToolRuntime`（见 [06-tools-and-evidence.md](06-tools-and-evidence.md)）是所有工具共享的
**单请求**状态容器：`kb_name`、`ctx`、取消回调、事件回调、证据累积、预算、诊断、记忆上下文。

### 3.2 capability 过滤（统一工具模式）

`src/agents/orchestration/tool_assembly.py`：

- `TOOL_CAPABILITIES` 是**模型可见性边界**：未在此表中的工具会被
  `filter_tools_by_capability` 直接丢弃（fail-closed）。
- 常用映射：检索工具 → `knowledge_base.search`；`list_kb_sources` → `knowledge_base.catalog`；
  附件读 → `attachment.search`；`attachment_edit` → `attachment.revise`；
  文档写入 → `document.authoring` / `document.revise`；读 → `document.task.read`；
  控制 → `document.task.control` / `document.task.resume`；
  `resolve_icd_scope_exception` → `document.scope.resolve`；`request_document_release` → `document.release.request`。
- `_ALWAYS_AVAILABLE = {"memory_search"}`。
- `_ACTION_PLAN_NARRATION_TOOLS`：执行计划后只保留检索类 + 附件读 + 状态查询 + `memory_search`，
  **所有写入类与 wiki_search 从「叙述循环」隐藏**。
- 已知缺口：`wiki_search`、`request_attachment_release`、`attachment_revision_history`
  未登记在 `TOOL_CAPABILITIES`，统一模式下会被过滤掉（legacy 分支仍可见）。

---

## 4. 路由判定：`ConversationOrchestrator`

```python
ConversationPlan(
    schema_version="v1",
    route ∈ {chat, retrieval, attachment_analysis, document_authoring},
    intent, action, target,
    authority="backend",                 # 恒为后端权威
    routed_by ∈ {explicit, deterministic, fallback},
    document_flow: bool,
    template_required, comparison_requested, export_requested, requested_formats,
    source_scope, has_attachments, has_kb, confidence, reason_codes, allowed_tools,
)
```

判定表：

| 条件 | route | document_flow | routed_by |
|---|---|---|---|
| `explicit_flow=True` 且有文档能力 | `document_authoring` | True | `explicit` |
| `explicit_flow=True` 但无能力 | 非文档路由 | False | `fallback`（原因 `document_context_missing/expired` 或 `document_tools_unavailable`） |
| `explicit_flow=False` | 非文档路由 | False | `explicit` |
| 有文档能力 且 intent ∈ {template_generation, document_authoring} | `document_authoring` | True | `deterministic` |
| 其它 | 非文档路由 | False | `deterministic` |

非文档路由（`_non_document_route`）：

1. 有附件 且 query 命中分析动词（`分析|解析|提取|识别|总结|读取|extract|analy[sz]e|parse|summari[sz]e`）
   → `attachment_analysis`；
2. intent ∈ {`knowledge_base_qa`,`attachment_qa`,`compare`,`export`} → `retrieval`；
3. 有 KB 或附件 → `retrieval`；
4. 否则 → `chat`。

工具白名单（`_allowed_tools`）：

| route | allowed_tools |
|---|---|
| `document_authoring` | `("document_authoring","evidence")` + 有附件时 `("attachment",)` |
| `attachment_analysis` | `("attachment",)` |
| `retrieval` | `("retrieval",)` + 有附件时 `("attachment",)` |
| `chat` | `()` |

runner 侧：`document_flow_routed = conversation_plan.document_flow`；
`routed_by` 映射为旧 trace 值（`deterministic+document → "regex"`，`fallback → "explicit"`）。

`source_scope` 非法值会被静默归一为 `auto` 并附 reason `invalid_source_scope_fallback`。

---

## 5. Action Plan（结构化动作计划）

三态灰度：`CONVERSATION_ACTION_PLAN_MODE ∈ {off, shadow, enabled}`。

| 模式 | 行为 |
|---|---|
| `off` | 完全不编译 |
| `shadow` | 编译并记录/发事件，但不执行、不限制工具 |
| `enabled` | 执行；注入收据；对模型隐藏写工具（`legacy_direct_shortcuts=False`） |

### 5.1 契约

- `contracts.py` 定义计划结构（动作、依赖、约束）。
- `intent_parser.py` 把模型输出解析为 `StructuredActionPlanParser` 结果；
- **确定性高风险护栏永远优先**：多格式/不支持格式、附件精确文本替换等；
- `validate_action_plan` 拦截：未知动作、未授权、未解析引用、能力缺失、循环依赖、
  显式 `forbid_action` 约束；
- `capabilities.py` 解析 `deployed ∩ authorized ∩ scope ∩ object state ∩ constraints`，
  同一集合既决定工具可见性也用于计划校验。

### 5.2 执行与收据

- `executor.py`：`ConversationActionExecutor.execute` 只派发给
  `GovernedToolActionAdapters` 注册的 handler，且只能调用「本 turn 已挂载」的工具；
  依赖状态必须是 `completed`。
- `adapters.py`：把工具结果归一化为 receipt 状态
  （`succeeded/ok/completed → completed`；`accepted/queued/running/waiting_human → accepted`；
  `blocked/unavailable → blocked`；`rejected/denied → rejected`；`failed/error → failed`），
  并从 payload 提取类型化引用（`task_id/work_order_id/run_id/artifact_id/attachment_id/revision_id/...`）。
- `receipts.py`（P2-001 持久收据）：
  - 稳定键 `(tenant_id, session_generation, turn_id, plan_version, action_id)`；
  - `action_receipts` 表 + `action_plans` 表（SQLite 在 `AUTH_DB_PATH`，PG 在控制面 schema）；
  - `REPLAYABLE_STATUSES={failed,skipped}`、`REUSABLE_STATUSES={completed,accepted,blocked,rejected}`；
    `needs_reconciliation` 时不可重放；
  - `upsert` 用单调 revision（旧 revision 到达被忽略并返回已存值）；
  - `settle_domain_job(reference, status, ...)` 让领域任务完成后回填收据并唤醒所属计划；
  - 收据存储创建失败 → `receipt_store=None`，**绝不影响 turn**。
- `decisions.py`：把领域「待决项」聚合成 `PendingDecision`（按 `(kind, group_key)` 分组、
  blocking 优先、`decision_id="decision:{kind}:{group}"`）；策略
  `resolve_decision_policy`（`continue_with_defaults | ask_before_write | block`）。
  例如 ICD 引脚暴露异常分组，默认 `include`（continue）或 `exclude`（ask）。
- 审计：`_conversation_action_audit_fields` 把 `conversation_action_plan_mode`、
  `conversation_action_plan`、`_issues`、`_execution` 写进 `retrieval_summary`（持久可查）。

---

## 6. Checkpoint 与 attempt 语义（P0a）

代码：`src/agents/checkpoints/`。

### 6.1 Provider

| 项 | 说明 |
|---|---|
| `build_checkpointer_provider(*, backend, sqlite_path, database_url, setup=True)` | 工厂；`get_checkpointer()` 进程单例；未知 backend 归一为 sqlite |
| SQLite | `SqliteSaver` + 单连接（`check_same_thread=False`） |
| Postgres | `psycopg_pool.ConnectionPool(min_size=1, max_size=AGENT_CHECKPOINT_POOL_SIZE(4), autocommit=True)`；**每次建图新建 saver**（避免并发 run 串行化）；`PostgresSaver.setup()` 幂等 |
| 配置 | `AGENT_CHECKPOINT_BACKEND`(sqlite\|postgres)、`AGENT_CHECKPOINT_DB_PATH`(storage/agent_checkpoints.db)、`AGENT_CHECKPOINT_DATABASE_URL`、`AGENT_CHECKPOINT_POOL_SIZE` |

### 6.2 attempt（尝试隔离）语义

目的：**同一可见 thread 的并发/重试互不污染**。

```
attempt_thread_id(visible, token, attempt_id="")
    = f"{visible}::attempt-{attempt_id or token}"     # token<=0 时保留可见 id
```

`prepare_attempt(saver, visible_thread, fencing_token, *, attempt_id, resume_ref, pointer_path)`：

1. 若给了 `resume_ref`（`thread_id/checkpoint_id/checkpoint_ns`）→ `copy_checkpoint_namespace`
   精确克隆（含 pending writes）；
2. 否则若存在已提交指针 → 从指针 checkpoint 克隆；
3. 否则 `copy_latest_checkpoint(..., copy_pending_writes=True)`；
4. 记录 `hdb_checkpoint_attempts(visible_thread, attempt_thread, fencing_token, created_at)`；
5. 指标 `hdb.checkpoint.fork_latency{reason=...}`。

DeepAgents 的 `messages/files` 使用 `DeltaChannel`：最新 checkpoint 可能不包含消息正文，
需要沿 parent chain 复制各 delta 通道到最近 snapshot（或根节点）的祖先与 writes。
`fork.py` 在 fork 和成功提交时都复制这条重建链；祖先缺失时直接失败，避免静默丢上下文。
SQLite/PG 回归覆盖 snapshot 间隔 1/3/1000，以及删除旧 attempt 后再打开 provider 续接。
附件 ACL 和文档任务使用显式的 `conversation_session_id`，与 opaque attempt thread 分离。

指针表（SQLite `CheckpointAttemptStore` / PG `PostgresCheckpointAttemptStore`）：

```sql
hdb_checkpoint_attempts(visible_thread, attempt_thread, fencing_token, created_at, PK(visible,attempt))
hdb_checkpoint_pointers(visible_thread PK, attempt_thread, checkpoint_id, fencing_token, updated_at)
```

- `cas_pointer` 用带 token 条件的原子 UPSERT 拒绝回退；规范复制与 CAS 在同一跨进程锁内执行。
  SQLite 以独立 `.commit-lock` 数据库的写事务互斥，PG 用 thread 维度 advisory transaction lock；
- `commit_attempt(...)`：attempt → visible 克隆 + 指针 CAS，若更新 token 已提交则抛 `StaleAttemptCommit`，
  随后 `purge_superseded_attempts` 清理旧 attempt；
- `forget_thread_state` 删除全部 attempt + 指针 + 规范 thread（会话清空/删除时调用，
  runner 暴露 `forget_thread(thread_id)`，fail-soft）；
- `attempt_checkpoint_ref(...)` 生成 JSON 安全的 `recoverable_checkpoint_ref`
  （`backend/thread_id/checkpoint_ns/checkpoint_id/attempt_id/fencing_token/generation`），
  由 API 通过 `conv.update_recoverable_checkpoint_ref(...)` 做 CAS 落库。

### 6.3 观察器（P0-003）

`install_checkpoint_observer(saver, callback, *, thread_filter, namespace_filter)`：
猴子补丁 `put/aput`，回调 `{"thread_id","checkpoint_ns","checkpoint_id"}`，
记录 `hdb.checkpoint.write_latency`；回调异常被吞；`uninstall()` 恢复原实现。
API 侧用它把 checkpoint 引用实时登记到 `conversation_execution_state`。

> 两个后端均有共享指针、pending writes、旧 attempt 拒绝及跨进程复制/CAS 回归。
> attempt 初始化失败时 turn 以失败事件结束，禁止直接在可见 thread 执行。

---

## 7. 回答收尾与诊断

| 产物 | 内容 |
|---|---|
| `footer` | 检索概览（证据数、来源数、降级原因、检索轮数） |
| `retrieval_summary` | 机器可读摘要：`status`、`evidence`、`tables`、`agent_status`、`tool_diagnostics`、`retrieval_rounds`、`claim_coverage`、`memory_context` |
| `diagnostics` | 每工具调用：`tool_name/status/hit_count/top_k/scoped/duration` |
| `token usage` | `get_last_token_usage_summary()`（来自 model gateway 账本） |
| `claim_coverage` | `_build_claim_coverage`：按回答里的 `[n]` 与证据 `citation_number` 对齐去重，状态为 `citation_valid`，仅表示编号有效 |
| `verification` | 记录无效引用与未引用段落；`semantic_verification_status=not_evaluated`，有证据时 `grounded=null`，`conflicts/unsupported_claims/weak_claims=null`；证据引用覆盖率为 distinct cited evidence / available evidence |
| `sufficiency_status` | 无证据为 `insufficient`；有证据为 `not_evaluated`，实际语义支持与冲突由固定数据集评测验证 |

降级事件（`degraded`）的 stage：

| stage | 触发 |
|---|---|
| `tool_<name>` | 工具异常（返回空证据 + `friendly_error_message`） |
| `agent_loop` | `GraphRecursionError`（工具步数用尽）→ 追加 `_agent_step_budget_answer(rt)`（证据/来源计数 + 收窄建议），turn 不失败 |
| `attachment_search` / `attachment_visual` | 附件检索/视觉失败（视觉失败另记 `record_attachment("visual_failure","degraded")`） |

控制路径的 `stage` 状态：`blocked`/`error` 标失败原因码
（`document_flow_unavailable`、`conversation_action_execution`、`document_edf_scope_replacement`、
`document_plan_confirmation`、`document_clarification_answer`、`attachment_edit`）。

---

## 8. `claim_evidence.py`（声明/需求/覆盖词表）

用途：文档生成与项目检索共享的**类型化契约**（chat 主链不用）。

| 类型 | 关键字段 |
|---|---|
| `CapabilityName` | `entity_lookup, relationship_lookup, tabular_lookup, document_claim_lookup, revision_lookup, identity_lookup, structure_lookup, datasheet_link_lookup` |
| `ClaimOperation` | `identity, attribute, relationship, aggregation, capability, requirement, revision, explanation` |
| `Claim` | `id, text, operation, subject_terms, required_capabilities, support_mode∈{direct,composite,inference_allowed}, required, project_id, baseline_id, source_version_scope, expected_value_type, expected_unit, verification_policy_id` |
| `InformationRequirement` | `requirement_id, semantic_unit_id, claim_type, subject, predicate, object_hint, retrieval_query_terms, required_capabilities, preferred_source_roles, 项目/基线/版本作用域, expected_value_type/unit, missing_policy∈{mark_tbd,block_section,optional}` |
| `ClaimCoverage` | `claim_id, status∈{unsearched,supported,partial,conflicting,missing,retrieval_failed,access_denied,source_unavailable,requires_human}, evidence_ids, missing_capabilities, ...` |
| `RetrievalOutcome` | `requirement_id, status, evidences, source_outcomes, query_fingerprint, applied_source_set_snapshot_id, applied_region_policy_versions, retrieved_at` |

`plan_claims(question, expected_evidence=())` 只产出一个泛化 `claim_1`，
`_claim_requirements` 用关键词分类 operation/capabilities/support_mode
（revision/relationship/requirement/capability/tabular/explanation/aggregation/identity 词表），
再按 `expected_evidence` 的种类增强（`circuit_design → relationship_lookup` 等）。

真正的证据匹配在：
- chat：`runner._build_claim_coverage`（引用近似，见 §7）；
- 文档生成：`InformationRequirement` + `ProjectEvidenceRetrievalService.retrieve`（严格作用域）。

---

## 9. 子代理（`src/agents/subagents/`）

> **重要现状**：两个子代理都**没有接入 runner / AppPipeline**，只被测试与
> `scripts/production_hardening/reviewer_eval.py` 使用；默认关闭。

### 9.1 research_agent

- `RESEARCH_AGENT_ENABLED`（默认 false）；`ResearchAgent(evidence_loader=..., max_depth=1)`；
- `research(goal, scope, evidence_refs, should_cancel)`：**不调用模型**，
  纯确定性证据收集器；只接受 `evidence_refs` 白名单内的证据（「永不扩大访问」）；
  产出 `ResearchResult(findings, evidence_refs, conflicts, missing_information, depth)`。

### 9.2 reviewer_agent

- `REVIEWER_AGENT_ENABLED`（默认 false）；`REVIEWER_VERSION="reviewer-v1"`；
- 确定性检查：`unknown_citation`（`[n]` 越界）、`no_evidence`、`missing_required_fact`、
  `forbidden_claim_present`；
- 可选语义检查（注入模型时）：中文 JSON 提示词，检查 `unsupported_claims` / `evidence_conflicts`；
  文档截断 6000 字符、证据 ≤20×400 字符；异常 → `{"available": False}` 并建议重跑；
- 状态聚合：任一 `fail` → `fail`；有 warn/建议 → `warn`；否则 `pass`；
- 只读：不改产物、不绕过放行门。

---

## 10. 与 API 层的接缝

`src/api/routes/query.py::_run_turn` 关键动作：

1. 构造 `ActionExecutionScope`（dict 形式：`tenant_id/session_generation/turn_id/plan_version`），
   失败则禁用收据（`receipt_store=None`）；
2. `claim_turn_result` → 装饰 turn（token/generation/attempt）；
3. checkpoint attempt：`prepare_attempt` + 观察器登记引用；初始化失败结束 turn，不执行可见 thread；
4. 心跳线程（`CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS=10`）续租 + 写心跳；
5. 逐事件转发到 SSE（`_turn_event_payload`），把 assistant 文本增量写 `delta`；
6. 结束时 `complete_turn_result`（回答归一化后）+ 按回答文本与 summary 推导 trace 状态落审计；
7. 取消：turn 级 `cancel_requested` + Redis 快路径 + `should_cancel` 回调；
8. `POST /turns/{id}/start` 生产可为 no-op（claim + 唯一 client_request_id 保证不重复执行）。

---

## 11. 配置项汇总（Agent 相关）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_MAX_RETRIEVAL_ROUNDS` | `3` | → recursion_limit（deep×12 / fast×6） |
| `FINAL_TOP_K` | `5` | 后端默认 top_k（agent 实际用 `rt.top_k`：deep 8 / fast 5） |
| `AGENT_EVIDENCE_BUDGET` / `AGENT_ATTACHMENT_EVIDENCE_BUDGET` | `30` / `80` | 证据堆预算 |
| `AGENT_MODEL_MAX_INPUT_TOKENS` | `65536` | 上下文压缩触发（0=关） |
| `AGENT_TEMPERATURE` / `AGENT_TIMEOUT_SECONDS` | `0.2` / `120` | 模型参数 |
| `AGENT_RATE_LIMIT_MAX_RETRIES` | `4` | custom 提供商重试（ollama 为 0） |
| `AGENT_LLM_PROVIDER` | `ollama` | `ollama|custom` |
| `SYSTEM_PROMPT` / `NO_CONTEXT_PROMPT` | 内置中文 | 可覆盖；须保留 `{kb_name}`/`{workflow}` 占位 |
| `AGENT_DOCUMENT_TOOLS_ENABLED` | `false` | 挂载文档生成工具 |
| `DOCUMENT_PLANNING_V2_ENABLED` | `false` | v2 计划面 |
| `CONVERSATION_ACTION_PLAN_MODE` | `off` | `off|shadow|enabled` |
| `CONVERSATION_UNIFIED_TOOLS_ENABLED` | `false` | 统一工具集装配 |
| `ACTION_DAG_PARALLEL_ENABLED` / `ACTION_EXECUTOR_MAX_PARALLEL` | `false` / `4` | action DAG 并行层 |
| `DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED` | `false` | 低风险计划自动确认 |
| `REVIEWER_AGENT_ENABLED` / `RESEARCH_AGENT_ENABLED` | `false` / `false` | 子代理（未接线） |
| `AGENT_CHECKPOINT_BACKEND` / `_DB_PATH` / `_DATABASE_URL` / `_POOL_SIZE` | `sqlite` / `storage/agent_checkpoints.db` / `""` / `4` | checkpoint |
| `SHARED_RETRIEVAL_EXECUTOR_ENABLED` + `RETRIEVAL_*` | `false` + 8/256/4/30/10 | 共享检索池 |
| `MULTI_KB_CHAT_ENABLED` / `MULTI_KB_MAX_SOURCES` | `false` / `4` | 多库独立开关与选库上限；目前完成池生命周期与授权基础，多库问答入口待接通 |
| `HDB_WIKI_SEARCH_EXPAND` | 未设置 | 环境变量级覆盖 `wiki_search` 的 `include_linked` |

API 与 Worker 在启动和运行时启用时初始化同一个进程级检索池。多库开关不改变
旧 `document_search_batch` 的共享池/本地池分支。多库启用但池未初始化成功时，
就绪探针返回 `multi_kb_retrieval=down`，服务层以 `feature_unavailable` 拒绝使用；
停止进程按 `RETRIEVAL_SHUTDOWN_DEADLINE_SECONDS` 回收，运行中的 Python 调用不会
被强制终止。关闭多库开关立即阻止新多库调用，已运行调用仍按取消/截止边界结束。

`services/knowledge_scope_models.py` 定义独立于附件 `source_scope` 的多库选择契约。
`answer`/`compose` 有且仅有一个 `primary`，其余为 `reference`/`standard`；
`compare` 至少两个来源且均为 `peer`。来源角色只表达使用方式。
`KnowledgeScopeResolver` 按稳定 KB ID 读取真实身份、复核当前用户部门与内容权限，
任一来源拒绝即整体拒绝；快照及 selection hash 不能代替实时授权。每库上下文只携带
自己的权限，不复用另一个来源的可变 metadata。会话持久化、Agent 接线和前端仍需后续实施。

---

## 12. 排障要点

| 现象 | 排查 |
|---|---|
| 回答很慢 | 看 footer 的检索轮数/工具耗时；`AGENT_MAX_RETRIEVAL_ROUNDS` 大 → 更慢更全；LLM 队列见 [07](07-model-and-resources.md) |
| 回答空/无证据 | 工具是否被 capability 过滤；RAGFlow 是否有 completed 记录；`degraded` 事件里的 reason |
| 工具没被调用 | 提示词是否被自定义 `SYSTEM_PROMPT` 破坏；工具是否登记在 `TOOL_CAPABILITIES` |
| 递归上限触发 | 回答里出现「单轮工具调用步数已达上限」+ 建议；调大 `AGENT_MAX_RETRIEVAL_ROUNDS` |
| 多轮上下文丢失 | thread 是否持久化（`persist_thread`）；检查 DeltaChannel 的祖先 checkpoint/writes 链，不能只复制最新快照 |
| 状态错乱/重复回答 | 检查 SQLite/PG attempt 指针表与 `conversation_execution_state`；旧 owner 的 fencing token 是否被拒绝 |
