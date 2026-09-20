# 13 · 文档生成（上）：概念、流程与 Harness

对应代码：`src/document_authoring/`（约 4.9 万行，仓库最大子系统）。
本页覆盖：业务概念、ID 与状态机、两条入口路径、`harness/`（图/agent/runtime/调度）、`planning/` 编译与确认。
渲染/持久化/ICD/API/开关见 [14-authoring-render-and-api.md](14-authoring-render-and-api.md)。

---

## 1. 业务目标

从以下输入生产**受控工程文档**（ICD 连接器/引脚文档、FPT、需求规格、通用报告）：

- 已批准**模板**（xlsx/xlsm/docx）+ 哈希绑定的分析与渲染策略，或服务端**免模板配方**；
- **冻结的来源快照**（知识库来源和/或对话附件）；
- 已接受的 **OutputSpec + DocumentPlan**（v2）或旧版 `DocumentSchema`（v1）。

保证：不可变、哈希绑定的输入（input fingerprint）；证据接地；确定性渲染；
受控文档类型的显式人工放行门；仅追加审计事件；幂等 + 租约执行。

设计原则（`models.py` 头注）：格式中立的契约分离，使「评审项不可能在运行期获得新的写入位置」。

---

## 2. 核心概念、ID 与状态机

### 2.1 DocumentTask（用户可见聚合）

- ID `document-task-<uuid4hex>`；字段：
  `tenant_id, user_id, origin∈{chat,workbench,api,legacy}, conversation_id, initiating_turn_id,`
  `project_id, knowledge_base_name, template_version_id, generation_session_id,`
  `output_spec_id/version, document_plan_id/version, work_order_id, current_run_id,`
  `current_artifact_id, artifact_ids[], status, idempotency_key, metadata`；
- 状态：`planned, needs_clarification, queued, running, waiting_human, awaiting_plan_confirmation,`
  `awaiting_release, completed, failed, cancelled, legacy`；
- 不变量：chat 任务必须有 `conversation_id` + `initiating_turn_id`；只有 `legacy` 来源可用 `legacy` 状态；
  `output_spec`/`document_plan` 的 id 与 version 必须成对出现。

### 2.2 GenerationSession（澄清/收集）

- ID `generation-session-<uuid4hex>`；`contract_version ∈ {legacy_brief_v1, output_spec_v1}`；
  旧契约必须有模板，且不能使用规划态；
- 状态：`needs_clarification, ready_to_generate, generating, completed, cancelled,`
  `awaiting_plan, awaiting_plan_confirmation, planned, blocked`；
- `GenerationBrief`：`purpose, scope, source_policy, output_policy,`
  `missing_data_policy∈{mark_tbd,keep_blank,block_generation},`
  `inference_policy∈{forbid,allow_labeled,allow_limited}, clarification_answers,`
  `unresolved_requirements, resolved_fields, allowed_derivations, confirmed, confidence`。

### 2.3 OutputSpec（生成什么，不可变）

- ID `output-spec-<uuid4hex>` + version；状态 `draft, proposed, accepted, stale, blocked`；
- `content_hash = planning_content_hash(...) = "sha256:<hex>"`——**排除生命周期字段**
  （`accepted_at/confirmed_at/confirmed_by/created_at/plan_hash/stale_at/status/updated_at`），
  递归且与顺序无关；
- 关键字段：`purpose, audience, document_type, target_identity,`
  `artifact.deliverables`（必须恰好一个 `role=primary`）、
  `layout_source`（判别式：`provided_template | system_recipe | generated_structure`）、
  `outline`（`section/paragraph/table/list/field/review_item`）、`table_requirements`、
  `source_scope`（KB/附件/项目/结构化输入/断言哈希 + 版本策略）、
  `missing_data_policy, inference_policy, approval_policy_id, accepted_recommendations`。

### 2.4 DocumentPlan（怎么执行，不可变）

- ID `plan:<output_spec_id>` + version；状态 `proposed, accepted, stale, blocked`；`plan_hash` 规范化；
- 含 `layout_contract`（`TemplateContract` 或 `StructureContract`）、`semantic_units`、
  `dependency_edges`、`coverage_contract`、`unit_tasks`、能力清单、`retrieval_specs`、
  评审/渲染/放行策略；
- `is_executable = not has_blocking_issues and not unresolved_capabilities`。

### 2.5 DocumentPlanSubmission（确认 outbox）

- ID `document-plan-submission-<uuid4hex>`；状态
  `pending, running, dispatched, waiting_human, retrying, failed, dead_letter, cancelled`；
