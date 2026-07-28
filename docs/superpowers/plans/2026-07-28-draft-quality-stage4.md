# 草稿质量（阶段 4）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-28-draft-quality-stage4-design.md`

**Goal:** `DeterministicEvidenceWriter` 多证据结构化汇总（单证据原样，闭环 P5）；回归锁定 `LLMManagedWriter._build_user_prompt` 传全部 evidence；新增 `RequirementFitChecker`（LLM-as-judge，闭环 P10）注入 graph，未通过置 `requires_human`，受 `requirement_fit_check` allowlist 守门，失败降级 pass。

**Architecture:** 改 `writers/managed.py`（`_deterministic_draft`）；新增 `writers/requirement_fit_checker.py`；改 `harness/graph.py`（`fit_checker` 接入）、`models.py`（allowed_tools +`requirement_fit_check`）、`service.py`（`_fit_checker_for_policy` + 透传）、`runtime.py`（fit_checker 形参）。

**Tech Stack:** Python 3，pytest，unittest.mock；`DocumentUnitDraft`、`InformationRequirement`、`LLMClient`、`DocumentValidator`。

## Global Constraints

- 不改 `DocumentValidator` 确定性校验逻辑、`LLMManagedWriter` 主路径、`WriterRequest`/`InformationRequirement` 模型、`_validated_evidence`、落域校验。
- `_deterministic_draft` 单证据保持 v1 原样（兼容 `test_template_upload_service.py:725-727`）；多证据枚举汇总、一条 summary assertion（避免单元内 cross-unit 冲突）；不捏造、不设 `validation_status`。
- `RequirementFitChecker` 独立注入 graph（仿 reranker），LLM 失败/旧 policy 降级 pass（不误判 requires_human）。
- `requirement_fit_check` 受 allowlist 守门；无 `prompt_version`/新预算字段变动。
- TDD：每任务先写失败测试再实现再提交。提交信息带 `Co-Authored-By: Claude <noreply@anthropic.com>`。`docs/superpowers/` 被 gitignore，新文件需 `git add -f`。

---

### Task 1: DeterministicEvidenceWriter 升级 + LLM prompt 回归锁定

**Files:**
- Modify: `src/document_authoring/writers/managed.py`（`_deterministic_draft`）
- Test: `tests/test_deterministic_writer.py`

- [ ] **Step 1: Write failing tests**

新建 `tests/test_deterministic_writer.py`：
1. `test_single_evidence_verbatim`：1 条 -> content=ev content、evidence_ids=[id]、1 assertion（text=content, evidence_ids=[id]）。
2. `test_multi_evidence_summarizes_all`：2 条 -> content 含两条 content、evidence_ids=两 id、1 assertion 引用两 id。
3. `test_multi_evidence_passes_validation_no_inner_conflict`：多证据 draft 过 `DocumentValidator().validate_unit_draft`（supported）且 `validate_cross_unit_consistency([draft])` 空。
4. `test_no_evidence_raises`：空 -> ValueError。
5. `test_llm_build_user_prompt_includes_all_evidence`：多证据 request -> `_build_user_prompt(request, None)` 含每条 id+content。

用 `WriterRequest(work_order_id=, run_id=, unit_id=, unit_label=, prompt_version=, evidence=[{id,content},...])`。验证 `_build_user_prompt` 直接 import 调用。

- [ ] **Step 2: Implement `_deterministic_draft` upgrade**

