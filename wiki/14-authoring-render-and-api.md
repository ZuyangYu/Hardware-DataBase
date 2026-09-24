# 14 · 文档生成（下）：Writer、渲染、持久化、ICD 与 API

本页是 [13-document-authoring.md](13-document-authoring.md) 的续篇：
写作与渲染实现、模板子系统、持久化与迁移、ICD 工具链、HTTP API、灰度开关与测试。

---

## 1. Writer（`writers/`）

### 1.1 调用链

```
service._writer_for_policy(policy) → ManagedWriter(provider)
图/runtime → ManagedWriter.generate(request)
ManagedWriter：校验 provider 返回的 draft 属于请求的 unit/run、generated_by ∈
  {managed_writer, external_agent} → 应用服务端修订编辑（_apply_server_revision_edit）
```

### 1.2 `DeterministicEvidenceWriter`（`provider_id="deterministic_evidence_writer"`）

**不编造**：

- 单条证据 → 原文；多条 → 枚举 `[i] text` 摘要；
- 一条断言引用全部证据 id；
- 类型化取值只从显式赋值模式（`[:：=]|为|\bis\b`）提取；
- 表格值只接受服务端形态 `metadata.table_rows/table_row/row_key+cells`；
- 支持冻结引脚表；身份缺失就留空。

### 1.3 `LLMManagedWriter`（`provider_id="llm_managed_writer"`，`_MAX_LLM_ATTEMPTS=2`）

**策略顺序**：确定性连接器函数解析 → 冻结引脚表 → 结构化输出 → JSON 兼容路径 → 确定性回退。

`writer_mode` 元数据：`deterministic_connector | deterministic_pin_table | structured | json_fallback`；
回退字段 `writer_fallback=true` 与 `fallback_reason ∈ {structured_output_validation_failed,`
`structured_output_unsupported_and_json_failed, json_compatibility_failed}`。

- `_validate_draft`：非空 content + assertions + typed_value；证据 id ⊆ 提供集合；
  确定性校验器返回 `supported`；
- JSON 路径：拒绝未知/缺失顶层字段、剥代码围栏；协调器自有的字段**仅在该兼容路径**被接受并覆写；
- `_WRITER_SYSTEM_PROMPT`（版本由 `HarnessPolicy.prompt_version="1"` 表达）定义严格 5 键 JSON 契约
  与规则：不编造 id、类型化行必须用服务端 row key/列、断言需词法锚定、不得杜撰事实。

### 1.4 辅助 LLM 组件（全部 profile `authoring_auxiliary`，20s 超时、0 重试）

| 模块 | 作用 |
|---|---|
| `query_rewriter.py` | 空结果时的查询改写（受 `max_query_rewrite_rounds` 门控） |
| `evidence_reranker.py` | LLM-as-judge 证据重排 |
| `requirement_fit_checker.py` | 单元草稿与需求的契合度检查（可选工具） |
| `provider.py` | `WriterRequest` / `WriterProvider` 协议 |

---

## 2. 渲染器（`renderers/`）

### 2.1 `xlsm.py`（模板工作簿渲染）

`inspect()` → `TemplateSecurityReport`（部件哈希、宏、外链、嵌入、`active_content_status`）。

`render(template_content, regions, fill_plan, policy, security_approved, table_schemas)` 校验：

- 活动内容 vs 策略（`preserve|strip|quarantine`）；
- **标量填充**：区域已注册、区域/单元格不重复、`write_policy ∈ {deterministic_only, validated_draft}`、
  受保护角色拒绝（`formula, human_input, human_approval, locked_template, legacy_example`）、
  类公式文本拒绝（`=,+,-,@`）；
- **基线保护**：`expected_value_hash` 必须匹配当前值；覆盖非空单元格需要
  `allow_nonempty_overwrite`；非空单元格缺基线 → `PermissionError`；
- **表格填充**：注册表 schema、语义单元匹配、行数上限（仅校验过的填充可放宽
  `max_output_rows`）、未知/缺失列拒绝、typed 与 legacy 行不可混用、
  `expected_row_keys` 的范围/顺序/重复强制、逐单元格基线哈希、行定位冲突拒绝；
- 长值重复扇出护栏（同一值 ≥80 字符出现多次 → 报错）；
- 只写 inline string，其余包部件**逐字节保留**；整包 OOXML 校验；输出现场重新检查活动内容；
- 完整性清单：`changed_parts, cell_changes, table_row_operations, policy_violations,`
  `cell_policy_violations, manifest_hash`；**非空策略违规即中止渲染**。

