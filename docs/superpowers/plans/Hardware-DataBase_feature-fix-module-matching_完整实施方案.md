# Hardware-DataBase 生产化演进完整实施方案

> 版本：v1.5
>
> 日期：2026-09-15
>
> 代码来源：`feature/fix-module-matching`；实施使用独立生产化分支
>
> 方案性质：分阶段实施计划，实际依赖版本和部署容量须经阶段验证
>
> 关联路线图：[Hardware-DataBase_feature-fix-module-matching_修改优化方案.md](./Hardware-DataBase_feature-fix-module-matching_修改优化方案.md)（v1.8）

v1.2 修订：补全 `next_turn_sequence` 数据契约和 SQLite 部署约束；明确附件协调者唯一性；落地 Checkpoint 恢复/提交指针结构；补充 `uncertain` permit 恢复流程；区分相对回归门槛与绝对 SLO；明确同一 PostgreSQL 事务边界；补齐版本、迁移和实施交接要求。

v1.3 修订：基线更新为 `01c807e`（2026-09-15 合并 PR #49）并对齐路线图 v1.6。LLM 并发改为“进程层复用 `llm_governor` + 本方案仅新增部署层 RAG/TOOL/HEAVY 额度”，消除双重限流；Worker 隔离复用已合并 `WORKER_TURN_CONCURRENCY`；入口覆盖与指标口径按 `llm_governor` / usage ledger 修订；迁移范围补入资产中心、Wiki 与附件元数据；`turn_sequence` 迁移补 `wiki_assist` 并存约束。

v1.4 修订：按 `01c807e` 复核并修正 v1.3 的四类事实错误。附件/资产索引与解析状态实际在 `storage/chat_attachments.db`（`CHAT_ATTACHMENT_INDEX_DB_PATH`），`auth.db` 只存资产/Wiki/导出元数据与 `turn_attachments` 关联（§2 事实 11、§7.4）；`waiting_for_attachments` 状态机与 `src/attachments/coordinator.py` settle/reconcile 已存在，P0-004 改为在其上补队头阻塞、600 秒超时、可见字段与单所有者扫描（§2 事实 12、§5.3、§13.5）；评测 RAGAS judge LLM 已经 `govern_ragas_llm` 接入 `llm_governor` batch，仅 `EVAL_EMBEDDING_*` 未治理（§6.2）；P0-003 明确租约配置名 `CHAT_TURN_LEASE_SECONDS` / `CHAT_TURN_LEASE_RENEW_SECONDS`，并补入 §10.1 新增配置清单。

v1.5 修订：根据实施审查补充模块匹配专项边界、现状差距、可执行接口契约、迁移分批策略、旧附件入口兼容策略、评测治理失败语义和 PostgreSQL 配置防误启用规则。

## 1. 实施目标与边界

本方案将当前的 Single Supervisor + Multi-Tool + Governed Workflow 演进为可稳定运行的 Hybrid Supervisor Harness，目标包括：

- 同一会话的 turn 严格按服务端顺序执行；
- Chat Worker 与文档、OCR、导出、Memory 等 Heavy Worker 资源隔离；
- LLM、RAG、工具和后台任务具有可解释的并发上限和背压行为；
- API、Worker、Checkpoint、Memory、会话权限和 Artifact 支持多实例共享；
- 任务具备幂等、租约、fencing、取消、重试和崩溃恢复能力；
- 所有容量和质量变化都有固定基线、指标和回归依据；
- 仅在评测证明有收益时增加 Reviewer/Research Agent。

本方案不包含模块匹配算法本身的修复。模块匹配问题应另建专项任务，提供错误样例、根因、算法修改和准确率回归集。本文不要求一次性创建统一 `src/jobs/` 队列；现有领域队列可以继续独立运行。

**与分支目标的关系**：保留现有文件名以免破坏引用；本文交付生产化能力，不以分支名称推断模块匹配根因。若实施模块匹配修复，先建立独立任务，包含脱敏输入、期望/实际匹配、根因、修改文件、准确率/召回率基线、兼容策略和回归命令。专项可独立推进，不阻塞 P0-001 基线采集；通过验收后将其样本纳入生产化回归集。

默认部署路径是先保持 SQLite 单机兼容，再迁移 PostgreSQL 和共享 Artifact 存储。任何多主机上线都必须先完成状态共享、租约和资源配额验收。方案授权范围是设计与规划；执行生产迁移、停写、上线和删除备份应遵循项目发布流程。

### 1.1 本方案采用的默认技术决策

以下为实施默认值，不代表当前代码已实现或服务器已具备这些组件：

| 问题 | 默认决策 | 适用阶段 |
|---|---|---|
| 会话排序 | 新增服务端 `turn_sequence`，每 session 单调递增；`chat_sessions.next_turn_sequence` 保存下一可用值，数据库唯一约束兜底 | P0a |
| 等待附件超时 | 从进入等待状态计时，600 秒后失败并推进后序；后台附件解析不被自动取消 | P0a |
| Worker 隔离 | 独立进程、按角色消费；初始每进程只执行一个 Chat/Heavy 任务（对齐已合并 `WORKER_TURN_CONCURRENCY=1`），通过进程数扩容 | P0a |
| 本机多进程配额 | SQLite 配额表短事务协调同主机 API/Worker；RAG/TOOL/HEAVY 由资源 governor 接入，LLM 由 `llm_governor` 扩展共享 limiter 接入同一存储；保留已有进程层限制 | P0b |
| 跨主机配额 | 将 P0b 的共享配额存储迁至 PostgreSQL，保持同一 LLM 入口与 lease 契约；本轮不新增 Redis | P1 |
| 遗留查询入口 | 保留客户端契约；复用 LLM 进程层治理，并通过共享适配补齐 LLM/RAG/tool 部署级预算 | P0b |
| 状态存储 | 同一个 PostgreSQL 服务内按领域分 schema，保持跨领域原子事务所需的连接边界 | P1 |
| Artifact 文件 | 第一版使用所有 API/Worker 可访问的共享文件服务；对象存储作为后续独立迁移 | P1 |
| 质量回归 | 使用固定数据集、模型和判分脚本，权限/顺序/fencing 为零容忍检查 | 全阶段 |

若环境检查发现默认部署条件不具备，先记录差异并调整该任务的部署设计；不阻塞其他独立任务，也不宣称该阶段已经验收完成。

P0-001 先登记配置误启用风险，P0-008 交付防护：对尚无实现的 `MEMORY_STORE_BACKEND=postgres`、`DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=postgres`，在配置保存与启动阶段给出明确错误；`AGENT_CHECKPOINT_BACKEND` 当前不存在，新增时只放开已交付 provider。provider 代码完成后可在迁移测试环境启用，生产切换另受 §7 验收约束；不能要求先完成迁移才允许测试 provider。

### 1.2 文档契约与实施状态

本文是待实施计划，SQL、接口、命令和新增配置均不代表已可执行；以 §2 代码事实为现状依据。路线图负责阶段目标，本文件负责接口、依赖和验收；冲突时先修订两份文档再实施。旧架构文档中的 Streamlit 描述不作为入口依据，按 `frontend/` + `src/api/` 定位实际调用。

文中 2/1/1 Worker、4 路 fan-out、RAG 8/4 额度为测试起始值；600 秒等待、120/15 秒租约等为拟议行为默认值。生产使用前将最终数值、适用环境与绝对 SLO 冻结在 baseline 报告中。缺少绝对 SLO 不阻止本地开发，但阻止生产容量验收。

## 2. 当前基线

实施以合并后的 `feature/fix-module-matching` 分支 commit `01c807e`（2026-09-15 合并 PR #49 `feature/asset-center-and-llm-governance`；v1.2 基线 `7fd340a` 已过期）为基线，重点代码包括：

```text
src/agents/runner.py
src/agents/orchestration/executor.py
src/agents/tools/document_rag_tool.py
src/api/routes/query.py
src/core/conversation.py
src/core/llm_governor.py
src/core/model_gateway.py
src/core/wiki.py
src/core/assets.py
src/core/doc_assets.py
src/memory/catalog.py
src/memory/store.py
src/memory/worker.py
src/document_authoring/job_store.py
src/document_authoring/harness/checkpointer.py
src/pipelines/document_rag/ragflow_backend.py
src/workers/main.py
src/observability/metrics.py
src/settings.py
```

当前事实：

1. `runner.py` 使用 `create_deep_agent`，禁用 general-purpose subagent；
2. Agent Checkpoint 默认使用 `SqliteSaver`；
3. `ConversationActionExecutor` 已支持依赖 DAG，但同层 ready action 目前串行；
4. `document_search_batch` 每次调用创建最多 4 路线程池任务；
5. 新式 turn 由持久化队列和 `HardwareWorker` 执行，聊天 turn 进程内并发由 `WORKER_TURN_CONCURRENCY`（默认 1）控制，Worker 尚无角色路由；
6. 遗留 `POST /query` 在 API 进程内同步执行 Agent 与检索；其 LLM 调用已由进程级 `llm_governor` 覆盖（`model_gateway` 全入口接入），RAG/tool 无预算；
7. Memory 的权威账本和可重建投影分层，PostgreSQL Memory provider 尚未实现；
8. Authoring Checkpoint 已有 fencing，但配置允许 postgres 而 builder 尚未支持；
9. 文档任务、Memory、Export 等领域已有不同形式的 lease、幂等、outbox 或死信机制；
10. 当前 OTel 指标已有 `hdb.agent.*`、`hdb.llm.*`、`hdb.chat.*`、`hdb.queue.*`、`hdb.worker.*`、`hdb.authoring.*`、`hdb.export.*`、`hdb.eval.*`、`hdb.memory.*`；进程级 LLM 准入状态经 `llm_governor.snapshot()` / `GET /system/status -> llm` 暴露；
11. `chat_turns` 已含 `wiki_assist` 列；资产中心、Wiki、导出任务与 `turn_attachments` 关联落在 `auth.db`，附件/资产索引与解析状态在 `storage/chat_attachments.db`（`CHAT_ATTACHMENT_INDEX_DB_PATH`），附件文件在 `storage/chat_attachments/`（PR #49 合并）；
12. `waiting_for_attachments` 状态、`transition_waiting_turn`（`conversation.py:1017`）与 `src/attachments/coordinator.py` 的 settle/reconcile 协调已存在；缺 claim 队头阻塞、600 秒等待上限、`blocked_by_turn_id/queue_reason/wait_deadline` 可见字段与单所有者扫描循环。

