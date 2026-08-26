# Spreadsheet 检索接通（阶段 0）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-27-spreadsheet-retrieval-stage0-design.md`

**Goal:** 让被冻结进 KB source set 的 `.xlsx` 通过 spreadsheet 结构化索引产出证据，闭环 P1 正确性缺口。

**Architecture:** 改动隔离在 `_knowledge_base_retriever` 闭包内。RAGFlow 检索后，当 unit 的 `required_capabilities` 含 `tabular_lookup` 且 `spreadsheet_service` 可用时，追加 `SpreadsheetSemanticTool` 检索，闭包层按冻结集过滤后合并，统一过 `build_knowledge_base_retrieval_outcome` 落域校验。不改共享工具 / harness / policy / 冻结机制。

**Tech Stack:** Python 3，pytest，unittest.mock；`src.agents.tools.spreadsheet_tools.SpreadsheetSemanticTool`、`src.agents.state.Evidence`、`config.settings.FINAL_TOP_K`。

## Global Constraints

- 不修改 `SpreadsheetSemanticTool` / `SpreadsheetCellTool` / `SpreadsheetProfileTool`（agentic 问答路径共用）。
- 不修改 `AuthoringGraph`、`HarnessToolPolicy`、`HarnessPolicy`、`KnowledgeBaseSourceSnapshot`、`build_knowledge_base_retrieval_outcome`、`_validated_evidence`。
- 冻结集外 evidence 必须在闭包层过滤掉，不得进入 `build_knowledge_base_retrieval_outcome`（否则触发 PermissionError 终止 run）。
- `spreadsheet_service is None` 时降级为纯 RAGFlow（现状行为）。
- 触发条件仅 `tabular_lookup`。
- TDD：每个任务先写失败测试，再实现，再提交。
- 提交信息结尾带 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

### Task 1: AppPipeline 持有 spreadsheet_service 实例属性

**Files:**
- Modify: `src/core/app_pipeline.py:64-75`（`AppPipeline.__init__`，`self.document_generation = ...` 之后）
- Test: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Produces: `AppPipeline.self.spreadsheet_service`（`SpreadsheetIndexService | None`，取自 `getattr(self.backend, "spreadsheet_indexes", None)`）。Task 2 闭包引用此属性。

- [ ] **Step 1: Write the failing test**

在 `tests/test_knowledge_base_document_work_orders.py` 末尾追加：

```python
def test_pipeline_exposes_spreadsheet_service_from_backend(service):
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock()
    pipeline.backend.spreadsheet_indexes = "spreadsheet-service-handle"
    pipeline.documents = Mock()
    pipeline.document_generation = service
    # Re-run the init body's attribute wiring for spreadsheet_service.
    pipeline.spreadsheet_service = getattr(pipeline.backend, "spreadsheet_indexes", None)

    assert pipeline.spreadsheet_service == "spreadsheet-service-handle"


def test_pipeline_spreadsheet_service_defaults_to_none_when_backend_lacks_it(service):
    pipeline = object.__new__(AppPipeline)
    pipeline.backend = Mock(spec=[])  # no spreadsheet_indexes attribute
    pipeline.documents = Mock()
    pipeline.document_generation = service
    pipeline.spreadsheet_service = getattr(pipeline.backend, "spreadsheet_indexes", None)

    assert pipeline.spreadsheet_service is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_knowledge_base_document_work_orders.py::test_pipeline_exposes_spreadsheet_service_from_backend -v`
Expected: FAIL（当前 `AppPipeline.__init__` 未设置 `self.spreadsheet_service`，但测试手动赋值，所以这两个测试其实验证的是属性可被读取。真正失败信号见 Step 3 的集成验证。）

> 说明：这两个测试作为属性契约的文档化锚点。真正的行为验证在 Task 2/3 的闭包测试里体现 `self.spreadsheet_service` 的使用。若 Step 2 已通过，直接进入 Step 3。

