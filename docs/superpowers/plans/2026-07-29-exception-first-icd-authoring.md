# 异常优先 ICD 正式生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让登录用户通过正式知识库工作单自动采用高置信度 ICD 事实，只批量处理异常，并以冻结范围和管脚集合校验保证候选文档可追溯、完整且可发布。

**Architecture:** 新增独立的 ICD 范围决策领域模块：它从冻结来源中的 EDF 结构化管脚、FPT/需求直接引用和模板字段语义生成可持久化决策。AppPipeline 在运行 Harness 前建立或读取该决策；未处理异常阻止运行，已冻结决策作为附加结构化证据注入检索。生成后，集合验证将冻结的 EDF 管脚集与候选表格逐项比对，批准入口拒绝任何阻断问题。

**Tech Stack:** Python 3.12、Pydantic、SQLite `DocumentAuthoringStore`、CircuitStore/CircuitIndexService、RAGFlow 检索、Streamlit、pytest。

## Global Constraints

- 不写死项目名、连接器位号、网络名、文件名或模板单元格坐标。
- 只使用工作单的冻结来源集合；每个决策项保存证据来源与版本。
- EDF 中的 NC、PGND 和其他管脚不得静默删除。
- 仅“批准并发布”可以发布制品；反馈和范围处理均不能绕过发布校验。
- 高置信度项无需逐项确认；全部异常只允许一次批量处理。

---

### Task 1: ICD 范围决策领域模型与纯规则

**Files:**
- Create: `src/document_authoring/icd_scope_decision.py`
- Create: `tests/test_icd_scope_decision.py`

**Interfaces:**
- Consumes: circuit `Evidence.metadata["pin_mappings"]`、文本证据的 `source_name` 与 `content`。
- Produces: `IcdScopeDecision.build(circuit_evidences, supporting_evidences) -> IcdScopeDecision`，包含 `auto_items`、`exceptions`、`frozen_pin_mappings`。

- [ ] **Step 1: Write the failing test**

```python
def test_direct_circuit_and_supporting_reference_are_auto_adopted():
    decision = build_icd_scope_decision(
        circuit_evidences=[pin_mapping("J7", [("1", "CAN_H")])],
        supporting_evidences=[evidence("FPT", "J7-1 CAN communication")],
    )
    assert decision.auto_items[0].refdes == "J7"
    assert decision.exceptions == []
    assert decision.frozen_pin_mappings == [{"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_icd_scope_decision.py`

Expected: FAIL because `icd_scope_decision` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
class IcdScopeDecision(BaseModel):
    decision_id: str
    auto_items: list[IcdScopeItem]
    exceptions: list[IcdScopeException]
    frozen_pin_mappings: list[dict[str, str | None]]

def build_icd_scope_decision(circuit_evidences, supporting_evidences) -> IcdScopeDecision:
    # Require a direct pin mapping and a direct RefDes-pin reference before auto adoption.
    ...
```

Treat unmapped pins as `NC`, produce `extra_pin_exposure` for PGND/other pins without direct external evidence, and produce `unsupported_reservation` for “预留/裁剪” wording without a direct source reference.

- [ ] **Step 4: Add exception tests**

```python
def test_pgnd_without_direct_external_reference_requires_one_exception():
    decision = build_icd_scope_decision([pin_mapping("J7", [("3", "PGND")])], [])
    issue = decision.exceptions[0]
    assert issue.kind == "extra_pin_exposure"
    assert issue.recommended_action == "mark_pending"
    assert issue.user_instruction == "确认该脚是否需要在对外 ICD 中暴露。"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m pytest -q tests/test_icd_scope_decision.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/document_authoring/icd_scope_decision.py tests/test_icd_scope_decision.py
git commit -m "feat: decide ICD scope from direct evidence"
```

### Task 2: 持久化范围快照和一次性异常处理

**Files:**
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_icd_scope_review_service.py`