## 3. 总体原则

### 3.1 先正确性，后容量

先完成会话顺序、所有权和迟到写入保护，再引入并行和水平扩展。不得用增加 Worker 数量掩盖状态竞争。

### 3.2 权威状态与可重建投影分离

权限、身份、会话、turn、任务生命周期、同意状态、来源和幂等信息属于权威状态；Memory 检索索引、缓存和部分向量数据属于可重建投影。两者分别迁移、分别校验。

### 3.3 每个阶段独立上线和回滚

每个阶段使用独立分支和适用的 Feature Flag，必须有回滚步骤、监控和验收报告；涉及 schema 时附数据库迁移，不涉及数据变更则标注“不适用”。阶段之间通过稳定接口交付。

### 3.4 资源限制必须覆盖所有入口

资源治理同时覆盖 API、Chat Worker、Heavy Worker、Memory Worker、Export Worker 和遗留 `POST /query`。进程级 semaphore 不得被描述为部署级全局限制。

LLM 并发分两层且各自唯一：进程层复用已合并的 `llm_governor`；P0b 在该模块扩展部署级共享 limiter，同主机多进程即纳入，不延后到跨主机。RAG/TOOL/HEAVY 通过资源 governor 使用共享配额基础设施，LLM 只由 `llm_governor` 获取共享 permit。一次调用可以同时受进程和部署两个不同作用域约束，禁止两个 LLM 包装器重复申请同一作用域额度。

## 4. Phase 0：基线与实施准备

### 4.1 交付物

- `baseline-report.md`：质量、延迟、吞吐和容量基线；
- `state-inventory.md`：全部状态、权威性、事务和迁移清单；
- `metric-catalog.md`：已有/新增指标目录；
- `rollback-runbook.md`：每阶段回滚和恢复流程；
- 固定回归数据集、判分脚本和压测脚本。

### 4.2 固定回归集

至少包含：

- 普通中文和英文问答；
- 多源文档检索和 zero-hit；
- 文档来源路由降级；
- Excel/SQL 查询；
- Circuit/BOM 查询；
- 附件解析中、解析失败和附件权限变化；
- 文档生成、修订、取消、重试和发布 Gate；
- Action routing、依赖环和未解决引用；
- Citation coverage、Numeric consistency、Unsupported claim。

### 4.3 基线负载

初始混合负载：

```text
60% ordinary QA
20% multi-source search
10% SQL / spreadsheet
5% document authoring
5% export / OCR
```

记录 P50/P95/P99、错误率、timeout、RAG 429、token usage、chat queue wait、heavy-job queue wait、Checkpoint latency、DB pool wait 和 Worker active。

### 4.4 Phase 0 通过标准

- 回归集可重复执行，判分结果可保存；
- 状态清单覆盖数据库、文件、索引、缓存和 Worker 队列；
- 所有现有指标能映射到采集位置；
- 基线测试至少执行 3 次，结果波动范围已记录；
- 每个后续阶段有明确的比较指标和回滚负责人。

Phase 0 工件使用同一 `baseline_id`，记录 commit、模型配置摘要、数据集 hash、依赖版本、进程数和测试命令。密钥不得进入报告。人员由项目分配；任务卡使用“后端负责人、存储负责人、测试负责人、运维负责人”四类角色指派，不虚构具体人员。

## 5. Phase 1a：会话顺序与 Worker 隔离

### 5.1 增加 `turn_sequence`

为 `chat_turns` 增加服务端生成的会话轮次：

```sql
ALTER TABLE chat_turns ADD COLUMN turn_sequence INTEGER;
CREATE UNIQUE INDEX IF NOT EXISTS ux_chat_turns_session_sequence
    ON chat_turns(session_id, turn_sequence);
CREATE INDEX IF NOT EXISTS ix_chat_turns_session_status_sequence
    ON chat_turns(session_id, status, turn_sequence);
```

实际迁移脚本必须根据 SQLite/PostgreSQL 方言分别实现，不能直接执行上述示例 SQL。

采用 expand → backfill → enforce 三步迁移：先添加 nullable 列，并在 `chat_sessions` 增加 `next_turn_sequence INTEGER NOT NULL DEFAULT 1`；新 session 从 1 开始。停写排空后按 session 回填旧行，并将计数器设置为 `MAX(turn_sequence) + 1`（无 turn 时为 1），验证数量、唯一性和顺序；最后设置 turn 序号非空约束（SQLite 必要时重建表并恢复外键/索引）。回填期间新增 turn 必须双写，否则不允许恢复写入。

`chat_turns` 已含 `wiki_assist` 列（PR #49 合并）；`turn_sequence` 的 expand/回填/INSERT 必须与该列并存，SQLite 重建表时禁止按旧列清单复制而丢列。

创建 turn 时，先检查 `(session_id, client_request_id)` 是否已存在，存在则返回原 turn，不再分配序号。SQLite 使用现有 `BEGIN IMMEDIATE`，数据库文件必须是所有本机进程可见的同一绝对路径，开启 WAL、`busy_timeout` 和可靠本地文件锁；禁止放在不保证 POSIX 文件锁的共享目录。PostgreSQL 锁定对应 session 行，在同一事务内读取并递增 `next_turn_sequence`、插入 turn。客户端不得传入 sequence。旧数据按 `session_id, user_message_id` 排序回填，发现重复、空值或顺序异常时终止迁移。删除或清空历史不复用旧序号，并推进会话 generation，使旧执行结果失效。

### 5.2 改造 `claim_turn`

`ConversationService.claim_turn` 必须：

1. 只检查 session 中最早的非终态 turn，只有该 turn 可执行时才领取；
2. 前序 turn 未完成时不领取后续 turn；
3. 领取、写入 worker、lease 和 fencing token 在同一事务内完成；
4. 完成、失败、取消、Checkpoint、事件和 Artifact 提交时重新校验所有权；
5. lease 过期后才能接管；
6. 接管时递增 fencing token；
7. 旧 Worker 的权威状态迟到写入返回明确的 stale-owner 错误。

领取状态统一保存在下面定义的 `conversation_execution_state` 表，禁止再在 session 上维护另一份可独立修改的 lease/token。token 在每次新领取/接管时递增；校验不能只有 worker_id。租约默认 120 秒（`CHAT_TURN_LEASE_SECONDS`）、每 15 秒续约（`CHAT_TURN_LEASE_RENEW_SECONDS`），使用存储端时间；续约失败即停止新增工具调用。超时接管只是执行所有权转移，不是任务已完成。

候选查询先按 session 选最早非终态 turn，再过滤可领取会话，最后应用批次 LIMIT，避免一个被阻塞会话占满候选列表。不得在网络调用期间持有 SQLite 写事务或 PostgreSQL 行锁。

候选列表仅用于提示，`claim_turn()` 必须在同一事务内重新计算 session 的最早可执行 turn。内部返回结构化领取结果（claimed/blocked/stale/cancelled/retry_exhausted），过渡适配器保留旧调用方需要的 `ChatTurn | None` 返回值。普通竞争只记录去重指标/trace；stale 调用方不能通过“写拒绝事件”重新获得会话写权限。用户可见状态由有效控制者条件更新，禁止每次轮询重复追加排队事件。

#### 5.2.1 Checkpoint 与跨存储 fencing

权威库新增每会话一行的 `conversation_execution_state`；与 turn、事件、最终答案使用同一数据库事务。字段契约如下，具体 DDL 由 P0-002b 验证后在 P0-003 交付：

| 字段 | 类型 / 约束 | 含义 |
|---|---|---|
| `session_id` | 整数主键、引用 chat_sessions | 继承会话的用户/租户授权，禁止按客户端任意 ID 访问 |
| `active_turn_id`、`active_attempt_id` | 可空文本 | 当前执行身份；attempt 使用服务器生成的唯一 ID |
| `lease_owner`、`lease_expires_at` | 可空文本 / UTC 时间 | 所有者与有效期，无活动执行时为空 |
| `fencing_token`、`generation` | 非空 64 位整数，默认 0 | 领取/接管推进 token；清空/逻辑删除推进 generation |
| `recoverable_checkpoint_ref` | 可空结构化引用 | 当前 turn 最近一次可恢复的检查点 |
| `committed_checkpoint_ref` | 可空结构化引用 | 最近一次成功完成且业务结果已提交的检查点 |
| `revision` | 非空 64 位整数，默认 0 | 指针与执行状态 CAS 版本，每次更新递增 |

引用包含 `backend`、内部 `thread_id`、`checkpoint_ns`、`checkpoint_id`、`attempt_id` 和 `generation`；SQLite 使用校验后的 JSON 文本，PostgreSQL 使用 JSONB。引用必须指向已持久化的数据，不能仅保存 namespace 后读取不确定的“latest”。全文 canonical pointer 是这些权威引用的统称，不再增加第三套 canonical 字段。

CAS 同时检查 session、turn、attempt、generation、token、revision、租约未过期和未取消状态；完成 turn 时在同一事务更新 committed 引用、最终答案和领域 outbox。取消或明确失败不推进 committed 引用，后序 turn 从最近 committed 状态开始；重试同一 turn 可用有效 recoverable 引用。清空会话推进 generation 并清空引用；物理删除会话后，旧 Worker 不能重新创建 execution state 行。被有效引用的数据不得被孤儿清理回收。

