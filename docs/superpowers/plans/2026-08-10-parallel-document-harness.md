# Parallel Document Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute independent document semantic units with a bounded four-or-fewer worker pool while retaining frozen-source, validation, and rendering guarantees.

**Architecture:** `AuthoringGraph` will create requirements once and dispatch independent per-unit work to a bounded executor.  The coordinator alone persists progress and merges results in schema order before cross-unit validation.  The frozen HarnessPolicy and AuthoringRunManifest carry the concurrency setting; retrieval cache access is serialized with a lock.

**Tech Stack:** Python 3.11, Pydantic, `concurrent.futures.ThreadPoolExecutor`, SQLite, pytest.

## Global Constraints

- Default parallelism is `3`; allowed range is exactly `1..4`.
- All evidence must remain inside the work order's frozen source snapshot.
- Results must be merged in schema order, regardless of completion order.
- Only the coordinator may update the durable HarnessRun/Checkpoint cursor.
- Do not touch existing user-owned changes outside files listed below.

---

### Task 1: Freeze and expose parallelism in Harness policy and manifest

**Files:**
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/harness/runtime.py`
- Modify: `src/document_authoring/service.py`
- Test: `tests/test_document_authoring_parallel.py`

**Interfaces:**
- Produces `HarnessPolicy.max_parallel_units: int = 3` with validation `1 <= value <= 4`.
- Produces `AuthoringRunManifest.max_parallel_units: int | None` copied from the approved policy.

- [ ] **Step 1: Write the failing tests**

```python
def test_harness_policy_defaults_to_three_parallel_units():
    assert HarnessPolicy(harness_policy_id="p", version="1").max_parallel_units == 3

@pytest.mark.parametrize("value", [0, 5])
def test_harness_policy_rejects_parallelism_outside_measured_limit(value):
    with pytest.raises(ValueError, match="max_parallel_units"):
        HarnessPolicy(harness_policy_id="p", version="1", max_parallel_units=value)

def test_manifest_freezes_policy_parallelism():
    manifest = InternalDocumentHarnessRuntime.build_manifest(order, policy, snapshot, template, schema)
    assert manifest.max_parallel_units == policy.max_parallel_units
```

- [ ] **Step 2: Run the focused tests and confirm they fail because the field is absent**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py -q`

- [ ] **Step 3: Add the frozen fields and automatic-policy default**

```python
class HarnessPolicy(BaseModel):
    max_parallel_units: int = 3

    @model_validator(mode="after")
    def validate_budget(self):
        # retain existing budget validation
        if not 1 <= self.max_parallel_units <= 4:
            raise ValueError("max_parallel_units must be between 1 and 4")
        return self

class AuthoringRunManifest(BaseModel):
    max_parallel_units: int | None = None
```

Copy `policy.max_parallel_units` in `InternalDocumentHarnessRuntime.build_manifest`; set `max_parallel_units=3` when `_schema_harness_policy` creates the automatic policy.

- [ ] **Step 4: Run focused tests and the existing authoring-policy tests**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py tests/test_harness_policy.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/models.py src/document_authoring/harness/runtime.py src/document_authoring/service.py tests/test_document_authoring_parallel.py
git commit -m "feat: freeze harness parallelism"
```

### Task 2: Execute independent semantic units through a bounded pool

**Files:**
- Modify: `src/document_authoring/harness/graph.py`
- Test: `tests/test_document_authoring_parallel.py`

**Interfaces:**
- Produces `AuthoringGraph._run_unit(...) -> UnitExecutionResult` without shared mutation.
- Produces `HarnessExecutionResult` merged in `_semantic_units(schema)` order.
- Uses `ThreadPoolExecutor(max_workers=policy.max_parallel_units)` only when more than one unit exists.

- [ ] **Step 1: Write failing ordering and concurrency tests**

```python
def test_parallel_graph_limits_in_flight_units_and_merges_schema_order():
    graph, schema, work_order, run, manifest, snapshot = parallel_graph_fixture(max_parallel_units=3)
    result = graph.run(..., retrieve=delayed_retriever)
    assert observed_max_in_flight <= 3
    assert [draft.unit_id for draft in result.drafts] == ["field:first", "field:second", "field:third"]

def test_parallel_graph_runs_cross_unit_validation_after_all_unit_drafts():
    validator = RecordingValidator()
    result = graph.run(...)
    assert validator.cross_unit_inputs == ["field:first", "field:second"]
```

- [ ] **Step 2: Run the focused tests and confirm serial implementation fails their concurrency assertions**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py -q`

- [ ] **Step 3: Extract per-unit execution and coordinator merge**

```python
@dataclass
class UnitExecutionResult:
    unit_id: str
    requirement: InformationRequirement
    outcome: RetrievalOutcome
    matrix_row: dict[str, Any]
    retrieval_ledger: dict[str, Any]
    draft: DocumentUnitDraft | None
    status: str
    issues: list[dict[str, Any]]
    step_count: int
    retrieval_round_count: int

with ThreadPoolExecutor(max_workers=min(policy.max_parallel_units, len(semantic_units))) as executor:
    futures = [executor.submit(self._run_unit, ...) for unit in semantic_units]
    completed = {entry.unit_id: entry for entry in (future.result() for future in futures)}
for unit in semantic_units:
    entry = completed[unit["unit_id"]]
    # append outcome, ledger, matrix row, draft and status in schema order
```

