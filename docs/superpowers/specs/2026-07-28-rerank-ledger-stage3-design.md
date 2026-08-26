# Rerank + 检索可观测性 ledger（阶段 3）设计

> 日期：2026-07-28
> 分支：`feature/template-upload-governed-authoring`
> 上游方案：`docs/Hardware-DataBase_文档生成改进方案.md` §3 阶段 3
> 状态：待实施

## 1. 背景与目标

阶段 2 用 `RetrieverRegistry` 把多检索器 evidence 合并去重、加分、跨单元复用，闭环了 P4/P7/P8。但合并后的证据**直接用 RAGFlow 混合分送 writer**，无相关性精排；且检索过程的可观测性仍弱：

- **P6（无 rerank）**：`AuthoringGraph.run` 在 `_validated_evidence` 之后直接把证据喂给 `draft_provider`（`graph.py:136`），无 cross-encoder/LLM 精排。RAGFlow 混合分跨来源不可比（spreadsheet token LIKE 分 vs RAGFlow 向量分），最相关证据未必排前。
- **P9（检索可观测性弱）**：`DocumentAuthoringState.retrieval_ledger` 字段（`graph.py:42`）已预留但**只被 `_rewrite_for_retry` 写入改写记录，且 `run()` 从不把它回填到 `HarnessExecutionResult`**——改写记录在 run 返回时被丢弃。matrix row 虽带 per-source `diagnostics`（`graph.py:129`，= `outcome.source_outcomes`）并经 `save_evidence_matrix` 持久化（`service.py:871`），但无"查询串/改写/各来源命中数/是否触发 fallback/最终证据"的统一 per-unit 视图，人工审核看不到"为什么这字段空了"。

**目标（阶段 3）**：
1. 在 `_validated_evidence` 之后、送 writer 之前，加轻量 LLM-as-judge reranker，按 requirement 相关性重排已校验证据（受 `allowed_tools` 守门，旧 policy 降级 pass-through）。
2. 为每个 unit 构造 `RetrievalLedgerRow`（`{unit_id, original_query, rewrites[], per_source[], fallback_triggered, final_evidence_ids}`），嵌入 matrix row（复用 `save_evidence_matrix` 现有持久化路径供人工审核 UI 展示），并回填 `HarnessExecutionResult.retrieval_ledger`（修复"算完即丢"）。

**非目标**：草稿质量与 `requirement_fit_check`（阶段 4）、自适应恢复（阶段 5）；rerank 不做 top_k 截断（见 §3.4 决策）；不改检索器/闭包/落域校验。

## 2. 现状关键事实（代码核查结论）

| 事实 | 位置 |
|---|---|
| 证据送 writer 前**无 rerank** | `src/document_authoring/harness/graph.py:121-145`（`_validated_evidence` -> 直送 `draft_provider`） |
| `DocumentAuthoringState.retrieval_ledger` 已声明 | `src/document_authoring/harness/graph.py:42` |
| 改写记录写入 state 但**从不回填 result**（latent 缺口） | `graph.py:235/250`（写）vs `graph.py:180-181`（result 只回填 step_count/retrieval_round_count）；`HarnessExecutionResult`（`graph.py:55-64`）无 `retrieval_ledger` 字段 |
| matrix row 带 per-source `diagnostics` 并持久化 | `graph.py:129` + `src/document_authoring/service.py:871` `save_evidence_matrix` |
| matrix row 在 rerank **之前**构造（evidence_ids 为 pre-rerank） | `graph.py:122-130` |
| `RetrievalSourceOutcome` 字段：`source_version_id`/`status`/`evidence_ids`/`diagnostics` | `src/agents/claim_evidence.py:121` |
| KB 闭包 `source_outcomes` 按 source_name 分组、`diagnostics` 为空 | `src/document_authoring/service.py:1187` `build_knowledge_base_retrieval_outcome` |
| RAGFlow fallback 标志进 **evidence metadata** | `src/pipelines/document_rag/ragflow_backend.py:967-968`（`ragflow_source_name_fallback`/`ragflow_metadata_condition_fallback`）+ `query_route_reason`（`:964`） |
| project 路径 `_validated_evidence` **丢弃 metadata**（只留 id/content/version/locator/fact_type） | `graph.py:376-382`；故 fallback 标志须从 `outcome.evidences`（未剥离）取，不能用 validated dict |
| Stage 1 `QueryRewriter` 注入范式（policy 守门 + runtime 透传 + graph 持有） | `service.py:1110` `_rewriter_for_policy` -> `service.py:772/837` -> `runtime.py:106/192` -> `graph.py:75/82` |
| `HarnessPolicy.allowed_tools` 默认含 `rewrite_query` | `src/document_authoring/models.py:336-339` |
| `LLMClient.chat(messages, **kwargs)`，`usage_stage` 为自由 kwarg | `src/core/llm_client.py:194`（`kwargs.get("usage_stage")`，无枚举校验） |
| `_strip_code_fences` 可复用 | `src/document_authoring/writers/managed.py:287` |
| `FINAL_TOP_K=5`（检索 top_k） | `config/settings.py:57` |

