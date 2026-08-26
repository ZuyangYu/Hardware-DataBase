# Rerank + 检索可观测性 ledger（阶段 3）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-28-rerank-ledger-stage3-design.md`

**Goal:** 在 `_validated_evidence` 之后、送 writer 之前加 LLM-as-judge `EvidenceReranker`（受 `allowed_tools` 守门，旧 policy pass-through），按 requirement 相关性重排已校验证据（v1 不截断）；为每个 unit 构造 `RetrievalLedgerRow`（`{unit_id, original_query, rewrites[], per_source[], fallback_triggered, final_evidence_ids}`），嵌入 matrix row 复用 `save_evidence_matrix` 持久化，并回填 `HarnessExecutionResult.retrieval_ledger`（修复预留字段从未写入 result 的 latent 缺口）。

**Architecture:** 新增 `src/document_authoring/writers/evidence_reranker.py`（`EvidenceReranker`，仿 `query_rewriter.py`）。改 `graph.py`（`HarnessExecutionResult.retrieval_ledger` + `AuthoringGraph.reranker` + per-unit rerank/ledger 接入 + `_retrieval_ledger_row`/`_evidence_fallback`）。改 `models.py`（allowed_tools 默认 +`rerank_evidence`）、`service.py`（`_reranker_for_policy` + 透传）、`runtime.py`（reranker 形参透传）。

**Tech Stack:** Python 3，pytest，unittest.mock；`InformationRequirement`、`LLMClient`、`HarnessPolicy`、`RetrievalOutcome`/`RetrievalSourceOutcome`。

## Global Constraints

- 不修改 `_validated_evidence`、`build_knowledge_base_retrieval_outcome`、`RetrieverRegistry`、`ProjectEvidenceRetrievalService.retrieve`、`SpreadsheetSemanticTool`、`InformationRequirement`、`RetrievalOutcome`/`RetrievalSourceOutcome`、`QueryRewriter`、`_rewrite_for_retry`。
- rerank 在 graph 层、`_validated_evidence` **之后**，只重排已校验证据 dict（v1 不截断），不增删、不改 evidence 内容；不破坏落域校验。
- `fallback_triggered` 从 `outcome.evidences`（保留 metadata）取，**不用** validated dict（project 路径已剥 metadata）。
- `rerank_evidence` 受 `HarnessPolicy.allowed_tools` 守门；旧冻结 policy -> reranker=None -> pass-through（与 Stage 1 `rewrite_query` 同前例）。不加 `prompt_version`/新预算字段变动。
- matrix_row 新增 `retrieval_ledger` 键、`HarnessExecutionResult` 新增 `retrieval_ledger` 字段为**追加**；现有测试若断言确切键集需同步更新。
- TDD：每任务先写失败测试再实现再提交。提交信息带 `Co-Authored-By: Claude <noreply@anthropic.com>`。
- `docs/superpowers/` 被 gitignore，新文件需 `git add -f`。

---

### Task 1: EvidenceReranker + 单测

**Files:**
- Create: `src/document_authoring/writers/evidence_reranker.py`
- Test: `tests/test_evidence_reranker.py`

**Interfaces:**
- Consumes: `InformationRequirement`（`src/agents/claim_evidence.py:58`）、`LLMClient.chat(messages, **kwargs)`（`src/core/llm_client.py:194`，`usage_stage` 自由 kwarg）、`_strip_code_fences`（`src/document_authoring/writers/managed.py:287`）。
- Produces:
  - `EvidenceReranker(provider_id="evidence_reranker")`，`__init__(client: LLMClient | None = None)`，`rerank(requirement, evidence: list[dict[str, Any]], top_k: int | None = None) -> list[dict[str, Any]]`。

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_evidence_reranker.py`，覆盖 spec §4.1 的 8 个用例。fake `LLMClient`：子类化或 `Mock(spec=LLMClient)`，`chat.return_value` / `chat.side_effect` 控制返回。evidence dict 用 `{"id": ..., "content": ...}`（含 `_validated_evidence` 产出的键）。

```python
from __future__ import annotations
from unittest.mock import Mock
from src.core.llm_client import LLMClient
from src.agents.claim_evidence import InformationRequirement
from src.document_authoring.writers.evidence_reranker import EvidenceReranker