**Interfaces:**
- Consumes: `IcdScopeDecision` 和 `DocumentWorkOrder`。
- Produces: `DocumentGenerationService.prepare_icd_scope_review(ctx, work_order_id, decision)`、`submit_icd_scope_resolution(ctx, work_order_id, resolutions, comment)`、`get_icd_scope_review(ctx, work_order_id)`。

- [ ] **Step 1: Write the failing service test**

```python
def test_scope_exceptions_are_resolved_in_one_batch_and_frozen():
    review = service.prepare_icd_scope_review(ctx, order.work_order_id, decision_with_one_exception())
    frozen = service.submit_icd_scope_resolution(
        ctx, order.work_order_id,
        resolutions=[{"exception_id": review.exceptions[0].exception_id, "action": "exclude"}],
        comment="PGND 不进入线束 ICD",
    )
    assert frozen.status == "frozen"
    assert service.get_icd_scope_review(ctx, order.work_order_id).pending_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_icd_scope_review_service.py`

Expected: FAIL because scope review persistence/service API does not exist.

- [ ] **Step 3: Add models and SQLite storage**

Create `IcdScopeReview` / `IcdScopeResolution` Pydantic records and a `document_icd_scope_reviews` table keyed by `work_order_id`. Store the decision JSON, its content hash, `pending|frozen` status, source snapshot hash, and batch resolution event. Reject a second resolution batch and reject a decision whose source snapshot differs from the work order.

- [ ] **Step 4: Add service authorization and state tests**

```python
def test_unresolved_scope_review_blocks_harness_execution():
    service.prepare_icd_scope_review(ctx, order.work_order_id, decision_with_one_exception())
    with pytest.raises(ValueError, match="unresolved ICD scope exceptions"):
        service.run_internal_harness(ctx, order.work_order_id, retrieve=retriever)
```

```python
def test_feedback_cannot_change_frozen_scope_review():
    service.submit_document_feedback(ctx, artifact_id, comment="please change PGND")
    assert service.get_icd_scope_review(ctx, order.work_order_id).status == "frozen"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest -q tests/test_icd_scope_review_service.py tests/test_document_generation_feedback.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/document_authoring/models.py src/document_authoring/work_order_store.py src/document_authoring/service.py tests/test_icd_scope_review_service.py
git commit -m "feat: freeze ICD scope review decisions"
```

### Task 3: 正式检索注入与生成前集合校验

**Files:**
- Modify: `src/circuit/index_service.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/document_authoring/service.py`
- Create: `src/document_authoring/icd_validation.py`
- Create: `tests/test_icd_scope_pipeline.py`
- Create: `tests/test_icd_validation.py`

**Interfaces:**
- Consumes: `CircuitIndexService.list_pin_mapping_evidence(kb_name, source_names, ctx)` 和冻结 `IcdScopeReview`。
- Produces: `_knowledge_base_retriever(..., icd_scope_review=review)`，在 `relationship_lookup` 请求中附加冻结 pin mapping Evidence；`validate_icd_pin_set(expected_mappings, artifact_content, target_format) -> list[dict]`。

- [ ] **Step 1: Write the failing pipeline test**

```python
def test_kb_auto_run_returns_scope_review_before_harness_when_exception_exists():
    result = pipeline.auto_generate_knowledge_base_document(ctx, knowledge_base_name="kb-a", ...)
    assert result["stage"] == "scope_review_required"
    assert result["exceptions"][0]["user_instruction"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_icd_scope_pipeline.py`

Expected: FAIL because AppPipeline always starts the Harness.

- [ ] **Step 3: Implement public circuit enumeration and decision creation**

Add `CircuitIndexService.list_pin_mapping_evidence` that enumerates pin mapping evidence only from specified frozen EDF source names and current department authorization. In `AppPipeline.auto_generate_knowledge_base_document`, build an ICD scope decision only when the selected template has relationship-lookup pin semantics. Return `scope_review_required` before Harness for unresolved exceptions; otherwise inject the frozen mappings into relationship retrieval so every table writer sees the complete selected pin set.

- [ ] **Step 4: Write the failing validation test**