### 2.2 `docx.py`

`inspect()` + `render()`：解析 `DocxRegionSchema` 定位符（段落/表格单元格/内容控件），
只写机器可写区域，文本替换保留原始 XML 区间，拒绝外部关系，整包校验，输出完整性清单。

### 2.3 `structured.py`（免模板 OOXML）

- `StructuredDocxRenderer`（python-docx）、`StructuredPdfRenderer`（reportlab，不变画布）、
  `StructuredXlsxRenderer`（手搭最小 OOXML 包，inline string，规范化 ZIP）；
- 校验：配方可用、格式支持、绑定 recipe/plan 哈希匹配、绑定**按顺序与种类覆盖每个块**、
  组件已注册、布局提示限定 `{level, emphasis, orientation, keep_with_next}`、
  profile 限制（`max_components/max_text_chars/max_table_rows/max_table_columns/max_artifact_bytes`）；
- 表格契约：行键非空唯一、期望范围完整、类公式文本拒绝、XML 文本净化；
- `validate_structured_artifact()` 独立解析产物，拒绝活动/外部部件与 PDF JavaScript 标记；
  `visual_baseline_fingerprint() = "sha256:<hex>"`（format+bytes）。

---

## 3. 模板子系统

### 3.1 消毒（`template_sanitizer.py`，fail-closed）

`sanitize_template(content, source_format) → SanitizedTemplate(content, format, removed_parts, removed_relationships)`

| 限制 | 值 |
|---|---|
| 压缩包 | ≤128 MiB；条目 ≤10000；单条目 ≤64 MiB；解压总量 ≤256 MiB；压缩比 ≤500（≥1 MiB 文件）；XML 部件 ≤16 MiB；XML 深度 ≤256 |

- ZIP 路径校验（无绝对路径/反斜杠/NUL/`//`/`./`/`..`）；重复条目拒绝；
- XML 安全：DTD/实体拒绝（`_ForbiddenXmlConstruct`）；元素深度上限；
- **删除**：路径片段（`/activex/, /ctrlprops/, /customui/, /embeddings/, /externallinks/, /macrosheets/`）、
  活动关系类型（`activexcontrol, control, ctrlprop, customui, externallink, macrosheet, oleobject,`
  `vbadata, vbaproject`）、活动 content-type 标记（`activex, controlproperties, externallink,`
  `macroenabled, macrosheet, oleobject, vba, visio`）、活动 XML 元素
  （`control(s), externalreference(s), object, oleobject(s)`）、VML 活动部件；
  剪除关系消费者；把主部件 content type 归一（`xlsm → xlsx` 等）；构造后重新校验整包。

`ooxml.py`：`sanitize_xml10_text()`（C0/未配对代理/U+FFFE/U+FFFF → U+FFFD）；
`validate_ooxml_package()`（ZIP 完整性、每种格式必需部件、每个 `.xml`/`.rels` 可解析、
`mc:Ignorable` 前缀已声明）。

### 3.2 盘点（`template_analysis.py` / `template_analyzers.py`）

`analyze_template(content, format)` 是**确定性、仅包内**（无 LLM）的：

- xlsx：单元格、共享字符串、合并、隐藏列、样式保护/指纹、邻居半径 2、≤256 字符预览；
- docx：段落/表格/内容控件；
- 产出 `TemplateAnalysisUnit`：`writable`、`blocked_reason`、
  `value_kind ∈ {blank,text,number,boolean,formula,error}`、
  `structural_role_hint ∈ {unknown,section_header,fixed_label,placeholder,sample_value,scalar_input,`
  `value,table_header,table_body,layout_blank}`、`candidate_for_auto_fill`、`value_hash`；
- `workbook_cell_coordinates()` 校验 A1 引用（XFD/1048576 上限）；
  `workbook_value_hash()` = 规范 JSON（含 null）的 SHA-256；
- `infer_field_contract(suggestion, units)` 从标签/表头/邻居**确定性**推断
  `value_type, required_capabilities, preferred_source_roles, missing_policy, allow_derivation,`
  `description, table_columns`。

### 3.3 建议（`template_suggester.py`）

