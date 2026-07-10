# Circuit Query Phase 1 Follow-Up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正阶段 1 电路查询增量接入中的验收缺口，并为 Phase 2 查询质量增强建立稳定边界。

**Architecture:** 保留 develop 的 `src/pipelines/*` 摄入架构与 `src/agents/*` LangGraph 多源查询架构。电路能力继续作为 `circuit_design` pipeline、`CircuitIndexService` service boundary、`CircuitQueryTool` agent tool 增量接入，不恢复旧 `src/query_router/*` 顶层路由。

**Tech Stack:** Python 3.12, unittest, LangGraph, Pydantic, SpyDrNet, local filesystem-backed pipeline stores.

## Global Constraints

- 不恢复 `src/query_router/*`。
- 不恢复旧 `src/core/rag_pipeline.py` 或旧 `src/rag_backends/*` 应用结构。
- 不新增独立于 `MultiSourceAgentRunner` 的顶层 query router。
- 不把 `.edf` / `.edif` 上传到 RAGFlow；电路文件只走本地结构化 pipeline。
- 电路查询必须返回标准 `src.agents.state.Evidence`。
- 新增测试必须可被 Git 跟踪，并在 CI 环境中运行。
- 当前阶段优先修正阶段 1 闭环，不把旧 feature 分支整包代码一次性并入 develop。

---

### Task 1: 收束阶段 1 提交边界

**Files:**
- Add if not tracked: `src/agents/tools/circuit_tools.py`
- Add if not tracked: `src/circuit/__init__.py`
- Add if not tracked: `src/circuit/index_service.py`
- Add if not tracked: `src/circuit/models.py`
- Add if not tracked: `src/circuit/store.py`
- Add if not tracked: `src/circuit/parsers/__init__.py`
- Add if not tracked: `src/circuit/parsers/edf_parser.py`
- Add if not tracked: `src/circuit/parsers/edf_partition.py`
- Add if not tracked: `src/circuit/parsers/edf_power.py`
- Add if not tracked: `src/circuit/parsers/edif_lite_parser.py`
- Add if not tracked: `tests/test_circuit_agent_tool.py`
- Add if not tracked: `tests/test_circuit_index_service.py`
- Add if not tracked: `tests/test_circuit_pipeline_handler.py`
- Add if not tracked: `tests/test_circuit_pipeline_routing.py`
- Do not add yet: old advanced modules such as `src/circuit/query_agent.py`, `src/circuit/query_engine.py`, `src/circuit/query_tool.py`, `src/circuit/session_context_store.py`, `src/circuit/recovery_manager.py`, `src/circuit/llm_controlled_planner.py`, `src/circuit/relations/*`, `src/circuit/analyzers/*`, `src/ui/*`

**Interfaces:**
- Consumes: current phase 1 code already present in the working tree.
- Produces: a minimal Git-trackable phase 1 boundary that does not accidentally import the old feature-branch architecture.

- [ ] **Step 1: Check which phase 1 files are untracked**

Run:

```powershell
git status --short src\agents\tools\circuit_tools.py src\circuit tests\test_circuit_*.py
```

Expected: phase 1 files appear as `??` until they are intentionally added.

- [ ] **Step 2: Verify the minimal parser dependency set**

Run:

```powershell
rg "from src\.circuit|import src\.circuit" src\circuit\index_service.py src\circuit\store.py src\circuit\parsers\edf_parser.py src\circuit\parsers\edif_lite_parser.py src\circuit\parsers\edf_partition.py src\circuit\parsers\edf_power.py
```

Expected: direct imports remain inside the minimal set listed above, except optional fail-soft imports inside `store.py`.

- [ ] **Step 3: Stage only the minimal phase 1 boundary**

Run:

```powershell
git add src\agents\tools\circuit_tools.py `
  src\circuit\__init__.py `
  src\circuit\index_service.py `
  src\circuit\models.py `
  src\circuit\store.py `
  src\circuit\parsers\__init__.py `
  src\circuit\parsers\edf_parser.py `
  src\circuit\parsers\edf_partition.py `
  src\circuit\parsers\edf_power.py `
  src\circuit\parsers\edif_lite_parser.py `
  tests\test_circuit_agent_tool.py `
  tests\test_circuit_index_service.py `
  tests\test_circuit_pipeline_handler.py `
  tests\test_circuit_pipeline_routing.py