- 唯一键：`(tenant,user,document_plan_id,version,hash)` 与 `(tenant,user,session_id,client_request_id)`；
- 租约：`lease_owner, lease_token, lease_expires_at, attempt, max_attempts=3`。

### 2.6 DocumentWorkOrder（冻结执行定义）

- ID `wo-<uuid4hex>`（KB 作用域）；项目作用域用 source set snapshot 的 `work_order_id`；
- 作用域 `scope_type ∈ {project, knowledge_base}`；项目必须有 `project_id+baseline_id`，
  KB 必须有 `knowledge_base_name` + 冻结的 `kb_scope_snapshot/source_scope_snapshot/`
  `attachment_refs_snapshot`；
- 状态：`planned, retrieving, ready_to_draft, drafting, waiting_human_input, validating,`
  `waiting_human_approval, ready_to_render, rendering, blocked, paused, complete, failed, cancelled`；
- **输入指纹 v1/v2/v3**：
  - v1 旧版原像；v2 绑定规范化 `requested_executor`；
  - v3 绑定 `output_spec_id/version/hash` + `document_plan_id/version/hash`；
  指纹**排除** `task_id/revision_id/状态/错误/时间戳/lock_version`，
  保证「关联关系变化不影响可复现性」；
- `execution_mode ∈ {internal_harness, deterministic_only, external_agent}`；
  `requested_executor` 必须一致。

### 2.7 HarnessRun（一次执行尝试）

- ID `harness-<uuid4hex>`；`run_manifest_id = manifest-<uuid4hex>`；
- 状态与转换（`harness/state_machine.py`）：

```
planned → {queued, cancelled}
queued → {running, cancelled}
running → {paused, waiting_human, failed, completed, cancelled}
paused → {queued, cancelled}
waiting_human → {running, failed, cancelled}
retrying → {queued}        # 进入 retrying 必须 lease_released=True
failed → {queued, cancelled}
completed / cancelled       # 终态
```

错误码：`harness_run_invalid_status_transition`、`harness_run_terminal_state`、
`harness_run_retrying_requires_lease_release`、`harness_run_unknown_status`；

- 租约/栅栏：`lease_owner, fencing_token, lease_expires_at, heartbeat_at`；
  `effective_executor ∈ {deterministic_rule, authoring_graph, agent_field_harness}`；
  `degraded_reasons[]`；`execution_route ∈ {legacy_schema, plan_dag}`；
  `migration_state ∈ {native, converted, legacy_terminal}`；
  `release_status ∈ {pending, blocked, needs_review, released}`；token 用量。

### 2.8 其它关键类型

| 类型 | 要点 |
|---|---|
| `AuthoringRunManifest` | 哈希绑定：输入指纹、策略/writer/prompt 版本、来源快照哈希、模板/schema/策略哈希、预算、计划/任务图身份、评审血缘哈希、`release_status` |
| `DocumentUnitDraft` | 单元产物：`unit_id, run_id, generated_by∈{managed_writer,external_agent,deterministic_rule}, content, proposed_value, typed_value, assertions, evidence_ids, validation_status∈{pending,supported,partial,unsupported,requires_human}, metadata` |
| `TypedFieldValue` | `kind∈{scalar,enumeration,table}, normalized_values, display_value, evidence_ids, rows[]`（≤10000） |
| `NodeExecutionReceipt` | `receipt_id=receipt-<hex>, node_name, unit_id, input_fingerprint, action_key, attempt, fencing_token, status∈{started,committed,failed}, output_hash/payload` |
| `EvidenceRegistryEntry` | 运行期不透明证据句柄：`evidence_id, 租户/run/work_order 作用域, snapshot hash, content_hash, source_identity, redacted_summary, reload_handle`（不含路径分隔符）, `expires_at`；KB/项目作用域**恰好绑定一个** |
| `AuthoringExecutionEvent` | 仅追加的净化审计事实（`authoring-event-<hex>`），`idempotency_key` 每 run 唯一；事件类型 26 种（`run_started … run_finalized`）；payload 禁止键 `prompt/content/evidence_content/credential/password/api_key` |
| `DocumentArtifact` | `artifact-<uuid4hex>`；`stage∈{draft_preview,review_candidate,approved_release}`；`validity_status∈{current,artifact_stale,revalidation_required}`；`policy_status∈{active,policy_obsolete}`；`access_status∈{granted,access_revoked}`；`regeneration_status∈{not_needed,recommended}`；`output_format∈{xlsm,xlsx,markdown,docx,pdf,pptx}`；`approval_subject_hash`；唯一幂等指纹索引 |
| `ArtifactRevision` | `artifact-revision-<uuid4hex>` 或由 `(task_id,work_order_id,client_request_id)` 派生的确定性 id；`request_type∈{field_update,section_update,full_regeneration}`；带父子计划引用、`plan_diff_hash`、`execution_scope_hash`、`affected_unit_ids`、`reused_unit_ids`、失效审批事件 |
| `DocumentAuthoringJob` | `document-job-<uuid4hex>`；操作 `generate_work_order/resume_work_order/convert_artifact`；状态 `queued,running,succeeded,failed,cancelled,dead_letter`；唯一 `(tenant,user,session_id,client_request_id,operation)`；资源锁 `(tenant, resource_type∈{project,knowledge_base,template,work_order}, resource_id)` 带栅栏与过期 |
| `DocumentReview` | `review-<uuid4hex>`；`pending → approved\|rejected\|changes_requested\|cancelled\|decided`；**创建幂等与决策幂等是两个命名空间** |
| `TemplateVersion` | `template-<uuid4hex>`、`template-schema-id`、schema version `"1"`；`status∈{draft,approved,obsolete}`；`renderer_policy_id`、`security_report_id` |
| `TemplateAnalysis` | `analysis-<uuid4hex>`；`status∈{ready_for_confirmation,requires_human,failed}`；激活判定 `{auto_accepted,requires_human}`；纠错产生新分析修订 |
| `IcdScopeReview` | 每个 work order 一个；`status∈{pending,frozen}`；异常解决 `include\|exclude`；阻塞异常 `connector_scope_unknown`、`connector_mapping_missing` |