def _req(subject="额定电流", predicate="电源拓扑"):
    return InformationRequirement(
        requirement_id="r", semantic_unit_id="field:f1", claim_type="attribute",
        subject=subject, predicate=predicate, required_capabilities=["entity_lookup"],
    )

def _ev(eid, content):
    return {"id": eid, "content": content, "source_name": "s.pdf",
            "metadata": {}, "locator": {}, "fact_type": None}

def _client(return_value="[]"):
    client = Mock(spec=LLMClient)
    client.chat.return_value = return_value
    return client
```

用例要点：
1. `test_rerank_reorders_by_llm_ranking`：3 条 evidence，LLM 返回 `"[2, 0, 1]"` -> 结果按该序。
2. `test_rerank_passthrough_on_empty`：`[]` -> `[]`，`chat` 不被调。
3. `test_rerank_passthrough_on_single`：1 条 -> 原样，`chat` 不被调。
4. `test_rerank_passthrough_on_llm_failure`：`chat.side_effect = RuntimeError` -> 原序。
5. `test_rerank_passthrough_on_parse_failure`：`chat.return_value = "not json"` -> 原序；`"{}"`（非 list）-> 原序。
6. `test_rerank_truncates_with_top_k`：LLM 返回 `"[2,0,1]"`，`top_k=2` -> 前 2。
7. `test_rerank_drops_invalid_indices_keeps_unreferenced`：4 条，LLM 返回 `"[3, 9, 0]"`（9 越界）-> 丢 9，序为 `[e3, e0]`，未引用的 `e1, e2` 原序追加 -> 4 条不丢。
8. `test_rerank_uses_evidence_rerank_usage_stage`：断言 `chat` 调用的 kwargs 含 `usage_stage="evidence_rerank"`。

- [ ] **Step 2: Implement `evidence_reranker.py`**

实现 `EvidenceReranker`：`rerank` 先处理空/单条短路；构 prompt（system 要求 JSON int 数组、0-based、最相关在前、ONLY 输出数组；user 给 requirement 字段 + 逐条 `{idx, id, content}`，content 截断到 ~500 字符防超长）；`self._client.chat([system, user], usage_stage="evidence_rerank")`；解析 `_strip_code_fences` -> `json.loads` -> 校验 list[int] 且 `0 <= i < len` -> 按序重排、未引用原序追加 -> `top_k` 截断 -> 异常/解析失败原序返回。`logger.warning` 记录失败。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_evidence_reranker.py -q` 全绿后提交：`feat: add EvidenceReranker for LLM-as-judge relevance rerank`。

---

### Task 2: graph 接入 rerank + ledger

**Files:**
- Modify: `src/document_authoring/harness/graph.py`
- Test: `tests/test_authoring_graph_rerank_ledger.py`

**Interfaces:**
- `HarnessExecutionResult` 增 `retrieval_ledger: list[dict[str, Any]] = field(default_factory=list)`。
- `AuthoringGraph.__init__` 增 `reranker: "EvidenceReranker | None" = None`（`TYPE_CHECKING` import，仿 `QueryRewriter`）。
- `run()` per-unit：`_validated_evidence` 后、matrix_row 前，插入 rerank（`if evidence and self.reranker is not None: self._step(...); self.policy.require_tool("rerank_evidence"); evidence = self.reranker.rerank(requirement, evidence)`）。
- 新增 `_retrieval_ledger_row(unit_id, requirement, outcome, evidence, state) -> dict`、`_evidence_fallback(evidence) -> bool`。
- per-unit 构造 ledger_row -> `result.retrieval_ledger.append` + `matrix_row["retrieval_ledger"] = ledger_row`；`matrix_row.evidence_ids` 用 post-rerank。

