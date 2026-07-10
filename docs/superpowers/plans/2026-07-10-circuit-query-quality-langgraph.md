# Circuit Query Quality and LangGraph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将结构化电路查询质量能力接入 `CircuitIndexService`，并让 LangGraph 对电路、文档和 BOM 问题做正确的多源规划与补检索。

**Architecture:** `CircuitIndexService` 是唯一的电路领域适配边界：它调用 `CircuitQueryEngine`，按现有 department/source filter 收窄结果，再把结构化行映射为 `Evidence`。`src/agents/graph.py` 仍是唯一顶层 planner；只增强其证据类型识别和 tool/source 对齐校验。

**Tech Stack:** Python 3.12, unittest, Pydantic, LangGraph, SpyDrNet, NetworkX.

## Global Constraints

- 不恢复 `src/query_router/*`、旧 query agent、session context 或旧 Streamlit router。
- 不迁移 PDF/图像解析和 `src/ui/*`。
- `.edf/.edif` 只走本地 `circuit_design` pipeline，不上传 RAGFlow。
- `CircuitQueryTool.run()` 保持 `list[Evidence]` 返回契约。
- 任何电路 Evidence 都必须来自已索引的结构化电路数据。
- 带 department scope 的查询必须严格匹配 metadata。
- 只允许 `indexed` circuit record 进入 planner。

---

### Task 1: 引入纯结构化查询依赖

**Files:**
- Add: `src/circuit/query_engine.py`
- Add: `src/circuit/vector_index.py`
- Add: `src/circuit/relations/__init__.py`
- Add: `src/circuit/relations/models.py`
- Add: `src/circuit/relations/extractor.py`
- Add: `src/circuit/relations/derivers.py`
- Add: `src/circuit/relations/views.py`
- Test: `tests/test_circuit_query_engine.py`

**Interfaces:**
- Consumes: `CircuitStore`, `CircuitDesign`.
- Produces: `CircuitQueryEngine.search_instances/search_nets/search_modules/search_module_connections/search_module_power_nets` returning dictionaries with `design_id` and entity fields.

- [ ] **Step 1: Write a failing engine fixture test**

```python
def test_search_net_connections_returns_connected_endpoints(self):
    engine = CircuitQueryEngine(store=_store_with_design())
    rows = engine.search_net_connections("kb_hw", "CAN0", limit=5)
    self.assertEqual(rows[0]["net_name"], "CAN0")
    self.assertEqual({row["refdes"] for row in rows[0]["connections"]}, {"U1200", "J3"})
```

- [ ] **Step 2: Run the test before adding query-engine files**

Run: `python -m unittest tests.test_circuit_query_engine -v`

Expected: import failure for `src.circuit.query_engine`.

- [ ] **Step 3: Add only query-engine runtime dependencies**

Add the files listed above from the existing untracked circuit implementation. Do not add query agent, query tool, query planner, recovery manager, session context, PDF parser, analyzer, or UI files.

- [ ] **Step 4: Run the engine test**

Run: `python -m unittest tests.test_circuit_query_engine -v`

Expected: PASS.

### Task 2: 将结构化查询行映射为稳定 Evidence

**Files:**
- Create: `src/circuit/evidence_mapper.py`
- Modify: `src/circuit/index_service.py`
- Modify: `tests/test_circuit_index_service.py`

**Interfaces:**
- Consumes: query-engine rows and `metadata_by_design: dict[str, dict]`.
- Produces:

```python
class CircuitEvidenceMapper:
    def build(self, *, kind: str, row: dict, metadata: dict, source_name: str, score: float) -> Evidence: ...
```

- [ ] **Step 1: Write failing service tests**

```python
def test_query_prefers_exact_net_connection_evidence(self):
    hits = service.query(kb_name="kb_hw", query="CAN0 connection", ctx=ctx)
    self.assertEqual(hits[0].locator["entity_type"], "net")
    self.assertEqual(hits[0].locator["entity_id"], "CAN0")
    self.assertIn("U1200.1", hits[0].content)

def test_query_returns_exact_instance_evidence(self):
    hits = service.query(kb_name="kb_hw", query="U1200", ctx=ctx)
    self.assertEqual(hits[0].locator["entity_type"], "instance")
    self.assertEqual(hits[0].locator["entity_id"], "U1200")
```

- [ ] **Step 2: Run tests and verify they fail under current simple matching**

Run: `python -m unittest tests.test_circuit_index_service.CircuitIndexServiceTests.test_query_prefers_exact_net_connection_evidence tests.test_circuit_index_service.CircuitIndexServiceTests.test_query_returns_exact_instance_evidence -v`

Expected: ordering/content assertion failure.

- [ ] **Step 3: Add mapper and engine-backed query candidates**

In `CircuitIndexService.__init__`, construct `self.query_engine = CircuitQueryEngine(self.store)` unless a test double is injected. In `query()`, build the allowed design set using existing metadata/filter checks, then collect:

```python
net_rows = self.query_engine.search_net_connections(kb_name, query, limit=top_k * 3)
instance_rows = self.query_engine.search_instances(kb_name, query, limit=top_k * 3)
module_rows = self.query_engine.search_modules(kb_name, query, limit=top_k * 2)
connection_rows = self.query_engine.search_module_connections(kb_name, query, limit=top_k * 2)
power_rows = self.query_engine.search_module_power_nets(kb_name, query, limit=top_k * 2)
```

Filter rows by allowed `design_id`; map net, instance, module, module_connection, and module_power rows through `CircuitEvidenceMapper` with scores `0.96`, `0.92`, `0.80`, `0.84`, and `0.82`. Preserve existing lightweight evidence only as a fallback when the engine yields no rows.