---

## 3. 端到端（Path A：旧版模板工单）

1. **模板上传**（`analyze_uploaded_template`）：后缀校验 →
   `sanitize_template`（剥宏/外链/嵌入/活动 XML）→ `inspect` 安全报告（必须 `clean`）→
   持久化不可变 TemplateVersion + 消毒报告 + 每模板 RendererPolicy
   （`macro_policy=strip, external_link_policy=strip, embedded_object_policy=strip`）→
   确定性 `analyze_template` 盘点 → `LLMTemplateSuggestionProvider.suggest` → 校验建议 →
   `decide_template_activation` → `ready_for_confirmation` 或 `requires_human`；
   技术失败保留已消毒草稿并抛 `TemplateSuggestionTechnicalFailure`。
2. **确认**（`confirm_template_analysis`）：要求 `ready_for_confirmation` + `auto_accepted` +
   当前分析修订 + 内容哈希未变 + KB 写权限 → 构建区域/绑定 + 批准 `DocumentSchema`
   （LLM 提示仅在置信度 ≥0.7 时采纳，否则确定性推断/白名单）→ 原子激活。
3. **创建工单**：模板/schema/policy 审批检查；冻结 KB/附件来源作用域；创建/复用 DocumentTask；
   有 plan 引用则 v3 指纹，否则 v2；随后失败的 **shadow planning**（`DOCUMENT_PLANNING_SHADOW_ENABLED`）。
4. **预检**（`_kb_document_generation_stages`）：需求解析快照（可选）、ICD 模板画像
   （`icd_sample` 直接阻塞）、ICD 连接器作用域决策（未冻结则阻塞执行）。
5. **执行**：`run_deterministic_work_order`（纯确定性）或 `run_internal_harness`。
6. **收尾**：旧版 `_finalize_internal_harness_result` 或计划版 `_finalize_plan_internal_harness_result`；
   验证报告；ICD 引脚校验；artifact `review_candidate`。
7. **评审/放行**：`waiting_human_approval`（或阻塞单元时 `waiting_human_input`）；
   `approve_document_artifact` 产出 `approved_release` 子产物，
   绑定 `approval_subject_hash = hash(artifact_content_hash, validation_report_hash, source_snapshot_hash)`；
   受控类型（icd/fpt/requirements/specification）与修订**永不自动发布**。
8. **下载/预览/转换**：下载重验哈希与活动内容清洁；转换产出新的不可变子产物 + 自己的验证报告。

## 4. 端到端（Path B：planning v2）

1. **Intake**（`OutputSpecIntakeService`）：`start_draft` → 一次一个问题
   （顺序：`purpose → document_type → deliverables → layout_source → outline →`
   `[target_identity（ICD+提供模板）] → recommendations → missing_data_policy →`
   `inference_policy → approval_policy`）→ `interpret_answer/apply_interpreted_answer` →
   `is_complete` → `to_output_spec`。