```python
def test_validation_blocks_missing_selected_edf_pin():
    issues = validate_icd_pin_set(
        [{"refdes": "J7", "pin_name": "1", "net_name": "CAN_H"}],
        generated_workbook_without("J7-1"), "xlsx",
    )
    assert issues == [{"code": "icd_pin_missing", "severity": "blocking", "key": "j7:1"}]
```

- [ ] **Step 5: Implement and wire validation**

Extract pin tables with the generic bilingual/header logic from `icd_comparison.py`. Add missing, duplicate, net-mismatch and unresolved-exception blocking issues to the candidate validation report. `approve_document_artifact` must reject a report containing an ICD blocking issue even if its coarse status was previously passed.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run python -m pytest -q tests/test_icd_scope_pipeline.py tests/test_icd_validation.py tests/test_knowledge_base_document_work_orders.py tests/test_circuit_index_service.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/circuit/index_service.py src/core/app_pipeline.py src/document_authoring/service.py src/document_authoring/icd_validation.py tests/test_icd_scope_pipeline.py tests/test_icd_validation.py
git commit -m "feat: gate ICD generation on frozen pin scope"
```

### Task 4: 简明异常待办界面与端到端回归

**Files:**
- Modify: `src/ui/document_generation_page.py`
- Modify: `tests/test_document_generation_page.py`
- Modify: `tests/test_document_generation_review_ui.py`
- Create: `tests/test_icd_login_flow_regression.py`

**Interfaces:**
- Consumes: `pipeline.get_icd_scope_review(ctx, work_order_id)` 和 `pipeline.submit_icd_scope_resolution(...)`。
- Produces: 一个按异常聚合的待办表和单个“应用处理结果并继续生成”动作。

- [ ] **Step 1: Write the failing UI test**

```python
def test_scope_review_ui_explains_only_exception_actions():
    rendered = render_with_scope_review(one_pgnd_exception())
    assert "发现的问题" in rendered.labels
    assert "系统建议" in rendered.labels
    assert "你需要做什么" in rendered.labels
    assert "X1900-1" not in rendered.text  # auto-adopted rows stay collapsed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_document_generation_page.py tests/test_document_generation_review_ui.py`

Expected: FAIL because scope-review UI is absent.

- [ ] **Step 3: Implement the compact review UI**

Show `已自动确认 N 项` as a collapsed summary. Render only unresolved exceptions with the four required explanatory fields. Use one selectbox per exception and one batch submit button; after success, offer “继续生成候选文档”. Keep existing candidate preview, feedback, download and approval controls unchanged.

- [ ] **Step 4: Add end-to-end regression**

```python
def test_logged_in_icd_flow_injects_frozen_pins_and_requires_only_pgnd_decision():
    result = run_logged_in_kb_flow(edf_pins=fixture_pins(), fpt=fixture_fpt(), template=icd_template())
    assert result.auto_adopted_count == 18
    assert result.exceptions == ["extra_pin_exposure"]
    assert result.candidate_pin_set == result.frozen_pin_set
```

- [ ] **Step 5: Run final verification**

Run: `uv run python -m pytest -q tests/test_icd_scope_decision.py tests/test_icd_scope_review_service.py tests/test_icd_scope_pipeline.py tests/test_icd_validation.py tests/test_icd_login_flow_regression.py tests/test_document_generation_page.py tests/test_document_generation_review_ui.py tests/test_document_generation_feedback.py tests/test_generic_circuit_authoring_flow.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ui/document_generation_page.py tests/test_document_generation_page.py tests/test_document_generation_review_ui.py tests/test_icd_login_flow_regression.py
git commit -m "feat: streamline ICD exception review"
```

## Plan Self-Review

- Spec coverage: tasks 1–2 implement evidence-backed automatic adoption and one-batch exceptions; task 3 freezes/injects scope and blocks invalid output; task 4 supplies concise UI and login-flow regression.
- Placeholder scan: no deferred behaviors; each task names files, interfaces, test behavior and commands.
- Type consistency: all later tasks consume `IcdScopeDecision`/`IcdScopeReview` created in tasks 1–2; pipeline and validator use the frozen pin mappings defined there.