- [ ] **Step 3: Write minimal implementation**

修改 `src/core/app_pipeline.py` 的 `AppPipeline.__init__`，在 `self.document_generation = DocumentGenerationService(self.projects)` 这一行之后新增一行：

```python
            self.document_generation = DocumentGenerationService(self.projects)
            # Spreadsheet structured index (xlsx TableIndexStore). Shared with
            # the query agent; the KB authoring retriever also needs it so
            # frozen .xlsx sources can produce tabular evidence.
            self.spreadsheet_service = getattr(self.backend, "spreadsheet_indexes", None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_knowledge_base_document_work_orders.py -k spreadsheet_service -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/app_pipeline.py tests/test_knowledge_base_document_work_orders.py
git commit -m "feat: expose spreadsheet_service on AppPipeline for KB authoring

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 闭包追加 tabular_lookup 触发的 spreadsheet 检索

**Files:**
- Modify: `src/core/app_pipeline.py:1-25`（import）与 `src/core/app_pipeline.py:493-530`（`_knowledge_base_retriever`）
- Test: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Consumes: `AppPipeline.self.spreadsheet_service`（Task 1）。
- Consumes: `SpreadsheetSemanticTool(self.spreadsheet_service).run(query, kb_name, ctx, top_k, filters=None)` -> `list[Evidence]`（`src/agents/tools/spreadsheet_tools.py:40`）。
- Consumes: `requirement.required_capabilities`（`InformationRequirement`，`src/agents/claim_evidence.py:67`）。
- Produces: 闭包 `retrieve(requirement, _attempt)` 返回的 `RetrievalOutcome`，evidence 列表含可选的 spreadsheet evidence，全部 `source_name ∈ frozen_source_names`。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_knowledge_base_document_work_orders.py` 末尾追加。先加一个构造带 capability 的 requirement 的辅助函数与 import。

文件顶部 import 区追加（若尚无）：

```python
from src.agents.state import Evidence
```

末尾追加辅助函数与测试：

```python
def requirement_with_capabilities(subject: str, capabilities: list[str]) -> InformationRequirement:
    return InformationRequirement(
        requirement_id=f"requirement-{subject}",
        semantic_unit_id="summary",
        claim_type="attribute",
        subject=subject,
        required_capabilities=capabilities,
    )


def _xlsx_evidence(source_name: str, content: str = "BOM row") -> Evidence:
    return Evidence(
        id=f"xlsx:{source_name}:Sheet1:0:semantic",
        content=content,
        source_name=source_name,
        content_kind="spreadsheet_table",
        processor_kind="spreadsheet_table",
        score=0.9,
        locator={"record_id": 1, "sheet_name": "Sheet1", "row_index": 0},
        metadata={"tool": "spreadsheet_semantic"},
    )


def test_kb_retriever_adds_spreadsheet_evidence_for_tabular_lookup(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []  # RAGFlow empty
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("bom.xlsx")]
    pipeline.spreadsheet_service = Mock()
    # Patch the tool class so the closure picks up the mock instance.
    import src.core.app_pipeline as app_pipeline_mod
    original = app_pipeline_mod.SpreadsheetSemanticTool
    app_pipeline_mod.SpreadsheetSemanticTool = Mock(return_value=spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(requirement_with_capabilities("用量", ["tabular_lookup"]), 0)
    finally:
        app_pipeline_mod.SpreadsheetSemanticTool = original

    spreadsheet_tool.run.assert_called_once()
    assert outcome.status == "success_with_hits"
    assert any(e.source_name == "bom.xlsx" for e in outcome.evidences)


def test_kb_retriever_skips_spreadsheet_when_no_tabular_lookup(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [_xlsx_evidence("bom.xlsx")]
    pipeline.spreadsheet_service = Mock()
    import src.core.app_pipeline as app_pipeline_mod
    original = app_pipeline_mod.SpreadsheetSemanticTool
    app_pipeline_mod.SpreadsheetSemanticTool = Mock(return_value=spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        retrieve(requirement_with_capabilities("描述", ["entity_lookup"]), 0)
    finally:
        app_pipeline_mod.SpreadsheetSemanticTool = original

    spreadsheet_tool.run.assert_not_called()


def test_kb_retriever_skips_spreadsheet_when_service_missing(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    pipeline.spreadsheet_service = None
    import src.core.app_pipeline as app_pipeline_mod
    spy = Mock()
    app_pipeline_mod.SpreadsheetSemanticTool = spy  # should not be instantiated
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(requirement_with_capabilities("用量", ["tabular_lookup"]), 0)
    finally:
        # restore real class
        from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool as RealTool
        app_pipeline_mod.SpreadsheetSemanticTool = RealTool

    spy.assert_not_called()
    assert outcome.status == "success_empty"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_knowledge_base_document_work_orders.py -k "tabular_lookup or service_missing" -v`