```

Expected: no old `src/circuit/query_*`, `relations`, `analyzers`, or `src/ui` files are staged.

- [ ] **Step 4: Inspect staged files**

Run:

```powershell
git diff --cached --name-only
```

Expected: only minimal phase 1 files plus already-reviewed develop integration files are staged.

- [ ] **Step 5: Run focused verification**

Run:

```powershell
python -m unittest discover -s tests -p "test_circuit_*.py" -v
python -m py_compile src\pipelines\registry.py src\pipelines\ingestion.py src\pipelines\runtime_factory.py src\agents\tools\circuit_tools.py src\circuit\index_service.py src\circuit\store.py src\circuit\models.py
```

Expected: circuit tests pass and compilation succeeds.

### Task 2: 保留电路索引失败记录，避免上传失败后被外层清理掉

**Files:**
- Modify: `src/pipelines/ingestion.py`
- Test: `tests/test_circuit_pipeline_handler.py`
- Create: `tests/test_circuit_ingestion_orchestrator.py`

**Interfaces:**
- Consumes: `HandlerResult`, `IngestionOrchestrator.upload_files()`, `CircuitPipelineHandler.submit()`.
- Produces: `HandlerResult.preserve_failed_record: bool = False`; circuit parser/index failure returns `success=False` but keeps failed ledger row and archived source.

- [ ] **Step 1: Write failing orchestrator test**

Create `tests/test_circuit_ingestion_orchestrator.py` with a test named `test_circuit_index_failure_preserves_failed_record_and_archive`.

The test should build an `IngestionOrchestrator` with:

```python
handlers={PROCESSOR_KIND_CIRCUIT: CircuitPipelineHandler(... circuit_index=_CircuitIndex(fail=True))}
```

Assert:

```python
self.assertEqual(result.failed_count, 1)
self.assertEqual(store.deleted, [])
self.assertTrue(os.path.exists(archived_path))
self.assertEqual(store.progress_updates[-1]["status"], "failed")
```

- [ ] **Step 2: Run the new test and confirm failure**

Run:

```powershell
python -m unittest tests.test_circuit_ingestion_orchestrator -v
```

Expected before implementation: fails because `IngestionOrchestrator._cleanup_failed_submission()` calls `CircuitPipelineHandler.rollback()` and deletes the failed record/archive.

- [ ] **Step 3: Add preserve flag to `HandlerResult`**

In `src/pipelines/ingestion.py`, extend the dataclass:

```python
@dataclass
class HandlerResult:
    success: bool
    message: str
    document_id: str = ""
    record_id: int | None = None
    status: str = ""
    uploaded_to_remote: bool = False
    warnings: list[str] = field(default_factory=list)
    audit_action: str = ""
    audit_metadata: dict = field(default_factory=dict)
    preserve_failed_record: bool = False
```

- [ ] **Step 4: Respect the flag in orchestrator failure handling**

In `IngestionOrchestrator.upload_files()`, replace the failed branch with:

```python
else:
    if not handler_result.preserve_failed_record:
        self._cleanup_failed_submission(handler, handler_result, scope, archived)
    failed_count += 1
    messages.append(handler_result.message)
```

- [ ] **Step 5: Set the flag on circuit indexing failure**

In `CircuitPipelineHandler.submit()` failure return:

```python
return HandlerResult(
    success=False,
    message=f"[failed] Circuit design indexing failed: {archived.filename}: {exc}",
    document_id=document_id,
    record_id=record_id,
    status=RAGFLOW_STATUS_FAILED,
    warnings=warnings,
    audit_action="circuit_upload_failed",
    audit_metadata={...},
    preserve_failed_record=True,
)
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
python -m unittest tests.test_circuit_ingestion_orchestrator tests.test_circuit_pipeline_handler -v
```

Expected: all tests pass; failed circuit records remain visible for retry/debug.

### Task 3: 查询端显式复用上传端的 `CircuitIndexService`

**Files:**
- Modify: `src/pipelines/document_rag/ragflow_backend.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/agents/runner.py`
- Modify: `tests/test_circuit_agent_tool.py`

**Interfaces:**
- Consumes: `PipelineRuntimeBundle.circuit_indexes`.
- Produces: `MultiSourceAgentRunner(..., circuit_service: CircuitIndexService | None = None)` and `CircuitQueryTool(index_service=circuit_service)`.

- [ ] **Step 1: Replace brittle runner registration test**

In `tests/test_circuit_agent_tool.py`, replace source-string assertion with behavior:

```python
def test_runner_uses_injected_circuit_service(self):
    circuit_index = _CircuitIndex()
    runner = MultiSourceAgentRunner(
        rag_backend=_FakeRAGBackend(),
        circuit_service=circuit_index,
    )
    self.assertIs(runner.tools["circuit_query"].index_service, circuit_index)
