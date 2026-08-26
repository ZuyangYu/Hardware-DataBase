# 文档生成优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让常规 XLSX/XLSM 模板只写入已验证的字段区，并在冻结来源范围内以字段契约取得全面、准确、可审计的多源证据。

**Architecture:** 模板由确定性规则先划分可填候选区，LLM 只提出字段契约。Harness 用契约构造查询、路由多源检索、截断并验证证据包；类型化草稿通过字段校验后才生成 FillPlan。DOCX 复用契约和检索约束，但首期不做专用结构识别。

**Tech Stack:** Python 3.11、Pydantic、pytest、OOXML/zipfile、RAGFlow、SpreadsheetSemanticTool、CircuitQueryTool。

## Global Constraints

- 检索只能访问 `SourceSetSnapshot` 或 `KnowledgeBaseSourceSnapshot` 的冻结来源。
- 不得自动写入公式、保护/隐藏单元、表头、固定标签、`layout_blank` 或 `unknown` 单元；仅有确定性标签锚点且为空的 `scalar_input` 可作为自动填充目标。
- 新增 Pydantic 字段必须有安全默认值，保证旧 Schema 与分析记录仍可读取。
- 每项按 TDD 执行；先写失败测试，再写最小实现。
- 不暂存或覆盖工作区无关用户改动。

---

## Files

- `src/document_authoring/template_analysis.py`：区域角色、字段建议合同。
- `src/document_authoring/template_analyzers.py`：XLSX/XLSM 可填写区分类。
- `src/document_authoring/template_suggester.py` 与 `template_activation.py`：建议解析和激活策略。
- `src/document_authoring/models.py`：字段契约、值模式和证据预算。
- `src/document_authoring/harness/graph.py`、`retriever_registry.py`、`src/core/app_pipeline.py`：查询、路由、选择与检索诊断。
- `src/document_authoring/writers/provider.py`、`writers/managed.py`、`validator.py`、`service.py`：类型化草稿、验证和 FillPlan。
- `tests/test_template_analyzers.py`、`tests/test_template_activation.py`、`tests/test_template_suggester.py`、`tests/test_retrieve_with_budget.py`、`tests/test_authoring_graph_rerank_ledger.py`、`tests/test_full_generation_flow.py`：回归测试。

### Task 1: 收紧自动激活与覆盖授权

**Files:** Modify `src/document_authoring/template_analysis.py`, `src/document_authoring/template_suggester.py`, `src/document_authoring/template_activation.py`; Test `tests/test_template_activation.py`, `tests/test_template_suggester.py`.

**Produces:** `TemplateAnalysisSuggestion.overwrite_basis: Literal["placeholder", "sample_value"] | None`; `approved_overwrite_unit_ids` 只包含经服务器规则确认的 sample value。

- [ ] **Step 1: 写失败测试。**

```python
def test_llm_target_does_not_bypass_layout_or_confidence_checks():
    analysis = _analysis(unit=TemplateAnalysisUnit(
        unit_id="sheet:Review!B1", locator={"sheet_name": "Review", "cell": "B1"},
        writable=True, value_kind="blank", structural_role_hint="layout_blank",
    ))
    analysis.approved_overwrite_unit_ids = ["sheet:Review!B1"]
    decision = decide_template_activation(analysis)
    assert decision.status == "requires_human"
    assert "layout_blank_target" in decision.reason_codes
```

- [ ] **Step 2: 运行失败测试。** Run `./.venv/bin/python -m pytest -q tests/test_template_activation.py tests/test_template_suggester.py`; expected: the new test fails because full-trust logic bypasses the check.
- [ ] **Step 3: 实现最小安全策略。** 新增 `overwrite_basis`; 仅当单元是 `placeholder`、有确定性标签锚点的空白 `scalar_input`，或单元角色为 `sample_value` 且建议 basis 为 `sample_value` 时允许自动写入。删除 `trusted = bool(approved_overwrite_unit_ids)` 对低置信度、布局和语义检查的短路；固定标签和表头永不自动覆盖。
- [ ] **Step 4: 运行 `./.venv/bin/python -m pytest -q tests/test_template_activation.py tests/test_template_suggester.py`; expected: PASS。**
- [ ] **Step 5: Commit。** Run `git add src/document_authoring/template_analysis.py src/document_authoring/template_suggester.py src/document_authoring/template_activation.py tests/test_template_activation.py tests/test_template_suggester.py && git commit -m "fix: restrict automatic template overwrites"`.

### Task 2: 确定性分类 XLSX/XLSM 候选填写区

**Files:** Modify `src/document_authoring/template_analysis.py`, `src/document_authoring/template_analyzers.py`; Test `tests/test_template_analyzers.py`.

**Produces:** `section_header`, `sample_value`, `scalar_input` 角色与 `TemplateAnalysisUnit.candidate_for_auto_fill: bool = False`。