> **关键约束**：rerank 在 graph 层、`_validated_evidence` **之后**进行，只对已通过落域校验的证据**重排顺序**（v1 不截断），不增删证据、不改 evidence 内容，不破坏落域正确性。fallback 标志须从 `outcome.evidences`（保留 metadata）取，不能从 validated dict（project 路径已剥 metadata）取。

## 3. 设计

### 3.1 架构与边界

```
新增 src/document_authoring/writers/evidence_reranker.py
  └─ EvidenceReranker（仿 QueryRewriter）
       ├─ __init__(client: LLMClient | None = None)
       └─ rerank(requirement, evidence: list[dict], top_k: int | None = None) -> list[dict]
            1. evidence 数 ≤1：原样返回
            2. 构 prompt（requirement + 带 idx/id 的 evidence 内容）
            3. LLMClient.chat(..., usage_stage="evidence_rerank")，要 JSON int 数组（0-based，最相关在前）
            4. 解析：strip_code_fences -> json.loads -> 校验 index ∈ [0,len) -> 按序重排，未引用者按原序追加（不丢证据）
            5. top_k 非 None：取前 top_k
            6. 任何异常/解析失败：原样返回（pass-through，零回归）

改 src/document_authoring/harness/graph.py
  ├─ HarnessExecutionResult 增 retrieval_ledger: list[dict] = field(default_factory=list)
  ├─ AuthoringGraph.__init__ 增 reranker: "EvidenceReranker | None" = None
  ├─ run() per-unit：_validated_evidence 后、构造 matrix_row 前，插入 rerank（reranker 非 None 且有证据时）
  ├─ run() per-unit：构造 RetrievalLedgerRow，append 到 result.retrieval_ledger，并作为 matrix_row["retrieval_ledger"]
  ├─ matrix_row 的 evidence_ids 改用 post-rerank 证据
  └─ 新增 _retrieval_ledger_row(...)、_evidence_fallback(...) 模块函数

改 src/document_authoring/models.py
  └─ HarnessPolicy.allowed_tools 默认追加 "rerank_evidence"

改 src/document_authoring/service.py
  ├─ 新增 _reranker_for_policy(policy)（仿 _rewriter_for_policy）
  └─ run_internal_harness / resume_internal_harness 构造 reranker 并透传 runtime

改 src/document_authoring/harness/runtime.py
  └─ run() 增 reranker 形参，透传 AuthoringGraph(..., reranker=reranker)
```

**边界（不改）**：`_validated_evidence`、`build_knowledge_base_retrieval_outcome`、`RetrieverRegistry`、`ProjectEvidenceRetrievalService.retrieve`、`SpreadsheetSemanticTool`、`InformationRequirement`、`RetrievalOutcome`/`RetrievalSourceOutcome`、`QueryRewriter`、`_rewrite_for_retry`（其写入的 state 改写记录被 ledger 行读取，行为不变）。

### 3.2 EvidenceReranker（改动点 1，闭环 P6）

- **位置**：`src/document_authoring/writers/evidence_reranker.py`，与 `query_rewriter.py` 同包同构（均为"检索侧 LLM 辅助器"，policy 守门 + runtime 透传 + graph 持有）。
- **接口**：`rerank(requirement: InformationRequirement, evidence: list[dict[str, Any]], top_k: int | None = None) -> list[dict[str, Any]]`。输入是 `_validated_evidence` 产出的证据 dict 列表（含 `id`/`content`）。
- **LLM 契约**：system prompt 要求返回 JSON int 数组（0-based 索引，按相关性降序）；user prompt 给 `requirement.subject`/`predicate`/`required_capabilities` + 逐条 `{idx, id, content}`（content 截断防超长）。
- **解析鲁棒性**：`_strip_code_fences` -> `json.loads`；非 list / 非全 int / 越界索引一律丢弃；按有效索引序重排，**未被引用的证据按原序追加到末尾**（绝不丢证据）；解析失败/LLM 异常 -> 原序返回。
- **top_k**：重排后 `evidence[:top_k]`。**v1 graph 调用传 `top_k=None`（纯重排、不截断）**——见 §3.4。
- **usage_stage**：`"evidence_rerank"`（自由 kwarg，与 `query_rewrite` 同性质）。
- **provider_id**：`"evidence_reranker"`。