- [ ] **Step 1: Write the failing tests**

创建 `tests/test_authoring_graph_rerank_ledger.py`，覆盖 spec §4.2 的 7 个用例。复用现有 harness 测试的 fake 模式：fake `retrieve` 闭包返回 `RetrievalOutcome`，fake `draft_provider`，fake `validator`，`HarnessToolPolicy(approved_policy)`。构造 `AuthoringGraph` 用 `object.__new__` 或正常 `__init__`。

关键辅助：
- `_policy(allowed=("retrieve_evidence","draft_ready_unit","validate_unit_draft","detect_template_contamination","validate_cross_unit","rerank_evidence"))`。
- fake reranker：`Mock()`，`rerank.side_effect = lambda req, ev, **k: list(reversed(ev))`（反转序，便于断言 post-rerank）。
- ledger 用例的 outcome：`RetrievalOutcome(requirement_id=..., status="success_with_hits", evidences=[带 fallback metadata 的 Evidence], source_outcomes=[2 个 RetrievalSourceOutcome], query_fingerprint=..., applied_source_set_snapshot_id=..., applied_region_policy_versions={})`；改写记录通过 `state` 初始注入或让 `_retrieve_with_budget` 触发（后者更真）。

用例要点：
1. `test_rerank_applied_when_reranker_injected`：2 条 evidence + 反转 reranker -> 送 draft 的 evidence_ids 为反转序；matrix_row.evidence_ids 同。
2. `test_rerank_skipped_when_reranker_none`：reranker=None -> 原序；`state["step_count"]` 不含额外 rerank step（对比注入时多 1）。
3. `test_rerank_step_gated_by_require_tool`：allowed_tools 去掉 `rerank_evidence` + 强注入 reranker -> run 抛 PermissionError（被 `HarnessBudgetExceeded`? 否，PermissionError 直传）。
4. `test_ledger_records_query_rewrites_per_source_fallback`：outcome.evidences 带 `metadata={"ragflow_source_name_fallback": True}` + 2 source_outcomes；触发改写（rewriter mock 返回串）-> ledger 行 `original_query`/`rewrites` 非空/`per_source` 2 项/`fallback_triggered=True`/`final_evidence_ids` 匹配。
5. `test_ledger_survived_in_result`：`result.retrieval_ledger` 含每 unit 一行。
6. `test_ledger_for_empty_unit`：retrieve 返回 `success_empty` -> 该 unit 仍有 ledger 行 + matrix row，`final_evidence_ids=[]`。
7. `test_matrix_row_embeds_ledger`：`matrix_row["retrieval_ledger"]` 与 `result.retrieval_ledger` 对应行相等。

- [ ] **Step 2: Implement graph.py changes**

按 §3.1/§3.3/§3.5 实现：`HarnessExecutionResult` 加字段；`AuthoringGraph.__init__` 加 `reranker`；`run()` 插入 rerank + ledger 构造 + matrix_row.evidence_ids 用 post-rerank；新增 `_retrieval_ledger_row`/`_evidence_fallback`。`TYPE_CHECKING` import `EvidenceReranker`。