- [ ] **Step 1: 写失败测试。** 为带标签的值对、placeholder、样例、表头、纯布局、公式和保护单元创建 fixture；断言 A2 标签为 `fixed_label`、B2 为 `scalar_input`、C2 布局空白不候选。
- [ ] **Step 2: 运行 `./.venv/bin/python -m pytest -q tests/test_template_analyzers.py`; expected: FAIL。**
- [ ] **Step 3: 实现 `_classify_workbook_regions(cells)`。** 在 `_analyze_workbook` 的每个 sheet 收集完单元后调用；顺序为：不可写保留阻断、显式 placeholder、邻接固定标签的空格、同结构样例值、连续标题文本、无锚点空格。分类器返回单元拷贝，不能改变 hash、坐标或可写限制。
- [ ] **Step 4: 运行 `./.venv/bin/python -m pytest -q tests/test_template_analyzers.py tests/test_template_activation.py`; expected: PASS。**
- [ ] **Step 5: Commit。** Run `git add src/document_authoring/template_analysis.py src/document_authoring/template_analyzers.py tests/test_template_analyzers.py && git commit -m "feat: classify workbook fill candidates"`.

### Task 3: 将字段契约写入真实查询

**Files:** Modify `src/document_authoring/models.py`, `src/document_authoring/harness/graph.py`, `src/core/app_pipeline.py`; Test `tests/test_retrieve_with_budget.py`, `tests/test_authoring_graph_rerank_ledger.py`.

**Produces:** `InformationRequirement.retrieval_query_terms: list[str] = []`，由 `_query_string(requirement)` 唯一构造真实查询。

- [ ] **Step 1: 写失败测试。** 构造 label=`额定电流`、description=`电源持续输出电流`、query_terms=`["continuous current"]`、subject_aliases=`["Iout"]` 的字段；断言 `_query_string(...) == "Iout continuous current 电源持续输出电流 额定电流"`。
- [ ] **Step 2: 运行 `./.venv/bin/python -m pytest -q tests/test_retrieve_with_budget.py tests/test_authoring_graph_rerank_ledger.py`; expected: FAIL。**
- [ ] **Step 3: 实现合同。** `_requirement_for_unit` 保序去重 aliases、terms、description、label，保存至 requirement；让 `_query_string` 使用此字段。替换 `AppPipeline._knowledge_base_retriever`、`_project_retriever` 内所有 label/description 手写拼接，并保持 `query_override` 优先。
- [ ] **Step 4: 运行 `./.venv/bin/python -m pytest -q tests/test_retrieve_with_budget.py tests/test_authoring_graph_rerank_ledger.py tests/test_full_generation_flow.py`; expected: PASS。**
- [ ] **Step 5: Commit。** Run `git add src/document_authoring/models.py src/document_authoring/harness/graph.py src/core/app_pipeline.py tests/test_retrieve_with_budget.py tests/test_authoring_graph_rerank_ledger.py && git commit -m "feat: use field contracts in document retrieval"`.

### Task 4: 有界多源证据包与一次定向补检

**Files:** Modify `src/document_authoring/models.py`, `src/document_authoring/retriever_registry.py`, `src/document_authoring/harness/graph.py`, `src/core/app_pipeline.py`; Test `tests/test_authoring_graph_rerank_ledger.py`, `tests/test_document_authoring_p2a.py`.

**Produces:** `DocumentFieldSchema.max_evidence_items: int = 5`（1..10），`select_field_evidence(...)` 和含 `discarded_evidence_ids`、`recovery_reason` 的 ledger。

- [ ] **Step 1: 写失败测试。** 提供 7 条命中（其中两条内容重复）；断言 Writer 只接收 5 条去重证据，ledger 记录被淘汰 ID。再测试 relationship 走 circuit、tabular 走 spreadsheet，且入选证据均在冻结范围内。
- [ ] **Step 2: 运行 `./.venv/bin/python -m pytest -q tests/test_authoring_graph_rerank_ledger.py tests/test_document_authoring_p2a.py`; expected: FAIL。**
- [ ] **Step 3: 实现选择与补检。** Rerank 后按首选来源角色、分数和稳定 ID 排序，按内容 hash 去重并截断；原始 outcome 不删除。仅首轮为空或值解析失败时允许一次 aliases 扩展或已有 rewriter 补检，不得放宽冻结来源限制。
- [ ] **Step 4: 运行 `./.venv/bin/python -m pytest -q tests/test_authoring_graph_rerank_ledger.py tests/test_document_authoring_p2a.py tests/test_project_retriever_dispatch.py`; expected: PASS。**
- [ ] **Step 5: Commit。** Run `git add src/document_authoring/models.py src/document_authoring/retriever_registry.py src/document_authoring/harness/graph.py src/core/app_pipeline.py tests/test_authoring_graph_rerank_ledger.py tests/test_document_authoring_p2a.py && git commit -m "feat: bound evidence per document field"`.

### Task 5: 类型化草稿、字段校验与 FillPlan 门槛

