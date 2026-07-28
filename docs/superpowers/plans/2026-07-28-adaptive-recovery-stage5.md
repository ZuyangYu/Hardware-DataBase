# 阶段 5 实施计划：自适应恢复（Adaptive Recovery）

- 关联 spec：`docs/superpowers/specs/2026-07-28-adaptive-recovery-stage5-design.md`
- 工作目录：worktree `feature/template-upload-governed-authoring`（HEAD `f199d50`）
- 流程：每任务 red（测试先写，失败）-> green（实现）-> commit；commit 信息以 `Co-Authored-By: Claude <noreply@anthropic.com>` 结尾。

## 预备

- 现状基线：`tests/test_retrieve_with_budget.py` 直接调用 `graph._retrieve_with_budget(state, requirement, retrieve)` 并断言 `outcome.status`。**本计划不改 `_retrieve_with_budget` 的返回签名**（用 `outcome.evidences[*].metadata["low_confidence"]` 传恢复信号），故该文件既有测试不受影响。
- `RetrievalProvider` 类型别名（`graph.py:52`）不改；`relaxed` 是闭包/fake 接受的可选 kwarg，仅在恢复分支传递。

---

## Task 1 — Policy 字段 + tool 守门

**目标**：`HarnessPolicy` 增加 `adaptive_recovery` 治理位与 `max_adaptive_recovery_rounds` 预算，默认 opt-in 关闭。

### 红（先写测试，失败）
文件：`tests/test_harness_policy.py`（追加）
- `test_default_allowed_tools_do_not_include_adaptive_recovery`：默认 `allowed_tools` 不含 `adaptive_recovery`。
- `test_adaptive_recovery_rounds_defaults_to_zero`：`max_adaptive_recovery_rounds == 0`。
- `test_adaptive_recovery_rounds_rejects_negative`：`max_adaptive_recovery_rounds=-1` -> `ValidationError`。
- `test_adaptive_recovery_rounds_accepts_positive`：`=1` 合法。

### 绿（实现）
文件：`src/document_authoring/models.py`
- `HarnessPolicy` 增 `max_adaptive_recovery_rounds: int = 0`。
- `validate_budget`：`self.max_adaptive_recovery_rounds >= 0`（否则 `ValueError`）。注意现有 `min(...)` 校验只覆盖必须 ≥1 的字段；`max_adaptive_recovery_rounds` 允许 0，单独校验。
- `allowed_tools` 默认列表**不**加 `adaptive_recovery`（保持阶段 4 现状）。

### 验证
`pytest tests/test_harness_policy.py -q` 全绿；`test_default_allowed_tools_do_not_include_requirement_fit_check` 等既有测试不回归。

### commit
`feat: add adaptive_recovery policy gate and budget (opt-in, defaults off)`

---

## Task 2 — RAGFlow `balanced_route`（drop source_group）

**目标**：`RAGFlowBackend.retrieve` 识别 `filters["balanced_route"]`，为真时强制 `routed_source_groups = ()`，仅 drop source_group，保留 source_names/kb/department。

### 红（先写测试，失败）
文件：`tests/test_ragflow_balanced_route.py`（新建，仿 `tests/test_ragflow_metadata_fallback.py` 的 `_Client`/`_Store`/`_backend` fake 模式）
- `test_balanced_route_omits_source_group_condition`：构造一个会触发 `should_filter`（高置信路由到单 group，如 query 含 ≥2 个 MATERIAL 关键词）的 query；`balanced_route=True` 时，`_Client.retrieve.calls[0][3]`（metadata_condition）的 conditions 中**无** `name=="source_group"`；对照 `balanced_route=False` 时**有**。
- `test_balanced_route_keeps_source_names_filter`：`balanced_route=True` + `source_names=["design.pdf"]`，`_filter_chunks` 仍丢弃非冻结 source_name 的 chunk（fake 一个 group 不匹配但 source_name 匹配、一个 source_name 不匹配的 chunk，断言只保留前者）。
- `test_balanced_route_survives_cross_group_chunk`：query 路由到 MATERIAL，但冻结记录 source_group="design"；`balanced_route=False` -> 服务端按 MATERIAL 过滤返回空（或非冻结 chunk 被本地 source_name 丢弃）-> 0 evidence；`balanced_route=True` -> 服务端不过滤 group -> 返回 design chunk（source_name 在冻结集）-> 1 evidence。（用 `_Client.responses` 控制两次 retrieve 的返回。）
- `test_balanced_route_does_not_set_routed_source_groups_in_metadata`：返回 evidence 的 metadata `query_route_source_groups == []`、`query_route_reason == "balanced query"`。