2. **提案**（`create_document_plan_proposal`）：要求 `output_spec_v1` 会话处于
   `awaiting_plan/awaiting_plan_confirmation` 且版本匹配；编译 `DocumentPlan`；
   持久化 OutputSpec + Plan + `document_plan_proposed` 事件。
3. **确认**（`confirm_document_plan`）：哈希绑定（`expected_output_spec_hash`/`expected_plan_hash`）→
   `planning_store.confirm_submission` 单事务：plan/OutputSpec → `accepted`，会话 → `planned`，
   恰好插入一条 `DocumentPlanSubmission`；重放幂等；哈希过期/错误拒绝；outbox 插入失败整体回滚。
4. **派发**（`dispatch_confirmed_document_plan`，worker 先排空 submissions 再处理 document jobs）：
   重新读取并重新授权（模板哈希必须等于 `plan.output_spec_summary.template_content_hash`）→
   幂等物化 v3 WorkOrder（键 `document-plan:<id>:<version>:<hash>`）→ 同一预检 →
   不就绪则 submission 置 `waiting_human`（不建 job）；就绪则建 `generate_work_order` job 并标 `dispatched`。
5. **执行路由**：`select_execution_route` 仅当三个计划字段齐全**且**
   `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED` 才返回 `plan_dag`；部分绑定抛 `PlanExecutionRouteError`
   （**绝不静默回退 legacy**）；runtime 重新加载精确的已接受 plan + 持久化编译图，任何不一致即 fail-closed。
6. **Plan-DAG 运行**：`TaskGraphCompiler.compile` → `CompiledTaskGraph`
   （`graph_id=document-plan:<plan_id>:v<version>`；节点 `preflight, unit:<task>, barrier:<id>,`
   `aggregate, pre-render-review, render, post-render-review, release`）→ `TaskGraphStore` 持久化 →
   `PlanDAGExecutor` 按稳定拓扑序运行（结构节点单独跑，单元节点有界并发，复用已提交收据）。
7. **收尾**（计划路径）：覆盖评估 → 单元评审 → 确定性聚合（`DocumentModel`）→ 渲染前语义评审 →
   渲染（模板绑定或结构化配方）→ ICD 叠加 → 渲染后包校验 → `DocumentReleaseGate` →
   验证报告 → artifact → 修订子绑定 → 允许时才自动放行，否则 `waiting_human_approval`/`blocked`。
8. **修订循环**：`create_revision`（哈希绑定父产物 + 快照）编译可执行子计划 → `diff_document_plans` →
   `compile_affected_subgraph` → 父产物标 `revalidation_required` →
   `restart_work_order_for_revision` 幂等建 v3 WorkOrder（键 `revision:<revision_id>`）→
   子产物用「保留/目标变更/差异」三类证明绑定；**修订永不自动发布**。

---

## 5. `harness/agent_loop.py` —— 执行器选择与字段级 Agent

### 5.1 契约与策略校验

- `validate_execution_contract()`：schema 与 work order 的 `execution_mode` 必须一致；
  所有传入的 `requested_executor`（schema/order/call/run/manifest）必须等于它；manifest 也要匹配。
- `validate_approved_harness_policy()` 要求：status 恰为 `approved`；id/version 非空；
  工单绑定匹配；manifest 匹配；未 revoke；已冻结；未过期；`max_agent_tool_calls`/
  `max_proposal_retries_per_field` 为正；`min_agent_confidence ∈ [0,1]`；
  受限工具 profile；策略哈希匹配。
  错误码：`approved_policy_required, policy_not_approved, policy_expired, policy_revoked,`
  `policy_hash_mismatch, policy_binding_mismatch, agent_tool_not_allowed, agent_profile_invalid`。

### 5.2 Agent 工具白名单（仅 4 个）

`read_field_brief, retrieve_evidence, propose_field_value, mark_missing`。
禁止：`filesystem, file(s), shell, command, exec, subprocess, task, general-purpose(-subagent)`。
受限 profile 必须排除 `{filesystem, shell, command, task, general-purpose}`。

### 5.3 执行器选择（4 道门，fail-closed）

1. 契约一致性（不满足即抛）；
2. 已批准策略 + 回退策略哈希一致（不满足即抛）；
3. `deterministic_only` → `DeterministicRuleExecutor`（effective `deterministic_rule`）；
   `internal_harness` → `InternalGraphExecutor`（effective `authoring_graph`）；
