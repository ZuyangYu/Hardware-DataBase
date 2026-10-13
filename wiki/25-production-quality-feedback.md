# 25 · 生产反馈与授权复核

对应代码：`src/quality/`、`src/api/routes/quality.py`、`src/core/conversation.py`、
`src/storage/control_plane/postgres.py`、`frontend/src/pages/admin/QualityPage.tsx`。

P1 在质量业务账本中保留反馈与固定材料，Phoenix 是标量标注的展示投影。
质量记录不依赖 trace 是否被采样。固定数据集、回答级实验、费用账本与发布关联的首版已接入；
详见 [26 · 固定实验与调用费用](26-quality-experiments-cost-ledger.md)。

## 用户与复核入口

1. 聊天的已完成原始助手消息可选择“有帮助 / 没帮助”，填写原因及补充说明，允许修改或撤销。
2. 点击“提交复核材料”，选择同部门员工、原始问答字段和证据。共享副本来自提交时固定材料；
   默认有效 7 天，可撤销。共享补充说明也固定到当时的反馈事件。
3. 员工在 `/admin/quality` 查看本部门队列及有权读取的副本；管理员可看全局元数据。
   管理员身份不会开放正文。带附件的回答暂不支持复核共享。
4. 复核分别填写问题判断、事实正确性、根因、参考答案与 rubric。确认问题不等于事实错误。
   结论保留修订历史；案例经过后续回归、发布和回访前不会被标为闭环完成。

### 管理员打开案例后看什么

案例详情的“问题概览（管理员可见）”会直接显示：

- 用户反馈结果：有帮助、没帮助或已撤销；
- 用户选择的问题分类：事实错误、证据不支持、资料未找到、版本冲突、未完成任务、格式不符；
- 最新复核判断、根因分类和事实正确性；
- 当前是否只有元数据权限，以及后续应由原用户共享选定材料给同部门员工复核。

管理员可以据此判断“这条案例在处理什么问题”。原始提问、回答、证据、反馈补充说明、
参考答案、rubric 和复核自由文本仍只给原用户或明确授权的同部门员工，避免管理员通过治理权限
越过正文授权。若要确认具体回答内容，应联系原用户发起共享，再由授权员工完成复核。

### 管理员如何推动修复

案例详情的“管理员修复管理”会根据当前状态和复核根因显示下一步：

1. `待复核/待补充材料`：联系原用户共享选定材料，安排同部门员工完成复核；
2. `已确认问题`：由原回答 owner 冻结问题、参考答案和证据，实施根因对应的修复；
3. 运行相同冻结输入的基线/候选配对评测，候选必须通过质量门禁；
4. 关联修改引用、run ID 和发布版本；上线后用同一问题回访；
5. 回访确认正确后，案例才进入 `已上线验证`。

根因会给出对应动作，例如“检索”检查召回和排序，“解析”检查文件/表格索引，
“Prompt”检查回答约束，“前端呈现”检查格式、引用展示和字段完整性。
管理员负责协调负责人、监督门禁和查看报告；正文修复和 owner 评测仍由授权员工执行。

### 回答下方没有反馈选项

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