- `LLMTemplateSuggestionProvider` 只发送安全盘点（**不含 OOXML 字节/路径**），
  以 **每批 50 个单元** 分块（`_SUGGESTION_CHUNK_SIZE`），严格 JSON 契约
  （`TemplateSuggestionItem: semantic_unit_id, label, target_unit_ids, retrieval_terms,`
  `confidence, overwrite_basis` + 可选建议性提示）；
- 单块失败只丢该块；模型 `create_chat_model(profile="template_suggestion")`（60s 超时），
  走 `invoke_structured`；
- `TemplateSuggestionTechnicalFailure` 保留消毒草稿供审计；技术失败把分析置 `failed` 且建议为空。

### 3.4 激活（`template_activation.py`）

`decide_template_activation(analysis, policy)`，策略
`TemplateActivationPolicy(min_mapping_confidence=0.90, max_target_ratio=0.20,`
`max_nonempty_overwrite_ratio=0.0, accept_ai_recommendations=<flag>)`：

- 硬拒绝原因：`mapping_conflict, scalar_target_fanout, repeating_table_requires_schema,`
  `formula_target, fixed_label_target, table_header_target, layout_blank_target,`
  `missing_semantic_context, nonempty_target_not_placeholder, destructive_target_ratio`，
  以及 AI 评审类（`low_mapping_confidence` 等）；
- AI 推荐模式（`DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS`）把「仅评审」原因转为审计告警，
  但冲突/受保护/畸形映射仍然致命；
- 结果 `TemplateActivationDecision{status∈{auto_accepted,requires_human}, reason_codes,`
  `suggestion_ids, metrics{total_unit_count,target_count,nonempty_target_count,target_ratio,`
  `nonempty_overwrite_ratio,min_confidence}}`；
- 确认（`confirm_template_analysis`）构建 `DocumentSchema`（`status=approved`；
  有建议则 `execution_mode=internal_harness`，否则 `deterministic_only`）、
  区域（`WorkbookRegionSchema`/`DocxRegionSchema`）与 `TemplateUnitBinding`，然后原子激活；
- 人工评审路径：`get_template_analysis_for_review`、`correct_template_analysis`
  （哈希绑定、过期校验、锁定/覆盖目标校验、创建**新分析修订** + 新激活判定）；
- 字段契约随建议持久化（`DocumentFieldSchema`）：`field_id=semantic_unit_id`、
  检索/验证策略 id、`authoring_policy="managed_writer"`、表格契约字段；
  LLM 提示只在置信度 ≥0.7 且通过 `contract_registry` 白名单时采纳；
- 免模板路径用配方哨兵：`template_version_id="system-recipe:<recipe>@<version>"`、
  `template_schema_id="system-recipe-schema:<recipe>@<version>"`。

---

## 4. 持久化

### 4.1 `work_order_store.py`（`document_authoring.db`）

模式：payload JSON + 可索引的身份/状态列；`PRAGMA foreign_keys=ON`；autocommit（`isolation_level=None`）；
30s 超时。

表清单（30+）：

```
template_versions, template_analyses, template_analysis_revisions, template_security_reports,
template_sanitization_reports, renderer_policies, document_schemas, workbook_regions, docx_regions,
template_unit_bindings, deterministic_rule_specs, harness_policies, document_work_orders,
knowledge_base_source_snapshots, document_icd_scope_reviews, evidence_matrices,
authoring_run_manifests, harness_runs, document_unit_drafts, harness_checkpoints,
node_execution_receipts, authoring_execution_events, evidence_registry, legacy_template_claims,
validation_reports, document_artifacts, document_human_events, document_outbox_events,
document_work_order_deletion_audits
```

关键约束：

- `document_work_orders` 唯一 `(tenant, scope_key, idempotency_key)`；
- `document_unit_drafts` PK `(harness_run_id, unit_id)`；
- `node_execution_receipts` 唯一 `(run, node, unit, input_fingerprint)` + 部分唯一 `(run, action_key)`；
- `authoring_execution_events` 唯一 `(run, idempotency_key)`；
- `evidence_registry` PK `(run, evidence_id)`；
- `document_artifacts` / `document_human_events` 用 `json_extract` 建幂等指纹唯一索引；
- `document_outbox_events` 唯一 `event_key`。

产物/模板字节存 `STORAGE_DIR/document_authoring/`，原子写、读取验哈希
（`read_artifact_content_for_validation` vs `read_artifact_content`）。