### 3.3 rerank 在 graph 的接入与守门（改动点 1 续）

`AuthoringGraph.run` per-unit 循环改为：

```python
outcome = self._retrieve_with_budget(state, requirement, retrieve)
result.outcomes[unit_id] = outcome
evidence = _validated_evidence(work_order, snapshot, outcome)
if evidence and self.reranker is not None:
    self._step(state, "rerank_evidence")          # 计入 step 预算
    self.policy.require_tool("rerank_evidence")   # allowlist 守门（仿 rewrite_query 双保险）
    evidence = self.reranker.rerank(requirement, evidence)   # top_k=None
ledger_row = _retrieval_ledger_row(unit_id, requirement, outcome, evidence, state)
result.retrieval_ledger.append(ledger_row)
result.matrix_rows.append({
    ...,
    "evidence_ids": [entry["id"] for entry in evidence],   # post-rerank
    "diagnostics": [source.model_dump(mode="json") for source in outcome.source_outcomes],
    "retrieval_ledger": ledger_row,
})
if not evidence:
    result.unit_statuses[unit_id] = _missing_status(unit_id, schema, outcome)
    continue
# ...draft（用 post-rerank evidence）...
```

- **守门**：`reranker` 由 `_reranker_for_policy` 仅在 `"rerank_evidence" in policy.allowed_tools` 时注入（旧冻结 policy -> None -> 跳过整段 rerank，零回归）。`require_tool("rerank_evidence")` 作为双保险，仿 `_rewrite_for_retry` 的 `require_tool("rewrite_query")`。
- **step 预算**：rerank 占 1 step/unit。运行时 policy `max_steps = max(300, unit_count*120)`（`service.py:1042`），1 step/unit 增量可忽略；`require_step` 照常守门。
- **空证据 unit 不 rerank**（`evidence and ...` 短路），但仍构造 ledger 行 + matrix row（见 §3.5）。

### 3.4 不截断决策（v1）

rerank **只重排、不截断**（graph 传 `top_k=None`）。理由：
- 截断会丢弃已通过落域校验的证据，可能让本可回答的字段被判 missing——与 harness fail-closed 主旨相悖。
- "是否够用"需语义判定，留给阶段 4 `requirement_fit_check`；在它就位前不冒截断风险。
- `top_k` 截断能力仍实现并有单测（`rerank_truncates_with_top_k`），便于阶段 4 启用。
- 上游方案"重排取 top_k"的"取 top_k"在此解读为"重排（并可选取 top_k）"，v1 选择可选关闭。

### 3.5 检索可观测性 ledger（改动点 2，闭环 P9）

新增模块函数 `_retrieval_ledger_row(unit_id, requirement, outcome, evidence, state) -> dict`：

| 字段 | 来源 | 说明 |
|---|---|---|
| `unit_id` | `unit_id`（= `requirement.semantic_unit_id`） | |
| `original_query` | `_query_string(requirement)` | subject+predicate+object_hint |
| `rewrites` | `state["retrieval_ledger"]` 中 `unit_id` 匹配且 `rewrite` 非空的 `rewrite` 列表 | 复用 Stage 1 改写记录（不改动 `_rewrite_for_retry`） |
| `per_source` | `outcome.source_outcomes` -> `[{source, status, hit_count: len(evidence_ids)}]` | 现有 per-source 诊断；KB 路径 source=source_name，project 路径 source=version_id |
| `fallback_triggered` | `any(_evidence_fallback(e) for e in outcome.evidences)` | **从 `outcome.evidences` 取**（保留 metadata）；project 路径 validated dict 已剥 metadata，故不能用 |
| `final_evidence_ids` | `[e["id"] for e in evidence]`（post-rerank） | 实际送 writer 的证据集 |