4. `external_agent`：仅 `DOCUMENT_AUTHORING_AGENT_MODE_ENABLED` 与基础设施探测两门可**降级**
   （原因 `agent_mode_disabled` / `agent_infrastructure_unavailable`）。

`ExecutorSelection` 记录四门结果与降级原因，并**注解既有 HarnessRun**（不新建/替换 run）。
Agent thread id：`(harness_run_id, field_id)` 的稳定 SHA-256。

### 5.4 `AgentFieldHarness.run()`

- 校验 external_agent 模式 + 策略绑定；重置每执行状态；
  派生待处理字段（排除 `deterministic`/`human_only`；已提交状态
  `committed, ready_to_render, draft_persisted, completed, succeeded` 跳过）；
- `agent_tools_implemented=False` → 降级 `agent_tools_not_implemented` 并走回退；
  无 agent runner → `agent_infrastructure_unavailable` → 回退；
- 真实路径 `_run_deep_agent()`：**每个待处理字段跑一个受限 deepagents 循环**：
  `create_deep_agent(model=create_chat_model(), tools=[4 个工具], backend=StateBackend(),`
  `permissions=[], interrupt_on={"propose_field_value": True}, checkpointer=...)`；
  提示词为中文，禁止文件/命令/DB/子代理；`recursion_limit = policy.max_steps`；
  `thread_id` 每字段；`fencing_token` 透传；token 用量从 `usage_metadata` 观测并持久化；
- 循环后仍未解决的字段 → 降级 `agent_proposal_budget_exhausted` → 字段级回退
  （`InternalGraphExecutor.run_field`，否则整图）；
- `_finish()` 附加 `AgentHarnessExecutionMetadata`（有效执行器/降级原因/线程 id/回退字段/token 用量）。

### 5.5 工具边界与语义

`_run_tool`：拒绝未注册字段（`field_not_registered`）、非白名单工具（`agent_tool_not_allowed`）、
工具预算（`agent_tool_budget_exhausted`，策略 `max_agent_tool_calls` 默认 60）；
校验 lease/fencing；发 `tool_called` + `tool_succeeded|tool_rejected`（净化载荷）；
OTel `hdb.authoring.agent.tool`；指标 `record_authoring_tool`。

| 工具 | 语义 |
|---|---|
| `read_field_brief` | 字段契约（label/description/required/value_type/missing_policy/allow_derivation/capabilities/preferred roles）+ 最近 10 条检索账本摘要 + brief 约束 |
| `retrieve_evidence` | 强制每字段检索预算；构造与固定图**相同**的 `InformationRequirement`；要求 `outcome.applied_source_set_snapshot_id` 匹配冻结快照（`evidence_snapshot_mismatch`）；每条证据注册到 run registry（新 `reload_handle`）；只返回 evidence refs |
| `propose_field_value` | 提案预算 `max_proposal_retries_per_field + 1`；校验值类型 vs 冻结契约；要求 ≥1 已注册证据；逐条证据访问校验（tenant/run/snapshot）；构造 `DocumentUnitDraft(generated_by="external_agent")`；跑 `DocumentValidator` + 类型校验 + 模板污染检测；拒绝时事件 `proposal_rejected` + 问题码；**置信度 < `min_agent_confidence`** → `waiting_human`（`pending_human_event` 30 分钟过期、actor 作用域、approve/reject 动作 schema）+ `human_waiting`；否则 `proposal_accepted` + `draft_persisted` → `ready_to_render` |
| `mark_missing` | 按有效 missing policy 映射状态（`block_generation→blocked`、`mark_tbd→tbd`、`keep_blank→ready_to_render`）+ `missing_marked` |

人工决策恢复（`resolve_agent_human_decision`）：`human_decision_key(run, pending_event_id, proposal_hash, decision)`；
store 做租户/哈希/过期/一次性校验并 fence 状态；`approve` + 有 retriever → 恢复**同一个** HarnessRun
（`resume_internal_harness`）；无 retriever 则保持 `retrying` 等显式重试。

降级原因集合：`agent_mode_disabled, agent_infrastructure_unavailable, agent_tools_not_implemented,`
`agent_tool_budget_exhausted, agent_proposal_budget_exhausted, agent_lease_lost`。

---

## 6. `harness/graph.py` —— 固定 LangGraph DAG