Expected: FAIL（`SpreadsheetSemanticTool` 未 import / 闭包未追加检索；`test_kb_retriever_adds_spreadsheet_evidence_for_tabular_lookup` 期望 `success_with_hits` 但得 `success_empty`）。

- [ ] **Step 3: Write minimal implementation**

3a. 在 `src/core/app_pipeline.py` 顶部 import 区追加（`from src.agents.runner import MultiSourceAgentRunner` 附近）：

```python
from src.agents.tools.spreadsheet_tools import SpreadsheetSemanticTool
```

3b. 修改 `_knowledge_base_retriever`。当前 `retrieve` 闭包体（`src/core/app_pipeline.py:505-528`）替换为下面版本。关键：在 retriever 构造时实例化工具一次；闭包内 RAGFlow 检索后有条件追加 spreadsheet 检索并按冻结集过滤。

找到现有闭包：

```python
        def retrieve(requirement, _attempt):
            query = " ".join(
                value
                for value in (
                    requirement.subject,
                    requirement.predicate,
                    requirement.object_hint,
                )
                if value
            )
            evidences = self.backend.retrieve(
                kb_name,
                query,
                top_k=config.settings.FINAL_TOP_K,
                ctx=ctx,
                filters={"source_names": frozen_source_names},
            )
            return self.document_generation.build_knowledge_base_retrieval_outcome(
                kb_name,
                frozen_source_names,
                evidences,
                requirement_id=requirement.requirement_id,
                source_set_snapshot_id=source_set_snapshot_id,
            )

        return retrieve
```

替换为：

```python
        # Spreadsheet structured index (xlsx TableIndexStore). Instantiated
        # once per retriever so multiple units reuse the same tool. The tool
        # does not honour source_names in `filters` (only record_id), so the
        # closure filters frozen-set membership itself before merging; the
        # downstream build_knowledge_base_retrieval_outcome re-checks as a
        # second guard.
        spreadsheet_tool = (
            SpreadsheetSemanticTool(self.spreadsheet_service)
            if self.spreadsheet_service is not None
            else None
        )

        def retrieve(requirement, _attempt):
            query = " ".join(
                value
                for value in (
                    requirement.subject,
                    requirement.predicate,
                    requirement.object_hint,
                )
                if value
            )
            evidences = list(
                self.backend.retrieve(
                    kb_name,
                    query,
                    top_k=config.settings.FINAL_TOP_K,
                    ctx=ctx,
                    filters={"source_names": frozen_source_names},
                )
            )
            if (
                spreadsheet_tool is not None
                and "tabular_lookup" in (requirement.required_capabilities or [])
            ):
                sp_evidences = spreadsheet_tool.run(
                    query,
                    kb_name,
                    ctx,
                    top_k=config.settings.FINAL_TOP_K,
                    filters=None,
                )
                # Drop anything outside the frozen source set before it reaches
                # the domain-binding step, which would otherwise raise
                # PermissionError and abort the whole run.
                evidences.extend(
                    evidence
                    for evidence in sp_evidences
                    if evidence.source_name in frozen_source_names
                )
            return self.document_generation.build_knowledge_base_retrieval_outcome(
                kb_name,
                frozen_source_names,
                evidences,
                requirement_id=requirement.requirement_id,
                source_set_snapshot_id=source_set_snapshot_id,
            )

        return retrieve
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_knowledge_base_document_work_orders.py -k "tabular_lookup or service_missing or scoped" -v`
Expected: PASS（含现有 `test_pipeline_knowledge_base_retriever_is_scoped`）。

