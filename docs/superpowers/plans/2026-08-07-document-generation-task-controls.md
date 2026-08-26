# Document Generation Task Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add resumable pause, cancel, auditable deletion, and lifecycle controls to the 5175 document-generation workbench.

**Architecture:** Keep Harness state authoritative in `DocumentAuthoringStore`; `DocumentGenerationService` converts user intent into guarded state transitions, and `AppPipeline` reconstructs the frozen KB retriever for background resume. The API returns server-authoritative action permissions. The React task view renders those operations and reloads state after every mutation.

**Tech Stack:** FastAPI/Pydantic, SQLite, existing document Harness and background worker, React 18/TypeScript/Vitest, pytest.

## Global Constraints

- Do not delete templates, schemas, KB source files, or frozen source snapshots.
- Only write-capable KB users can pause, resume, cancel, or delete.
- Delete only terminal work orders (`cancelled`, `blocked`, `failed`, `complete`), retain minimal deletion audit data, and remove only files owned by the work order.
- Pause is resumable and must persist `DocumentWorkOrder.status="paused"`, never `blocked`.
- Preserve renderer safety, source snapshot, lease/fencing, and evidence checks.
- Use RED → GREEN tests before production changes.
- Preserve unrelated dirty workspace changes; stage only this task's paths.

---

### Task 1: Persist a paused work-order state and auditable terminal deletion

**Files:**
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_document_generation_task_controls.py`

**Interfaces:**
- Produces `DocumentWorkOrder.status="paused"` and `DocumentWorkOrderDeletionAudit`.
- Produces `DocumentAuthoringStore.delete_terminal_work_order(work_order_id, *, actor_id, reason) -> DocumentWorkOrderDeletionAudit`.
- Produces `DocumentGenerationService.delete_document_work_order(ctx, work_order_id, *, reason) -> DocumentWorkOrderDeletionAudit`.

- [ ] **Step 1: Write the failing state/deletion tests**

```python
def test_pause_marks_work_order_paused(service, ctx, running_run):
    service.pause_harness_run(ctx, running_run.harness_run_id)
    assert service.store.get_work_order(running_run.work_order_id).status == "paused"


def test_terminal_delete_removes_owned_records_and_keeps_audit(store, completed_order):
    audit = store.delete_terminal_work_order(
        completed_order.work_order_id, actor_id="writer", reason="用户确认删除",
    )
    assert store.get_work_order(completed_order.work_order_id) is None
    assert store.list_artifacts(completed_order.work_order_id) == []
    assert audit.work_order_id == completed_order.work_order_id


def test_running_work_order_cannot_be_deleted(store, running_order):
    with pytest.raises(ValueError, match="terminal"):
        store.delete_terminal_work_order(running_order.work_order_id, actor_id="writer", reason="删除")
```

- [ ] **Step 2: Run the tests and observe RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_document_generation_task_controls.py -q`

Expected: FAIL because `paused` is not a work-order status and terminal deletion/audit APIs do not exist.

- [ ] **Step 3: Add models and SQLite transaction**

Add `paused` to the `DocumentWorkOrder.status` literal and define:

```python
class DocumentWorkOrderDeletionAudit(BaseModel):
    audit_id: str
    work_order_id: str
    tenant_id: str
    actor_id: str
    reason: str
    deleted_at: datetime = Field(default_factory=utc_now)
```

Create `document_work_order_deletion_audits` in `_init_db`. Implement a single `BEGIN IMMEDIATE` store transaction that validates the terminal status, inserts the audit payload, removes human events, outbox events for the work order/artifacts, artifacts, drafts, receipts, checkpoints, runs, manifests, validation reports, evidence matrices, ICD scope review and the work order. Capture artifact storage paths before row deletion; after commit unlink only resolved paths under `artifact_root`. Keep source snapshots and generation sessions.

- [ ] **Step 4: Make the service transition and authorization explicit**

Change `pause_harness_run` to persist `status="paused"`; retain cancellation behavior. Add:

```python
def delete_document_work_order(self, ctx, work_order_id, *, reason):
    order = self._order(ctx, work_order_id, "run_deterministic_work_order")
    return self.store.delete_terminal_work_order(
        order.work_order_id, actor_id=ctx.user_id, reason=reason.strip() or "用户确认删除",
    )
```

Use `require_work_order_capability(..., "run_deterministic_work_order")` so direct service calls cannot bypass write authorization.