### 绿（实现）
文件：`src/pipelines/document_rag/ragflow_backend.py`
- `retrieve` 内，读 `balanced_route = bool((filters or {}).get("balanced_route"))`。
- `route = route_source_groups(query)`；`routed_source_groups = () if balanced_route else (route.source_groups if route.should_filter else ())`。
- 其余（`metadata_condition`、`apply_routed_source_groups`、fallback、`_filter_chunks`、metadata 打标）**逻辑不变**--`routed_source_groups=()` 自然使 `metadata_condition` 不含 source_group、`apply_routed_source_groups=False`、metadata `query_route_source_groups=[]`。
- log 行可追加 `balanced=...` 便于观测。

### 验证
`pytest tests/test_ragflow_balanced_route.py tests/test_ragflow_metadata_fallback.py -q` 全绿（既有 fallback 测试不回归：`balanced_route` 缺省 False）。

### commit
`feat: support balanced_route filter in RAGFlow retrieve to drop source_group hard filter`

---

## Task 3 — Graph 自适应恢复 + low_confidence 路由

**目标**：`_retrieve_with_budget` 用尽 attempts 后做一次 balanced 恢复检索；命中则打 `low_confidence` 标，loop 强制 `requires_human` + issue + ledger。返回签名不变。

### 红（先写测试，失败）
文件：`tests/test_authoring_graph_adaptive_recovery.py`（新建，仿 `test_retrieve_with_budget.py` + `test_authoring_graph_rerank_ledger.py` 的 KB-scope `graph.run` fake 模式）
- `test_recovery_fires_on_success_empty_when_enabled`：policy `allowed_tools` 含 `adaptive_recovery` + `max_adaptive_recovery_rounds=1`；fake `retrieve(req, attempt, query_override=None, relaxed=False)`：attempt 1 -> `success_empty`，attempt 2（rewrite，需 rewriter 或 `max_query_rewrite_rounds=0` 跳过）-> `success_empty`，`relaxed=True` -> `success_with_hits`（带 1 条 evidence）。断言：`relaxed=True` 被调用一次；`unit_statuses[field:f1]=="requires_human"`；`issues` 含 `kind=="low_confidence_recovery"`；ledger `recovery_triggered==True`。
  - 注：为避免 rewrite 干扰，可用 `max_query_rewrite_rounds=0`（rewriter 不触发）或 rewriter=None。需确认 `max_query_rewrite_rounds` 允许 0（现状 `min(...)>=1` 校验会拒 0）--故用 rewriter=None 路径：attempt 2 仍 `success_empty`（`_retrieve_with_budget` attempt 2 走 `last.status==success_empty` -> rewrite 分支，rewriter=None -> `override=None` -> retrieve(attempt, None)）。