内置行为：模板内容哈希校验、`activate_template_analysis` 事务、工单幂等、
run 租约 claim/心跳/栅栏/重试排队、`resolve_harness_human_decision` 一次性校验、
收据 begin/commit/fail（带栅栏）、草稿带租约保存、执行事件幂等追加、证据注册、
产物保存（后缀 + 完整性）、outbox 投影、终态工单删除审计。

库内即席迁移：`_migrate_docx_regions`（复合主键重建）、`_migrate_document_work_orders`
（加 scope 列并回填 `project:<id>`）、`_migrate_node_execution_receipts`（加
`action_key/attempt` + 部分唯一索引）、模板分析修订回填。

### 4.2 `planning/store.py`

表：`document_output_specs (output_spec_id, version) PK`、
`document_plans (document_plan_id, version) PK`、`document_planning_events`（唯一 task+幂等键）、
`document_plan_submissions`（两个幂等唯一索引 + 队列索引）。
`confirm_submission()` 是唯一的哈希绑定接受事务；`accept_plan_for_revision()` 支持修订子计划；
`mark_plan_stale()`；`_require_owner` 归属校验；每次读取都重新校验 payload 身份/哈希。

### 4.3 `job_store.py`（`AUTH_DB_PATH`）

表：`document_authoring_jobs`、`document_authoring_job_outbox`、`document_resource_locks`（WAL）。
原子操作：幂等 `create_job`（载荷/工单/任务不同即冲突）、带资源锁的 `claim`（lease token）、
`heartbeat`、`complete`、`fail`（退避 → dead letter）、`cancel`/`cancel_session`、`cleanup_session`、
资源锁 acquire/renew/release（带栅栏）、队列指标。

### 4.4 `tasks.py`

表：`document_tasks`（按 owner 的幂等唯一）、`document_task_events`、`conversation_document_states`、
`document_task_stream_events`（SSE 投影）。
`attach_work_order/advance_work_order/attach_run/attach_artifact/update_status/bind_plan/clear_plan`
维护用户聚合；每次变更都追加事件。

### 4.5 其它

- `artifact_revisions`（`(task, client_request_id)` 部分唯一 + task/parent 索引）；
- `document_reviews` + `document_review_decision_commands`（创建与决策两套幂等命名空间）；
- `document_generation_sessions` + `document_generation_messages`（模板可空迁移）。

### 4.6 迁移框架（`migrations/runner.py`）

- `MIGRATION_ID="0001_harness_run_state"`、`SCHEMA_VERSION=1`；账本表
  `schema_migrations`、`document_authoring_migration_ledger (outcome∈{converted,legacy_terminal})`、
  `document_authoring_run_lineage`；
- 迁移域：`harness_checkpoints → harness_runs`（+ receipts/orders/manifests）；
  **旧 checkpoint 表永不删除或重写**；
- 安全性质：所有校验/对账在写事务之前；迁移前备份并验证（文件哈希 + `integrity_check` + 逐表摘要）；
  dry-run 只读；apply 由账本标记保证幂等；源/目标表摘要对账；收据键冲突即中止且无部分写入；
  SQLite 写失败回滚 schema+行；apply 后拒绝新的 checkpoint 写入；
  `reverse_migration_drill` 只读；`rollback_migration` 在取安全备份后恢复已验证备份；
- 转换契约（`harness_run_state.py`）：只有业务字段完整保留且版本化图状态通过 schema/尺寸校验
  （可选后端往返）才记 `converted`，否则 `legacy_terminal` 并带稳定原因
  （`legacy_payload_invalid, legacy_payload_missing_identity, legacy_active_lease_unverifiable,`
  `legacy_active_lease_expired, legacy_graph_state_size_exceeded, legacy_checkpoint_round_trip_failed`）；
  `GRAPH_STATE_VERSION=1`；
- CLI：`migrate` / `run_migration` / `rollback_migration` / `reverse_migration_drill`。

---

## 5. 需求解析器与 ICD 工具链

### 5.1 `requirement_resolver.py`（纯确定性边界对象）

- 输入：`document_schema` 或显式 `field_contracts`、`evidence`、`project_context`、
  `available_sources`、`generation_policy`；**不用模型/检索/writer**；
- 逐契约匹配证据（字段别名 `field_id,target_id,field,field_name,semantic_unit_id,unit_id`），
  收集候选值（上下文、结构化证据 metadata、显式赋值模式），检测上下文与证据冲突；
