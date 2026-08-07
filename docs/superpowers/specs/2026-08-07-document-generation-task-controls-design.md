# Document Generation Task Controls Design

## Goal

让 5175 文档生成工作台的运行中工单可暂停、继续、取消和删除，并让页面状态与 Harness 的真实生命周期一致。删除清理业务数据，但保留最小审计记录。

## Scope

- 在任务状态面板提供 Pause / Resume / Cancel / Delete 操作。
- 将暂停从当前误导性的 `blocked` 工单状态改为明确、可恢复的 `paused` 状态。
- 增加继续执行的 API，使用已持久化的 Harness checkpoint 恢复。
- 增加仅面向终态工单的删除 API。
- 删除工单、关联 Harness 运行、checkpoint、草稿、证据矩阵、校验报告和工件文件；保留最小删除审计记录。
- 在前端以中文状态、危险操作确认和写权限限制展示这些行为。

## Non-goals

- 不删除模板、Schema、来源快照或知识库源文件。
- 不绕过证据、渲染、租约和权限检查。
- 不对正在运行的工单直接删除；用户必须先取消或等待其进入终态。

## Lifecycle

```text
retrieving / drafting / validating / rendering
          | pause
          v
        paused -- resume --> 原 Harness checkpoint 后续节点
          | cancel
          v
       cancelled

blocked / failed / complete / cancelled -- delete --> deleted audit record
```

`paused` 不是 `blocked`：前者是用户主动、可继续的状态；后者表示安全或系统错误。取消会停止运行但保留全部业务记录。删除只允许 `cancelled`、`blocked`、`failed` 或 `complete` 状态，且保留 `{work_order_id, tenant_id, actor_id, deleted_at, reason}` 审计事件。

## Backend

1. Extend `DocumentWorkOrder.status` with `paused` and persist it when `pause_harness_run` succeeds.
2. Add pipeline and API resume endpoint. It resolves the frozen source snapshot and registered retriever exactly as a normal KB submission does, then resumes the matching paused Harness run.
3. Add a deletion service transaction guarded by write capability and terminal-state validation. It removes only records/files owned by the work order and writes the audit event before committing deletion.
4. Return status action metadata (`can_pause`, `can_resume`, `can_cancel`, `can_delete`) from the run status DTO. The server is authoritative; the UI never infers permission from status alone.
5. Keep pause/cancel/delete idempotent where a repeat request has an unambiguous safe result; return 409 for invalid lifecycle transitions.

## Frontend

The existing three-column workbench remains. The runs section changes as follows:

- The selected work order drives the phase rail through `resolveDocumentPhase(status)`, not the selected tab.
- The status card displays current unit, live Harness status and an action row.
- Pause is shown for active tasks; Resume for paused tasks; Cancel for active or paused tasks; Delete only for terminal tasks.
- Cancel and Delete each use a confirmation dialog. Delete states its exact deletion scope and the retained audit trace.
- After an action succeeds, the selected status and order list reload. Polling stops for paused/cancelled/terminal tasks and resumes after Resume.
- Read-only users see operation reasons but no enabled lifecycle controls.

## Verification

- Backend tests cover valid and invalid state transitions, write-permission enforcement, deletion scope/audit retention, and resume using a persisted checkpoint.
- API tests cover action DTOs and 409/403 responses.
- Frontend tests cover action visibility, confirmation, disabled read-only state, phase-rail synchronization and polling behavior.
- Run document-generation regression suites, frontend tests/build, Ruff and `git diff --check`.
