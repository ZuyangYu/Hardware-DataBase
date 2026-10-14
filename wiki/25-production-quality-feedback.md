# 25 · 反馈工单与观测记录

对应代码：`src/quality/`、`src/api/routes/quality.py`、`src/core/conversation.py`、
`src/storage/control_plane/postgres.py`、`src/api/routes/quality_tickets.py`、
`frontend/src/pages/admin/FeedbackTicketsPage.tsx`。

P1 在质量业务账本中保留反馈与固定材料，Phoenix 是标量标注的展示投影。
质量记录不依赖 trace 是否被采样。固定数据集、回答级实验和发布关联保留为内部 API；费用账本有独立页面。
详见 [26 · 固定实验与调用费用](26-quality-experiments-cost-ledger.md)。

## 日常反馈工单

员工在已完成回答下点击“回答有问题”，选填分类、补充说明和联系方式后点击“提交工单”。
提交界面明确告知：处理管理员可以查看本次提问、回答、补充说明及填写的联系方式。
该授权仅用于本次工单，不开放其他对话、知识库文件或固定检索证据。

- 员工通过“治理与运营 → 我的反馈”（`/admin/quality`）查看自己的工单、发送补充、重新打开已解决工单或关闭工单。
- 系统管理员通过“反馈工单”查看全局工单，包括提交员工、提交时部门、说明、联系方式、原始问答和沟通记录。
- 管理员可以认领、转派给其他启用的管理员、发送回复、请求补充、记录解决办法、关闭或删除。
- 同部门其他员工不自动拥有该工单的查看权。内部质量记录和知识库正文继续遵守原有授权规则。

工单有五种状态：`待处理 → 处理中 ↔ 待补充 → 已解决 → 已关闭`。
新建工单提交成功后通知所有启用的系统管理员，点击通知直接打开对应工单；重复提交和同一工单的
补充反馈不重复发送新建提醒。转派时提醒新负责人，员工补充已认领工单时提醒负责人。
通知正文不包含问答、补充说明和联系方式；投递异常保留工单并记录告警，通知列表目前约每 30 秒刷新。
员工补充待补充工单后恢复处理；已解决工单允许员工重新打开；关闭后停止补充，由管理员重新打开。
管理员标记已解决必须填写解决说明，站内通知提醒员工查看；通知故障不影响已经保存的工单记录。
员工可以随时关闭自己的工单，不必进行评测或填写参考答案。

关闭和删除均有确认弹窗。删除仅限系统管理员，会从员工和管理员的列表中移除工单，并清除工单
说明、联系方式、解决办法和沟通正文；保留不可读取的删除标记、操作元数据以及原始问答和原有质量记录。
旧请求重试不会恢复已删除工单；员工之后重新提交新的反馈可建立新工单。

撤销反馈会关闭关联工单、撤销处理材料授权并清除工单正文。原始消息脱敏时同步清除派生工单正文；
会话删除时工单级联删除。只有员工再次明确提交材料，才能恢复撤销后的授权；管理员不能自行恢复授权。

工单状态独立于质量案例状态：提供资料或解释即可解决员工工单，不能据此宣称系统问题经过回归或上线验证。
需要系统修复时，由维护人员完成修复和验证，管理员在工单中记录处理办法。

前端已移除高级质量页面、复核请求弹窗和案例/工单中的高级入口。旧的
`/admin/quality/advanced`、`/admin/quality?case=...` 链接以及历史质量通知统一进入反馈工单列表。
已有问答、工单和观测记录保留；内部复核、固定数据集、实验与发布关联 API 不作为员工日常操作步骤。

### API 与存储

- `POST /api/v1/chat/turns/{turn_id}/feedback`：差评附加 `share_with_support=true` 才建立工单；可传 `support_contact`。
  缺省不建立工单，兼容旧客户端的原有授权边界。反馈和工单在同一数据库事务中写入，重复请求不重复创建。
- `GET /api/v1/quality/tickets`：按状态、部门和员工/部门/工单编号搜索，带 `limit/offset` 分页；员工只返回本人记录。
- `GET /api/v1/quality/tickets/{ticket_id}`：读取当前可访问的本次问答和沟通记录。
- `POST /api/v1/quality/tickets/{ticket_id}/actions`：`reply/claim/assign/wait/resolve/reopen/close`，携带当前 `row_version` 和 `client_request_id`。
- `DELETE /api/v1/quality/tickets/{ticket_id}?row_version=...`：管理员删除，拒绝覆盖更新后的记录。
- `GET /api/v1/quality/ticket-assignees`：管理员查询可转派的启用管理员。

新增 `quality_tickets`、`quality_ticket_events`，沿用控制面数据库。SQLite 自动增量建表；PostgreSQL
升级后先运行 `uv run python -m src.storage.bootstrap`（容器内可直接运行 Python 模块），再启动新版 API/worker。既有反馈不自动转为工单；
员工重新点击“回答有问题”并提交，才授权管理员查看该次材料。

