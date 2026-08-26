# Deterministic Circuit Authoring Fast Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Skip model work for uniquely grounded circuit facts and publish incremental parallel Harness progress.

**Architecture:** `AuthoringGraph` will select a deterministic draft provider after evidence selection only when a conservative classifier recognizes one structured or directly anchored circuit fact.  Parallel futures will be consumed by the coordinator in completion order for persistence while final results remain merged in schema order.

**Tech Stack:** Python 3, Pydantic models, `concurrent.futures`, pytest.

## Global Constraints

- Preserve frozen source snapshots and existing validation/contamination checks.
- Keep model-backed behavior unchanged for non-qualifying or ambiguous evidence.
- Allow at most eight parallel semantic units, as frozen in `HarnessPolicy`.
- Do not write Harness persistence records from worker threads.

---

### Task 1: Specify and test deterministic electrical-fact selection

**Files:**
- Modify: `tests/test_document_authoring_parallel.py`
- Modify: `src/document_authoring/harness/graph.py`

**Interfaces:**
- Consumes: `WriterRequest`, selected evidence dictionaries, semantic unit schema metadata.
- Produces: `_use_deterministic_evidence_writer(unit_id, schema, requirement, evidence) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
def test_structured_pin_fact_bypasses_reranker_and_configured_writer():
    reranker_calls = []
    writer_calls = []
    # One `U1-PA0: CAN_RX` evidence item with circuit metadata is returned.
    # Assert reranker_calls == [] and writer_calls == [] after graph.run().
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py::test_structured_pin_fact_bypasses_reranker_and_configured_writer -q`

Expected: FAIL because the existing graph calls the reranker and configured writer.

- [ ] **Step 3: Write minimal implementation**

```python
if _use_deterministic_evidence_writer(unit_id, schema, requirement, evidence):
    draft = DeterministicEvidenceWriter().generate(request)
else:
    draft = self.draft_provider(request)
```

Add the conservative classifier that requires one selected item, electrical-fact terminology, and structured-circuit metadata or a query-anchor assignment.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py::test_structured_pin_fact_bypasses_reranker_and_configured_writer -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/harness/graph.py tests/test_document_authoring_parallel.py
git commit -m "feat: fast-path grounded circuit facts"
```

### Task 2: Publish completion-order parallel progress

**Files:**
- Modify: `tests/test_document_authoring_parallel.py`
- Modify: `src/document_authoring/harness/graph.py`
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/harness/runtime.py`

**Interfaces:**
- Consumes: `Future[tuple[str, HarnessExecutionResult]]` and `ProgressCallback`.
- Produces: state keys `completed_units: int`, `total_units: int`, persisted by `HarnessRun` and `HarnessCheckpoint`.

- [ ] **Step 1: Write the failing test**

```python
def test_parallel_graph_reports_completed_unit_before_slowest_future_finishes():
    observed = []
    # Make one retrieval return immediately and one wait on an Event.
    # Assert an observed parallel_units state has completed_units == 1 before releasing the slow unit.
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py::test_parallel_graph_reports_completed_unit_before_slowest_future_finishes -q`

Expected: FAIL because `executor.map` returns only after all futures finish.

- [ ] **Step 3: Write minimal implementation**

```python
for future in as_completed(futures):
    unit_id, unit_result = future.result()
    on_completed(unit_id, unit_result)
```

The coordinator increments aggregate counters and invokes progress callback.  Add defaulted completion counters to the Pydantic run/checkpoint models and copy them in runtime `save_progress`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py::test_parallel_graph_reports_completed_unit_before_slowest_future_finishes -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/harness/graph.py src/document_authoring/harness/runtime.py src/document_authoring/models.py tests/test_document_authoring_parallel.py
git commit -m "feat: publish parallel harness progress"
```

### Task 3: Verify and launch the final optimized work order

**Files:**
- No source changes required.

- [ ] **Step 1: Run focused verification**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py tests/test_harness_policy.py -q`

Expected: PASS.

- [ ] **Step 2: Restart the frozen-source work order at eight-way concurrency**

Cancel the current slow run through the service API, create its traceable restart with `max_parallel_units=8`, and invoke the normal continuation API.

- [ ] **Step 3: Verify artifact delivery**

Inspect the completed work order status and artifact metadata; confirm preview/download are available through the existing 5175 UI.