**状态**：`DocumentAuthoringState` + `PersistedAuthoringState`——**只有 ID/计数/状态被 checkpoint**；
原始证据与域对象留在请求作用域；`_json_safe_graph_value` 限制持久化值。

**节点**：

```
load_context → plan_units → (Send 批次) retrieve_evidence → generate_draft → validate_draft
             → persist_draft → route_next_unit → {await_human | finalize} → END
```

- `_semantic_units`：`authoring_policy ∈ {managed_writer, external_agent_draft}` → `field:<id>`；
  `evaluation_mode=semantic_assisted` 的评审项 → `review:<id>`；
- **扇出**：`_send_unit_batch` 每波发 ≤ `policy.max_parallel_units` 个 `Send("retrieve_evidence")`；
  `dispatch_cursor` 推进直至等于单元数再汇合；
- **汇合/finalize**：必须有 `validate_cross_unit`；冲突单元置 `conflicting`；
  所有集合按 schema 顺序重排（产物/清单与调度顺序无关）；
- **重试语义** `_retrieve_with_budget`：≤ `max_retrieval_attempts_per_unit`；
  每次耗一步 + 一个检索轮；首轮 `success_empty` → `rewrite_query`（受 `allowed_tools`
  与 `max_query_rewrite_rounds` 门控）后重试；硬失败重试原查询；
  预算耗尽后可选**自适应恢复**（`adaptive_recovery` 工具 + `max_adaptive_recovery_rounds>0`）
  做一次放宽路由检索（**不计入检索轮预算**），恢复到的证据标低置信并路由到人工评审；
- **证据选择** `_select_field_evidence`：每单元上限（字段默认 5，最大 10）+ 可选重排
  （`rerank_evidence`）；结构化引脚事实走**确定性快路径**（跳过重排与 writer）；
  电路元数据做能力增强；
- **writer 调用**：`build_writer_request()` → `self.draft_provider(request)`，
  生产中即 `run_context.draft_with_receipt`（收据包裹）；快路径用 `DeterministicEvidenceWriter`；
- **校验**：`validate_unit_draft` → `validate_typed_field_draft`（字段）→ `detect_template_contamination`
  → 可选 `requirement_fit_check`；恢复证据强制 `requires_human`；
- **预算**：`HarnessToolPolicy.require_step/require_retrieval_round/require_tool`；
  超限 → `HarnessBudgetExceeded` → 单元 `requires_human` + 问题 `harness_budget_exceeded`；
- **人工暂停**：任一单元状态 ∈ `{requires_human, blocked, conflicting, retrieval_failed}`
  → `await_human`（run 置 `waiting_human`）。

`_run_legacy` / `_run_parallel_units` 仅留给旧集成/测试；`run()` 不再调用。
`run_field()` 把 schema 过滤到单字段供 agent 回退。

**结果** `HarnessExecutionResult`：需求、outcome、矩阵行、检索账本、草稿、单元状态、问题、
步数/检索轮数、agent token 用量，外加计划字段（路由/plan id/任务图 id/节点输出/覆盖/评审/
文档模型/前后评审/放行/产物/计数/草稿 id）。

**Writer 请求契约**：把已确认 brief 策略翻译为**规范枚举**——
`missing_or_conflicts[{kind: missing_data_policy|effective_missing_policy, policy}]`、
`allowed_derivations[{kind: labeled_inference, policy}]`；
表格契约带 `table_mode=typed_rows, expected_row_keys, expected_columns, row_key_schema, row_order,`
`duplicate_policy`；**原始用户文本永不进入请求**；
`build_writer_system_prompt()` 只输出 `<<USER_CONSTRAINTS>>...<<END_USER_CONSTRAINTS>>` 规范块。

---

## 7. `harness/runtime.py` —— 持久化运行时

`InternalDocumentHarnessRuntime` 拥有全部副作用：

**`create_run()`**：`_bind_plan_graph()` 选路由（计划路由会编译+持久化任务图）；
`build_manifest()` 冻结策略/writer/prompt、快照、模板/schema/策略哈希、预算、计划/图身份；
创建 `HarnessRun(status="queued")`。

**`build_manifest()`**：`run_manifest_id=manifest-<hex>`；`tool_policy_hash`、`document_schema_hash`、
`template_content_hash`、`source_set_snapshot_hash`、`input_fingerprint`(+version)、执行路由。

**`execute()`**：

1. 租约 claim（`lease_owner=harness-worker-<hex>`）+ 取消检查 + checkpointer
   （memory/sqlite/postgres；默认 `DOCUMENT_AUTHORING_CHECKPOINTER_PATH`）+ fencing token provider；