- [ ] **Step 5: Run GREEN tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_document_generation_task_controls.py -q`

Expected: PASS.

- [ ] **Step 6: Commit task-only paths**

```bash
git add src/document_authoring/models.py src/document_authoring/work_order_store.py src/document_authoring/service.py tests/test_document_generation_task_controls.py
git commit -m "feat: add document generation task lifecycle controls"
```

### Task 2: Expose resume/delete and action metadata through the KB API

**Files:**
- Modify: `src/core/app_pipeline.py`
- Modify: `src/api/routes/document_generation.py`
- Modify: `src/api/schemas.py`
- Modify: `tests/test_document_generation_api_artifacts.py`
- Modify: `tests/test_document_generation_api_work_orders.py`
- Modify: `tests/test_generation_sessions.py`

**Interfaces:**
- Produces `AppPipeline.resume_knowledge_base_document_generation(ctx, work_order_id) -> str`.
- Produces `AppPipeline.delete_knowledge_base_document_work_order(ctx, work_order_id, *, reason)`.
- Produces `POST /document-generation/work-orders/{id}/resume` and `DELETE /document-generation/work-orders/{id}`.
- Extends run-status DTO with `can_pause`, `can_resume`, `can_cancel`, and `can_delete`.

- [ ] **Step 1: Write failing API/pipeline tests**

```python
def test_paused_status_exposes_resume_only_for_writer():
    status = pipeline.get_document_run_status("wo-1", writer_ctx)
    assert status["can_resume"] is True
    assert status["can_pause"] is False


def test_resume_endpoint_submits_background_resume(client, writer_headers, stub):
    stub.resume_knowledge_base_document_generation = lambda ctx, work_order_id: "bg-resume"
    response = client.post("/api/v1/document-generation/work-orders/wo-1/resume?kb=shared", headers=writer_headers)
    assert response.status_code == 200
    assert response.json()["run_id"] == "bg-resume"


def test_delete_endpoint_rejects_reader_and_forwards_reason(client, reader_headers, writer_headers, stub):
    assert client.delete("/api/v1/document-generation/work-orders/wo-1?kb=shared", headers=reader_headers).status_code == 403
    response = client.delete(
        "/api/v1/document-generation/work-orders/wo-1?kb=shared",
        headers=writer_headers, json={"reason": "重复任务"},
    )
    assert response.status_code == 200
```

- [ ] **Step 2: Run the focused tests and observe RED**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_document_generation_api_artifacts.py tests/test_document_generation_api_work_orders.py tests/test_generation_sessions.py -q`

Expected: FAIL because resume/delete routes and action fields do not exist.

- [ ] **Step 3: Wire background resume and API DTOs**

Add `DeleteDocumentWorkOrderRequest(reason: str = "")`. Implement pipeline resume by resolving the existing order, its frozen snapshot and ICD review, creating `_knowledge_base_retriever(...)`, and submitting `resume_internal_harness(...)` to the existing document worker. Add delete forwarding. In `get_document_run_status`, derive action booleans from the order and latest Harness run after capability validation:

```python
active = latest_run is not None and latest_run.status in {"queued", "running", "retrying"}
paused = order.status == "paused" and latest_run is not None and latest_run.status == "paused"
terminal = order.status in {"cancelled", "blocked", "failed", "complete"}
status.update({
    "can_pause": active and can_write,
    "can_resume": paused and can_write,
    "can_cancel": (active or paused) and can_write,
    "can_delete": terminal and can_write,
})
```

Use `_write_ctx` for both new mutation endpoints. Map invalid lifecycle transitions to HTTP 409, permissions to 403, and missing work orders to 404.

- [ ] **Step 4: Run GREEN tests**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_document_generation_api_artifacts.py tests/test_document_generation_api_work_orders.py tests/test_generation_sessions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit task-only paths**

```bash
git add src/core/app_pipeline.py src/api/routes/document_generation.py src/api/schemas.py tests/test_document_generation_api_artifacts.py tests/test_document_generation_api_work_orders.py tests/test_generation_sessions.py
git commit -m "feat: expose document task resume and deletion APIs"
```

### Task 3: Add lifecycle actions and state-synchronized task UI

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/pages/documentGenerationModel.ts`
- Modify: `frontend/src/pages/documentGenerationModel.test.ts`
- Modify: `frontend/src/pages/documentGenerationWorkbench.tsx`
- Modify: `frontend/src/pages/documentGenerationWorkbench.test.tsx`
- Modify: `frontend/src/pages/DocumentGenerationPage.tsx`

**Interfaces:**
- Extends `WorkOrderStatus` with lifecycle action booleans and `paused` status support.
- `RunStatusPanel` accepts callbacks for `onPause`, `onResume`, `onCancel`, `onDelete`, plus `actionBusy`.
- `RunsSection` reloads selected status/list after each successful operation and reports its selected phase to `DocumentGenerationPage`.

- [ ] **Step 1: Write failing UI/model tests**

```tsx
it('renders resume and cancel for a paused work order', () => {
  const html = renderToStaticMarkup(
    <RunStatusPanel status={{ ...pausedStatus, can_resume: true, can_cancel: true }} onResume={() => undefined} onCancel={() => undefined} />,
  );
  expect(html).toContain('继续生成');
  expect(html).toContain('取消任务');
  expect(html).not.toContain('暂停任务');
});