- [ ] **Step 4: Deduplicate and rank deterministically**

Sort by `score` descending, then `id`; retain first evidence for each id. Keep `top_k` behavior unchanged.

- [ ] **Step 5: Run focused service tests**

Run: `python -m unittest tests.test_circuit_index_service -v`

Expected: PASS.

### Task 3: 将复合问题翻译为 LangGraph 的确定性 source fanout

**Files:**
- Modify: `src/agents/graph.py`
- Modify: `tests/test_circuit_agent_planning.py`
- Modify: `tests/test_agentic_runner.py`

**Interfaces:**
- Consumes: `question`, catalog sources, `processor_kind`.
- Produces: source plans that pair circuit evidence with document/BOM evidence when the question requests both.

- [ ] **Step 1: Write failing planning tests**

```python
def test_expected_evidence_for_refdes_and_bom_includes_circuit_and_spreadsheet(self):
    self.assertEqual(set(_expected_evidence("U1200 BOM quantity")), {"circuit_design", "spreadsheet_table"})

def test_plan_source_selection_fans_out_circuit_and_document_sources(self):
    result = plan_source_selection(_state_with_circuit_and_document_sources("CAN0 connection design report"))
    tools = {call["tool_name"] for item in result["source_plan"]["source_plan"] for call in item["tool_calls"]}
    self.assertEqual(tools, {"circuit_query", "document_rag"})
```

- [ ] **Step 2: Run tests and verify missing circuit evidence classification**

Run: `python -m unittest tests.test_circuit_agent_planning -v`

Expected: refdes+BOM assertion fails before adding refdes/net token recognition.

- [ ] **Step 3: Extend deterministic evidence recognition**

In `_expected_evidence()`, append `circuit_design` when the question contains a refdes-shaped token (`[A-Za-z]{1,4}\d{1,}`) or a net-shaped token (`[A-Z][A-Z0-9_]{2,}`) together with circuit terms such as connection, pin, net, topology, module, or power. Retain existing document and spreadsheet conditions so result types fan out instead of replacing each other.

- [ ] **Step 4: Verify follow-up calls respect source processor type**

In `plan_next_retrieval()`, when a `source_name` resolves to catalog source, reject calls unless:

```python
{
    "ragflow": {"document_rag"},
    "spreadsheet_table": {"spreadsheet_semantic", "spreadsheet_cell"},
    "circuit_design": {"circuit_query"},
}[processor_kind]
```

- [ ] **Step 5: Add and run a failing-then-passing mismatch test**

```python
def test_plan_next_retrieval_rejects_circuit_tool_for_document_source(self):
    result = plan_next_retrieval(_state_with_document_source_and_circuit_suggestion(), _FakeLLM(), _CatalogTool())
    self.assertEqual(result["next_retrieval_calls"], [])
```

Run: `python -m unittest tests.test_circuit_agent_planning tests.test_agentic_runner -v`

Expected: PASS.

### Task 4: 增加电路模块、电源和复合检索回归覆盖

**Files:**
- Modify: `tests/test_circuit_index_service.py`
- Modify: `tests/test_circuit_agent_planning.py`
- Modify: `tests/test_agentic_runner.py`

**Interfaces:**
- Consumes: engine-backed `CircuitIndexService.query()` and planner source plans.
- Produces: regression coverage for module connection, module power, multi-source planning, and source filter behavior.

- [ ] **Step 1: Add module and power tests**

```python
def test_query_returns_module_connection_evidence(self):
    hits = service.query(kb_name="kb_hw", query="Power MCU connection", ctx=ctx)
    self.assertTrue(any(hit.locator["entity_type"] == "module_connection" for hit in hits))

def test_query_returns_module_power_evidence(self):
    hits = service.query(kb_name="kb_hw", query="Power supply nets", ctx=ctx)
    self.assertTrue(any(hit.locator["entity_type"] == "module_power" for hit in hits))
```

- [ ] **Step 2: Run new tests and verify the expected failure before mapper support**

Run: `python -m unittest tests.test_circuit_index_service -v`

Expected before Task 2 implementation: module assertions fail.

- [ ] **Step 3: Complete mapper formats**

`CircuitEvidenceMapper` must produce readable content for module connections and module power facts, while locator includes `entity_type` plus module id/name or power-net identity.

- [ ] **Step 4: Run final verification**

Run:

```powershell
python -m unittest discover -s tests -v
python -m py_compile src\circuit\query_engine.py src\circuit\evidence_mapper.py src\circuit\index_service.py src\agents\graph.py
```

Expected: all tests pass and target modules compile.

### Task 5: 提交受控迁移边界

**Files:**
- Add: files from Tasks 1-2
- Modify: files from Tasks 2-4

**Interfaces:**
- Produces: one reviewable Phase 2/3 commit without legacy router, session, PDF, analyzer, or UI files.

- [ ] **Step 1: Verify staged boundary**

Run:

```powershell
git diff --cached --name-only
```

Expected: includes only query engine, relations, vector index, evidence mapper, index service, graph, and their tests.

- [ ] **Step 2: Commit after green verification**

Run:

```powershell
git commit -m "feat: add structured circuit query quality"
```

Expected: commit succeeds without adding `src/query_router/*`, old circuit agents, PDF parsers, analyzers, or UI.

## Self-Review

- Spec coverage: tasks implement service-owned structural retrieval, grounded Evidence, deterministic multi-source fanout, source/tool matching, and tests.
- Placeholder scan: no deferred implementation steps or undefined interfaces are used.
- Type consistency: all callers preserve `CircuitIndexService.query(...) -> list[Evidence]` and `CircuitQueryTool.run(...) -> list[Evidence]`.
