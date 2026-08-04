# Hardware-DataBase 知识库受控写作（文档生成）改进方案

> 版本：v0.1 ｜ 日期：2026-07-27 ｜ 范围：`feature/template-upload-governed-authoring` 分支的文档生成（harness）链路
>
> 本文档基于对 Excel 解析/存储/索引、知识库受控写作整体流程、RAGFlow 检索机制三轮代码走查的结论整理，目标是：先给出问题诊断，再给出**可分阶段交付、可回滚**的改进方案，并明确推荐起步点与验收标准。

---

## 1. 背景与现状

知识库受控写作（下称"文档生成"）链路：

```
选项装配 ──► 创建工单+冻结来源 ──► 检索+生成(harness) ──► 人工审批/发布
```

- **冻结来源**：`create_knowledge_base_document_work_order`（`src/core/app_pipeline.py:415`）把"创建那一刻 KB 内所有 readable 文档名"（`list_file_infos`，不区分 processor_kind，含 `.xlsx`）冻结进 `KnowledgeBaseSourceSnapshot`，并算 `content_hash` 守护。
- **检索+生成**：`AuthoringGraph.run`（`src/document_authoring/harness/graph.py:79`）对 DocumentSchema 每个语义单元（field/review item）顺序执行：建 `InformationRequirement` → 检索（`_knowledge_base_retriever` 闭包 → `RAGFlowBackend.retrieve`）→ `_validated_evidence` 落域校验 → `ManagedWriter` 起草 → 校验草稿 → 跨单元一致性 → 渲染进模板产出候选 artifact。
- **审批**：`approve_document_artifact` 复算 content_hash 后发布。

受控写作与 agentic 问答是**两条独立的检索路径**：agentic 问答（`src/agents/graph.py`）有 LLM planner、`plan_next_retrieval`、多轮检索、spreadsheet 三工具；而文档生成 harness 走静态单串查询，只调 RAGFlow 文本检索。**本方案的问题与改进几乎全部集中在 harness 这条路径。** 另：`_project_retriever`（`app_pipeline.py:537`，project-scoped 工单）同样只调 RAGFlow，存在与 P1 同构的 spreadsheet 不可达缺口；其改造列入阶段 2 RetrieverRegistry 一并处理，不在阶段 0。

---

## 2. 问题诊断

按"严重度 × 影响面"排序。核心结论：**检索层是最大瓶颈，且存在一个静默失败的正确性缺口（spreadsheet）**；草稿层在非 LLM 部署下是占位实现。

| # | 问题 | 关键位置 | 严重度 |
|---|------|---------|--------|
| P1 | **spreadsheet 内容在受控写作中不可达**：`source_names` 全量冻结含 `.xlsx`，但 `RAGFlowBackend.retrieve` 只认 `processor_kind == RAGFLOW`（`ragflow_backend.py:855`），Excel 结构化索引（`TableIndexStore`）从不被查。需要 BOM/用量/参数的字段会**静默判 missing**，而非报错 | `app_pipeline.py:426`、`ragflow_backend.py:855` | **P0 正确性** |
| P2 | **查询串静态、无改写**：query = `"{label} {description}"`（`app_pipeline.py:505`）。schema 术语 ≠ 文档术语时召回崩塌，无同义词/重写/分解 | `app_pipeline.py:505`、`graph.py:224` | **P0 召回** |
| P3 | **空结果不重试**：`_retrieve_with_budget` 只在 `retrieval_failed/…` 时重试，`success_empty` 直接判 missing/blocked；预算 `max_retrieval_attempts_per_unit=2` 被浪费 | `graph.py:200` | **P0 召回** |
| P4 | **`required_capabilities` 形同虚设**：`tabular_lookup`/`entity_lookup` 声明了却不分流检索器，全压给 RAGFlow 文本检索 | `graph.py:261` | P1 |
| P5 | **`DeterministicEvidenceWriter` 是占位**：`proposed_value = evidence[0].content`（逐字复制首个 chunk，`managed.py:52`）。非 LLM 部署下草稿=原始片段，非综合答案 | `managed.py:52` | P1 |
| P6 | **无 rerank**：直接用 RAGFlow 混合分，无 cross-encoder 精排，无近似 chunk 去重 | `ragflow_backend.py:841` | P2 |
| P7 | **`preferred_source_roles` 忽略**：声明了却不用来源优先级 | `graph.py:232` | P2 |
| P8 | **字段间无证据复用**：每字段独立检索，无法用 A 字段命中辅助 B 字段（阶段 0–5 暂不闭环，并入阶段 2 Coordinator 跨单元 evidence 缓存） | `graph.py:113` | P2 |
| P9 | **检索可观测性弱**：RAGFlow 的 route reason/fallback 标志进了 evidence metadata，但未按字段聚合成可审阅的 retrieval ledger；人工审核看不到"为什么这字段空了" | `graph.py:117` | P2 |
| P10 | **缺 draft-requirement 语义契合校验**：validator 只查模板污染+跨单元一致性，不校验"草稿是否真的回答了该字段需求" | `graph.py:144` | P2 |