Agent Checkpoint 和会话权威状态当前不在同一个 SQLite 文件。仅“先读 token，再调用 saver.put”存在检查后接管的竞争，不能作为验收通过依据。

默认采用按执行 attempt 隔离的 Checkpoint namespace，并在 turn 权威事务中 CAS 提交 canonical checkpoint 指针：旧 attempt 即使产生物理 checkpoint，也只能成为不可见的孤儿数据，不得覆盖当前会话恢复指针；最终回答、事件和指针提交检查同一个 session generation/token。用户可见 `thread_id` 保持不变，由服务端解析到内部 namespace。

先以独立验证任务 `P0-002b` 确认当前 saver 的 fork/恢复和 pending writes 能力，结果作为 P0-003 入口条件；不支持可靠复制时，先完成最小存储适配和恢复测试再启用接管，不能通过丢弃 pending writes 绕过问题。恢复、历史迁移、取消、清空和删除必须覆盖全部 attempt namespace。孤儿清理由保留期任务执行。

每次成功图状态写入后登记带 token 的恢复指针：先完成 attempt checkpoint 写入，再用权威事务 CAS 更新可恢复指针；指针永不指向尚未持久化的数据。最终业务提交另有 committed 指针。崩溃接管从最新有效可恢复指针分叉；取消后下一轮从 committed 指针开始。任何可能重放的领域写工具必须使用稳定的 turn/action 幂等键，不能依赖 checkpoint 恢复提供“恰好一次”外部副作用。

Authoring 保留既有 fencing，并测试 token 校验和实际写入之间发生接管的情况；若现有包装不能覆盖该窗口，需加入事务校验或同类隔离协议。文档中的“拒绝迟到 Checkpoint 写入”指拒绝写入权威可恢复状态，不要求已失效 attempt 的临时物理数据完全不存在。

同一 session 的正确性不能只依赖进程内锁。单机 keyed lock 只能降低竞争，多实例必须依靠共享数据库锁、事务条件更新或分布式 lease。

取消运行中 turn 时，服务端原子撤销提交权限并推进 token，再向计算线程发送取消信号；后续 turn 只使用最后已提交的历史。已正式接受的 Document/Export Job 由自身租约管理，不因发起它的 chat turn 完成而失效；取消聊天与取消已接受文档任务遵循现有独立接口，不能混用会话 token 撤销全部领域任务。

### 5.3 `waiting_for_attachments` 状态机

现行行为（`01c807e` 已有，本阶段复用）：turn 在附件解析期间进入 `waiting_for_attachments`；解析失败置 `failed`、用户取消置 `cancelled`（后续 turn 可继续）；`src/attachments/coordinator.py` 在资产 settle/deletion 时条件迁移到 `pending`/`failed`，并由 `reconcile_waiting_turns` 做恢复扫描。本任务是在此之上补下列缺口，不新建状态机：

- 等待中的 turn 阻塞同会话后续 turn（claim 队头阻塞，当前不存在）；
- 默认等待上限为 600 秒（`WAITING_TURN_MAX_SECONDS`），可通过配置调整；
- 超时标记为 `failed`，错误码为 `attachment_wait_timeout`；
- 会话列表和 SSE 显示阻塞 turn、当前状态和预计处理规则。

API 保留当前状态枚举，在响应与持久化事件中新增可选字段 `blocked_by_turn_id`、`queue_reason`、`wait_deadline`；后续 turn 可继续保持 `pending`，无需为界面另造一个不可恢复状态。超时扫描由维护/附件协调任务至少每 5 秒执行一次，按原状态和 generation 条件更新；附件完成事件与超时事件竞争时只允许一个状态迁移成功。实际前端消费组件在入口盘点中定位并同步兼容字段。

建议配置（附件等待上限；与 §6.4 的聊天排队 deadline 含义不同）：

```env
WAITING_TURN_MAX_SECONDS=600
```

### 5.4 Chat/Heavy Worker 隔离

第一阶段先拆分消费角色，不强制统一底层队列。角色与现有消费逻辑必须完整映射，拆分后每个队列都有明确归属，避免无人领取：

```text
Chat Worker（--role chat）
  └── chat_turns：claim/lease、心跳、requeue_stale_turns、队列状态上报

Authoring Worker（--role authoring）
  ├── document plan submissions（_process_document_plan_submissions）
  └── document authoring jobs（generate/resume/convert）

Parse Worker（--role parse）
  ├── RAGFlow/大文件解析（runtime.run_once）
  └── AttachmentWorker：附件解析；协调扫描使用独立控制循环

独立入口（不在本角色路由内）
  ├── Memory Worker
  └── Export Worker
```

Chat Worker 必须有保留容量，Heavy Worker 不得消耗全部聊天执行槽位。聊天 turn 的进程内并发已由合并基线 `WORKER_TURN_CONCURRENCY`（默认 1）控制，本阶段直接复用作 chat 单进程池大小，不新增 `CHAT_WORKER_MAX_CONCURRENCY` 等重复配置；初始保持 1（等价于“每进程一个任务”）。在 `src/workers/main.py` 增加显式角色路由，例如 `--role chat|authoring|parse|all`；`all` 仅用于兼容本地开发。生产初始配置为 2 个 chat 进程、1 个 authoring 进程、1 个 parse 进程；先验证 SQLite 承载能力再增加。单进程内暂不并发使用共享 AppPipeline。

角色拆分后必须验证：文档计划提交仍被 authoring 角色消费；附件 waiting turn 的超时扫描（§5.3 要求至少每 5 秒）使用独立控制循环及数据库连接，不能在同步 `AttachmentWorker.run_once()` 或 `runtime.run_once()` 返回之后才执行。默认由 parse 角色启动专用协调线程，不执行解析、文件清理或网络调用；整进程故障由备用 parse 实例接管，控制循环卡死则由健康检查告警并重启。

前述单 parse 起始配置仅支持进程管理器重启恢复。要验收协调者高可用接管，测试/部署必须提供至少两个候选控制循环；没有备用实例时，故障恢复上限还需计入进程重启时长，不能套用仅含 lease 的接管指标。

附件协调采用单一逻辑扫描所有者：生产同一数据库范围使用 `coordinator_leases` 行选主，默认 lease 15 秒、每 5 秒续约，接管递增 token。多个 parse 控制循环可以候选运行，只有持有有效 lease 的循环扫描；扫描结果提交仍检查 turn 原状态与 generation，不能仅相信“单例”。在途完成/失败回调也必须做条件更新，并与终态事件在同一事务提交。

仓库已有 `python -m src.attachments.worker` 模块入口，`pyproject.toml` 当前没有名为 `hardware-database-attachment-worker` 的 console script。保留该模块入口时，必须复用相同选主协议或关闭其协调职责，不能与新控制循环分别管理扫描所有权。解析任务可并行领取；租约失效的旧扫描即使迟到返回，也不能重复迁移或重复发终态事件。

兼容切换采用维护窗口：先交付新选主协议及覆盖旧模块入口的兼容开关，暂停新增相关任务，排空或封存在途解析，停止旧扫描与旧完成回调的写入者；然后启用新控制循环及受条件更新保护的回调，验证只有一个有效扫描所有者，再恢复准入。部署清单逐项记录主 Worker、独立附件 Worker 与服务管理器的启动命令；不支持协议的旧进程必须停止，不能靠“新版本只读扫描”约束仍在运行的旧二进制。回滚先停新消费者，再按兼容单消费者模式恢复。

独立进程解决进程故障与调度等待隔离；RAG/tool 部署级保留额度由 P0b 完成（LLM 进程层已由 `llm_governor` 保留 interactive 容量）。在 P0b 之前不把“下游服务满载时仍满足聊天 SLO”作为 P0a 的单独验收承诺。文档/计划生成仍受原有服务端确认 Gate、作业权限和租约约束。

### 5.5 测试与验收

- 两个同 session turn 并发提交，只有较小 `turn_sequence` 被执行；
- 两个不同 session 可以并行；
- waiting turn 阻塞后续 turn；
- waiting 超时、附件失败、用户取消均能推进后续 turn；
- lease 过期后新 Worker 可接管；
- 旧 Worker 不能提交 canonical Checkpoint、事件、Artifact 或最终答案；
- Chat Worker 崩溃不影响 Heavy Worker；
- 多个 Parse Worker 同时竞争或旧协调者暂停后恢复时，仅有效协调者可提交；解析阻塞超过 600 秒仍按时扫描。健康控制循环下超时后 10 秒内终态迁移；协调进程故障时上限为其 lease 过期后两个扫描周期，单独记录故障恢复时延；
- 在下游容量未饱和的故障注入测试中，Heavy Worker 的阻塞不会阻止 Chat Worker 领取任务；满载 SLO 在 P0b 联合验收。

## 6. Phase 1b：资源治理与背压

### 6.1 Resource Governor

**职责边界（与合并基线对齐）**：LLM 进程级准入已由 `src/core/llm_governor.py` 实现（`interactive` 使用全部额度、`batch` 单独上限、队列超时，覆盖全部 `model_gateway` 调用），是进程层的唯一 LLM 权威；本方案不新增第二层进程级 LLM 上限。

新增（仅部署级共享额度与背压）：

```text
src/core/concurrency/resource_governor.py
src/core/concurrency/rate_limiter.py
```

资源类型（由资源 governor 接入；LLM 共享额度由 `llm_governor` 接入，见 §6.3）：

```text
RAG
TOOL
CHAT
HEAVY_JOB
OCR
EXPORT
MEMORY
```

每次实际下游调用前获取额度，调用确实结束后释放。上层超时/取消不等于下游结束。额度 lease 必须携带请求、租户、资源类型和截止时间。LLM 部署级总量是 P0b 多进程验收必选项，由 `llm_governor` 扩展共享 limiter；每个进程各自限制 4 路不能保证部署总量为 4 路。