注意：rerank 在 `if not evidence: continue` **之前**对有证据的 unit 执行；空证据 unit 跳过 rerank 但仍构造 ledger/matrix_row。matrix_row 构造从"验证后立即 append"改为"rerank 后 append"。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_authoring_graph_rerank_ledger.py -q` 全绿后提交：`feat: rerank evidence and persist per-unit retrieval ledger in authoring graph`。

---

### Task 3: policy/服务/运行时接线

**Files:**
- Modify: `src/document_authoring/models.py`（allowed_tools 默认）
- Modify: `src/document_authoring/service.py`（`_reranker_for_policy` + 透传）
- Modify: `src/document_authoring/harness/runtime.py`（reranker 形参透传）
- Test: `tests/test_harness_policy.py`

**Interfaces:**
- `HarnessPolicy.allowed_tools` 默认追加 `"rerank_evidence"`。
- `service.py` 增 `_reranker_for_policy(policy)`（仿 `_rewriter_for_policy`，`models.py:1110`）；`run_internal_harness`/`resume_internal_harness` 构造 `reranker` 透传 `runtime.run`。
- `runtime.py run()` 增 `reranker: "EvidenceReranker | None" = None` 形参，`AuthoringGraph(..., reranker=reranker)`。

- [ ] **Step 1: Write/extend failing tests**

扩展 `tests/test_harness_policy.py`：
- `test_default_allowed_tools_includes_rerank_evidence`：`HarnessPolicy(...)` 默认 allowed_tools 含 `rerank_evidence`。
- `test_reranker_for_policy_returns_reranker_when_allowed`：`_reranker_for_policy`（policy 含 `rerank_evidence`）-> `EvidenceReranker` 实例。
- `test_reranker_for_policy_returns_none_when_not_allowed`：allowed_tools 去掉 -> `None`。

- [ ] **Step 2: Implement wiring**

`models.py:336` allowed_tools 默认 list 追加 `"rerank_evidence"`。`service.py` 加 `_reranker_for_policy`（`if "rerank_evidence" in policy.allowed_tools: from ... import EvidenceReranker; return EvidenceReranker()` else `None`）；`run_internal_harness`/`resume_internal_harness` 在 `rewriter = self._rewriter_for_policy(policy)` 旁加 `reranker = self._reranker_for_policy(policy)`，透传 `runtime.run(..., reranker=reranker)`（`runtime.py:772/837` 两处）。`runtime.py run()` 签名加 `reranker` 形参，`AuthoringGraph(..., reranker=reranker)`（`runtime.py:186`）。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_harness_policy.py -q` 全绿后提交：`feat: gate evidence rerank via HarnessPolicy allowlist and wire through runtime`。

---

### Task 4: 更新改进方案 .md + 最终回归

**Files:**
- Modify: `docs/Hardware-DataBase_文档生成改进方案.md`

- [ ] **Step 1: Apply doc corrections (spec §7)**

1. §3 阶段 3 改动点 1 补"v1 只重排不截断、top_k 截断能力预留待阶段 4 启用"。
2. §3 阶段 3 改动点 2 补"ledger 行嵌入 matrix row 复用 `save_evidence_matrix` 持久化、回填 `HarnessExecutionResult.retrieval_ledger`（修复预留字段从未写入 result 的 latent 缺口）"。
3. §3 阶段 3 补"fallback 标志从 `outcome.evidences` 取（project 路径 validated dict 已剥 metadata）"。
4. §7 实施状态表标记阶段 3 已实施（拆"阶段 3–5 | 未实施"为"阶段 3 | 已实施" + "阶段 4–5 | 未实施"）。

- [ ] **Step 2: Final regression**

`pytest tests/test_evidence_reranker.py tests/test_authoring_graph_rerank_ledger.py tests/test_harness_policy.py tests/test_retriever_registry.py tests/test_knowledge_base_document_work_orders.py tests/test_project_retriever_dispatch.py tests/test_retrieve_with_budget.py tests/test_query_rewriter.py tests/test_document_authoring_p2a.py tests/test_full_generation_flow.py tests/test_app_pipeline_scope.py tests/test_document_auto_generation.py -q` 全绿。重点核查：现有测试断言 matrix_row 键集 / `HarnessExecutionResult` 字段处是否需同步更新。

- [ ] **Step 3: Commit doc**

`git add -f` spec/plan；提交：`docs: mark stage 3 (rerank + retrieval ledger) as implemented`。

---

## Rollback

每任务独立提交，可逐 commit 回滚。Task 1 纯新增模块（无副作用）；Task 2 改 graph（回滚后恢复"无 rerank、无 ledger 回填"，matrix_row 键集复原）；Task 3 改 policy 默认值 + 接线（回滚后 reranker 不注入）；Task 4 仅文档。