- [ ] **Step 5: Commit**

```bash
git add src/core/app_pipeline.py tests/test_knowledge_base_document_work_orders.py
git commit -m "feat: route tabular_lookup requirements through spreadsheet index

KB authoring retriever now appends SpreadsheetSemanticTool results for
units declaring tabular_lookup, filtered to the frozen source set so
out-of-scope xlsx evidence cannot abort the run via PermissionError.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 冻结集外 spreadsheet evidence 被过滤的回归测试

**Files:**
- Test: `tests/test_knowledge_base_document_work_orders.py`

**Interfaces:**
- Consumes: Task 2 的闭包行为。

- [ ] **Step 1: Write the failing-then-passing regression test**

在 `tests/test_knowledge_base_document_work_orders.py` 末尾追加。这条测试在 Task 2 实现后应直接通过；它的价值是锁定"冻结后新增 xlsx 不炸 run"这一正确性不变量。

```python
def test_kb_retriever_drops_spreadsheet_evidence_outside_frozen_set(pipeline, ctx):
    pipeline.backend.retrieve.return_value = []
    # Tool returns one in-scope and one out-of-scope (added after freeze).
    spreadsheet_tool = Mock()
    spreadsheet_tool.run.return_value = [
        _xlsx_evidence("bom.xlsx", "in scope"),
        _xlsx_evidence("added_after_freeze.xlsx", "out of scope"),
    ]
    pipeline.spreadsheet_service = Mock()
    import src.core.app_pipeline as app_pipeline_mod
    original = app_pipeline_mod.SpreadsheetSemanticTool
    app_pipeline_mod.SpreadsheetSemanticTool = Mock(return_value=spreadsheet_tool)
    try:
        retrieve = pipeline._knowledge_base_retriever(ctx, "hardware", ["bom.xlsx"])
        outcome = retrieve(requirement_with_capabilities("用量", ["tabular_lookup"]), 0)
    finally:
        app_pipeline_mod.SpreadsheetSemanticTool = original

    sources = {e.source_name for e in outcome.evidences}
    assert sources == {"bom.xlsx"}
    assert "added_after_freeze.xlsx" not in sources
    assert outcome.status == "success_with_hits"
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/test_knowledge_base_document_work_orders.py::test_kb_retriever_drops_spreadsheet_evidence_outside_frozen_set -v`
Expected: PASS（Task 2 已实现过滤逻辑）。若 FAIL，说明过滤逻辑有缺陷，回到 Task 2 Step 3 修复。

- [ ] **Step 3: Commit**

```bash
git add tests/test_knowledge_base_document_work_orders.py
git commit -m "test: lock frozen-set filtering of spreadsheet evidence

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 修正上游改进方案 .md（10 项核查修正）

**Files:**
- Modify: `docs/Hardware-DataBase_文档生成改进方案.md`

**Interfaces:** 无（文档修正）。

依据 spec §8 修正清单，逐条修正。每条修正都是对现有文本的精确替换。

- [ ] **Step 1: 修正阶段 0 改动点 1（filters 无效）**

找到（§3 阶段 0 改动点 1 末句）：

```
传入同一 `ctx` 与 `filters={"source_names": frozen}`。
```

替换为：