配额表至少包含 `resource_key`、`capacity`、`reserved_interactive` 和租约行 `permit_id/owner_id/attempt_id/weight/expires_at/state`。SQLite 在短 `BEGIN IMMEDIATE` 中检查在途权重、排队上限及插入租约；PostgreSQL 锁定单个资源配额行完成同样操作。连接在申请事务结束后归还池，不跨下游调用持有。

`resource_key` 按实际共享配额的 provider/endpoint/账号范围划分；不同模型共享账号 RPM/TPM 时不能各自分配整份额度。所有工具、后台模型和重试路径均通过真实客户端边界接入。SDK 自带重试需关闭并由统一重试层接管，或证明每次底层请求都会重新获取额度。

#### 6.1.1 接口与状态契约（P0-006 交付）

接口采用领域 dataclass/Pydantic schema，具体文件可随实现调整，必须保持以下语义：

| 操作 | 输入/结果 | 原子边界 |
|---|---|---|
| `acquire(request)` / `acquire_async(request)` | request 包含唯一 request_id、owner/attempt、服务端冻结 tenant、resource_key、priority、weight、deadline 与取消信号；返回 permit 或结构化拒绝 | 排队登记、队列上限、总量与 batch 上限校验、permit 创建均在配额事务中；等待在事务外 |
| `renew(permit_id, owner, revision)` | 返回新 revision/到期时间或 lease_lost | 所有者及 revision 条件更新；失败停止后续调用，已发送请求进入结束确认流程 |
| `finish(permit_id, outcome, evidence)` | 结束确认或 uncertain；重复确认返回已处理 | 更新 permit 与额度释放同一事务；不能以客户端异常作为远端结束证据 |
| `cancel_wait(request_id)` | 撤销未准入项 | 与准入竞争只能有一个胜者；已准入则转执行取消，不直接释放 |
| `snapshot(resource_key)` / `reconcile(permit_id, evidence)` | 脱敏统计或审计恢复结果 | 恢复按状态/revision CAS，审计与释放同事务 |

等待项状态为 queued → granted/cancelled/expired/rejected；permit 为 active → released/uncertain，uncertain → released 仅接受 §6.3.1 的结束证据。队列登记和 permit 分配具有稳定幂等键，连接重试不能多扣。持久化等待 deadline 使用存储时间，进程内剩余超时使用单调时钟。

CHAT/HEAVY_JOB 是任务执行槽，RAG/LLM/OCR 等是下游调用槽，生命周期分别计量。固定获取顺序为任务槽 → 本地执行器槽 → 进程 LLM 槽（仅 LLM）→ 部署下游槽，按逆序退出；禁止父工具占着子调用所需的同一资源额度等待子任务。协调线程不运行在检索池内。仅等待后台任务回执时持久化续执行点并释放任务槽。

部署级 capacity/batch_cap/reserved_interactive 存在配额库中并带 config_revision，不从每个 Worker 的环境值各自推导。增加 capacity 必须使用已验收配置并审计；减小到在途量以下时不撤销现有调用，停止新准入直至降到新上限。设置 batch_cap=0 应立即拒绝新 batch 请求，不能无限等待。无供应商 RPM/TPM 限额时标记“不适用”，不能宣称已验证供应商限速。

### 6.2 入口接入

必须接入（LLM 进程层已由 `llm_governor` 覆盖，以下针对 RAG/TOOL/HEAVY 等部署级额度）：

- `POST /query` 同步 API（保留 LLM 进程层，并补 LLM/RAG/tool 共享额度）；
- `_run_turn` Chat Worker；
- Document Authoring Worker；
- Memory Worker；
- Export/OCR Worker；
- Wiki 蒸馏等批量后台任务（其 LLM 走 `llm_governor` 的 `batch` 类）；
- Evaluation 评估任务（`src/evaluation/`）：RAGAS judge LLM 已经由 `govern_ragas_llm`（`ragas_adapter.py:1253-1255`，实现于 `model_gateway.py:477`）接入 `llm_governor` 的 `batch` 类，无需重复限流；仅 `EVAL_EMBEDDING_*`（embedding，非 LLM）尚未治理，在真实边界接入部署级 governor，若经评估确认不纳入，须在基线报告和指标目录中显式标注为范围外；
- `document_search_batch` 的每个 RAG 调用。

每个入口必须登记客户端边界、资源类型、取消信号、重试策略和 permit 释放条件。生产 deployment 模式下 governor 装配/准入失败必须阻止新的下游调用。现有 `govern_ragas_llm` 包装失败时返回原对象，P0-006 应增加显式装配状态和验收断言；如开发评估选择 fail-open，结果须标记 `governance_bypassed`，只允许使用与生产隔离的账号/服务，不能作为生产容量验收依据。判分失败与治理装配失败分别计数。

`POST /query` 在过渡期接入相同部署级预算；迁移完成后再决定是否重定向到 turn 接口或下线。P0a 同时核对该入口是否会写共享 Checkpoint 或业务状态；若会写，必须在该阶段接入同一所有权/领域幂等边界，不能等 P0b 才处理正确性。无持久化会话的查询仍保留独立请求上下文。

### 6.3 作用域

单实例开发：

```env
RESOURCE_GOVERNOR_SCOPE=process
```

本方案多进程及多主机生产统一使用共享配额存储：

```env
RESOURCE_GOVERNOR_SCOPE=deployment
```

`deployment` 在 P0b 同主机映射到 SQLite，在 P1 跨主机映射到 PostgreSQL；不同时实现 Redis 或静态分配方案。共享配额存储承载 RAG/TOOL/CHAT/HEAVY/OCR/EXPORT/MEMORY，以及由 `llm_governor` 申请的 LLM permit。LLM 部署总量、batch 总上限和互动保留量必须覆盖所有 API/Chat/Heavy/Memory/Export/Evaluation 进程；P1 只替换存储后端，不首次引入 LLM 总量治理。SQLite 配额库使用可靠本地存储；所有进程访问同一文件，不放在 NFS 或未验证文件锁的共享目录。配额库不可用时拒绝新申请；已接受任务有界等待或按 deadline 失败，不回退本地无限放行。

SQLite 启动与连接要求：

- 启动时将 `RESOURCE_GOVERNOR_SQLITE_PATH`（新增配置）解析成绝对路径并验证部署清单；容器必须挂载同一宿主文件，不能各自创建同名库。路径之外还校验库内固定 `deployment_id`、schema 版本与配置摘要，缺失预配置库的生产进程不得静默新建。
- 初始化时设置并验证 `journal_mode=WAL`；每个连接设置 `foreign_keys=ON`、`synchronous=FULL`。SQLite 会话连接 busy timeout 默认 5 秒，配额连接默认 1 秒，并裁剪到当前请求剩余 deadline；忙错误有界重试，不占着事务睡眠。
- 单进程单连接或连接池都不得跨线程无保护共享事务；申请、续约、释放均是短事务，网络调用完全在事务外。测试包含多进程写竞争、错误挂载、磁盘满和进程重启。
- WAL 文件与数据库一起保留，在线备份使用数据库备份接口或完成一致性停写；不能只复制主 `.db` 文件。配置路径/库身份不同、WAL 不生效或 schema 不兼容时启动失败。

进程崩溃后不能仅因心跳过期就假定远端 LLM 请求已终止。无法确认结束的额度进入 `uncertain` 隔离状态，直到已验证的下游执行 deadline 或人工/服务端取消确认；告警并暂停相应容量放大。验收区分“客户端准入上限”和故障期间不可直接观察的远端执行量，不宣称网络分区下天然精确计数。

#### 6.3.1 `uncertain` 恢复流程

由独立 governor 维护循环每 5 秒扫描；多实例通过配额库中的维护 lease 选主，所有更新仍做状态/revision CAS。permit 增加 `provider_request_id`（若支持）、`started_at`、`hard_deadline_at`、`uncertain_since`、`recovery_reason` 和审计引用。

| 触发/证据 | 状态处理 | 配额行为 |
|---|---|---|
| 进程失联、传输中断，无法确认远端停止 | `active → uncertain`，记录原因 | 继续占用原额度并立即告警 |
| 下游确认请求已完成/取消，或远端保证的最大执行期限已过 | CAS `uncertain → released`，记录证据 | 幂等归还一次额度 |
| 只达到客户端 timeout，没有远端结束依据 | 保持 `uncertain` | 不自动释放 |
| 隔离超过 15 分钟 | 升级运维告警，暂停该 resource_key 的新准入与扩容 | 其他独立 provider 不受影响 |
| 运维确认远端进程重启、执行已终止或完成了可验证取消 | 经带理由的恢复命令 CAS 释放 | 审计操作者、证据和影响范围 |

新增 `RESOURCE_UNCERTAIN_ESCALATE_SECONDS=900` 是升级处理时限，不是强制释放 TTL。无法证明停止时不存在安全的自动“最长保留时间”；状态持续保留、停止该资源新准入，直到确认后恢复。permit 不转业务 dead-letter，业务 job 的失败/重试由其自身状态机决定。维护 lease 过期、重复确认、迟到正常释放和人工恢复竞争必须测试，确保额度不被释放两次。

恢复工具至少提供 list/show/reconcile，release 必须检查证据类型并写审计，不提供直接清空计数的快捷路径。下游不支持请求状态查询时不反复调用不存在的查询 API；只使用其已验证的服务端 deadline 或运维确认。告警恢复和新准入恢复在审计后执行，不能仅因 uncertain 行消失自动提高 capacity。

### 6.4 背压规则

```env
RESOURCE_QUEUE_MAX_WAIT_SECONDS=30
RESOURCE_QUEUE_MAX_DEPTH=256
CHAT_QUEUE_DEADLINE_SECONDS=600
```