`_run_unit` uses a local `DocumentAuthoringState`, never calls the global progress callback, and returns all output.  The coordinator sends one `parallel_units` progress update when a task completes; it adds worker counts to its own state and performs the existing `validate_cross_unit` only after ordered merge.

- [ ] **Step 4: Run graph and full authoring flow tests**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py tests/test_document_authoring_p2a.py tests/test_full_generation_flow.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/harness/graph.py tests/test_document_authoring_parallel.py
git commit -m "feat: run document units in parallel"
```

### Task 3: Make cross-unit reuse and progress persistence safe under concurrency

**Files:**
- Modify: `src/document_authoring/retriever_registry.py`
- Modify: `src/document_authoring/harness/runtime.py`
- Modify: `src/ui/document_generation_page.py`
- Test: `tests/test_document_authoring_parallel.py`
- Test: `tests/test_retriever_registry.py`

**Interfaces:**
- `CrossUnitEvidenceCache.ingest` and `.offer` use one internal `threading.Lock`.
- `HarnessRun` progress displays `parallel_units` as `检索/撰写/校验并行处理中` with completed/total counts.

- [ ] **Step 1: Write failing safety and status-presentation tests**

```python
def test_cross_unit_cache_is_safe_when_ingest_and_offer_overlap():
    # run concurrent cache operations; every returned item has intact reuse metadata
    assert errors == []

def test_parallel_run_status_reports_completed_and_total_units():
    payload = pipeline.get_document_run_status(work_order_id, ctx)
    assert payload["harness_run"]["current_node"] == "parallel_units"
    assert payload["harness_run"]["completed_units"] == 2
    assert payload["harness_run"]["total_units"] == 3
```

- [ ] **Step 2: Run the focused tests and confirm the new behavior is absent**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py tests/test_retriever_registry.py -q`

- [ ] **Step 3: Guard cache access and persist coordinator-only progress**

```python
class CrossUnitEvidenceCache:
    def __init__(self, max_reuse_per_unit: int = 5):
        self._lock = threading.Lock()
        self._store = {}

    def ingest(self, evidences, unit_id):
        with self._lock:
            ...

    def offer(self, requirement, query, unit_id):
        with self._lock:
            snapshot = list(self._store.values())
        # rank and copy outside the lock
```

Add `completed_units` and `total_units` to `HarnessRun` and `HarnessCheckpoint`; update both only inside runtime `save_progress`.  Map `parallel_units` in the page's node-to-stage labels and show `已完成 n/m 个单元` when values exist.

- [ ] **Step 4: Run focused tests plus UI status tests**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py tests/test_retriever_registry.py tests/test_document_generation_page.py -q`

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/retriever_registry.py src/document_authoring/harness/runtime.py src/ui/document_generation_page.py tests/test_document_authoring_parallel.py tests/test_retriever_registry.py tests/test_document_generation_page.py
git commit -m "feat: report safe parallel harness progress"
```

### Task 4: Traceably restart a cancelled work order and migrate the authorized serial work order

**Files:**
- Test: `tests/test_document_authoring_parallel.py`
- Modify: `docs/superpowers/specs/2026-08-10-parallel-document-harness-design.md`
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/core/app_pipeline.py`

**Interfaces:**
- Produces `DocumentWorkOrder.restart_of_work_order_id: str | None`.
- Produces `restart_cancelled_knowledge_base_document_generation(ctx, work_order_id)`.

- [ ] **Step 1: Write a failing regression test for restart after cancellation**

```python
def test_cancelled_work_order_restarts_as_a_traceable_parallel_work_order():
    cancelled = service.cancel_harness_run(ctx, old_run.harness_run_id)
    assert cancelled.status == "cancelled"
    replacement = pipeline.restart_cancelled_knowledge_base_document_generation(ctx, order.work_order_id)
    assert replacement.restart_of_work_order_id == order.work_order_id
    assert replacement.source_set_snapshot_id == order.source_set_snapshot_id
```

- [ ] **Step 2: Run the regression test and adjust the state transition only if it fails for a valid restart path**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py::test_cancelled_work_order_restarts_as_a_traceable_parallel_work_order -q`

- [ ] **Step 3: Perform live migration only after all tests pass**

Implement the replacement creator by copying immutable template, schema, target
format, KB name, and source-snapshot identifiers from the cancelled order. It
must create a new work-order ID and idempotency key, set
`restart_of_work_order_id`, and invoke `_schema_harness_policy` so the new
policy freezes parallelism. Use running work order
`wo-9edb44c621534c7ba5887b70fb11f7fd`: request cancellation through the
authenticated application service, restart the server and worker to load the
deployed code, then submit the replacement work order. Verify the new manifest
says `max_parallel_units=3` and the status API reports `parallel_units`.

- [ ] **Step 4: Run the complete relevant verification suite**

Run: `.venv/bin/pytest tests/test_document_authoring_parallel.py tests/test_document_authoring_p2a.py tests/test_full_generation_flow.py tests/test_retriever_registry.py -q`

- [ ] **Step 5: Commit documentation and report the exact new run ID**

```bash
git add docs/superpowers/specs/2026-08-10-parallel-document-harness-design.md tests/test_document_authoring_parallel.py
git commit -m "docs: record parallel harness migration"
```