`_evidence_fallback(evidence) -> bool`：`metadata = getattr(evidence, "metadata", None) or {}`；返回 `bool(metadata.get("ragflow_source_name_fallback") or metadata.get("ragflow_metadata_condition_fallback"))`。

- **嵌入 matrix row**：`matrix_row["retrieval_ledger"] = ledger_row`，随 `save_evidence_matrix`（`service.py:871`）落库供人工审核 UI 展示——复用现有持久化路径，不新增 store API。
- **回填 result**：`HarnessExecutionResult` 增 `retrieval_ledger: list[dict] = field(default_factory=list)`；`run()` 每 unit append。`HarnessBudgetExceeded` 分支不显式处理（已 append 的行保留，未到的 unit 无行——符合"部分进度"语义）。
- **命名**：`state["retrieval_ledger"]`（Stage 1 改写日志，原始）与 `result.retrieval_ledger`（聚合 per-unit 行，对外）同名但在不同对象（TypedDict state vs dataclass result）；spec 显式区分，避免误读。

### 3.6 policy / 注入接线（仿 Stage 1）

- `HarnessPolicy.allowed_tools` 默认追加 `"rerank_evidence"`（`models.py:336`）。
- `service.py` 增 `_reranker_for_policy(policy)`：`if "rerank_evidence" in policy.allowed_tools: return EvidenceReranker()` else `None`。
- `run_internal_harness` / `resume_internal_harness`：`reranker = self._reranker_for_policy(policy)`，透传 `runtime.run(..., reranker=reranker)`。
- `runtime.py run()` 增 `reranker` 形参，`AuthoringGraph(..., reranker=reranker)`。
- **冻结版本语义**：`allowed_tools` 是工单冻结版本字段；新 policy 含 `rerank_evidence`，已在途工单冻结旧版本不追溯（reranker=None -> pass-through）。与 Stage 1 `rewrite_query` 同前例，可接受。
- **不改** `prompt_version`（reranker 自有 prompt，非 writer prompt）、不加新预算字段（rerank 复用 step 预算，allowlist 即守门）。

### 3.7 降级

- 旧 policy 无 `rerank_evidence`：reranker=None -> 不 rerank（现状排序）。
- reranker 注入但 LLM 不可用/超时/解析失败：`rerank` 原序返回 -> 等价不 rerank。
- 证据 ≤1 条：`rerank` 原样返回（不调 LLM）。
- `outcome.source_outcomes` 为空（project 路径某些情况）：`per_source=[]`，ledger 行仍完整。
- `outcome.evidences` 无 fallback 标志：`fallback_triggered=False`。

## 4. 测试策略

### 4.1 新增 `tests/test_evidence_reranker.py`（Task 1，fake LLMClient，无 backend）
1. `rerank_reorders_by_llm_ranking`：LLM 返回 `[2,0,1]` -> 证据按该序重排。
2. `rerank_passthrough_on_empty`：空列表 -> `[]`（不调 LLM）。
3. `rerank_passthrough_on_single`：1 条 -> 原样（不调 LLM）。
4. `rerank_passthrough_on_llm_failure`：LLM 抛异常 -> 原序。
5. `rerank_passthrough_on_parse_failure`：LLM 返回非 JSON / 非 list -> 原序。
6. `rerank_truncates_with_top_k`：`top_k=2` -> 重排后取前 2。
7. `rerank_drops_invalid_indices_keeps_unreferenced`：LLM 返回含越界索引 + 未引用项 -> 丢越界、按有效序排、未引用者原序追加（不丢证据）。
8. `rerank_uses_evidence_rerank_usage_stage`：断言 `chat` 收到 `usage_stage="evidence_rerank"`。

### 4.2 新增 `tests/test_authoring_graph_rerank_ledger.py`（Task 2，fake retrieve + fake/真 reranker）
1. `rerank_applied_when_reranker_injected`：注入 reranker（返回反转序）-> 送 writer 的 evidence_ids 为 post-rerank 序；matrix_row.evidence_ids 同。
2. `rerank_skipped_when_reranker_none`：reranker=None -> 原序，无 `rerank_evidence` step。
3. `rerank_step_gated_by_require_tool`：policy.allowed_tools 去掉 `rerank_evidence` + 强行注入 reranker -> `require_tool` 抛 PermissionError（守门双保险有效）。
4. `ledger_records_query_rewrites_per_source_fallback`：retrieve 返回带 fallback metadata 的 evidence + 2 source_outcomes + 触发改写 -> ledger 行含 original_query、rewrites 非空、per_source 2 项、fallback_triggered=True、final_evidence_ids。
5. `ledger_survived_in_result`：`result.retrieval_ledger` 非空且含每 unit 一行（修复"算完即丢"）。
6. `ledger_for_empty_unit`：空证据 unit 仍得 ledger 行（per_source/final_evidence_ids 反映空）+ matrix row。
7. `matrix_row_embeds_ledger`：`matrix_row["retrieval_ledger"]` 与 `result.retrieval_ledger` 对应行一致。