> 备注：`lease_seconds=60` 只是 `models.py:334` 的默认值；实际 schema 自动策略由 `_schema_harness_policy` 推导为 `max(300, unit_count*120)`（`service.py:1042`）。runtime 在 writer 调用前后都 heartbeat（`runtime.py:152/168`），单次 LLM 调用 < 当前 lease 即可，属 watch item 而非缺陷。

### 2.1 问题间的因果链

```
spreadsheet 不可达(P1) ─┐
静态查询(P2) ───────────┼─► 召回不足 ─► success_empty ─► 不重试(P3) ─► 字段判 missing/blocked
capabilities 不分流(P4) ─┘                                          ─► 进 waiting_human_input(人工兜底)
```

即：检索能力弱 → 召回差 → 空结果又无自适应 → 只能依赖人工审核兜底。改进的主线是**先把检索做宽做对，再补草稿与可观测性**。

---

## 3. 改进方案（分阶段）

**设计原则**：所有改动都在现有"冻结-校验-预算"框架内扩展，不破坏 evidence 落域校验（`_validated_evidence`）与 `HarnessPolicy` allowlist；每个阶段独立可交付、可回滚。

### 阶段 0：接通 spreadsheet 检索 —— 闭环 P1（最高优先）

**目标**：让被冻结进 source set 的 `.xlsx` 真正能产出证据。

**改动点**：
1. 新建 `KBEvidenceCoordinator`（或直接在 `_knowledge_base_retriever` 闭包内）：在 `RAGFlowBackend.retrieve` 之后，当 unit 的 `required_capabilities` 含 `tabular_lookup`（或始终）时，追加调用 `SpreadsheetSemanticTool` / `SpreadsheetCellTool`（`src/agents/tools/spreadsheet_tools.py`），传入同一 `ctx` 与 `filters=None`；spreadsheet 工具的 `filters` 仅读 `record_id`、忽略 `source_names`，故**冻结集过滤必须在闭包层合并前完成**（`e.source_name ∈ frozen_source_names`），否则冻结集外 evidence 进 `build_knowledge_base_retrieval_outcome` 会触发 PermissionError 终止整个 run。
2. 合并两路 evidence，**统一过 `build_knowledge_base_retrieval_outcome`**（`service.py:1174`）：它会给每条 evidence 盖 `knowledge_base_name` 戳并校验 `source_name ∈ frozen_source_names`。spreadsheet evidence 的 `source_name` 是 xlsx 文件名，本就在冻结集内，可过。
3. `AppPipeline.__init__` 新增 `self.spreadsheet_service = getattr(self.backend, "spreadsheet_indexes", None)`。当前 `backend.spreadsheet_indexes` 仅以形参注入给 `self.agent`（`app_pipeline.py:57`），文档生成路径未持有，需存为实例属性方可在闭包引用。

**为何无需改 harness 校验**：`_validated_evidence`（`graph.py:266`）要求 `metadata["knowledge_base_name"] == kb_name` 且 `source_name ∈ snapshot.source_names`；步骤 2 的盖章 + 冻结集成员身份恰好满足。

**风险**：spreadsheet 工具是 token LIKE 检索（非向量），召回依赖分词——在阶段 1 的 query 改写中兼顾。

**验收**：含 `tabular_lookup` 的字段在仅有 .xlsx 的 KB 里能命中证据，unit 状态从 `blocked` 变 `ready_to_render`。

### 阶段 1：查询改写 + 空结果重试 —— 闭环 P2/P3（召回最大收益）

**目标**：把静态单串查询升级为"原串 + LLM 改写"，并在 `success_empty` 时换串重试。