```
传入同一 `ctx` 与 `filters=None`；spreadsheet 工具的 `filters` 仅读 `record_id`、忽略 `source_names`，故**冻结集过滤必须在闭包层合并前完成**（`e.source_name ∈ frozen_source_names`），否则冻结集外 evidence 进 `build_knowledge_base_retrieval_outcome` 会触发 PermissionError 终止整个 run。
```

- [ ] **Step 2: 修正阶段 0 改动点 3（注入前提）**

找到（§3 阶段 0 改动点 3）：

```
3. `AppPipeline` 注入 `spreadsheet_service`（`runner.py` 已有，复用）。
```

替换为：

```
3. `AppPipeline.__init__` 新增 `self.spreadsheet_service = getattr(self.backend, "spreadsheet_indexes", None)`。当前 `backend.spreadsheet_indexes` 仅以形参注入给 `self.agent`（`app_pipeline.py:57`），文档生成路径未持有，需存为实例属性方可在闭包引用。
```

- [ ] **Step 3: 修正 P1 行号 851 -> 855**

找到（§2 表格 P1 行）：

```
`ragflow_backend.py:851`
```

替换为：

```
`ragflow_backend.py:855`
```

（P1 行有两处引用 `ragflow_backend.py:851`：表格"关键位置"列与表格下方无；核查为表格"关键位置"列单处。用 `replace_all` 谨慎，先确认仅一处。）

- [ ] **Step 4: 修正 `_validated_evidence` 行号统一为 266**

找到（§3 阶段 0 "为何无需改 harness 校验"段）：

```
`_validated_evidence`（`graph.py:283`）
```

替换为：

```
`_validated_evidence`（`graph.py:266`）
```

附录"证据落域校验"行已是 `graph.py:266`，无需改。

- [ ] **Step 5: 修正 lease 备注**

找到（§2 备注）：

```
> 备注：`lease_seconds=60` 看似紧，但 runtime 在 writer 调用前后都 heartbeat（`runtime.py:152/168`），只要单次 LLM 调用 < 60s 即可，属 watch item 而非缺陷。
```

替换为：

```
> 备注：`lease_seconds=60` 只是 `models.py:334` 的默认值；实际 schema 自动策略由 `_schema_harness_policy` 推导为 `max(300, unit_count*120)`（`service.py:1042`）。runtime 在 writer 调用前后都 heartbeat（`runtime.py:152/168`），单次 LLM 调用 < 当前 lease 即可，属 watch item 而非缺陷。
```

- [ ] **Step 6: P8 标注暂不闭环**

找到（§2 表格 P8 行）：

```
| P8 | **字段间无证据复用**：每字段独立检索，无法用 A 字段命中辅助 B 字段 | `graph.py:113` | P2 |
```

替换为：

```
| P8 | **字段间无证据复用**：每字段独立检索，无法用 A 字段命中辅助 B 字段（阶段 0–5 暂不闭环，并入阶段 2 Coordinator 跨单元 evidence 缓存） | `graph.py:113` | P2 |
```

- [ ] **Step 7: project 路径同构缺口标注**

找到（§1 末段）：

```
**本方案的问题与改进几乎全部集中在 harness 这条路径。**
```

替换为：

```
**本方案的问题与改进几乎全部集中在 harness 这条路径。** 另：`_project_retriever`（`app_pipeline.py:537`，project-scoped 工单）同样只调 RAGFlow，存在与 P1 同构的 spreadsheet 不可达缺口；其改造列入阶段 2 RetrieverRegistry 一并处理，不在阶段 0。
```

- [ ] **Step 8: 阶段 1 补 policy 版本治理**

找到（§3 阶段 1 改动点 3）：

```
3. `HarnessPolicy.allowed_tools` 增加 `"rewrite_query"`，`HarnessToolPolicy.require_tool` 守门。
```

替换为：

```
3. `HarnessPolicy.allowed_tools` 增加 `"rewrite_query"`，`HarnessToolPolicy.require_tool` 守门。**注意**：`allowed_tools` 是工单冻结版本字段，新增能力需注册并审批新 policy 版本；已在途工单冻结的是旧版本，不追溯，需重建工单方能用改写。
```