### 4.3 扩展 `tests/test_harness_policy.py`（Task 3）
- `default_allowed_tools_includes_rerank_evidence`：默认 allowed_tools 含 `rerank_evidence`。
- `_reranker_for_policy` 返回类型随 allowed_tools 切换（None / EvidenceReranker）。

### 4.4 回归（Task 4）
- 阶段 0/1/2 全部测试保持绿（rerank 在旧 policy/无 reranker 时 pass-through，ledger 行为叠加不破坏现有断言）。
- `tests/test_full_generation_flow.py`、`tests/test_document_authoring_p2a.py`、`tests/test_app_pipeline_scope.py` 等集成测试保持绿。
- 注意：现有测试若断言 matrix_row 的**确切键集合**或 `HarnessExecutionResult` 字段，需同步更新（matrix_row 新增 `retrieval_ledger` 键、result 新增 `retrieval_ledger` 字段）。

## 5. 验收标准

- 注入 reranker 时，送 writer 的证据按 LLM 相关性重排；reranker=None 或旧 policy 时原序（零回归）。
- `rerank_evidence` 受 `allowed_tools` 守门：旧冻结 policy 不 rerank。
- 每个 unit（含空证据 unit）在 `result.retrieval_ledger` 有一行，含 `unit_id`/`original_query`/`rewrites`/`per_source`/`fallback_triggered`/`final_evidence_ids`。
- ledger 行嵌入 matrix row 并经 `save_evidence_matrix` 持久化（人工审核可读）。
- `fallback_triggered` 正确反映 RAGFlow `ragflow_source_name_fallback`/`ragflow_metadata_condition_fallback`（从 `outcome.evidences` 取，兼容 project 路径 metadata 剥离）。
- matrix_row.evidence_ids 为 post-rerank 证据集。
- 阶段 0/1/2 全部回归绿；新增 `rerank_evidence` 到 allowed_tools 默认值；无 `prompt_version`/预算字段变动。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| LLM rerank 增延迟/成本 | 每 hit unit 一次 LLM 调用 | 证据 ≤1 不调；失败 pass-through；allowlist 守门可关；usage_stage 纳入计费观测 |
| rerank 改动证据集破坏落域 | 正确性 | rerank 只重排已校验证据 dict，不增删不改内容；v1 不截断 |
| rerank 解析错误丢证据 | 字段误判 missing | 未引用证据原序追加；解析失败原序返回；单测 4.1.7 专测 |
| matrix_row 键集变化破坏现有断言 | 测试红 | Task 4 同步更新断言；新键为追加，语义不变 |
| `fallback_triggered` 在 project 路径取不到（metadata 被剥） | ledger 字段失真 | 从 `outcome.evidences`（未剥离）取，不用 validated dict；单测覆盖 |
| step 预算被 rerank 耗尽 | HarnessBudgetExceeded | 1 step/unit，运行时 max_steps=max(300,unit_count*120) 充裕；`require_step` 照常守门 |
| 旧 policy 工单无 rerank_evidence | 不享受 rerank | reranker=None pass-through；与 Stage 1 同前例，需重建工单方启用 |

## 7. 对上游改进方案 .md 的修正

实施时同步修正 `docs/Hardware-DataBase_文档生成改进方案.md`：
1. §3 阶段 3 改动点 1 补"v1 只重排不截断、top_k 截断能力预留待阶段 4 启用"的决策（避免被读成"立即截断丢证据"）。
2. §3 阶段 3 改动点 2 补"ledger 行嵌入 matrix row 复用 `save_evidence_matrix` 持久化、并回填 `HarnessExecutionResult.retrieval_ledger`（修复预留字段从未写入 result 的 latent 缺口）"。
3. §3 阶段 3 补"fallback 标志从 `outcome.evidences` 取（project 路径 validated dict 已剥 metadata）"。
4. §7 实施状态表标记阶段 3 已实施。
