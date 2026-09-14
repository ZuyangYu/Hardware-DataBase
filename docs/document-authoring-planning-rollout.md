# 对话驱动文档创作 Phase 0–1 上线与回滚手册

- 关联设计：`docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md`
- 关联计划：`docs/superpowers/plans/2026-09-08-conversation-led-document-authoring-phase0-1.md`
- 范围：planning contracts、shadow planner、对话澄清、计划提案、显式确认、提交 outbox、状态卡片、深链恢复。

## 1. Schema 迁移

`document_authoring.db` 新增/扩展（全部幂等，可重复执行）：

- `document_output_specs` / `document_plans` / `document_planning_events`：版本化 OutputSpec、DocumentPlan 与追加式规划事件。
- `document_plan_submissions`：提交 outbox，含租约/重试/幂等键；唯一索引
  `(tenant_id, user_id, document_plan_id, document_plan_version, document_plan_hash)`
  保证一个已接受计划最多一条提交。
- `document_generation_sessions`：`template_version_id` 可空重建（v2 草案允许无模板），
  新增 `contract_version` 与 spec/plan 指针列；历史 payload 逐字节保留。
- `document_tasks`：新增 spec/plan 指针列与状态 `awaiting_plan_confirmation`/`awaiting_release`。

`auth.db` 无 schema 变更（durable job store 复用原表）。

## 2. 开关

| 开关 | 默认 | 作用 |
|---|---|---|
| `DOCUMENT_PLANNING_SHADOW_ENABLED` | false | 在既有 WorkOrder 创建后追加 shadow 计划（只写 planning 表/事件，fail-soft） |
| `DOCUMENT_PLANNING_V2_ENABLED` | false | 对话侧启用 v2 工具面：提案/确认/任务状态；低阶工单工具下线 |

上线顺序：

1. 先部署代码（两开关均 false），确认 schema 幂等迁移与旧链路回归全绿。
2. 测试环境开启 `DOCUMENT_PLANNING_SHADOW_ENABLED`，观察 shadow 计划稳定性、issue 分布、执行零漂移。
3. `DOCUMENT_PLANNING_V2_ENABLED` 仅对允许清单内的租户/文档类型开启；旧端点保持可用。
4. 提案→确认转化率、stale 率、失败率达到记录阈值后再扩大范围。

## 3. Worker 与 outbox 恢复

- `HardwareWorker.run_once` 先排空 `document_plan_submissions`（每轮 `DOCUMENT_PLAN_SUBMISSION_BATCH_SIZE`，默认 2），再处理普通文档任务，双队列互不饿死。
- 提交状态机：`pending → running → dispatched | waiting_human | retrying → failed/dead_letter`。
- 租约过期后由任意存活 worker 认领（lease/fencing 与 durable job store 一致）。
- 幂等键 `document-plan:{plan_id}:{version}:{plan_hash}`：worker 崩溃重放不会复制 Work Order/job。
- preflight 人工门（ICD 样例模板、ICD 范围待办）→ `waiting_human` + WorkOrder 引用，不创建生成 job。

## 4. 指标与告警

关注事件（均已脱敏，不含来源名/证据正文/路径）：`document_plan_confirmed`、提交状态迁移、
`planning_shadow_succeeded|failed`。告警建议：

- `dead_letter` 提交数 > 0：检查 worker 日志与权限/模板状态。
- 确认后 409（stale）占比突增：模板或来源频繁变化，联系业务确认版本策略。

## 5. 陈旧提案 remediation

确认失败（409）时：前端展示"计划已更新"，用户通过 `propose_document_plan` 重新提案；
旧提案被服务端标记 `stale`（reason_code: `output_spec_changed|source_changed|template_changed`），
不能把旧确认应用到新输入。

## 6. 回滚

- 关闭 `DOCUMENT_PLANNING_V2_ENABLED`：对话回到 legacy 工具面；不再创建新 v2 会话。
- 已接受 spec/plan 与提交行保持可读，outbox 中 pending 行会继续被 worker 安全排空（幂等）。
- 如需完全停用：同时关闭 shadow；不删除任何表，历史数据只读保留。