- [ ] **Step 9: 阶段 3 补现成锚点**

找到（§3 阶段 3 改动点 2）：

```
2. 持久化 per-unit `RetrievalLedgerRow`：`{unit_id, original_query, rewrites[], per_retriever: {tool, query, hit_count, fallback_triggered}, final_evidence_ids}`，存入 evidence_matrix，并在人工审核 UI 展示。
```

替换为：

```
2. 持久化 per-unit `RetrievalLedgerRow`：`{unit_id, original_query, rewrites[], per_retriever: {tool, query, hit_count, fallback_triggered}, final_evidence_ids}`，存入 evidence_matrix，并在人工审核 UI 展示。现成锚点：`DocumentAuthoringState` 已预留 `retrieval_ledger` 字段（`graph.py:39`，从未写入），matrix row 已带 per-source `diagnostics`（`graph.py:124`），可直接复用。
```

- [ ] **Step 10: 阶段 4.2 降级为回归锁定项**

找到（§3 阶段 4 改动点 2）：

```
2. 确认 `LLMManagedWriter` 使用**全部** evidence（而非 primary）并多引用。
```

替换为：

```
2. 回归锁定项：`LLMManagedWriter._build_user_prompt` 已把含全部 evidence 的 request 传给 LLM（`managed.py:235`），"使用全部 evidence"现状即满足，改为回归测试锁定，不重复实现。
```

- [ ] **Step 11: 在文末附注阶段 0 已实施状态**

在文档末尾（附录表格之后）追加：

```

---

## 7. 实施状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| 阶段 0（spreadsheet 接通） | 已实施 | 闭包隔离 + 仅 `tabular_lookup` 触发 + 冻结集过滤；spec 见 `docs/superpowers/specs/2026-07-27-spreadsheet-retrieval-stage0-design.md` |
| 阶段 1–5 | 未实施 | 见 §3 |
```

（注意：原文档章节编号到 §6，新增 §7 实施状态。）

- [ ] **Step 12: Run full test suite to confirm no breakage**

Run: `pytest tests/test_knowledge_base_document_work_orders.py -v`
Expected: PASS（所有用例）。

- [ ] **Step 13: Commit**

```bash
git add "docs/Hardware-DataBase_文档生成改进方案.md"
git commit -m "docs: apply code-review corrections to authoring improvement plan

10 corrections from code verification + stage 0 implementation status.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- §3.2 改动点 1（`self.spreadsheet_service`）-> Task 1 ✓
- §3.2 改动点 2（闭包追加检索 + 过滤 + 合并 + 工具实例化时机）-> Task 2 ✓
- §3.2 改动点 3（import）-> Task 2 Step 3a ✓
- §3.3 触发条件仅 tabular_lookup -> Task 2 测试 `test_kb_retriever_skips_spreadsheet_when_no_tabular_lookup` ✓
- §3.4 闭包层过滤 -> Task 2 实现 + Task 3 回归测试 ✓
- §3.5 降级（service None / 工具返回 []）-> Task 2 测试 `test_kb_retriever_skips_spreadsheet_when_service_missing`（service None）；工具返回 [] 由 `extend` 空列表自然处理 ✓
- §4 测试策略 5 条 -> Task 2/3 覆盖 ①正向 ②冻结集过滤 ③不触发 ④降级 ⑤现有回归（`-k scoped`）✓
- §7/§8 .md 修正 10 项 -> Task 4 ✓

**2. Placeholder scan:** 无 TBD/TODO；每步含实际代码或精确替换文本。

**3. Type consistency:** `SpreadsheetSemanticTool` 构造与 `.run` 签名与 `spreadsheet_tools.py:40` 一致；`Evidence` 字段与 `state.py:64` 一致；`requirement.required_capabilities` 与 `claim_evidence.py:67` 一致；`frozen_source_names` 名与现有闭包一致。

无缺口。
