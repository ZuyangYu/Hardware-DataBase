# Document Generation Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** 将 5175 React 文档生成页升级为单页工作台，提供聊天式需求澄清、可解释状态和可恢复的生成需求简报，并把已确认需求安全接入现有文档 Harness。

**Architecture:** 在现有模板分析和工单 API 外增加轻量的持久化 Generation Session/Brief；前端工作台通过会话消息逐问逐答，确认后调用现有工单创建/生成接口。后端只让澄清服务生成结构化建议，所有来源冻结、证据校验、模板渲染和发布继续由确定性 DocumentAuthoringService 控制。先兼容轮询，事件接口保留为可选扩展。

**Tech Stack:** FastAPI/Pydantic、现有 SQLite `DocumentAuthoringStore`、React 18、TypeScript、Vite、Tailwind/shadcn、Vitest、pytest。

## Global Constraints

- 不绕过来源快照、Evidence Package、模板 hash、renderer safety 或权限检查。
- `needs_clarification` 只表示需求缺口；`needs_review` 表示内容风险；`blocked/failed` 表示系统或安全失败。
- 需求确认后，只有通过证据、内容一致性和渲染安全校验的结果才能自动发布。
- 保留现有未提交改动；每次提交只包含本任务相关文件。
- 先写失败测试并观察 RED，再写生产代码。

---

### Task 1: 建立状态文案与工作台领域类型

**Files:**
- Create: `frontend/src/pages/documentGenerationModel.ts`
- Create: `frontend/src/pages/documentGenerationModel.test.ts`
- Modify: `frontend/src/api/types.ts:604-725`

**Interfaces:**
- Produces `DOCUMENT_PHASES`, `WORK_ORDER_STATUS_LABELS`, `describeWorkOrderStatus(status)` and `nextActionsForStatus(status)` for all workbench UI consumers.

- [ ] **Step 1: Write the failing test**

```ts
it('maps blocked renderer failures to an actionable Chinese status', () => {
  expect(describeWorkOrderStatus('blocked')).toEqual({
    label: '生成被阻止',
    tone: 'danger',
    action: '查看原因并重试',
  });
  expect(nextActionsForStatus('retrieving')).toContain('refresh');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- --run src/pages/documentGenerationModel.test.ts`

Expected: FAIL because the model helpers do not exist.

- [ ] **Step 3: Write minimal implementation**

Define the phase list and status map with labels for `draft`, `analyzing_template`, `needs_clarification`, `ready_to_generate`, `retrieving`, `generating`, `validating`, `rendering`, `completed`, `needs_review`, `blocked`, `failed`, and `cancelled`. Extend `WorkOrderStatus` with optional `phase`, `display_label`, `progress`, `error_code`, `error_message`, `retryable`, `next_actions`, `generation_brief`, and `clarification_session_id`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- --run src/pages/documentGenerationModel.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/documentGenerationModel.ts frontend/src/pages/documentGenerationModel.test.ts frontend/src/api/types.ts
git commit -m "feat: add document generation status model"
```

### Task 2: 重构 React 工作台布局并加入聊天式澄清 UI

**Files:**
- Create: `frontend/src/pages/documentGenerationWorkbench.tsx`
- Create: `frontend/src/pages/documentGenerationWorkbench.test.tsx`
- Modify: `frontend/src/pages/DocumentGenerationPage.tsx`

**Interfaces:**
- Consumes Task 1 status helpers and existing `api`, `uploadFiles`, `WorkOrderStatus`, `DocumentAnalysis`, and `GenerationOptions` types.
- Produces a `DocumentGenerationWorkbench` component that renders the left phase rail, central clarification transcript/composer, and right template/evidence panels.

- [ ] **Step 1: Write the failing test**

```tsx
it('shows clarification input and hides raw retrieving text', () => {
  render(<DocumentGenerationWorkbench {...fixtures.needsClarification} />);
  expect(screen.getByText('需要补充需求')).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '回复 AI' })).toBeInTheDocument();
  expect(screen.queryByText('状态：retrieving')).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test -- --run src/pages/documentGenerationWorkbench.test.tsx`

Expected: FAIL because the workbench component and accessible labels do not exist.

- [ ] **Step 3: Write minimal implementation**

Extract the current upload/create/runs behavior into the workbench while retaining the existing API calls. Add:

- `PhaseRail` with translated phase labels and progress.
- `ClarificationPanel` with assistant messages, option buttons, free-text input labelled `回复 AI`, confirmed/pending brief summaries, and a `确认需求并开始生成` button.
- `TemplateSummaryPanel` with template format, unit/suggestion counts and confidence colors.
- `RunStatusPanel` showing translated state, current unit, error reason, retryability and action buttons.
- Responsive Tailwind layout (`grid-cols-[220px_minmax(0,1fr)_300px]`, collapsing right panel below `lg`).

Keep template auto-activation behavior and existing ICD scope review controls. The workbench must accept the current `kbs`/`auth` props and preserve old routes.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npm test -- --run src/pages/documentGenerationWorkbench.test.tsx && npm run build`