- `test_recovery_not_fired_when_tool_not_allowed`（零回归核心）：默认 policy（无 `adaptive_recovery`）+ `max_adaptive_recovery_rounds=0`；fake 同上但断言 `relaxed=True` **从未**被调用；`unit_statuses` 维持 `blocked`/`tbd`（无草稿）。
- `test_recovery_not_fired_when_budget_zero`：`adaptive_recovery` 在 allowlist 但 `max_adaptive_recovery_rounds=0` -> 恢复不触发（`relaxed` 未调用）。
- `test_recovery_no_hits_keeps_original_status`：恢复 `relaxed=True` -> 仍 `success_empty`；维持原 `blocked`/`tbd`，无 issue，ledger `recovery_triggered==False`。
- `test_recovery_not_fired_on_hard_failure`：终态 `retrieval_failed` -> 恢复不触发（仅 `success_empty` 触发）。
- `test_recovery_does_not_exceed_retrieval_round_budget`：`max_retrieval_rounds=2`；恢复触发后不抛 `HarnessBudgetExceeded`（恢复不调 `require_retrieval_round`）。

### 绿（实现）
文件：`src/document_authoring/harness/graph.py`
- `_retrieve_with_budget` 末尾（for 循环后、`return last` 前）插入恢复块：
  ```python
  if (
      "adaptive_recovery" in self.policy.policy.allowed_tools
      and last is not None
      and last.status == "success_empty"
      and self.policy.policy.max_adaptive_recovery_rounds > 0
  ):
      self._step(state, "adaptive_recovery")
      self.policy.require_tool("adaptive_recovery")
      recovery = retrieve(requirement, attempt + 1, None, relaxed=True)
      if recovery.status == "success_with_hits" and recovery.evidences:
          _tag_low_confidence(recovery)
          last = recovery
  return last
  ```
  （`attempt` 为循环变量最终值；`retrieve` 第 4 参 `relaxed=True` 传给闭包/fake。）
- 新增 `_tag_low_confidence(outcome)`：对 `outcome.evidences` 每个对象 `metadata = dict(getattr(e, "metadata", {}) or {}); metadata["low_confidence"] = True; e.metadata = metadata`（KB `Evidence` 与 project `EvidenceEnvelope` 均有可写 `.metadata`）。
- 新增 `_evidence_low_confidence(evidence)`：`bool((getattr(evidence, "metadata", {}) or {}).get("low_confidence"))`（仿 `_evidence_fallback`）。
- run loop（`graph.py:126` 附近）：`outcome = self._retrieve_with_budget(...)` 后，`recovery_triggered = any(_evidence_low_confidence(e) for e in outcome.evidences)`。传给 `_retrieval_ledger_row`（新增 `recovery_triggered` 字段）。
- 在 rerank/ledger/draft/validate 之后、`result.unit_statuses[unit_id] = "ready_to_render"` 各分支处：**若 `recovery_triggered` 且 evidence 非空**，覆盖为 `requires_human` 并 append issue `{"kind": "low_confidence_recovery", "unit_id": unit_id, "reason": "evidence recovered via balanced-route retry"}`。实现方式：在 `result.drafts.append(validated)` 之前统一加一个 recovery 覆盖块（覆盖 contamination/fit_check/ready_to_render 的 status 判定），确保低置信必进人审。
- `_retrieval_ledger_row` 签名增 `recovery_triggered: bool`，row dict 增 `"recovery_triggered": recovery_triggered`。调用点同步。

### 验证
`pytest tests/test_authoring_graph_adaptive_recovery.py tests/test_retrieve_with_budget.py tests/test_authoring_graph_rerank_ledger.py tests/test_authoring_graph_fit_check.py -q` 全绿（既有 graph 测试不回归：默认 policy 不触发恢复）。

### commit
`feat: adaptive recovery in authoring graph with low_confidence routing (closes P3 extreme)`

---

## Task 4 — 闭包 wiring（KB + project `relaxed`）+ 集成回归 + 文档

**目标**：两条 retrieve 闭包接受 `relaxed`，`relaxed=True` 时给 RAGFlow filters 追加 `balanced_route: True`；跑集成回归确认默认 policy 零触发；更新改进方案文档。