- 可用性码：`required_source_unavailable`、`required_capability_unavailable`；
- 覆盖状态：`supported, partial, missing, conflicting, retrieval_failed, source_unavailable, access_denied`；
- 解析原因：`context_evidence_conflict, multiple_candidates, evidence_without_candidate, no_evidence,`
  `partial_evidence, <可用性码>`；
- **只有一个无歧义值才解析成功**，否则 `UnresolvedRequirement`（字段/标签/原因码/覆盖/候选/风险/
  有效默认策略/允许的默认策略/是否需澄清）；
- 策略强度：`optional=keep_blank < mark_tbd < block_section=block_generation`；
  别名 `ask/clarify/user_input → ask`；
- 输出 `RequirementResolutionResult{unresolved_requirements, resolved_fields, coverage_by_field}`。

### 5.2 ICD 生成（`icd_generation.py`）

证据驱动、模板驱动的工具函数：`connector_refdes_from_front_view_template`、`is_example_sheet_name`、
`build_front_view_fills`、`render_icd_front_views`、`render_icd_pin_table`、`build_connector_rows`、
`load_selected_connectors`、`extract_fpt_pin_notes`、`extract_project_metadata`、`generate_icd_workbook`。

`render_icd_pin_table`：把冻结引脚映射叠加到工作簿（替换模板示例行），
支持可选规则推断（仅当 brief `inference_policy ∈ {allow_labeled, allow_limited}`），
注入目标元数据（产品/客户/PCB/线束连接器/位置）与证据支撑的引脚功能；
返回内容 + 完整性清单 + 问题。`render_icd_front_views` 用同一份冻结映射填连接器正视图插槽
（布局从表头标签发现：pin number/管脚号、pin definition/管脚定义、board pin labels）。

服务集成：`_apply_icd_front_view_layout` 仅在存在**完全冻结**的 ICD 作用域评审且格式为 xlsx/xlsm 时
在渲染后执行；清单用 `_combine_icd_overlay_manifests` 合并（base+overlay 清单哈希 + after 内容哈希）。

### 5.3 ICD 对比（`icd_comparison.py`）

- `compare_workbooks(reference, generated)`：按中英文表头发现引脚表
  （`_PIN_HEADERS`/`_DEFINITION_HEADERS`/`_FUNCTION_HEADERS`/`_NOTICE_HEADERS`/`_CONNECTOR_HEADERS`/
  `_LOCATION_HEADERS`），计算 match/mismatch/reference-only/generated-only + 功能/告示质量 + `parity`；
- `normalize_pin_definition_workbook()`：行键 `(connector, pin_number)`，必需列
  `connector, pin_number, pin_definition`，含 function/notice/证据绑定与重复；
- `compare_pin_definition_workbooks()` 指标（版本 `icd-pin-definition-v1`，方向
  `not_below_baseline`/`not_above_baseline`）：
  `row_key_precision, row_key_recall, row_key_f1, required_column_completeness, exact_order_rate,`
  `relative_order_rate, duplicate_rate, missing_row_rate, extra_row_rate, cross_field_consistency_rate,`
  `evidence_support_rate, physical_protection_rate`；
- `inspect_ooxml_package()`/`compare_ooxml_packages()`（版本 `icd-ooxml-package-v1`）：
  部件完整性、VBA 保留、外部/活动内容；无包时 `physical_protection_rate` 为未知。

### 5.4 校验/作用域/画像

- `icd_validation.validate_icd_pin_set(mappings, artifact_content, format)` →
  阻塞问题码（`icd_pin_missing`, `icd_pin_definition_mismatch`, `icd_scope_unresolved` 等）；
  服务层 `_append_icd_pin_validation` 还对 `icd_sample` 模板追加 `icd_formal_template_required`；
- `icd_scope_decision.build_icd_scope_decision(...)`：冻结 EDF 已声明引脚，
  只自动采纳**直接支持**的映射，产物异常
  （`connector_scope_unknown`、`connector_mapping_missing` 为阻塞）带建议动作/用户指令/候选位号；
  `effective_frozen_pin_mappings(review)` 应用 include/exclude 决议；
- `icd_profile.classify_icd_template(content, format)` → `kind ∈ {icd, icd_sample, ...}` + 连接器块/问题。

---

## 6. 兼容性边界（`compatibility.py`）