Expected: PASS and TypeScript/Vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/DocumentGenerationPage.tsx frontend/src/pages/documentGenerationWorkbench.tsx frontend/src/pages/documentGenerationWorkbench.test.tsx
git commit -m "feat: add document generation workbench UI"
```

### Task 3: 持久化 Generation Session 与 Brief

**Files:**
- Create: `src/document_authoring/generation_sessions.py`
- Create: `tests/test_generation_sessions.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/models.py`

**Interfaces:**
- Produces `GenerationBrief`, `ClarificationMessage`, `GenerationSession`, and `GenerationSessionStore` with `create_session`, `get_session`, `append_message`, `update_brief`, and `confirm` methods.
- Store methods must scope reads/writes by `tenant_id` and `user_id` and use SQLite transactions/unique IDs consistent with `DocumentAuthoringStore`.

- [ ] **Step 1: Write the failing test**

```python
def test_generation_session_round_trips_brief_and_messages(tmp_path):
    store = GenerationSessionStore(tmp_path / "authoring.db")
    session = store.create_session("tenant-a", "user-a", "kb-a", "template-a")
    store.append_message(session.session_id, role="assistant", content="请选择项目版本", options=["v1", "v2"])
    store.update_brief(session.session_id, {"scope": {"revision": "v2"}, "confirmed": False})
    store.confirm(session.session_id)
    loaded = store.get_session(session.session_id, tenant_id="tenant-a", user_id="user-a")
    assert loaded.brief.confirmed is True
    assert loaded.messages[0].options == ["v1", "v2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_generation_sessions.py::test_generation_session_round_trips_brief_and_messages -q`

Expected: FAIL because the session models/store do not exist.

- [ ] **Step 3: Write minimal implementation**

Add SQLite tables `document_generation_sessions` and `document_generation_messages` with JSON Brief/options, status, timestamps and foreign-key-safe IDs. Implement transactional updates, reject cross-tenant/user reads with `PermissionError`, and make `confirm` idempotent. Add optional `generation_session_id` and `generation_brief` fields to the work-order model payload without changing existing required columns.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_generation_sessions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/generation_sessions.py tests/test_generation_sessions.py src/document_authoring/work_order_store.py src/document_authoring/models.py
git commit -m "feat: persist document generation briefs"
```

### Task 4: 增加会话 API 与需求澄清服务

**Files:**
- Create: `src/document_authoring/requirement_clarifier.py`
- Create: `tests/test_requirement_clarifier.py`
- Create: `tests/test_document_generation_api_sessions.py`
- Modify: `src/api/routes/document_generation.py`
- Modify: `src/api/schemas.py`
- Modify: `src/core/app_pipeline.py`

**Interfaces:**
- `RequirementClarifier.next_message(template_analysis, brief) -> ClarificationMessage` returns one question or a ready signal.
- Pipeline methods: `create_document_generation_session`, `get_document_generation_session`, `answer_document_generation_session`, `confirm_document_generation_session`.

- [ ] **Step 1: Write the failing test**

```python
def test_clarifier_asks_for_revision_before_generation():
    message = RequirementClarifier().next_message({"units": [{"label": "版本"}]}, GenerationBrief())
    assert message.question_id == "scope.revision"
    assert message.options
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_requirement_clarifier.py::test_clarifier_asks_for_revision_before_generation -q`

Expected: FAIL because `RequirementClarifier` and `GenerationBrief` are not implemented.

- [ ] **Step 3: Write minimal implementation**

Implement deterministic minimum-question clarification first (revision, missing-data policy, inference policy), with an optional LLM adapter behind a feature flag. Add request/response DTOs for creating sessions, appending user messages, and confirming. Route all accesses through `_ctx`/`_write_ctx`; return 403/404/409 for permission, missing session, or already-bound/confirmed conflicts. On confirm, persist the Brief and expose `ready_to_generate` to the UI; do not start a work order twice.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_requirement_clarifier.py tests/test_document_generation_api_sessions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/requirement_clarifier.py tests/test_requirement_clarifier.py tests/test_document_generation_api_sessions.py src/api/routes/document_generation.py src/api/schemas.py src/core/app_pipeline.py
git commit -m "feat: add clarification session API"
```

### Task 5: 将确认的 Brief 接入工单并修复收尾卡死

**Files:**
- Create: `tests/test_document_generation_finalize_state.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/api/routes/document_generation.py`

**Interfaces:**
- Work-order creation accepts optional `generation_session_id` and immutable `generation_brief`.
- `run_internal_harness`/finalization converts renderer or artifact exceptions into persisted `blocked`/`failed` with `error_code`, `error_message`, `retryable`, and `next_actions`; it never leaves a completed harness with an old `retrieving` work-order status.

- [ ] **Step 1: Write the failing test**

```python
def test_finalize_renderer_error_persists_blocked_status(service, monkeypatch):
    order = make_retrieving_order(service)
    monkeypatch.setattr(service, "_finalize_internal_harness_result", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("abnormal duplicate long value fan-out is not allowed")))
    service.run_internal_harness(ctx, order.work_order_id, retrieve=retrieve)
    saved = service.store.get_work_order(order.work_order_id)
    assert saved.status == "blocked"
    assert saved.error_code == "renderer_safety_violation"
    assert "duplicate long value" in saved.error_message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_document_generation_finalize_state.py::test_finalize_renderer_error_persists_blocked_status -q`

Expected: FAIL because the current exception path leaves the work order in `retrieving`.

- [ ] **Step 3: Write minimal implementation**

Wrap the finalization call in a narrow exception handler. Map renderer safety/value errors to `blocked` and unexpected infrastructure errors to `failed`; persist a sanitized message, retryability and actions. Preserve the original exception for logs. Extend status serialization so the API returns the new fields. When the Harness is already `waiting_human`/`complete`, finalization errors must still update the work order.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_document_generation_finalize_state.py tests/test_document_authoring_p2a.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/app_pipeline.py src/document_authoring/service.py src/document_authoring/work_order_store.py src/api/routes/document_generation.py tests/test_document_generation_finalize_state.py
git commit -m "fix: persist document generation finalize failures"
```

### Task 6: 端到端回归与交付验证

**Files:**
- Modify: `frontend/src/pages/documentGenerationWorkbench.test.tsx`
- Modify: `tests/test_document_generation_api_sessions.py`
- Create: `tests/test_document_generation_workbench_flow.py`

- [ ] **Step 1: Write the failing integration tests**

Cover template upload → session creation → one clarification answer → confirmation → exactly one work-order submission, plus blocked renderer failure returning an actionable status DTO.

- [ ] **Step 2: Run the focused suites and observe RED**

Run: `cd frontend && npm test -- --run src/pages/documentGenerationModel.test.ts src/pages/documentGenerationWorkbench.test.tsx` and `pytest tests/test_document_generation_api_sessions.py tests/test_document_generation_workbench_flow.py -q`.

- [ ] **Step 3: Implement only integration wiring needed for the tests**

Connect the workbench to session endpoints, pass `generation_session_id`/Brief when creating a work order, and render returned `next_actions`.

- [ ] **Step 4: Run full verification**

Run: `cd frontend && npm test -- --run && npm run build`; `cd .. && pytest -q`; `ruff check src tests`; `git diff --check`.

Expected: all focused and existing tests pass, frontend build succeeds, Ruff and diff checks are clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages frontend/src/api/types.ts src tests
git commit -m "test: verify document generation workbench flow"
```