- 同步 API 在未接受任务前超限返回 `429` 和 `Retry-After`；
- 已持久化任务返回 task/turn ID，通过状态显示排队；
- 排队超时进入明确的 failed/cancelled 状态；
- 排队项取消立即移除；
- 执行中的调用依靠下游 timeout/cancel 结束；
- 重试重新申请额度，退避期间不占执行槽；
- 流式 LLM 直到真实流关闭才释放额度。

资源队列等待默认 30 秒，与整个业务任务的排队 deadline 分开；聊天任务默认最长排队 600 秒（`CHAT_QUEUE_DEADLINE_SECONDS=600`，区别于 §5.3 的附件等待上限 `WAITING_TURN_MAX_SECONDS`），文档/导出任务继续使用各领域 SLA，不因一次额度申请失败立即丢弃已接受任务。失败前必须检查取消状态与重试总预算。SSE 已建立后用持久化事件报告限流/失败，不能改变 HTTP 状态码。

LLM 进程级额度复用 `LLM_MAX_CONCURRENCY`（默认 4）与 `LLM_BATCH_MAX_CONCURRENCY`（默认 1）。部署级额度统一登记在配额库：RAG 压测起始 capacity=8、reserved_interactive=4；LLM 按实际共享账号/endpoint 的实测承载能力登记 capacity/batch_cap，不能按进程数乘出容量。配置初始化工具导入带版本的配额清单，不再为每个 Worker 新建一套可漂移的全局环境上限。后台不能借用保留额度；在线可用总额度空闲槽。RPM/TPM 按供应商限额登记，按每次真实发送扣 RPM、预留估算输入及输出上限并在返回后结算 TPM；未发送可退预留，缺失 usage 或结束不明不得直接退还全部 token 预算。

资源等待队列上限不等于持久化 turn/job 数量上限。P0-006 在创建任务的事务中检查部署/租户/用户的未终态数量及请求幂等键：相同请求重试返回原任务，不多计额度；超限不插入任务，返回 `429`。队列容量取 Phase 0 冻结值；资源故障返回 `503`，客户端取消不伪装成限流。已返回 task ID 后不再用 HTTP 拒绝冒充未受理。

`CHAT_QUEUE_DEADLINE_SECONDS` 从 turn 接受时起计到首次开始执行，包含附件等待与同会话阻塞；附件 deadline 从进入 waiting 时独立计算，先到者决定终态。控制循环扫描所有已过期未开始 turn，不受是否为队头限制；按状态/generation 条件迁移一次。运行中任务沿用执行 deadline，不能误按排队超时终止。已发送响应头的遗留无持久化会话 SSE 通过现有 error 事件返回失败；持久化 turn 则将失败和事件同事务写入。

### 6.5 Shared Retrieval Executor

把 `document_search_batch` 改为：

```text
Bounded Retrieval Queue
        ↓
Shared RetrievalExecutor
        ↓
RAG Resource Governor
        ↓
RAGFlow Client
```

要求：

- 进程启动时创建，进程退出时统一关闭；
- 有界线程数和有界等待队列；
- 每请求 fan-out 仍最多 4 路；
- 使用 `submit_with_current_context` 传播 ContextVar 和 OTel context；
- 未运行 Future 可取消，已运行调用依靠下游取消/超时；
- ToolRuntime 结束后迟到结果不得写入 evidence；
- 保留现有证据去重、预算和 citation 编号行为。

#### 6.5.1 实现与取消边界（P0-007 交付）

- 工厂注入 `submit_with_current_context(callable, *, request_id, deadline, cancel_event)`，返回 Future 或明确的 queue_full/deadline/cancelled；全局生命周期由 API lifespan 与各 Worker 启停钩子持有，工具不能创建/关闭共享池。
- 使用显式有界提交层；`ThreadPoolExecutor(max_workers=N)` 的内部无界队列不能直接作为验收实现。部分子任务已提交后遇到满载，要取消未运行项并返回结构化部分结果，不能重复提交已完成查询。
- 同步 RAGFlow 调用不能由 `Future.cancel()` 强制结束。接入现有客户端超时/异步取消能力并分别验证；上层可以先结束等待，但未确认结束的调用仍保留 permit。客户端本地结束也不自动证明远端已结束。
- ToolRuntime 增加结束标记及 generation；结束检查和证据/预算/引用编号登记使用同一锁或串行合并通道，防止“检查未结束、随后结束、再登记”的竞争。每个子提交独立复制 ContextVar/OTel context，不跨线程共享一个 Context 对象。
- 停机先停止提交、取消队列项，再在 shutdown deadline 内等待已运行调用；超过 deadline 的未确认下游调用进入 uncertain，进程退出不能直接清零配额。
- 验收包含两个请求交叉取消、满队列部分提交、晚到结果、关闭中提交、异步准入期间取消。`asyncio.to_thread` 中的申请即使外层被取消仍可能成功，必须回收迟到 permit；不得在事件循环线程用同步 condition 等待配额。

### 6.6 Phase 1b 验收

- API 和 Worker 总调用量不超过部署预算；
- 队列满载会背压或明确拒绝；
- batch fan-out 不再随请求数无限增长；
- 取消、超时、重试不会错误释放额度；`uncertain` 有告警与恢复流程，不能通过直接重置计数掩盖远端执行未知；
- Trace、Evidence、ContextVar 在子任务中保持一致；
- RAG/LLM/queue 指标可在 Grafana 查询。

## 7. Phase 2：状态迁移与多实例

### 7.1 Agent Checkpoint

交付：

1. 统一 Checkpoint provider 接口；
2. 保留 SQLite provider；
3. 增加经版本验证的 PostgreSQL saver；
4. 锁定 LangGraph、saver 和驱动版本；
5. 初始化 schema；
6. 迁移和校验历史 checkpoint；
7. 验证恢复、删除、并发和 fencing。

`pyproject.toml` 当前只包含 SQLite Checkpoint 依赖。新增 PostgreSQL 依赖时必须同时更新锁文件、镜像、离线安装包、CI 和部署初始化脚本。

### 7.2 Authoring Checkpoint

当前配置允许 `DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=postgres`，但 builder 对 sqlite/memory 以外的值会报错。必须在 PostgreSQL provider 完成前收紧配置校验，或同步补齐 provider，禁止出现“配置成功、运行失败”。

保留 `FencedCheckpointer` 的 token 校验，不能因更换存储而降低旧 Worker 拒写能力。

### 7.3 Memory 迁移

分两层实施：

1. 权威账本、jobs、outbox、generation、lease、同意和来源；
2. 可重建的 Memory 检索投影。

先影子双读或离线校验，再切换写入。Memory 权威账本、会话和相关 outbox 是同一个原子切换组，虽然可以分 PR 实现，但不能一半写 SQLite、一半写 PostgreSQL。使用同一 PostgreSQL database 内的一条连接、一个事务保持原子提交；按 schema 分域不等于按 database 分库，多个连接也不自动共享事务。本轮不引入跨库双写。Memory 投影在权威组切换后重建并追平 outbox，再开放检索。

### 7.4 会话、权限和 Artifact

迁移范围包括：

- 用户、权限、会话、消息和 turn；
- 附件元数据及解析状态（`storage/chat_attachments.db`，`CHAT_ATTACHMENT_INDEX_DB_PATH`）与附件文件（`storage/chat_attachments/`）；`auth.db` 的 `turn_attachments` 关联须与 `auth.db` 同组迁移，两库之间的引用一致性做迁移校验；
- Document/Export Job；
- Artifact 元数据和实际文件；
- 资产中心（`src/core/assets.py`、`src/core/doc_assets.py`）与 Wiki 页面/蒸馏结果（`src/core/wiki.py`）落在 `auth.db`（PR #49 合并）；
- Circuit、Spreadsheet、Document 索引；
- 本地配置缓存中仍被视为权威的内容。

首版 Artifact 使用共享文件服务，数据库只保存逻辑 storage key，不保存节点专属绝对路径；所有 API/Worker 挂载同一共享根目录。临时文件与目标文件在同一文件系统内完成原子改名，并验证断连、权限、大小/hash、下载与孤儿清理。共享文件服务不可用时任务重试/失败，不发布缺失文件的成功记录。

迁移按事务依赖分批，代码 PR 与数据切换单元分别登记：

| 切换单元 | 范围 | 切换条件 |
|---|---|---|
| A：会话权威组 | auth/permission/session/turn/事件/执行指针 + Memory ledger/jobs/outbox，以及与其共享事务的领域表 | 同一数据库连接提交；不能将会话与 Memory 权威账本拆开切换 |
| B：领域权威组 | Authoring/Attachment/Export/Wiki/资产等剩余元数据 | 按 Phase 0 事务清单细分；与 A 共享原子事务的表并入 A；已跨库的关联保留幂等协调与故障恢复协议 |
| C：文件存储 | Artifact、附件与相关归档文件 | 在引用它们的写入者跨主机运行前完成；文件 hash 与元数据 storage key 成对校验 |
| D：检索投影 | Memory/Circuit/Spreadsheet/Document 中已确认可重建的部分 | 对应权威组完成后重建/追平；不可重建原始数据先并入 A/B/C |

每个实际切换单元独立演练 §7.5；“尚未迁移就单实例路由”仅适用于完整独立单元，不能让同一原子事务分别写 SQLite 和 PostgreSQL。

本地向量索引不直接放到共享目录供多实例写入：可重建索引采用只读版本快照与单写者发布，或独立索引服务。权限/元数据以共享数据库为准。该域未完成前，相关能力保持单实例路由，不能宣称所有工具已经支持横向扩展。

### 7.5 迁移流程