### 实现
文件：`src/core/app_pipeline.py`
- `_knowledge_base_retriever` 内：
  - `default_retriever(query, requirement)` -> 增 `*, balanced_route=False`：`filters = {"source_names": frozen_source_names}`；`balanced_route` 时 `filters["balanced_route"] = True`。
  - `RetrieverRegistry.retrieve` 需把 `balanced_route` 透传给 `default_retriever`。方案：`RetrieverRegistry.retrieve(requirement, query, *, balanced_route=False)`，调用 `self.default_retriever(query, requirement, balanced_route=balanced_route)`（`default_retriever` 现接受该 kwarg）。specialized retriever 调用不变（2 参）。
    - **注意**：`Retriever` 协议是 `(query, requirement)`；`default_retriever` 作为闭包可多接一个 kwarg，registry 显式传即可。需同步 `src/document_authoring/retriever_registry.py` 的 `retrieve` 签名与 `default_retriever` 调用，并更新 `tests/test_retriever_registry.py` 既有用例（确保不传 `balanced_route` 时行为不变）。
  - 外层 `retrieve(requirement, _attempt, query_override=None, relaxed=False)`：`evidences = registry.retrieve(requirement, query, balanced_route=relaxed)`；其余（`build_knowledge_base_retrieval_outcome`）不变。
- `_project_retriever` 内 `retrieve_one`（`:619`）：`retrieve(requirement, _attempt, query_override=None, relaxed=False)` 透传 `relaxed` 到 `retrieve_one`；`retrieve_one` 的 `self.backend.retrieve(..., filters={"source_names": [document.title], **({"balanced_route": True} if balanced_route else {})})`。

### 集成回归
`pytest tests/test_full_generation_flow.py tests/test_template_authoring_integration.py tests/test_document_auto_generation.py tests/test_app_pipeline_scope.py tests/test_project_retriever_dispatch.py tests/test_retriever_registry.py tests/test_retrieve_with_budget.py tests/test_knowledge_base_document_work_orders.py -q`
- 默认 policy（无 `adaptive_recovery`）-> 恢复零触发 -> 行为与阶段 4 一致 -> 全绿。
- 若某集成测试因 `retrieve` 闭包签名变化（新增 `relaxed` kwarg）而 TypeError：仅当该测试直接构造闭包并以位置/关键字调用时可能受影响；以默认参数兼容（`relaxed=False`）应无碍。逐一确认。

### 文档
文件：`docs/Hardware-DataBase_文档生成改进方案.md`
- §3 阶段 5 改写：补走查结论（source_group 在 harness 路径的服务端 `metadata_condition` 形态 + 0-chunk fallback 盲区）、机制（`balanced_route` drop source_group、`max_adaptive_recovery_rounds` 预算、opt-in、low_confidence->requires_human）、零回归保证。
- §7 状态表：阶段 5 标"已实施"，无"未实施"剩余。
- spec/plan 文件 `git add -f`（`docs/superpowers/` gitignored）。

### 验证
全量回归：阶段 0–4 + 阶段 5 新测试 + 集成测试全绿。

### commit
`feat: wire relaxed balanced_route through KB and project retrievers`
`docs: mark stage 5 (adaptive recovery) as implemented`（可与上一 commit 合并或分开；按阶段 3/4 惯例分开）

---

## 风险与回退

- **风险 1**：`_retrieve_with_budget` 恢复块改变终态语义。缓解：仅在 `adaptive_recovery` allowlist + 预算 > 0 + `success_empty` 三重条件下触发；默认全闭。
- **风险 2**：`balanced_route` 误放宽至冻结集外。缓解：`_filter_chunks` 的 `source_names` 过滤保留 + `_validated_evidence`/`build_knowledge_base_retrieval_outcome` 双重落域校验不变。
- **风险 3**：闭包签名变化破坏既有 fake/集成测试。缓解：`relaxed`/`balanced_route` 均默认 False；既有调用不传则行为不变；Task 4 回归逐一确认。
- **回退**：任一任务可独立 revert；默认 policy 行为在 Task 1–4 全完成后仍与阶段 4 一致（opt-in）。