2. `runtime_event("run_started")`；
3. `save_progress()` 心跳 + 在栅栏租约下写进度字段；
4. `draft_with_receipt()`：`NodeExecutionReceipt(node_name="draft_ready_unit",`
   `input_fingerprint=hash(writer_provider_id+request))` → `begin_node_execution_owned` →
   已提交则直接返回存量 payload → writer 前后心跳 → 提交 payload 或失败收据；
5. `observed_retrieve()` 包裹 OTel + 指标；
6. 执行器分派：计划路由 → `_execute_plan_route`（**永不构建旧图**）；
   旧路由 → `AuthoringGraph` + `select_harness_executor`，把 requested/effective + 降级原因写回 run；
7. 执行后：合并已持久化草稿（人工恢复场景）→ 终态（任一单元 `requires_human/blocked/conflicting/`
   `retrieval_failed` → `waiting_human`，否则 `completed`）→ 带租约持久化草稿 → 末次心跳 →
   更新计数/状态 → 用证据内容哈希 + `completed_at` 替换 manifest → `run_finalized`；
8. 任何异常 → run `failed`（含错误类型/消息）并清租约。

**计划路由（`_execute_plan_route`）**：加载精确已接受 plan + 持久化图并核对
run/manifest/work-order/snapshot 身份；`_plan_execution_schema()` 在请求内适配单元到类型化 writer 边界
（永不持久化）；**修订复用**：`_plan_reuse_drafts()` 仅复用 `validation_status="supported"` 且证据注册表完整、
快照哈希一致的父草稿，`_register_reused_evidence()` 克隆不透明句柄（新 `reload_handle`）而不复制原文，
失败则回退重算；逐单元：带预算检索 → 注册证据 → writer 请求（含已接受子计划的
`revision_instruction`/`revision_edit`）→ 收据包裹草稿 → 校验 + 污染检测 → **先持久化草稿再提交收据**；
结构节点通过 `plan_node_callback` 持久化收据（release 节点可返回 artifact 并设置 `finalization_result`）；
`PlanDAGExecutor(max_workers=policy.max_parallel_units)` 运行；结果按 plan 顺序重排；
**已提交单元收据但没有持久化草稿 → `PlanExecutionRouteError`**。

**栅栏/租约**：store 的 `_require_active_lease` 拒绝陈旧 worker（`HarnessLeaseLost`）；
所有写入走 `update_harness_run_owned` / receipt-owned 方法并带 `fencing_token`；
`persist_executor_run` 只允许进度字段白名单。

---

## 8. `harness/plan_graph.py` —— DAG 调度语义

- 就绪 = 所有依赖 `committed`；
- **结构节点（kind != unit）串行执行**（barrier），让回调看到确定的依赖状态；
  就绪的单元节点按 `max_workers` 分批并发；
- 已提交收据按拓扑序重放；依赖未提交的收据 → 报「committed receipt bypasses graph dependencies」；
- 失败：节点标 `failed` 并抛 `PlanDAGExecutionError(statuses, node_id)`；
  无任何可推进节点 → 「compiled graph cannot make progress」；
- 结果按图顺序合并（与完成顺序无关）。

---

## 9. `planning/` —— 编译、确认与评审

### 9.1 Intake

- Draft 在 `to_output_spec()` 之前是普通 JSON；`_ALLOWED_UPDATE_FIELDS` 限定可合并字段；
- `next_question()` 一次一问（顺序见 §4.1），完成返回 `None`；
- `merge_answer/merge_user_answer` 要求 `expected_version`（过期 → `OutputSpecVersionConflict`）；
  `interpret_answer` 支持选项字母/中英文；`merge_missing_data_resolution` 处理缺失数据对话；
  `recommendations`/`accept_recommendation`（`recommended_defaults_v1`、`custom_policy_questions_v1`）。

### 9.2 证据与注册表

- 计划来源快照由 `AppPipeline._planning_source_snapshot`（`planning/sources.py` 适配器）构建，
  绑定进 plan 的 `source_snapshot_id/hash`；确认时重新校验；
- 执行期证据由请求作用域 provider（`evidence.py`：KB + 附件组合）获取，
  经 `_validated_evidence` / `_validate_retrieval_outcome` 对照冻结快照；
  run registry 只存不透明句柄，agent 只看到 `evidence_id`。

### 9.3 能力注册表