1. 备份源库和文件；
2. 在备份副本演练迁移；
3. 运行 schema migration；
4. 导入数据并执行数量、hash、外键和权限校验；
5. 开启影子读；
6. 比较新旧读结果；
7. 停止所有相关 API、Worker、维护任务写入并排空/封存执行；执行最终全量同步或可验证的增量补齐，再次核对总量、hash、外键和恢复指针；
8. 在旧消费者全部停止后切换写入和 Worker；同一权威组不得按随机请求混用新旧数据库；
9. 灰度放量，同一原子切换组采用一致的后端，不逐请求选择数据库；
10. 保留旧库和旧文件至回滚窗口结束。

目标库已经接收新写入后，不允许直接切回旧库。回滚必须采用反向同步、操作重放或明确恢复点。

默认策略为：开放目标库写入前可无损回滚；开放后优先回滚到仍兼容目标 schema 的上一应用版本。必须恢复旧库时先停写、导出并校验目标新增状态后再切回；反向迁移没有演练通过则保持目标库并前向修复，不能为赶回滚窗口丢弃已接受任务。

### 7.6 Phase 2 验收

- 任意 API 节点可恢复同一会话历史；
- 任意同角色、版本兼容的 Worker 可接管过期任务；
- 删除、权限、租户隔离行为与迁移前一致；
- Memory 账本与投影一致；
- Artifact 可跨节点访问；
- 多实例下不产生重复 Work Order、Artifact 或有效迟到写入；
- 模拟迁移失败可恢复到明确时间点。

必须通过事务注入测试：在答案、turn、执行指针、Memory job 和 outbox 的每个写入边界抛异常，确认同一事务全部回滚；repository 接收调用方连接，不擅自开启第二连接提交。部署检查实际 database 身份和 schema 搜索路径，不能仅凭 DSN 指向同一个 PostgreSQL 主机就判定原子组共享事务。无法共享同一事务的领域必须先重设计切换组，不能在本方案下直接启用跨库写入。

## 8. Phase 3：Action DAG 受控并行

### 8.1 Action 契约

扩展服务端校验后的 Action 元数据：

```text
access_mode = READ_ONLY | WRITE
cost_class = NORMAL | HEAVY
resource_key
dependency_requirements = {dependency_action_id: ACCEPTED | COMPLETED}
```

`access_mode`、`cost_class` 和 `resource_key` 不能直接信任模型输入，必须由服务端根据 action kind、真实目标和权限补全或校验。

依赖完成条件定义在依赖边上；同一个动作可以依赖 A 的提交回执，同时依赖 B 的完成结果。默认 `COMPLETED`，仅服务端白名单允许 `ACCEPTED`。需要异步等待时保存计划版本、动作 ID、任务引用和 receipt，通过任务完成事件幂等唤醒；禁止占用线程轮询数分钟。现有计划 schema 保持 v1 解析兼容，新依赖语义使用版本化契约。

### 8.2 调度规则

- 同层只读且资源键不同的 Action 才允许并行；
- 同资源写写、读写冲突必须串行或使用明确快照；
- 未知动作默认串行；
- `accepted` 只表示后台任务已提交；
- 依赖实际结果的动作必须等待 `completed`；
- 子动作结果由协调线程按稳定 action 顺序合并；
- 部分失败、取消和证据重复必须产生结构化 receipt；
- 资源获取顺序固定，避免嵌套池死锁。

### 8.3 测试

覆盖独立只读并行、同资源写串行、读写冲突、accepted/completed 依赖、部分失败、取消、依赖环、迟到结果和重复 evidence。只有 Phase 0 基线证明串行执行是实际瓶颈时才开启默认并行。

并行前由 P2-001 交付持久化计划/receipt repository：键为 `(tenant_id, session_generation, turn_id, plan_version, action_id)`，领域任务提交沿用该稳定键；receipt 含 status、job_ref、结果引用、错误、revision。已完成 receipt 不重做，缺失结果的 action 才进入恢复调度；部分并行动作按各自 receipt 恢复，不能将整个层级当成一项重放。写动作提交与本地 receipt 无法同事务时，依靠领域幂等键回查并补记，不能声称跨存储恰好一次。领域无法提供幂等/回查时，停止自动重试并进入待协调状态。

Action 的资源键表示权限范围内的目标实体，与 provider 配额键分开命名。跨计划的写冲突必须复用领域版本 CAS/锁，不能仅靠一个 DAG 内串行。取消后不再调度新 action，已提交异步任务保留其领域生命周期；迟到结果只能更新所属计划的有效版本。契约未实现时不得打开并行开关。

## 9. Phase 4：观测、评测和压测（从 Phase 0 开始贯穿）

### 9.1 指标目录

复用已有指标：

```text
hdb.agent.runs
hdb.agent.duration
hdb.llm.calls
hdb.llm.duration
hdb.chat.turns
hdb.chat.queue.wait
hdb.queue.depth
hdb.worker.task.processed
```

新增指标契约如下；现有同义指标优先复用，不能重复计数：

| OTel 名称 | 类型 / 单位 | 标签 | 采集位置 |
|---|---|---|---|
| `hdb.resource.active` | ObservableGauge / `{calls}` | resource, scope | 配额存储观测器 |
| `hdb.resource.queue.wait` | Histogram / `s` | resource, status | 申请额度结束 |
| `hdb.resource.rejected` | Counter / `{requests}` | resource, reason | 准入拒绝 |
| `hdb.conversation.lock.wait` | Histogram / `s` | status | 会话领取 |
| `hdb.conversation.stale_write_rejected` | Counter / `{writes}` | operation | 权威状态拒写 |
| `hdb.conversation.attachment_wait` | Histogram / `s` | status（ready/timed_out/failed/cancelled） | 离开 `waiting_for_attachments` 的条件迁移，成功解析进入 pending 也须采集 |
| `hdb.chat.reserved_capacity` | ObservableGauge / `{slots}` | resource | 配额配置观测器 |
| `hdb.governor.lease_lost` | Counter / `{leases}` | resource, reason | 续约/接管 |
| `hdb.resource.uncertain` | ObservableGauge / `{calls}` | resource | 不确定额度隔离 |

LLM 相关维度不新增第二套采集：active/queued/timeouts/max_wait_ms 直接导出 `llm_governor.snapshot()`（`GET /system/status -> llm`），token/渠道用量导出 `model_gateway` usage ledger；表中 `hdb.resource.*` 的 LLM 行仅在部署级协调落地后补充，不得与进程层快照重复计数。

资源、原因标签使用固定枚举，并更新 `src/observability/semantic.py` 白名单（当前 `METRIC_LABEL_KEYS` 尚无 `resource`/`reason`），否则过滤器会丢弃新标签。user/session/turn/tenant ID、查询文本和原始异常不能进入指标标签；授权范围内的 Trace 可以关联这些身份。部署级 gauge 由单一观测角色读取，或仪表盘去重，不能对每个 Worker 上报的同一部署总量求和。聊天 queue wait 继续复用 `hdb.chat.queue.wait`。

### 9.2 Grafana/Phoenix

至少建立：

- Chat SLO 面板；
- Heavy Job 面板；
- LLM/RAG 资源面板；
- 会话锁等待和 stale write 面板；
- Checkpoint/DB pool 面板；
- Worker lease、重试、死信和接管面板；
- Phoenix 中按 route、tenant、mode、tool 和状态筛选的 trace 维度。

### 9.3 压测

顺序为：

1. 单实例 1→5→10→20→50 并发；
2. Chat/Heavy 混合负载；
3. API + Worker 多进程；
4. PostgreSQL/共享 Artifact 多实例；
5. LLM/RAG/DB/Worker 故障注入；
6. 恢复、接管和回滚演练。

压测报告必须写明实例数量、模型、RAGFlow 版本、数据库版本、负载比例、P50/P95/P99、错误率、队列等待和容量拐点。

增加固定到达速率测试：在目标流量下预热 5 分钟、测量至少 30 分钟，并重复 3 次；再用 2 倍到达率施压 5 分钟后恢复。分别记录提交成功率、接受后完成率、拒绝率和队列增长趋势，禁止只统计 HTTP 2xx。长任务另统计等待/执行时长；不足 1,000 个完成样本时不把 P99 当作稳定结论。

### 9.4 可计算的验收门槛

Phase 0 冻结同模型、同样本、同硬件的三次基线中位数 B，并**按会话内并发度分层采集**：同一 session 单 turn、同一 session 多 turn 并发、跨 session 并发。延迟门槛必须使用对应口径的基线，基线报告必须逐样本记录 session 并发度，否则门槛不可复现。

P0a 的同会话串行化会按设计增加同会话后序 turn 的等待，因此延迟验收拆成两个口径：

- 跨会话口径：聊天端到端 P95、首 token P95 不超过跨会话基线 B 的 1.10 倍；
- 同会话口径：后序 turn 的 queue wait（含 `waiting_for_attachments` 阻塞时长）单独报告，门槛采用 Phase 0 冻结的会话内排队规则推算值，不使用 1.10 × B，不把串行化设计记为延迟回归。

以下是工程默认门槛，环境实测发现无法达到时须在该阶段上线前记录调整依据，不在报告中冒充已通过：

| 维度 | 默认门槛 |
|---|---|
| 权限、会话顺序、取消后有效写入、幂等冲突 | 确定性测试全部通过，越权和重复发布为 0 |
| 同目标负载下跨会话聊天端到端 P95、首 token P95 | 各自不超过跨会话基线 B 的 1.10 倍；同会话 queue wait 单独报告 |
| 相同负载下 token/成功任务 | 不超过 1.10 × B |
| 接受后内部失败率 | 不超过 1%，且较基线上升不超过 0.2 个百分点；独立列出用户取消和主动拒绝 |
| 质量 | routing/citation/正确率下降均不超过 1 个百分点；Unsupported claim rate 上升不超过 1 个百分点 |
| 等待附件超时 | 健康协调者下超时后 10 秒内进入终态；协调者故障时不晚于其 lease 过期后两个扫描周期，分别报告 |
| 过载 | 准入等待不超过配置 deadline；队列不超上限，恢复负载后 5 分钟内回到过载前深度 |
| Worker 恢复 | 同角色接管不晚于 lease 过期后两个轮询周期；只有新 attempt 能提交 |
| 存储迁移 | 权威记录/附件文件校验无未解释差异；已接受任务 RPO=0 |