it('shows delete only when the server permits terminal deletion', () => {
  const html = renderToStaticMarkup(
    <RunStatusPanel status={{ ...completedStatus, can_delete: true }} onDelete={() => undefined} />,
  );
  expect(html).toContain('删除任务');
});

it('maps paused to a resumable Chinese state', () => {
  expect(describeWorkOrderStatus('paused').label).toBe('任务已暂停');
});
```

- [ ] **Step 2: Run the tests and observe RED**

Run: `cd frontend && npm test -- --run src/pages/documentGenerationModel.test.ts src/pages/documentGenerationWorkbench.test.tsx`

Expected: FAIL because paused status and lifecycle controls are absent.

- [ ] **Step 3: Implement UI types, controls and reload flow**

Add paused labels/actions and DTO fields. In `RunStatusPanel`, render server-authorized buttons only:

```tsx
{status.can_pause && <Button onClick={onPause} disabled={actionBusy}>暂停任务</Button>}
{status.can_resume && <Button onClick={onResume} disabled={actionBusy}>继续生成</Button>}
{status.can_cancel && <Button variant="outline" onClick={onCancel} disabled={actionBusy}>取消任务</Button>}
{status.can_delete && <Button variant="destructive" onClick={onDelete} disabled={actionBusy}>删除任务</Button>}
```

Use `window.confirm` before Cancel and Delete. The delete confirmation copy must state that artifacts and intermediate data are removed while an audit trace remains. Call pause/cancel by `harness_run.run_id`, resume/delete by work-order ID. On success, reload the status/list; clear selection after deletion. Replace the static `runs -> retrieving` phase assignment with selected run status projected through `resolveDocumentPhase`.

- [ ] **Step 4: Run GREEN tests and production build**

Run: `cd frontend && npm test -- --run && npm run build`

Expected: all Vitest files PASS and Vite build exits 0.

- [ ] **Step 5: Commit task-only paths**

```bash
git add frontend/src/api/types.ts frontend/src/pages/documentGenerationModel.ts frontend/src/pages/documentGenerationModel.test.ts frontend/src/pages/documentGenerationWorkbench.tsx frontend/src/pages/documentGenerationWorkbench.test.tsx frontend/src/pages/DocumentGenerationPage.tsx
git commit -m "feat: add document generation lifecycle actions"
```

### Task 4: Regression verification and delivery review

**Files:**
- Modify only if a test exposes a regression in task-owned files.

- [ ] **Step 1: Run backend lifecycle and document-generation suites**

Run:

```bash
PYTHONPATH=. .venv/bin/pytest \
  tests/test_document_generation_task_controls.py \
  tests/test_document_generation_api_artifacts.py \
  tests/test_document_generation_api_sessions.py \
  tests/test_document_generation_api_work_orders.py \
  tests/test_document_generation_finalize_state.py \
  tests/test_document_generation_prepare.py \
  tests/test_generation_sessions.py \
  tests/test_knowledge_base_document_work_orders.py -q
```

Expected: PASS.

- [ ] **Step 2: Run static checks**

Run: `.venv/bin/ruff check src tests && git diff --check`

Expected: `All checks passed!` and no diff-check output.

- [ ] **Step 3: Review lifecycle safety manually**

Verify that pause uses `paused`, delete cannot target an active run, delete removes only paths below `artifact_root`, a read-only user gets no action authorization, and `resume` uses the frozen source snapshot.

- [ ] **Step 4: Commit only any final task-owned corrections**

```bash
git add src/document_authoring/models.py src/document_authoring/work_order_store.py src/document_authoring/service.py src/core/app_pipeline.py src/api/routes/document_generation.py src/api/schemas.py frontend/src/api/types.ts frontend/src/pages/documentGenerationModel.ts frontend/src/pages/documentGenerationWorkbench.tsx frontend/src/pages/DocumentGenerationPage.tsx tests/test_document_generation_task_controls.py tests/test_document_generation_api_artifacts.py tests/test_document_generation_api_work_orders.py
git commit -m "test: verify document generation lifecycle controls"
```