```

Use the existing fake backend pattern from `tests/test_agentic_runner.py`; do not require a real RAGFlow connection.

- [ ] **Step 2: Run the test and confirm failure**

Run:

```powershell
python -m unittest tests.test_circuit_agent_tool.CircuitAgentToolTests.test_runner_uses_injected_circuit_service -v
```

Expected before implementation: `MultiSourceAgentRunner` does not accept `circuit_service`.

- [ ] **Step 3: Expose circuit indexes from RAGFlow backend**

In `RAGFlowBackend.__init__()` after `self.spreadsheet_indexes = runtime_bundle.spreadsheet_indexes`, add:

```python
self.circuit_indexes = runtime_bundle.circuit_indexes
```

- [ ] **Step 4: Add runner constructor parameter**

In `src/agents/runner.py`:

```python
from src.circuit.index_service import CircuitIndexService

def __init__(
    self,
    *,
    rag_backend: RAGBackend,
    document_store: PipelineDocumentStore | None = None,
    spreadsheet_service: SpreadsheetIndexService | None = None,
    circuit_service: CircuitIndexService | None = None,
    llm_client: LLMClient | None = None,
):
    ...
    self.circuit_service = circuit_service or CircuitIndexService()
```

Then build tools with:

```python
"circuit_query": CircuitQueryTool(self.circuit_service),
```

- [ ] **Step 5: Wire AppPipeline query side to backend runtime service**

In `src/core/app_pipeline.py`:

```python
self.agent = MultiSourceAgentRunner(
    rag_backend=self.backend,
    document_store=getattr(self.backend, "store", None),
    spreadsheet_service=getattr(self.backend, "spreadsheet_indexes", None),
    circuit_service=getattr(self.backend, "circuit_indexes", None),
)
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
python -m unittest tests.test_circuit_agent_tool -v
python -m py_compile src\core\app_pipeline.py src\agents\runner.py src\pipelines\document_rag\ragflow_backend.py
```

Expected: injected service is used; compilation succeeds.

### Task 4: 收紧电路证据的部门隔离

**Files:**
- Modify: `src/circuit/index_service.py`
- Test: `tests/test_circuit_index_service.py`

**Interfaces:**
- Consumes: `RequestContext.metadata["department_id"]` or `RequestContext.metadata["resource_department_id"]`.
- Produces: when a request is department-scoped, only matching indexed circuit metadata can return evidence.

- [ ] **Step 1: Add failing cross-department tests**

Add two tests:

```python
def test_query_excludes_other_department_circuit_metadata(self):
    ...
    service.index_file(... department_id="dept_a")
    hits = service.query(
        kb_name="kb_hw",
        query="CAN0",
        ctx=RequestContext(user_id="bob", metadata={"department_id": "dept_b"}),
    )
    self.assertEqual(hits, [])

def test_query_excludes_missing_department_metadata_when_context_is_scoped(self):
    ...
    service.index_file(... department_id="")
    hits = service.query(
        kb_name="kb_hw",
        query="CAN0",
        ctx=RequestContext(user_id="bob", metadata={"department_id": "dept_b"}),
    )
    self.assertEqual(hits, [])
```

- [ ] **Step 2: Run tests and confirm the missing-metadata case fails**

Run:

```powershell
python -m unittest tests.test_circuit_index_service -v
```

Expected before implementation: other department is excluded, but missing department metadata can leak.

- [ ] **Step 3: Tighten department check**

In `CircuitIndexService.query()` replace:

```python
if department_id and meta.get("department_id") and meta.get("department_id") != department_id:
    continue
```

with:

```python
if department_id and str(meta.get("department_id") or "") != department_id:
    continue
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m unittest tests.test_circuit_index_service -v
```

Expected: all index service tests pass and scoped requests cannot read unscoped circuit metadata.

### Task 5: 避免 agent 对未索引/失败的电路记录发起查询

**Files:**
- Modify: `src/agents/graph.py`
- Modify: `src/agents/tools/pipeline_catalog_tool.py`
- Test: `tests/test_agentic_runner.py` or a new `tests/test_circuit_agent_planning.py`

**Interfaces:**
- Consumes: catalog source `status`, `processor_kind`, `content_kind`.
- Produces: `circuit_query` only planned for `status == "indexed"` circuit records; catalog summary includes circuit count.

- [ ] **Step 1: Add deterministic planning test**

Create `tests/test_circuit_agent_planning.py` with:

```python
def test_plan_source_selection_skips_failed_circuit_sources(self):
    state = {
        "user_query": "CAN0 连接到哪里",
        "question_analysis": {
            "sub_questions": [
                {"id": "sq_1", "question": "CAN0 连接到哪里", "expected_evidence": ["circuit_design"]}
            ]
        },
        "catalog": {
            "sources": [
                {
                    "document_name": "bad.edf",
                    "processor_kind": "circuit_design",
                    "content_kind": "circuit_design",
                    "status": "failed",
                },
                {
                    "document_name": "good.edf",
                    "processor_kind": "circuit_design",
                    "content_kind": "circuit_design",
                    "status": "indexed",
                },
            ]
        },
        "trace": [],
    }
    result = plan_source_selection(state)
    planned = result["source_plan"]["source_plan"]
    self.assertEqual([item["source_name"] for item in planned], ["good.edf"])
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```powershell
python -m unittest tests.test_circuit_agent_planning -v
```