**改动点**：
1. 新建 `QueryRewriter`（复用 `LLMClient`，`usage_stage="query_rewrite"`）：输入 `(subject, predicate, required_capabilities, field_label)`，输出 1 个改写串（同义词/实体别名/替代表述）。
2. 改 `_retrieve_with_budget`（`graph.py:186`）：attempt 1 用原串；若 `success_empty`，attempt 2 用改写串。**复用已存在的 `max_retrieval_attempts_per_unit=2` 预算**（目前被浪费）。
3. `HarnessPolicy.allowed_tools` 增加 `"rewrite_query"`，`HarnessToolPolicy.require_tool` 守门。**注意**：`allowed_tools` 是工单冻结版本字段，新增能力需注册并审批新 policy 版本；已在途工单冻结的是旧版本，不追溯，需重建工单方能用改写。
4. 改写结果写入 `retrieval_ledger`（见阶段 3）。

**风险**：LLM 调用增加延迟与成本；需在 `HarnessPolicy` 加 `max_query_rewrite_rounds` 预算并纳入 `max_steps`。

**验收**：`success_empty` 字段在 attempt 2 命中率显著提升；空字段率下降。

### 阶段 2：capability-aware 检索分发 —— 闭环 P4（泛化阶段 0）

**目标**：把 `required_capabilities` 真正路由到对应检索器。

**改动点**：
1. 定义 `RetrieverRegistry`（`src/document_authoring/retriever_registry.py`）：capability → retriever（`document_claim_lookup`/`entity_lookup`/`relationship_lookup` → RAGFlow 作为 default 始终调用；`tabular_lookup` → spreadsheet 工具作为 specialized 叠加调用；`revision_lookup` → 预留，走 default）。**分派语义**：RAGFlow default 始终调用（保留阶段 0 行为、不回归文本召回），specialized 检索器按声明 capability **叠加**调用（`tabular_lookup` 字段 = RAGFlow + spreadsheet，与阶段 0 一致）。
2. Registry 合并多检索器 evidence，按 `content` 哈希去重（保留 `score` 高者）；KB 闭包与 project 闭包共用去重函数。
3. 顺带处理 `preferred_source_roles`（P7）：对匹配 role 的来源 `score *= 1.5` 并打 `preferred_source_role_match` 标。**注**：KB 路径 `state.Evidence` 无 `document_role` 字段，加分为 no-op；project 路径 `EvidenceEnvelope.document_role` 有值时加分生效。
4. 跨单元 evidence 复用缓存（P8）：`CrossUnitEvidenceCache` 在闭包内一次 run 一个实例，当前 unit 新检索**结果为空**时回填前序 unit 的命中证据（带 `reused`/`reused_from_unit` 标，受 `max_reuse_per_unit=5` 约束）；复用证据已在同冻结集内通过落域校验，不破坏正确性。
5. project 路径同构改造（`_project_retriever`）：`retrieve_one` 内补 spreadsheet 分派，**落域约束**：spreadsheet evidence 须绑 `source_version_id` + `processing_artifact_id`（镜像 RAGFlow envelope 构造）以过 `ProjectEvidenceRetrievalService.retrieve`（`retrieval.py:70`）的 per-version 三重校验，避免触发 `filter_unsupported`。闭包对 outcome 做去重 + role 加分 + 跨单元复用后处理；复用命中时 status 升级为 `success_with_hits`，避免 harness 误判空结果触发改写。

**验收**：不同 capability 的字段走不同检索器；`tabular_lookup` 字段命中 spreadsheet 证据（KB 与 project 路径均成立）。

### 阶段 3：rerank + 检索可观测性 -- 闭环 P6/P9

**改动点**：
1. Coordinator 合并后、送 writer 前，加轻量 reranker（LLM-as-judge，复用 `LLMClient`，受 `allowed_tools` 守门），按 requirement 相关性重排已校验证据。**v1 只重排不截断**（避免丢弃已过落域校验的证据致字段误判 missing）；`top_k` 截断能力已实现并有单测，待阶段 4 `requirement_fit_check` 就位后启用。旧 policy 无 `rerank_evidence` -> reranker 不注入 -> pass-through（与阶段 1 `rewrite_query` 同前例）。
2. 持久化 per-unit `RetrievalLedgerRow`：`{unit_id, original_query, rewrites[], per_source[], fallback_triggered, final_evidence_ids}`，嵌入 matrix row 复用 `save_evidence_matrix` 持久化供人工审核 UI 展示，并回填 `HarnessExecutionResult.retrieval_ledger`（修复 `DocumentAuthoringState.retrieval_ledger` 预留字段从未写入 result 的 latent 缺口）。现成锚点：matrix row 已带 per-source `diagnostics`（= `outcome.source_outcomes`），`per_source` 直接聚合；`rewrites` 复用阶段 1 改写记录。
3. `fallback_triggered` 从 `outcome.evidences`（保留 metadata）取 RAGFlow `ragflow_source_name_fallback`/`ragflow_metadata_condition_fallback` 标志，**不用** validated dict（project 路径 `_validated_evidence` 已剥 metadata）。