回归集建议至少 200 个固定业务样本；随机模型评测重复三次，报告置信区间，落在不确定区间的结论不得直接视为通过。容量基线若已不满足内部失败率门槛，先归因修复再进入扩容验收。绝对产品 SLO 在 Phase 0 根据该基线和业务要求登记；本方案不虚构服务器实际可达到的秒数。

#### 9.4.1 相对门槛与绝对 SLO 必须同时通过

相对基线用于判断本次变更是否退化，绝对 SLO 用于判断服务是否满足业务要求。基线已经很慢时，`1.10 × B` 不构成放宽业务门槛的理由。跨会话延迟按 `实测 P95 ≤ min(1.10 × 对应 B, 已登记的绝对 P95 上限)` 验收；同会话排队仍按前序阻塞规则单独计量，同时受业务等待 deadline 约束。

P0-001 的 `baseline-report.md` 必须包含以下完整字段，不得只填写“目标值”：

| 字段 | 登记要求 |
|---|---|
| `profile_id`、环境、模型/版本、并发分层、目标到达率 | 唯一标识本次 SLO 适用负载，禁止跨环境套用 |
| `chat_e2e_p95_limit_s`、`chat_ttft_p95_limit_s` | 普通聊天端到端及首 token 的业务绝对秒数上限 |
| `chat_queue_p95_limit_s` | 单独标明资源等待、同会话阻塞及总等待口径 |
| `heavy_queue_limit_s_by_kind` | 文档、导出、解析分别登记，不能合成一个无法定位的均值 |
| `accepted_failure_rate_limit` | 默认不高于 1%；业务更严格时取更小值 |
| `llm_timeout_rate_limit`、`rag_timeout_rate_limit` | 使用 Phase 0 实测及业务要求冻结，429 与主动准入拒绝独立报告 |
| `approver_role`、冻结日期、版本、调整原因 | 业务/服务负责人确认绝对目标；调整必须形成记录并重测 |

绝对延迟值需要真实环境与业务输入，本文不虚构具体数值。缺少上述数值不阻塞本地实现和基线采集，但阻止生产容量验收；只通过相对门槛的结果必须标记“回归通过，业务 SLO 未验收”。全套冻结门槛在容量测试前确定，不在看到失败结果后无记录放宽。

## 10. 发布与回滚

### 10.1 Feature Flag

建议配置（布尔/后端开关与前五项为新增，当前 `settings.py` 不存在；最后一项为已有配置）：

```env
# 新增
CONVERSATION_SERIALIZE_SAME_THREAD=false
RESOURCE_GOVERNOR_ENABLED=false
SHARED_RETRIEVAL_EXECUTOR_ENABLED=false
AGENT_CHECKPOINT_BACKEND=sqlite
ACTION_DAG_PARALLEL_ENABLED=false

# 新增（P0-003/P0-004 数值配置，非开关）
CHAT_TURN_LEASE_SECONDS=120
CHAT_TURN_LEASE_RENEW_SECONDS=15
WAITING_TURN_MAX_SECONDS=600

# 已有，保持默认 sqlite
DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=sqlite
```

已合并配置直接复用：`WORKER_TURN_CONCURRENCY`（默认 1，P0a 保持）、`LLM_MAX_CONCURRENCY` / `LLM_BATCH_MAX_CONCURRENCY` / `LLM_QUEUE_TIMEOUT_SECONDS` / `LLM_QUEUE_WARN_SECONDS`。P0b 不新增同层 LLM 上限，同机与跨主机的部署级协调见 §6.3。

布尔开关默认关闭，后端默认 SQLite。完成阶段验收后逐项打开；使用已有 Authoring backend 配置，避免另造相互矛盾的 PostgreSQL 布尔开关。`turn_sequence` schema 可以先以兼容方式部署，再开启强制校验。多 Worker 生产启动必须验证 serialization 与 fencing 已开启；完成 P0b 后部署级 governor 是多 Worker 放量的前置条件，LLM 进程层继续由 `llm_governor` 承担，不能简单关闭治理后维持原规模继续放量。

### 10.2 灰度顺序

1. 测试环境全量；
2. 隔离测试队列的单个 Chat Worker 灰度；
3. 单租户或内部用户灰度；
4. 10% 流量；
5. 50% 流量；
6. 全量。

每一步至少观察一个完整业务高峰周期，并确认 queue wait、错误率、stale write、重复 Artifact 和 token usage 没有异常。

灰度按租户/会话固定分组，不能让同一 session 的不同 turn 随机落入新旧 claim 协议。未完成协议兼容时先停旧消费者再启新消费者；随机 10%/50% 流量仅适用于已兼容的无状态请求或隔离分组，不适用于同一权威库的新旧写入路由。

### 10.3 回滚规则

- P0a：先停止新 turn 创建并排空或撤销全部在途执行，再停止旧消费者；关闭 serialization 只能回到经过验证的单消费者兼容模式，不能让新旧 claim 规则混用；
- P0b：暂停准入及新 lease 申请，保留已有 lease 的释放和 uncertain 状态处理；回滚到上一兼容版本后恢复准入，禁止通过关闭 governor 让全部 Worker 无限制执行；
- Checkpoint：切回 SQLite 前必须确认没有只写入 PostgreSQL 的新状态；
- Memory：回滚到旧投影时从权威账本重建，不直接复制可能过期的索引；
- Worker：恢复旧 Worker 前先停止新 Worker 并等待 lease 过期/显式撤销；
- Action DAG：关闭并行 flag，保留串行执行路径；
- Artifact：不删除已发布 Artifact，使用状态和版本标记隐藏无效产物。

## 11. 完成定义

整个路线图完成需要满足：

- P0a/P0b 的正确性、隔离、背压和取消测试通过；
- 目标存储迁移完成并通过恢复、权限和回滚演练；
- API/Worker 多实例总资源不超过部署预算；
- Chat 与 Heavy Job 在混合负载下分别满足 SLO；
- Action DAG 并行只在回归和压测证明有收益后启用；
- Grafana/Phoenix 能定位队列、锁、资源、下游和任务失败；
- 文档生成、Artifact、Gate、幂等和审计语义保持不变；
- Reviewer/Research Agent 是否启用有明确评测依据；
- 每个阶段都有独立提交、验收报告和回滚记录。

## 12. 首批执行任务

建议建立以下任务单；本文只定义任务，未在外部项目管理系统创建记录。依赖关系以 §13 为准：

1. `P0-001`：Phase 0 回归集和容量基线（含会话内并发度分层）；
2. `P0-002`：turn_sequence schema、回填和校验；
3. `P0-002b`：Checkpoint attempt 分叉/恢复与 pending writes 能力验证（P0-003 入口条件）；
4. `P0-003`：同会话 claim/lease/fencing；
5. `P0-004`：waiting_for_attachments 队头阻塞、600 秒超时与可见状态（扩展已有协调器）；
6. `P0-005`：Chat/Heavy Worker 角色隔离与队列归属；
7. `P0-006`：Resource Governor、`llm_governor` 同机共享适配、持久化任务准入、全部入口盘点与恢复工具；
8. `P0-007`：Shared RetrievalExecutor 和取消测试；
9. `P1-001`：Checkpoint/Authoring provider 兼容性矩阵；
10. `P1-002`：Memory 权威账本与投影迁移设计；
11. `P1-003`：会话、权限、Artifact 多实例迁移演练；
12. `P2-001`：Action DAG 服务端契约和串行兼容实现；
13. `P2-002`：指标目录、Grafana/Phoenix 看板和故障压测；
14. `P3-001`：Reviewer Agent 评测和灰度设计；
15. `P3-002`：Research Agent 可行性评估。

配套任务 `P0-008`：未实现 provider 的配置保存/启动防护及安装 smoke 测试，依赖 P0-001，随 P0a 验收交付。

## 13. 开发执行规范

### 13.1 分支和提交

每个任务使用独立分支，提交保持单一主题：

```text
feature/p0-turn-sequence
feature/p0-checkpoint-fork-validation
feature/p0-turn-claim-fencing
feature/p0-waiting-attachments
feature/p0-worker-roles
feature/p0-resource-governor
feature/p0-retrieval-executor
feature/p1-checkpoint-postgres
feature/p1-state-migration
feature/p2-action-parallelism
feature/p2-observability-eval
```

每个 PR 必须包含变更说明、测试命令、指标、灰度和回滚步骤；迁移脚本与 Feature Flag 按变更适用性提供。生产分支不直接合并未经 Phase 0 基线验证的并发参数。

### 13.2 测试命令

后端默认使用仓库虚拟环境执行：

```bash
uv run python -m pytest -q tests/test_document_search_batch.py tests/test_conversation_action_executor.py tests/test_worker_fairness.py
uv run python -m pytest -q tests/test_document_authoring_job_store.py tests/test_chat_attachments.py
uv run python -m pytest -q tests/evaluation/test_hardware_metrics.py
```

多进程、SQLite 锁、PostgreSQL 迁移和共享 Artifact 必须增加真实临时数据库/容器测试；纯单元测试不能替代这些验收。新增测试应优先放在现有对应测试文件或同目录测试中。

### 13.3 阶段退出门槛

阶段完成必须同时满足：

1. 功能测试和故障测试通过；
2. Phase 0 质量指标没有超过回归容差；
3. 资源、队列、租约和 stale write 指标可查询；
4. 灰度期间没有重复任务、Artifact 或越权读取；
5. 回滚演练成功且恢复点已记录；
6. 下一阶段依赖的接口和数据契约已冻结。

