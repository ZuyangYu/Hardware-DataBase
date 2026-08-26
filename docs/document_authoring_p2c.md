# P2c（第一增量）：Harness 持久化控制与防重提交

这一增量将 P2b 的同步内置 Harness 提升为带 SQLite 持久化控制面的受限运行时。它保留原有的项目授权、冻结 Source Set 和 Writer 输入边界。

## 已实现

- `HarnessRun` 持久化 `checkpoint_id`、重试次数、租约 owner/过期时间、heartbeat 和单调递增 `fencing_token`。
- Worker 必须先原子认领 queued/retrying Run；暂停或取消会立即递增 fencing token 并撤销租约，旧 worker 后续保存 checkpoint、Draft 或节点回执都会被拒绝。
- 每个 Authoring Graph 步骤保存 `HarnessCheckpoint`，包括当前节点、已用步骤和检索轮次。失败、完成和人工等待都会留下终态 checkpoint。
- `draft_ready_unit` 使用唯一键 `(run_id, node_name, unit_id, input_fingerprint)` 的 `NodeExecutionReceipt`。已提交 Draft 在重试时复用结构化结果，避免再次调用 Writer；未完成节点采用 at-least-once 语义。
- `DocumentArtifact` 以工作单、Run、阶段、内容 hash、验证报告和审批主体计算稳定指纹；相同候选物或正式发布的重复提交返回同一 Artifact，而不是再插入记录。
- `DocumentHumanEvent` 以 Artifact、操作、主体 hash、操作者、值和评论计算稳定指纹。重复网络提交同一审批/人工操作会返回第一次事件；审批重试因而复用同一 `approved_release`。
- Work Order 的创建/状态切换、Artifact 写入和 Human Event 写入会在同一 SQLite 事务中写入 `DocumentOutboxEvent`。Outbox 仅携带 ID、状态和 hash；消费者可通过 pending/failed 查询和 delivered/failed 标记以 at-least-once 方式投递，避免依赖进程内回调。
- Service 提供 pause、cancel 和 resume 内部 API；resume 会重新校验当前项目权限、冻结输入指纹、SourceSetSnapshot 和固定 HarnessPolicy，再在重试预算内认领同一 HarnessRun。
- 文档页面可显示 checkpoint、fencing token 和重试次数，并对 queued/running/retrying Run 提供暂停/取消操作。

## 验证

专项测试验证：暂停后旧 fencing token 无法更新 Run；重试取得新 token；重复 Writer 节点返回同一 committed receipt；重复人工审批只产生一个 Event 和一个正式 Artifact；Outbox 状态变更可失败重试并最终标记 delivered。所有现有 P2a/P2b 文档生成回归仍可通过。

## 后续 P2c 工作

当前 resume 的检索回调由受控内部 API 在恢复时重新注入，因此还没有跨进程的任务队列/回调配置重建。以下仍待后续增量完成：多实例调度与恢复扫描、Artifact/人工事件之外的 NodeExecutionReceipt、Outbox 消费器与外部事件路由、超时与 token 预算、恢复时的部分 Evidence Matrix/Draft 增量重放，以及可观测事件流。