## 回答下方没有反馈选项

反馈按钮依赖该回答的固定质量快照，与是否命中随机抽样无关。历史已完成回答可能没有快照；
页面会显示“该回答尚无可用质量记录；历史回答需由管理员补采”，并提供“重试加载反馈”。
功能关闭时显示“回答反馈功能当前未启用”；网络或读取异常可重试。

管理员可显式调用 `POST /api/v1/quality/backfill?limit=1000`，或执行下文的
`--once --backfill` 命令（单次最多 100 条）。补采从持久化终态回答生成快照，
跳过有源消息编辑记录的回答；完成后重开会话、刷新页面或点击“重试加载反馈”。
补采不保证每条回答都进入案例队列，案例仍由反馈、随机抽样或诊断触发。
流式临时回答、没有持久化 turn ID、已编辑或已脱敏的消息不展示反馈入口。

## 权威数据和一致性

- `src/quality/store.py` 直接使用 `CONTROL_PLANE_*` 对应数据库；SQLite 与 PG 都将快照与采集事件
  写入最终回答事务。独立质量 DSN 会破坏原子性，因此不存在该配置。
- `quality_schema` 当前版本 1；新表为 snapshots、cases、feedback_events、reviews、outbox。
  PG 先运行 `python -m src.storage.bootstrap`；API/worker 只校验，缺字段/关键唯一索引/版本时拒绝启动。
- 回答 revision 为规范内容指纹。消息编辑、来源更新及 SSE 重放不修改原始反馈目标。
  固定证据保存实际检索内容及其哈希；完整文件/解析版本取决于上游是否提供相应元数据。
- 消息脱敏清理质量正文与说明；来源删除撤销关联副本；会话删除级联清理。
  共享撤销、过期、部门变更作用于后续读取及写入，包含复核自由文本。
- `quality_outbox` 同时是同事务事件和共享队列。DB 时间、60 秒租约及 fencing 控制消费权；
  Redis/OTel/Phoenix 均不是质量事实的权威来源。

## 配置和执行

```dotenv
QUALITY_ENABLED=true
QUALITY_CAPTURE_CONTENT=true
QUALITY_SAMPLE_RATE=0.01
QUALITY_SAMPLE_DAILY_LIMIT=100
HDB_API_SPAWN_QUALITY_WORKER=1
QUALITY_PHOENIX_SYNC_ENABLED=false
QUALITY_PHOENIX_ENDPOINT=
QUALITY_PHOENIX_API_KEY=
QUALITY_PHOENIX_MAX_ATTEMPTS=20
```

关闭正文采集时仍能保留版本/事件/反馈标量，但不能共享并未保存的固定回答。
抽样率记录为剩余日预算条件下的入样概率；反馈、诊断及随机抽样触发分别展示，不能把差评队列当全站质量均值。

```bash
uv run python -m src.storage.bootstrap
uv run python -m src.quality.worker
uv run python -m src.quality.worker --once
uv run python -m src.quality.worker --once --backfill
uv run python -m src.quality.worker --once --requeue-projections
```

显式补采仅使用未编辑/未脱敏的旧终态记录，标记为 backfill，缺失 attempt 不伪造。
Docker 使用独立 `worker-quality`，API 不再托管子进程。

## Phoenix 19.13.0

- 使用稳定 `feedback:<event_id>` / `review:<review_id>` identifier；修订保留独立历史。
- `sync=true` 写入后按 project/span/identifier 读回确认；span 晚到、429、5xx 有界重试。
- 确认未采样或没有结果 span 的任务记为 `no_span`，不阻止业务复核。
- 仅同步受控标签、明确评分及关联 ID；问答、评论、来源标题、参考答案和解释不出站。
- `QUALITY_PHOENIX_ENDPOINT` 是服务端可达 REST 地址；API key 只由服务端发送并在配置接口中脱敏。
  Phoenix 自身认证/项目权限按部署设置；本项目共享权限不授予 Phoenix UI 的外部访问权。

## 验证与后续

工程验证涵盖 SQLite/PG 并发幂等、真实多进程 claim、事务回滚、fencing 接管、正文边界及撤销、
隔离 Phoenix 19.13.0 的 late span/upsert、真实 API 浏览器链路和 PG 备份恢复。
详见 [实施记录](../docs/superpowers/plans/2026-10-8-production-feedback-cost-observability-implementation.md#p1-生效契约)。

P2 首版提供固定材料评测、配对证据、发布/回访关联、成本与确定性报告。P3 已实现费用 spool、
Collector 韧性和聊天 SLO 首版；完整检索/Agent 实验、预算/结算与其余 workload SLO 继续完善。
生产领域案例、实际供应商价目/账单及生产发布需对应验收窗口。