**Files:** Modify `src/document_authoring/models.py`, `src/document_authoring/writers/provider.py`, `src/document_authoring/writers/managed.py`, `src/document_authoring/validator.py`, `src/document_authoring/harness/graph.py`, `src/document_authoring/service.py`; Test `tests/test_document_authoring_safety.py`, `tests/test_full_generation_flow.py`.

**Produces:** `TypedFieldValue(kind, normalized_values, display_value, evidence_ids)`（挂在 `DocumentUnitDraft` 且具有兼容默认值）和 `validate_typed_field_draft(...)`。

- [ ] **Step 1: 写失败测试。** 为 scalar 字段传入 `proposed_value=["12 A", "15 A"]`，断言验证结果 `unsupported` 且说明必须有唯一规范值；测试枚举去重、逐项证据、冲突/低置信度/无证据均不产生 FillPlan。
- [ ] **Step 2: 运行 `./.venv/bin/python -m pytest -q tests/test_document_authoring_safety.py tests/test_full_generation_flow.py`; expected: FAIL。**
- [ ] **Step 3: 实现合同和校验。** `WriterRequest` 增加 value type；确定性 Writer 仅在安全抽取 scalar 或 enumeration 时生成类型化值，禁止全文拼接 evidence。LLM Writer 必须输出类型化值和现有 evidence IDs。`validate_typed_field_draft` 验证数量、格式、来源、低置信度与冲突；Harness 写入失败 unit status；`_semantic_fills` 只使用通过验证的 `display_value`。
- [ ] **Step 4: 运行 `./.venv/bin/python -m pytest -q tests/test_document_authoring_safety.py tests/test_full_generation_flow.py tests/test_template_authoring_integration.py`; expected: PASS。**
- [ ] **Step 5: Commit。** Run `git add src/document_authoring/models.py src/document_authoring/writers/provider.py src/document_authoring/writers/managed.py src/document_authoring/validator.py src/document_authoring/harness/graph.py src/document_authoring/service.py tests/test_document_authoring_safety.py tests/test_full_generation_flow.py && git commit -m "feat: validate typed document field drafts"`.

### Task 6: 真实模板评测与发布 Gate

**Files:** Create `evaluation/datasets/document_generation_v1.jsonl`; Modify `src/evaluation/dataset_loader.py`, `src/evaluation/hardware_metrics.py`, `src/evaluation/gates.py`; Test `tests/evaluation/test_dataset_loader.py`, `tests/evaluation/test_hardware_metrics.py`, `tests/evaluation/test_gates.py`.

**Produces:** 独立 `DocumentGenerationEvalRecord` 和 `template_mapping_precision`、`fixed_content_overwrite_rate`、`field_recall_at_k`、`evidence_support_rate`、`auto_approval_rate`。

- [ ] **Step 1: 写失败测试。** 加载 `{id, template_fixture, field_id, expected_value, allowed_sources}`，断言 `expected_value` 和允许来源被保留；不得改变现有问答 JSONL 的加载合同。
- [ ] **Step 2: 运行 `./.venv/bin/python -m pytest -q tests/evaluation/test_dataset_loader.py tests/evaluation/test_hardware_metrics.py tests/evaluation/test_gates.py`; expected: FAIL。**
- [ ] **Step 3: 实现评测与 Gate。** 独立记录/装载器/指标计算器；硬 gate 固定为 `fixed_content_overwrite_rate == 0.0`、`source_scope_violation_count == 0`、`unsupported_required_field_fill_count == 0`。映射与 Recall 先按模板类型告警，直至标注样本足够。
- [ ] **Step 4: 运行 `./.venv/bin/python -m pytest -q tests/evaluation/test_dataset_loader.py tests/evaluation/test_hardware_metrics.py tests/evaluation/test_gates.py`; expected: PASS，`AI_database测试_0804.jsonl` 继续可加载。**
- [ ] **Step 5: Commit。** Run `git add evaluation/datasets/document_generation_v1.jsonl src/evaluation/dataset_loader.py src/evaluation/hardware_metrics.py src/evaluation/gates.py tests/evaluation/test_dataset_loader.py tests/evaluation/test_hardware_metrics.py tests/evaluation/test_gates.py && git commit -m "test: add document generation quality gates"`.

## Final verification

- [ ] Run `./.venv/bin/python -m pytest -q tests/test_template_analyzers.py tests/test_template_activation.py tests/test_template_suggester.py tests/test_retrieve_with_budget.py tests/test_authoring_graph_rerank_ledger.py tests/test_document_authoring_safety.py tests/test_full_generation_flow.py tests/test_template_authoring_integration.py tests/evaluation`; expected: PASS.
- [ ] Run `./.venv/bin/python -m ruff check src tests`; expected: no violations.
- [ ] Run a frozen-source XLSX fixture end-to-end; assert fixed labels unchanged, every filled cell has a field contract plus supporting evidence, and missing required fields yield `waiting_human_input`.
- [ ] Run `git diff --check` and `git status --short`; stage no unrelated user changes.