- 记录 `legacy_direct_execution | plan_backed_execution | auto_confirmation_attempt | new_write | legacy_write`；
- `DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED=true` 时，`CompatibilityClosureError`
  阻止直接 schema/WorkOrder 写入，除非能校验已接受的 OutputSpec+DocumentPlan 引用；
- 就绪判定要求 ≥2 次发布且零 legacy 写入；
- `create_sqlite_backup`/`restore_sqlite_backup` 校验文件哈希 + `PRAGMA integrity_check`；
- 兼容性 DB 默认 `storage/document_authoring_compatibility.sqlite`。

---

## 7. 其它组件

| 文件 | 作用 |
|---|---|
| `validator.py` | 确定性校验：草稿、类型化值、表格契约、模板污染、跨单元一致性 |
| `aggregation.py` | 从已接受草稿 + 单元评审**确定性**聚合 `DocumentModel` |
| `document_model.py` | 严格格式中立模型：Section/Paragraph/List/TypedTable/CrossReference 块 + 引用 + 缺失项 |
| `render_bindings.py` | `RenderBindingResolver` + `LegacyFillPlanAdapter`：语义块 → 服务端区域/绑定 id |
| `table_contracts.py` | 从盘点单元格推导可重复表的写入权威（`repair_repeating_table_targets`、`table_schema_from_targets`） |
| `contract_registry.py` | 推断字段契约的白名单注册表（值类型/能力/角色/missing policy） |
| `deterministic_rules.py` | 版本化规则注册表 + 无副作用执行器（exact_match、set_compare、range_check、regex_check、existence_check、count_compare、derived_calculation） |
| `template_mapping_review.py` | 非破坏性映射同意准备 + 显式评审应用 |
| `template_progress.py` | 模板上传管道进度与阶段上报 |
| `conversion.py` | 已填充产物的确定性语义转换（PDF/PPTX） |
| `artifact_preview.py` | Office 产物的有界只读预览 |
| `chat_context.py` | 版本化、服务端持有的对话上下文 |
| `circuit_capabilities.py` | 从模板字段语义推断电路检索能力 |
| `retriever_registry.py` | 能力感知的检索分派 + 跨单元证据缓存 |
| `evidence.py` | 文档生成共享的证据 provider |
| `reviews.py` | 任务绑定的评审记录 + 决策命令幂等 |
| `pilot.py` | 可重复的离线生产试点证据 |
| `worker.py` | 单实例 ThreadPool 后台执行器（`bg-<hex>`） |
| `generation_sessions.py` | 持久化澄清会话 + `GenerationBrief` + OutputSpec v2 会话生命周期 |

---

## 8. API Surface

路由：`src/api/routes/document_generation.py`（挂 `/api/v1`，tag `document-generation`，
全部拒绝 system_admin；`_ctx` 读 / `_write_ctx` 写 + `document_task_origin="workbench"`）。

| 方法与路径 | 后端调用 |
|---|---|
| POST `/document-generation/templates/analyze` | `analyze_uploaded_template`（可按 `DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES` 自动激活） |
| POST `/document-generation/templates/analyze-attachment` | 同上，来源为对话附件 |
| GET `/templates/{analysis_id}/review` | `get_template_analysis_for_review` |
| POST `/templates/{analysis_id}/correct` | `correct_template_analysis` |
| GET `/templates/{template_version_id}/sanitization` | 消毒摘要 |
| POST `/templates/{analysis_id}/confirm` | `confirm_template_analysis` |
| GET `/document-generation/options` | KB 生成选项（仅 approved 基线/模板/schema/policy） |
| POST `/document-generation/sessions` | 创建会话（legacy brief 或 v2 intake） |
| GET `/sessions/{session_id}` | 会话详情 |
| POST `/sessions/{session_id}/plan-proposals` | `create_document_plan_proposal` |
| GET `/sessions/{session_id}/plan-proposals/{plan_id}/versions/{version}` | `get_document_plan` |
| POST `/sessions/{session_id}/plan-confirmation` | `confirm_document_plan`（哈希绑定） |
| POST `/sessions/{session_id}/messages` | `answer_document_generation_session` |
| POST `/sessions/{session_id}/confirm` | 旧版 brief 确认 |
| POST `/document-generation/work-orders` | `prepare_knowledge_base_document_generation` |
| GET `/work-orders` / GET `/work-orders/{id}/status` | 列表 / 状态投影（含待决项） |
| POST `/work-orders/{id}/generate` | `submit_knowledge_base_document_generation` |
| POST `/work-orders/{id}/resume` | `resume_knowledge_base_document_generation` |
| DELETE `/work-orders/{id}` | 删除 |
| GET/POST `/work-orders/{id}/icd-scope-review` / `icd-scope-resolution` | 作用域评审与冻结 |
| POST `/harness-runs/{run_id}/pause` / `cancel` | 运行控制 |
| GET `/artifacts/{id}/preview` / POST `/convert` / GET `/download` | 预览/转换/下载 |
| POST `/artifacts/{id}/feedback` / `/approve` / `/release-request` | 反馈/批准/放行请求 |
| GET `/tasks/{task_id}/projection`、`/tasks`、`/tasks/{id}/reviews`、`/reviews/{id}`、POST `/reviews/{id}/decision` | 任务与评审投影 |
| GET/POST `/tasks/{id}/revisions`、GET `/revisions/{id}`、POST `/revisions/{id}/complete`、POST `/tasks/{id}/resume` | 修订服务 |
| GET `/chat-tasks/current` / `/chat-tasks/events`（SSE） / `/chat-tasks` | 对话任务流投影（`_safe_chat_task_*` 消毒） |