**验收**：人工审核页能看到每个字段的"查询串/改写/各来源命中数/是否触发 fallback/最终证据"；rerank 后送 writer 的证据按相关性重排（reranker=None 时原序，零回归）。

### 阶段 4：草稿质量 -- 闭环 P5/P10

**改动点**：
1. `DeterministicEvidenceWriter` 从"逐字 evidence[0]"升级为"结构化汇总全部证据"：单证据保持原样、多证据枚举拼接 + 一条引用全部证据的 summary assertion（避免单元内 cross-unit 冲突），不捏造。同时升级 `LLMManagedWriter` 的 fallback 草稿。
2. 回归锁定项：`LLMManagedWriter._build_user_prompt` 已把含全部 evidence 的 request 传给 LLM（`managed.py:235`），"使用全部 evidence"现状即满足，改为回归测试锁定，不重复实现。
3. 新增独立 `RequirementFitChecker`（LLM-as-judge，仿 `EvidenceReranker`）注入 graph：判定草稿是否回答了 requirement，未通过置 `requires_human`。**opt-in**（不进默认 allowlist，因 fit check 是 status-changing LLM 门控，与 status-preserving 的 rerank/rewrite 不同），受 `requirement_fit_check` 守门，LLM 失败降级 pass。

**验收**：非 LLM 部署下草稿不再是裸片段（多证据含全部证据）；草稿与字段需求不匹配时被标记而非直接放行（启用 fit check 的部署）。

### 阶段 5：自适应恢复 -- 闭环 P3 的极端情况

**目标**：`success_empty` 且改写仍空时，在策略允许范围内放宽 scope 重试一次，标记低置信交人工。

**改动点**：Coordinator 在用尽 attempts 后，若策略允许，drop source_group 硬过滤（退回 balanced route）再查一次，证据打 `low_confidence` 标。

**走查对齐**：source_group 硬过滤在 harness 路径有两层——服务端 `metadata_condition`（`_metadata_condition` 把 `routed_source_groups` 加入 RAGFlow 检索条件，**两路径都生效**）与本地 `_filter_chunks` 后过滤（`apply_routed_source_groups = bool(routed) and not source_names`，harness 传 `source_names` -> **已关**）。现有 0-chunk fallback（`ragflow_backend.py:882`）仅当 raw chunks=0 才丢 metadata_condition 重查。真实盲区：高置信路由到 group X 但冻结集相关文档在别的 group 时，服务端按 X 返回非冻结集 chunks（>0，**fallback 不触发**）-> 本地 `source_names` 全丢 -> `success_empty`，冻结集内 group Y 文档从未被查。"drop source_group"= 让 RAGFlow 跨 group 检索、再用冻结 `source_names` 兜底，即"退回 balanced route……仍在冻结 source_names 内"。