单证据原样；多证据 `body = "\n".join(f"[{i+1}] {text}")`、`evidence_ids=all`、一条 summary assertion（text=body, evidence_ids=all, consistency_key=unit_id）。抽 `_build_assertion(request, evidence_ids, text, assertion_id)` 辅助。不设 validation_status。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_deterministic_writer.py tests/test_template_upload_service.py -q` 全绿后提交：`feat: summarize all evidence in deterministic writer (closes P5)`。

---

### Task 2: RequirementFitChecker + graph 接入

**Files:**
- Create: `src/document_authoring/writers/requirement_fit_checker.py`
- Modify: `src/document_authoring/harness/graph.py`
- Test: `tests/test_requirement_fit_checker.py`、`tests/test_authoring_graph_fit_check.py`

- [ ] **Step 1: Write failing tests for RequirementFitChecker**

新建 `tests/test_requirement_fit_checker.py`（fake LLMClient）：
1. `check_fit_true`、2. `check_fit_false`、3. `check_degrades_on_llm_failure`、4. `check_degrades_on_parse_failure`、5. `check_uses_usage_stage`（见 spec §4.2）。

- [ ] **Step 2: Implement `requirement_fit_checker.py`**

`RequirementFitChecker.check(draft, requirement) -> {"fit": bool, "reason": str}`：构 prompt（requirement subject/predicate + draft.content[:500]）；`chat(..., usage_stage="requirement_fit_check")`；`_strip_code_fences`+`json.loads` -> dict -> `bool(payload.get("fit"))`/`str(payload.get("reason") or "")`；异常/非 dict -> `{"fit": True, "reason": "fit check unavailable"}`。

- [ ] **Step 3: Write failing tests for graph integration**

新建 `tests/test_authoring_graph_fit_check.py`（复用 Stage 3 测试的 KB-scope `graph.run()` fake 模式）：
1. `fit_check_marks_requires_human_when_unfit`、2. `fit_check_passes_when_fit`、3. `fit_check_skipped_when_none`、4. `fit_check_skipped_for_unsupported_unit`、5. `fit_check_gated_by_require_tool`（见 spec §4.3）。fit_checker 用 Mock；unsupported 用让 validator 返回 unsupported 的 draft（或 evidence 无 lexical anchor）。

- [ ] **Step 4: Implement graph.py changes**

`AuthoringGraph.__init__` 增 `fit_checker` 参数（TYPE_CHECKING import）；`run()` 校验流按 spec §3.5 扩展 `elif self.fit_checker is not None` 分支。

- [ ] **Step 5: Run tests, commit**

`pytest tests/test_requirement_fit_checker.py tests/test_authoring_graph_fit_check.py -q` 全绿后提交：`feat: add RequirementFitChecker and gate draft-requirement fit in authoring graph (closes P10)`。

---

### Task 3: policy/服务/运行时接线

**Files:**
- Modify: `src/document_authoring/models.py`、`src/document_authoring/service.py`、`src/document_authoring/harness/runtime.py`
- Test: `tests/test_harness_policy.py`

- [ ] **Step 1: Write/extend failing tests**

`tests/test_harness_policy.py` 增：`test_default_allowed_tools_do_not_include_requirement_fit_check`（opt-in，不进默认）、`test_fit_checker_for_policy_*`（None / RequirementFitChecker）。

- [ ] **Step 2: Implement wiring**

`models.py`：`requirement_fit_check` **不进默认 allowed_tools**（opt-in，因 fit check 是 status-changing LLM 门控，与 status-preserving 的 rerank/rewrite 不同；部署方显式启用）。`service.py` 加 `_fit_checker_for_policy` + `run_internal_harness`/`resume_internal_harness` 透传（`fit_checker=fit_checker`，两处 execute 调用）；`runtime.py execute()` 增 `fit_checker` 形参 + `AuthoringGraph(..., fit_checker=fit_checker)` + TYPE_CHECKING import。

- [ ] **Step 3: Run tests, commit**

`pytest tests/test_harness_policy.py -q` 全绿后提交：`feat: gate requirement fit check via HarnessPolicy allowlist and wire through runtime`。

---

### Task 4: 更新改进方案 .md + 最终回归

**Files:**
- Modify: `docs/Hardware-DataBase_文档生成改进方案.md`

- [ ] **Step 1: Apply doc corrections (spec §7)**

1. §3 阶段 4 改动点 1 补"单证据原样、多证据枚举汇总 + 一条 summary assertion"。
2. §3 阶段 4 改动点 3 修正为"独立 RequirementFitChecker 注入 graph，allowlist 守门，降级 pass"。
3. §7 状态表拆"阶段 4–5"为"阶段 4 | 已实施" + "阶段 5 | 未实施"。

- [ ] **Step 2: Final regression**

`pytest tests/test_deterministic_writer.py tests/test_requirement_fit_checker.py tests/test_authoring_graph_fit_check.py tests/test_authoring_graph_rerank_ledger.py tests/test_evidence_reranker.py tests/test_harness_policy.py tests/test_retriever_registry.py tests/test_knowledge_base_document_work_orders.py tests/test_project_retriever_dispatch.py tests/test_retrieve_with_budget.py tests/test_query_rewriter.py tests/test_template_upload_service.py tests/test_document_authoring_p2a.py tests/test_full_generation_flow.py tests/test_template_authoring_integration.py tests/test_app_pipeline_scope.py tests/test_document_auto_generation.py -q` 全绿。

- [ ] **Step 3: Commit doc**

`git add -f` spec/plan；提交：`docs: mark stage 4 (draft quality) as implemented`。

---

## Rollback

每任务独立提交。Task 1 改 `_deterministic_draft`（回滚恢复 v1 逐字）；Task 2 改 graph + 新增 checker（回滚恢复无 fit check）；Task 3 改 policy 默认 + 接线（回滚后 fit_checker 不注入）；Task 4 仅文档。