Expected before implementation: failed circuit source may still be selected.

- [ ] **Step 3: Skip unindexed circuit sources in `_source_matches_analysis`**

In `src/agents/graph.py`, before returning true for circuit sources:

```python
if processor == "circuit_design":
    if str(source.get("status") or "") != "indexed":
        return False, "电路文件尚未索引成功，跳过结构化电路检索。"
    if "circuit_design" in expected:
        return True, "该文件是结构化电路设计数据，适合查询网表、引脚、网络连接和拓扑事实。"
```

- [ ] **Step 4: Add circuit count to catalog summary**

In `PipelineCatalogTool.scan()` summary:

```python
"circuits": sum(1 for item in sources if item.processor_kind == "circuit_design"),
```

- [ ] **Step 5: Run planning and circuit tests**

Run:

```powershell
python -m unittest tests.test_circuit_agent_planning -v
python -m unittest discover -s tests -p "test_circuit_*.py" -v
```

Expected: failed circuit sources are skipped; indexed sources still plan `circuit_query`.

### Task 6: 恢复完整 agent 验证环境

**Files:**
- No source edit expected unless dependency files are stale.
- Verify: `pyproject.toml`
- Verify: `requirements.txt`
- Verify: `uv.lock`

**Interfaces:**
- Consumes: declared `langgraph` dependency.
- Produces: local and CI environments can import `src.agents.graph`.

- [ ] **Step 1: Confirm dependency declaration**

Run:

```powershell
Select-String -Path pyproject.toml,requirements.txt -Pattern "langgraph"
```

Expected: `langgraph` appears in project dependencies.

- [ ] **Step 2: Sync or install dependencies in the active environment**

Run one of:

```powershell
uv sync
```

or:

```powershell
python -m pip install -r requirements.txt
```

Expected: `python -c "import langgraph"` exits with code 0.

- [ ] **Step 3: Run full agent runner tests**

Run:

```powershell
python -m unittest tests.test_agentic_runner -v
```

Expected: tests import `src.agents.graph` successfully and either pass or expose real behavior failures unrelated to missing dependencies.

- [ ] **Step 4: Run final focused verification**

Run:

```powershell
python -m unittest discover -s tests -p "test_circuit_*.py" -v
python -m unittest tests.test_agentic_runner -v
python -m py_compile src\core\app_pipeline.py src\pipelines\ingestion.py src\pipelines\runtime_factory.py src\pipelines\document_rag\ragflow_backend.py src\agents\runner.py src\agents\graph.py src\agents\tools\circuit_tools.py src\circuit\index_service.py
```

Expected: all commands pass in the synced environment.

### Task 7: Phase 2 入口，只迁移被 `CircuitIndexService` 消费的查询质量能力

**Files:**
- Modify later: `src/circuit/index_service.py`
- Add later only when directly used: selected helpers from `src/circuit/query_engine.py`, `src/circuit/entity_resolver.py`, `src/circuit/query_evidence.py`, `src/circuit/response_policy.py`
- Do not add: `src/query_router/*`

**Interfaces:**
- Consumes: stable `CircuitIndexService.query(...) -> list[Evidence]`.
- Produces: better entity matching, scoped circuit lookup, and evidence ranking without changing top-level query architecture.

- [ ] **Step 1: Write a Phase 2 fixture-driven quality test**

Add tests that query realistic aliases:

```python
hits = service.query(kb_name="kb_hw", query="CAN PHY 的 CAN0 网络连接", ctx=ctx)
self.assertTrue(any("U1200" in hit.content and "J3" in hit.content for hit in hits))
```

- [ ] **Step 2: Move only the minimum helper behind `CircuitIndexService`**

Import helper code only through `src/circuit/index_service.py`; no caller outside `src/circuit` should instantiate old `CircuitQueryAgent` or old `CircuitQueryTool`.

- [ ] **Step 3: Keep external contract unchanged**

Run:

```powershell
python -m unittest discover -s tests -p "test_circuit_*.py" -v
```

Expected: phase 1 tests still pass while quality tests improve.

## Self-Review

- Spec coverage: preserves develop architecture, keeps circuit query incremental, adds failure retention, shared service injection, department isolation, indexed-only planning, and dependency verification.
- Placeholder scan: no `TBD` or open-ended "add tests" steps remain; each task contains concrete files, commands, and expected output.
- Type consistency: `HandlerResult.preserve_failed_record`, `CircuitIndexService.query`, `MultiSourceAgentRunner.circuit_service`, and `CircuitQueryTool(index_service=...)` are named consistently across tasks.