**实施**：
1. `HarnessPolicy` 增 `max_adaptive_recovery_rounds: int = 0`（默认 0=关），`allowed_tools` 默认**不含** `adaptive_recovery`——工具 allowlist + 预算字段双开关（镜像 `rewrite_query`+`max_query_rewrite_rounds`），**opt-in**（与 `requirement_fit_check` 同前例：status-changing + 真实 backend 调用，默认关以零回归）。
2. `RAGFlowBackend.retrieve` 识别 `filters["balanced_route"]`，为真则 `routed_source_groups=()`——`metadata_condition` 不加 source_group、本地后过滤仍关。**保留** kb_name/department/source_names 条件。route reason/confidence 仍反映计算所得路由（可观测），仅硬过滤被 drop。
3. `_retrieve_with_budget` 用尽 attempts 后，若 `adaptive_recovery` 在 allowlist + `max_adaptive_recovery_rounds>0` + 终态 `success_empty`，做一次 `retrieve(req, attempt+1, None, relaxed=True)`；命中则 `_tag_low_confidence`。**仅 `success_empty` 触发**（hard-fail 是源不可用，balanced 无益）。恢复走独立预算，**不调 `require_retrieval_round`**（避免与 `max_retrieval_rounds` 冲突）。返回签名不变（信号经 `outcome.evidences[*].metadata["low_confidence"]` 传递，既有 `_retrieve_with_budget` 测试不破坏）。
4. KB/project 两条 retrieve 闭包接受 `relaxed`，`relaxed=True` 时给 RAGFlow filters 追加 `balanced_route: True`（KB 经 `RetrieverRegistry.retrieve(balanced_route=)`，project 经 `retrieve_one` 闭包捕获 `balanced` 标）。
5. run loop 从 `outcome.evidences`（raw，metadata 完整）读 `recovery_triggered`（在 `_validated_evidence` 剥 metadata 之前），记入 ledger row；**若 `recovery_triggered` 且 evidence 非空**，强制 `unit_status="requires_human"` + issue `low_confidence_recovery` + 把 supported 草稿翻为 requires_human（低置信必进人审）。

**风险与约束**：放宽 scope 仅 drop source_group，**不放宽冻结 `source_names`/`source_version_ids`**——`_validated_evidence` + `build_knowledge_base_retrieval_outcome` 双重落域校验不变，恢复证据必在冻结集内。不动 writer/validator 确定性逻辑、不改 artifact 寻址。

**验收**：默认 policy（无 `adaptive_recovery`）行为与阶段 4 byte-identical（零回归，现有测试全绿）；opt-in 启用后，原本 `success_empty`->`blocked` 的 `block_section` 字段若 balanced 恢复命中冻结集内证据，则产低置信草稿、`requires_human`、issue、ledger `recovery_triggered=True`，人审页可见证据与草稿；未命中维持原 `blocked`/`tbd`。spec 见 `docs/superpowers/specs/2026-07-28-adaptive-recovery-stage5-design.md`，计划见 `docs/superpowers/plans/2026-07-28-adaptive-recovery-stage5.md`。

---

## 4. 实施顺序与建议

```
阶段 0 (spreadsheet 接通) ──► 阶段 1 (查询改写+空结果重试)
   │                              │
   └─ 闭环 P0 正确性缺口           └─ 闭环 P0 召回，并为阶段 2 的改写能力铺路
        ▼
阶段 2 (capability 分发) ──► 阶段 3 (rerank + 可观测性) ──► 阶段 4 (草稿) ──► 阶段 5 (自适应)
```

**强烈建议从阶段 0 起步**：
1. 它是**正确性缺口**而非优化点——系统当前对 Excel 来源是"承诺纳入、实际查不到"的静默失败，任何需要表格事实的文档生成都是错的。
2. 改动面小、可控：本质是在 `_knowledge_base_retriever` 闭包里追加一路检索并合并，复用已有的 `build_knowledge_base_retrieval_outcome` 落域校验，**不动 harness 图、不动 policy、不动冻结机制**。
3. 阶段 1 紧随其后收益最大，且改写能力能直接缓解阶段 0 中 spreadsheet token LIKE 召回弱的问题。

---

## 5. 验收与度量（建议）

| 指标 | 基线（现状） | 目标 |
|------|------------|------|
| 含 `tabular_lookup` 字段在纯 .xlsx KB 的命中率 | 0%（spreadsheet 不可达） | >90% |
| `success_empty` 字段占比（一次检索） | 高（无重试） | 显著下降（改写重试后） |
| 字段级 evidence_matrix 覆盖完整度 | 仅诊断 source_outcome | 含查询串/改写/各检索器命中 |
| 非 LLM 部署草稿可用性 | 裸 chunk 文本 | 结构化汇总 |

---

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 引入 LLM query 改写，增加延迟/成本 | 生成变慢、调用增多 | 设 `max_query_rewrite_rounds` 预算并纳入 `max_steps`；可开关 |
| spreadsheet LIKE 召回弱 | 阶段 0 命中仍受限 | 阶段 1 改写串同时喂给 spreadsheet 工具；后续可考虑 FTS5/向量 |
| 放宽 scope 越权 | 破坏落域校验 | 放宽仅限"去掉 source_group 硬过滤"，仍在冻结 source_names 内；低置信标记交人工 |
| 多路 evidence 合并引入重复 | 证据冗余、分数不可比 | 按 `content` 哈希去重；rerank 统一重排 |