任务模型见 [13](13-document-authoring.md#26-documentworkorder冻结执行定义) 的 job 段；
`/exports` 与 `/artifacts/document/{id}` 的统一协议见 [16](16-exports-and-external-conversations.md)。

---

## 9. 配置项与预算

### 9.1 灰度开关

| 标志 | 默认 | 作用 |
|---|---|---|
| `DOCUMENT_AUTHORING_AGENT_MODE_ENABLED` | false | external_agent 特性门（第 3 道门） |
| `AGENT_DOCUMENT_TOOLS_ENABLED` | false | chat 内文档工具 |
| `DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND/_PATH/_DATABASE_URL` | sqlite / `storage/document_authoring_checkpoints.sqlite` / `""` | 生成侧 checkpointer |
| `DOCUMENT_TASK_WRITE_ENABLED` / `_READ_ENABLED` / `_STREAM_ENABLED` / `_ASSOCIATION_REQUIRED` | true/true/true/false | 任务投影写/读/SSE |
| `DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED` | false | 预检中的需求解析快照 |
| `DOCUMENT_PLANNING_SHADOW_ENABLED` | false | 工单创建后影子计划编译（fail-soft） |
| `DOCUMENT_PLANNING_V2_ENABLED` | false | v2 chat 工具面/收集路径 |
| `DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED` | false | 低风险计划自动确认 |
| `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED` | false | 计划 DAG 执行路由 |
| `DOCUMENT_PLAN_DAG_ALLOWLIST_{TENANTS,DOCUMENT_TYPES,FORMATS}` | 空 | 精确 CSV 白名单（支持 `*`） |
| `DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED` + 4 个白名单 | false / 空 | 免模板配方执行 |
| `DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED` | false | 关闭 legacy 直写 |
| `DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES` | false | 上传端点自动激活 |
| `DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS` | false | AI 建议转告警 |
| `DOCUMENT_AUTO_PUBLISH_VERIFIED` | false | 自动发布已验证候选（受控类型/修订除外） |
| `DOCUMENT_PREVIEW_RETRIEVAL_ATTEMPTS` / `_BACKOFF_SECONDS` | 3 / 0.5 | 预览检索重试 |
| `DOCUMENT_AUTHORING_JOB_LEASE_SECONDS` / `_BATCH_SIZE` / `_BATCH_TIME_BUDGET_SECONDS` | 300 / 4 / 10 | 任务 worker |
| `ADMISSION_DOCUMENT_RPM_PER_USER` / `_PER_TENANT` | `""` | 准入限流 |

### 9.2 Harness 策略预算（`models.HarnessPolicy`，仅 approved 行生效）

```
max_steps=40, max_retrieval_rounds=2, max_retrieval_attempts_per_unit=2,
max_units_per_run=20, max_parallel_units=3 (1..8), max_retries=1, lease_seconds=60,
max_query_rewrite_rounds=1, max_adaptive_recovery_rounds=0,
max_agent_tool_calls=60, max_proposal_retries_per_field=2, min_agent_confidence=0.7,
backend_profile="restricted"
allowed_tools 默认: retrieve_evidence, draft_ready_unit, validate_unit_draft,
  detect_template_contamination, validate_cross_unit, rewrite_query, rerank_evidence
  （可选 requirement_fit_check, adaptive_recovery）
excluded_tools 默认: filesystem, shell, command, task, general-purpose
```

自动生成的 schema 策略：id `schema-<schema_id>-<version>-managed-writer[-parallel-N]`、
version `units-<n>-attempts-2-rewrite`、`max_steps=2+n*(2+4)`、
`lease_seconds=max(300, n*120)`、writer `llm_managed_writer`；
硬上限 `_MAX_AUTO_HARNESS_UNITS=500`、`_MAX_AUTO_HARNESS_RETRIEVAL_ROUNDS=1000`、
`_MAX_AUTO_HARNESS_STEPS=3600`。external_agent schema 只用 4 工具白名单。

### 9.3 模型使用

| 场景 | profile | operation |
|---|---|---|
| 托管写作（结构化/JSON） | `default` | `document_authoring` |
| 模板建议 | `template_suggestion`（60s） | `invoke_structured` |
| 查询改写 / 证据重排 / 契合检查 | `authoring_auxiliary`（20s, 0 重试） | — |
| external agent 循环 / 服务层 writer 回退 | `default` | — |

版本号：`prompt_version="1"`、`validator_version="p2b-1"`、`renderer_version="p2a-1"`。

### 9.4 结构上限

- 草稿/产物行 ≤10000；`TypedTableRow rows ≤10000`；
- 计划模型：outline ≤10000、table requirements ≤10000、依赖边 ≤100000、unit tasks ≤100000、
  `max_attempts 1..20`、`timeout_seconds 1..86400`；
- 每字段 `max_evidence_items 1..10`（默认 5）；agent 检索 top_k 1..10；
- 结构化配方 profile：`generic-report`（256 组件 / 200k 字符 / 10k 行 / 32 列）、
  `structured-table`（128 / 100k / 10k / 100）。

---

## 10. 测试映射（补充 [13](13-document-authoring.md#10-单元测试本页对应部分)）

| 测试 | 不变量 |
|---|---|
| `test_template_analysis*.py` / `test_template_suggester.py` / `test_template_activation.py` | 盘点确定性、分块建议、激活策略 |
| `test_template_sanitizer.py` / `test_ooxml_validation.py` | 消毒 fail-closed、包校验 |
| `test_document_authoring_safety.py` | 绑定/填充计划安全：标量多目标拒绝、占位基线 + 覆盖许可冻结、多目标 legacy 绑定拒绝、无类型值忽略、枚举去重、低置信拒绝、显示值优先、互补断言不算冲突 |
| `test_document_authoring_parallel.py` | 并发策略默认 3/上限 8、manifest 冻结并发、结构化引脚快路径 |
| `test_document_authoring_durable_resume.py` | 批准入队同一 run 并重放同一 job；拒绝不产生 resume |
| `test_document_authoring_migration.py` | 迁移幂等、dry-run 只读、备份验证、回滚与反向演练 |
| `test_document_reviews.py` / `test_document_artifact_review.py` / `test_document_artifact_conversion.py` | 评审持久化与幂等、subject 哈希绑定、放行一次、包校验、转换 |
| `test_document_generation_baseline.py` / `_dataset_manifest.py` / `_metrics.py` / `_parity.py` / `_pilot.py` | 固定夹具基线不可变、指标、parity、离线试点 |
| `test_document_compatibility.py` | 兼容性遥测、回填不伪造哈希、闭包需已接受引用、备份/恢复 |
| `test_icd_pin_definition_benchmark.py` / `test_icd_generation.py` | ICD 指标与生成 |

---

## 11. 扩展指引（改这个子系统前）

1. **新增写入位置**必须走 `WorkbookRegionSchema`/`DocxRegionSchema` 注册 + `write_policy`；
   运行时无法「临时获得」新的可写区域（这是设计红线）。
2. **新增字段契约**先加 `contract_registry` 白名单，否则 LLM 提示不会被采纳。
3. **改指纹**要同时处理 v1/v2/v3 兼容窗口；绝不允许把 `task_id`/`revision_id` 纳入冻结输入。
4. **新增渲染格式**需：注册渲染器能力 → 结构化配方 profile → 包校验 → 预览支持 →
   评估指标（`document_generation_metrics.py`）与基线。
5. **任何模型调用**都必须有确定性回退路径（offline/无模型时仍能产出候选并转人工）。