门槛按阶段适用范围执行：Phase 0 负责产出基线，不要求尚未实现的 governor 指标；P0a 验收会话正确性和进程隔离，P0b 才验收联合满载容量；P1 才验收跨主机迁移与共享存储。

任何一项不满足，只能继续在当前阶段修复，不能跳过验收进入下一阶段。

### 13.4 任务依赖图

```text
P0-001 基线（含会话并发度分层）
   ├── P0-002 turn_sequence
   │      ├── P0-002b Checkpoint 分叉/恢复验证
   │      │      └── P0-003 claim/lease/fencing
   │      └── P0-004 waiting_for_attachments
   ├── P0-005 Worker 角色隔离
   ├── P0-008 provider 配置防误启用
   └── P0-006 入口盘点/governor 设计
          └── P0-007 retrieval executor

P0-002b + P0-003 + P0-004 + P0-005 + P0-008
   └── P0a 验收
          └── P0-006 实现（含同机 LLM 共享额度）+ P0-007 → P0b 全量容量验收
                 └── P1 状态迁移/多实例
                        └── P2 Action DAG 并行
                               └── P3 Reviewer/Research 决策
```

P0-005 Worker 隔离可以和 P0-003/P0-004 并行开发，但必须在 P0a 联合验收前合并到同一测试环境。P1 的各存储域可以分别开发，切换前必须按同一权威状态组完成演练。

### 13.5 任务交付与文件映射

下表“新增”为拟创建文件，不表示文件已经存在；标注“扩展”的条目是在已合并实现上修改（`src/attachments/coordinator.py`、`src/attachments/worker.py` 已存在，见 §2 事实 12 与 §5.3）。验证任务、指标建设可并行；P0b 上线必须等 P0a 验收，不因设计可以并行而越过发布依赖。

| 任务 | 主要代码/交付物 | 必测边界 | 负责角色 |
|---|---|---|---|
| P0-001 | `src/evaluation/`、基线报告、状态清单、测试数据清单 | 固定样本重复三次；两类查询入口 | 测试 + 后端 |
| P0-002 | `src/core/conversation.py`、`src/api/schemas.py`、新增 turn 迁移脚本 | `next_turn_sequence` 初始化/回填、并发分配、幂等请求、非空/唯一约束、清空会话不复用 | 后端 + 存储 |
| P0-002b | 验证脚本与 checkpoint attempt 适配设计（`agents/runner.py`、`harness/checkpointer.py` 只读评估） | saver fork/恢复、pending writes、namespace 隔离、指针 CAS 可回读 | 后端 + 存储 |
| P0-003 | `conversation.py`、`api/routes/query.py`、`agents/runner.py`、新增执行状态表与 checkpoint attempt 适配 | 双指针 CAS、续约失败、检查后接管竞争、事件 CAS、取消/失败后恢复与写工具幂等 | 后端 + 存储 |
| P0-004 | 扩展 `src/attachments/coordinator.py`（已有 settle/reconcile，补 600 秒超时扫描与单所有者选主）、`src/attachments/worker.py`、turn API 与前端状态消费 | 完成/超时竞争、取消、旧事件、队头阻塞与后序推进、可见状态字段 | 后端 + 前端 |
| P0-005 | `src/workers/main.py`、`src/attachments/worker.py`、部署启动清单、已有 Memory/Export 入口 | role 路由、独立控制循环、协调选主/模块入口竞争、解析阻塞与故障接管 | 后端 + 运维 |
| P0-006 | 新增 `core/concurrency/resource_governor.py` 与配额 store，扩展 `core/llm_governor.py` / `model_gateway.py` 共享适配；客户端、任务创建、恢复工具 | 同机 LLM 总量/batch 保留、任务和资源队列分别有界、全部入口/SDK 重试、uncertain 恢复与审计、异步取消后不漏 permit | 后端 + 存储 |
| P0-007 | `agents/tools/document_rag_tool.py`、`agents/tools/runtime.py`、`observability/context.py` | 有界提交、取消、ContextVar、证据一致性、嵌套死锁 | 后端 + 测试 |
| P0-008 | `settings.py`、配置 API/启动校验、provider factory 测试 | 未实现 backend 拒绝保存/启动、sqlite/memory 兼容、provider 交付后迁移环境可启用 | 后端 + 测试 |
| P1-001 | 新增 Agent provider、`document_authoring/harness/checkpointer.py`、`pyproject.toml`、`uv.lock` | 锁版本安装、schema 初始化、saver 协议与恢复、fencing | 存储 + 后端 |
| P1-002 | `core/auth.py`、`core/conversation.py`、`memory/catalog.py` / `jobs.py` / `store.py` / `worker.py` | 同一 database/连接/事务故障注入、会话/Memory/outbox 原子切换、投影追平、权限与删除 | 存储 + 后端 |
| P1-003 | Authoring/Attachment/Export 领域 store、Artifact storage 适配、迁移与部署清单 | 最终停写同步、文件 hash、挂载故障、多节点接管与回滚 | 存储 + 运维 |
| P2-001 | `agents/orchestration/contracts.py` / `executor.py`、领域 adapters、持久化计划续执行 | 依赖边条件、v1 兼容、部分失败、稳定合并 | 后端 + 测试 |
| P2-002 | `observability/metrics.py` / `semantic.py`、`deploy/observability/`、评测/压测脚本 | 标签白名单、gauge 去重、固定到达率、过载恢复 | 测试 + 运维 |
| P3-001/002 | 可选 `agents/subagents/`、服务端工具装配与预算、离线评测报告 | 权限闭包、取消与额度、证据引用、盲测收益 | 后端 + 测试 |

P1-002 是会话/权限/Memory 权威组，P1-003 中与该组共享事务的表必须加入同一切换单元；不要机械地按目录拆库。P2-002 的指标增量随 P0/P1 PR 交付，编号不表示延后监控建设。

P0-006 按提交拆为接口/入口清单、共享配额与恢复工具、`llm_governor` 共享适配、客户端接入、任务准入与联合容量测试。恢复工具作为新增运维脚本交付（非恢复已移除的业务 CLI），提供 list/show/reconcile/release；至少演示重复 release、缺少证据拒绝、旧 revision 冲突和审计查询。P0a 后才能启用共享准入；设计与独立测试可提前进行。

### 13.6 P1 迁移拆分顺序

1. provider 与 repository 适配代码分 PR 合入，生产仍指向原存储。
2. Checkpoint/provider 兼容性与 attempt 指针恢复在测试副本验收。
3. 会话、权限、Memory 权威账本、jobs/outbox 原子组迁移并重建 Memory 投影。
4. Authoring/Attachment/Export 迁移按事务清单划组；checkpoint 指针及对应数据始终成对验证。
5. 配额存储切换时暂停准入并排空在途 permit，不能把活跃配额复制后两边同时接受请求。
6. 共享 Artifact/索引发布和全部权威状态通过后，增加 API/Worker 跨主机副本。

每个切换单元都复用 §7.5 的最终停写同步和校验流程；代码可拆 PR，不等于权威数据可以任意拆迁移事务。

## 14. Phase 5：可选 Reviewer / Research Agent（P3）

本阶段可通过“评测未证明收益，保持关闭”完成决策，不要求为了完成路线图强制上线两个 Agent。

### 14.1 Reviewer

先提供纯评测调用，读取冻结的文档 revision、证据列表和审查规则；结果绑定文档 hash，返回 `status`、`issues`、`evidence_refs`、`unsupported_claims`、`reviewer_version`。数值、格式和引用存在性优先确定性校验；模型只补充需要语义判断的结论。

Reviewer 不修改正式文件，不自行放行发布 Gate。调用使用现有权限上下文的子集、统一资源 governor、总 token/时间上限和取消信号。对需要审查的正式发布，超时/失败必须标记为未完成审查，不能默认为 pass；聊天可返回明确的未验证结果。独立审查结果不得污染权威 Memory。

### 14.2 Research

提供服务端受控入口 `research(goal, scope, evidence_refs)`，范围由鉴权结果冻结；工具能力仅取父请求的允许集合，禁止任意扩大知识库/附件访问。返回结构化 findings、可解析的 evidence refs、conflicts、missing information，不返回无限展开的内部过程。

默认只允许一次委派层级，禁止递归创建子 Agent。若使用 DeepAgents 的委派能力，仅启用白名单 specialist，保留 general-purpose subagent 关闭；对新增 `task` 工具的可见性重新做能力校验回归。

### 14.3 上线门槛

在同样本/同预算下和现有 Supervisor 比较质量、引用、完成率、延迟和 token 成本。Reviewer 检测 unsupported claims 的 precision/recall 分别报告；Research 以复杂多源任务完成率和证据正确性为主。Phase 0 数据集不足时扩展标注集再评估；收益不能超出统计不确定性则保持功能关闭。

## 15. 工件存放与交接

本文件仅交付实施计划，未运行其中的迁移脚本、压测或新增应用代码。实际执行时建议将报告按 `baseline_id`/任务号保存在 `docs/superpowers/implementation/production-hardening/`；脚本、测试和应用代码进入各自正常目录，不与报告混放。

当前仓库 `.gitignore` 忽略 `/docs/`。实施计划已保存到本地，但不会自动出现在普通 `git status` 中。进入团队评审时显式纳管选定文档，或上传到项目文档系统；不要为纳管本文件直接取消整个 docs 目录的忽略规则。

交接记录至少包含本文件版本、SHA-256、关联路线图版本及评审位置；只有发布该选定文档的版本后，才能将它作为多人实施的共同基线。本次文档完善不修改 `.gitignore`、不自动暂存/提交，也不上传外部系统。

各阶段交接提供：合入 commit、启用配置、数据库版本、可重放测试命令、验收报告、已知限制和恢复步骤。默认从 P0-001 开始；不会把本文中的默认值当作未经测试即可使用的生产参数。