---

## 附：关键代码位置速查

| 作用 | 位置 |
|------|------|
| 工单创建 + 来源冻结 | `src/core/app_pipeline.py:415` |
| KB 检索闭包（harness） | `src/core/app_pipeline.py:493` `_knowledge_base_retriever` |
| RAGFlow 检索（混合 + fallback） | `src/pipelines/document_rag/ragflow_backend.py:841` |
| harness 执行图（per-unit） | `src/document_authoring/harness/graph.py:79` |
| 证据落域校验 | `src/document_authoring/harness/graph.py:266` `_validated_evidence` |
| 检索结果落域封装 | `src/document_authoring/service.py:1174` `build_knowledge_base_retrieval_outcome` |
| spreadsheet 检索工具 | `src/agents/tools/spreadsheet_tools.py` |
| Managed Writer（deterministic/LLM） | `src/document_authoring/writers/managed.py` |
| Harness 预算/allowlist | `src/document_authoring/models.py:324` `HarnessPolicy`、`src/document_authoring/harness/policy.py` |

---

## 7. 实施状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| 阶段 0（spreadsheet 接通） | 已实施 | 闭包隔离 + 仅 `tabular_lookup` 触发 + 冻结集过滤；spec 见 `docs/superpowers/specs/2026-07-27-spreadsheet-retrieval-stage0-design.md`，计划见 `docs/superpowers/plans/2026-07-27-spreadsheet-retrieval-stage0.md` |
| 阶段 1（查询改写+空结果重试） | 已实施 | retrieve 签名加 `query_override`；`QueryRewriter` 仿 writer 注入 harness，`require_tool("rewrite_query")` 守门，`success_empty` 触发改写重试，LLM 失败降级原串；spec 见 `docs/superpowers/specs/2026-07-27-query-rewrite-stage1-design.md` |
| 阶段 2（capability-aware 分发） | 已实施 | `RetrieverRegistry`（default RAGFlow 始终调用 + specialized 叠加）泛化阶段 0；按 `content` 哈希去重；`preferred_source_roles` 加分（P7）；`CrossUnitEvidenceCache` 跨单元复用（P8）；project 路径补 spreadsheet 分派（绑 version_id+artifact 过 `retrieval.py:70`）；spec 见 `docs/superpowers/specs/2026-07-27-capability-dispatch-stage2-design.md` |
| 阶段 3（rerank + 检索可观测性） | 已实施 | `EvidenceReranker`（LLM-as-judge，受 `rerank_evidence` allowlist 守门，v1 只重排不截断）送 writer 前重排；per-unit `RetrievalLedgerRow` 嵌 matrix row + 回填 `HarnessExecutionResult.retrieval_ledger`（修复预留字段从未写入 result）；`fallback_triggered` 从 `outcome.evidences` 取；spec 见 `docs/superpowers/specs/2026-07-28-rerank-ledger-stage3-design.md` |
| 阶段 4（草稿质量） | 已实施 | `DeterministicEvidenceWriter` 多证据结构化汇总（单证据原样）+ LLM fallback 升级；回归锁定 `_build_user_prompt` 传全部 evidence；独立 `RequirementFitChecker`（LLM-as-judge，**opt-in** 不进默认 allowlist，受 `requirement_fit_check` 守门，失败降级 pass）注入 graph，unfit 置 `requires_human`；spec 见 `docs/superpowers/specs/2026-07-28-draft-quality-stage4-design.md` |
| 阶段 5（自适应恢复） | 已实施 | `RAGFlowBackend.retrieve` 支持 `balanced_route`（drop source_group 硬过滤，保留冻结 source_names）；`_retrieve_with_budget` 用尽 attempts 后 opt-in 做一次 balanced 恢复检索，命中打 `low_confidence` 标 -> 强制 `requires_human` + issue + ledger `recovery_triggered`；`HarnessPolicy.max_adaptive_recovery_rounds`（默认 0）+ `adaptive_recovery` allowlist 双开关（**opt-in**）；KB/project 闭包透传 `relaxed`；默认 policy 零回归；spec 见 `docs/superpowers/specs/2026-07-28-adaptive-recovery-stage5-design.md` |