- 精确版本的域策略/布局适配器/渲染器注册表；`resolve()` 返回 `PlanIssue` 而非猜测；
- 内置域：`legacy_document_schema@1, generic_report@1, icd@1, fpt@1, requirements@1`；
- 布局适配器：`provided_template@1`（xlsx/xlsm/docx/markdown）、`system_recipe@1`、
  `generated_structure@1`（docx/pdf/xlsx）；
- 渲染器：`xlsx, xlsm, docx, pdf, markdown` @1。

### 9.4 编译

- `LegacyTemplatePlanningAdapter.compile()`：从 schema 字段构建语义单元（保留
  label/description/query terms/capabilities/表格列）、覆盖要求、单元任务（`action_key`/依赖/barrier）、
  布局契约，生成缺失绑定/能力的问题；`document_plan_id="plan:<output_spec_id>"`；
- `TemplateFreePlanningAdapter.compile()`：为配方构建结构化计划，校验结构 profile 门，
  不支持则给阻塞问题；
- `DocumentPlanningService.compile()` 按 `layout_source.mode` 分派；
  `persist_proposal()` 写不可变 OutputSpec + Plan + 事件；`propose_shadow()` 只编译+持久化不执行；
- 确认：`planning/store.py:confirm_submission` 校验哈希 → plan/OutputSpec `accepted` →
  会话 `planned` → 恰好一条 submission。

### 9.5 派发与风险门

- `dispatch_confirmed_document_plan()` 重新读取/授权 → 物化 v3 WorkOrder → 预检 →
  就绪才建 `generate_work_order` job，否则 submission `waiting_human`（无 job）；
- `DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED` 由 `agents/tools/document_authoring_tools.py` 消费
  （chat 工具的自动确认风险门）；
- `DOCUMENT_PLAN_DAG_ALLOWLIST_{TENANTS,DOCUMENT_TYPES,FORMATS}` 决定计划可否执行
  （不在白名单的计划带阻塞问题）。

### 9.6 评审与放行组件

| 模块 | 职责 |
|---|---|
| `coverage.py` | `CoverageEvaluator` 对照不可变覆盖契约 |
| `unit_review.py` | `UnitReviewer`：单单元有界确定性评审 |
| `document_review.py` | `DocumentReviewer`（渲染前/后）、`DocumentReleaseGate`、`DocumentReworkRouter` |
| `review_contracts.py` | `ReviewIssue`/`CoverageReport`/`UnitReviewResult` + 安全投影 |
| `artifact_review.py` | 渲染后包校验 |
| `revision.py` | 修订文本编辑解析、目标解析、补丁编译（`RevisionPatch`、`RevisionNotExecutable`） |
| `diff.py` | `diff_document_plans`、`compile_affected_subgraph` |
| `submissions.py` | 计划确认 outbox |
| `release.py` | 审批策略、门事实、放行签名、产物血缘、`ReleasePolicyService` |
| `legacy.py` / `sources.py` | 旧 brief → OutputSpec 兼容转换 / 安全来源视图适配 |

---

## 10. 单元测试（本页对应部分）

| 测试 | 保护的不变量 |
|---|---|
| `test_document_plannning_contracts.py` | OutputSpec/Plan 严格性：恰好一个 primary 交付物、判别式布局来源、重复单元/悬空边、敏感字段禁止、顺序无关哈希、可执行性、类型化表格契约 |
| `test_document_task_graph.py` | 编译稳定拓扑序、环/悬空拒绝、非可执行计划拒绝、图哈希对依赖/action_key 敏感而对映射顺序不敏感 |
| `test_document_plan_confirmation.py` | 每计划恰一条 submission、聊天流式 planned 状态、幂等重放、错误/过期哈希拒绝、需 v2 会话、模板变更拒绝、outbox 失败回滚、并发确认只成一条 |
| `test_document_plan_dag_execution.py` | 计划路由身份（run/manifest 精确引用）、标志门控显式路由、部分绑定拒绝、依赖/barrier 等待、有界扇出、收据复用/恢复、结构节点回调、计划路由不建旧图 |
| `test_document_plan_submission_worker.py` | 派发物化唯一 WorkOrder+job、预检人工门停靠无 job、权限/快照校验、submission 先于 job 排空、重试→死信、租约过期接管 |
| `test_authoring_graph_*` / `test_langgraph_authoring.py` | 重排账本、适配检查、自适应恢复 |
| `test_agent_authoring_observability.py` | agent 路径可观测性 |
| `test_document_phase3_*` / `test_document_structure_recipes.py` | 免模板配方/结构化渲染 |
