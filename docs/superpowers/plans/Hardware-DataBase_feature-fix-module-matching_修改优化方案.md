# Hardware-DataBase `feature/fix-module-matching` 修改与优化方案

> 版本：v1.8  
> 日期：2026-09-15  
> 目标分支：`feature/fix-module-matching`  
> 文档性质：独立架构路线图，不是该分支的执行计划  
> 范围：Supervisor Agent Harness、知识检索、文档生成、多用户高并发、状态持久化、观测评测与后续 Multi-Agent 演进

> 代码核对基线：v1.4 基于 `feature/fix-module-matching` commit `7fd340a`；v1.5 曾按“`01c807e` + 工作区未提交实现”记录；**v1.8 以合并后的干净基线 `01c807e`（2026-09-15 合并 PR #49 `feature/asset-center-and-llm-governance`，工作区无未提交实现）为唯一核对对象**；v1.7 已逐条复核代码坐标，v1.8 补齐接口契约、同机 LLM 共享额度和迁移边界。计划项与现状差异、待实施落点及与已合并子系统的适配见附录 A。

**文档定位**：本文与文件名中的分支名称不对应。`feature/fix-module-matching` 的实际目标是模块匹配修复；本文是跨 Phase 的生产化演进路线图，模块匹配算法修改需另列错误样例、根因、修改点和准确率回归集。本路线图落地时须按 Phase 拆分为可独立评审、可独立回滚的工程任务，不允许作为单次变更整体合入该分支。

v1.3 修订重点：修正 turn 队列与遗留 `POST /query` 的现状表述；将 `user_message_id` 降为历史回填依据并要求正式 `turn_sequence` 契约；补充架构图中的 Chat/Heavy Worker、目标共享状态和 API 过渡路径；区分已有与建议新增指标；明确多实例 governor、故障隔离目标状态和 PostgreSQL saver 兼容性验证。

v1.4 修订重点：将 turn 等待状态名修正为代码真实常量 `waiting_for_attachments`；为 Phase 2/3/5 补齐与优先级 P1/P2/P3 的映射；架构图补出遗留 `POST /query` 的汇入关系与待决状态；Phase 1a 增加 `turn_sequence` 分配与旧数据回填校验项。

v1.5 修订重点：同步 2026-09-15 代码基线（`01c807e` + 未提交 P0–P3 实现），新增附录 A 记录实施落点、PR #49 新增子系统（含双层 LLM 配额）、配置名映射与剩余差距；§3–§5、§10、§13–§15、§17、§18 保持原路线图表述不变，不标注“已实现”。

v1.6 修订重点：以合并后干净基线 `01c807e` 重新核对全文。将未落地实现统一改回“待实施提案”并移除工作区实现叙述；§4.4 明确 LLM 并发以已合并 `llm_governor` 为进程层唯一权威，本方案只新增部署级共享额度；§4.6/§14 复用已合并 `WORKER_TURN_CONCURRENCY`，取消重复配置名；§2.3/§7.3/§8/§10 补入 Wiki 回灌、附件对话式编辑、资产中心、多模态网关与 `model_gateway` usage ledger；§17 精确化遗留 `POST /query` 的治理现状；附录 A 重写为“合并基线对照与待实施落点”。

v1.7 修订重点：按 `01c807e` 逐条复核代码坐标后修正四类事实错误。附件/资产索引与解析状态实际在 `storage/chat_attachments.db`（`CHAT_ATTACHMENT_INDEX_DB_PATH`，`src/attachments/store.py:57`），`auth.db` 只存资产/Wiki/导出元数据与 `turn_attachments` 关联，§5.4/A.3/A.5 已更正；`waiting_for_attachments` 状态机与 `src/attachments/coordinator.py`（settle/reconcile）已存在，缺口收窄为队头阻塞、600 秒等待上限、可见状态字段与单所有者扫描（A.2）；评测 RAGAS judge LLM 已经 `govern_ragas_llm` 接入 `llm_governor` 的 batch 类，仅 `EVAL_EMBEDDING_*` 未治理（与实施方案 §6.2 对齐）；§4.4 与实施方案 §6.3 统一 P0b 多进程配额口径，§14/A.4 补充租约与等待上限配置名。

本文新增配置和目录均为建议契约，除明确标注“已有/已合并”的内容外，不表示当前代码已经支持。2026-09-15 合并后的现状对照、配置名映射与待实施落点统一见附录 A；§3–§5、§10、§13–§15、§17、§18 保持路线图表述，不标注“已实现”。

v1.8 修订重点：对齐《完整实施方案》v1.5，明确 P0b 同主机即需 LLM 共享配额，补齐入口准入、异步取消与恢复验收；迁移按事务依赖成组，保持会话与 Memory 权威账本原子性。模块匹配专项独立推进，不阻塞 Phase 0 采集；保留现有文件名以维护引用。本文件负责阶段目标，实施接口、切换单元与测试边界以完整实施方案对应章节为准，冲突需同步修订。

---

## 1. 执行摘要

当前分支已经具备较完整的 **Supervisor Agent Harness**：以 `deepagents.create_deep_agent` 为核心，通过一个主 Supervisor 统一进行任务理解、能力过滤、工具调用、证据整合和最终回答；文档生成则通过服务器侧计划、确认、后台 Worker 和 Artifact 管理形成受治理工作流。

因此，不建议把项目目标设为“全面 Multi-Agent”。更适合 Hardware-DataBase 的目标架构是：

> **Hybrid Supervisor Harness = 1 个主 Supervisor + 多个专业 Tool + 确定性 Workflow + 按需 Specialist Sub-Agent + 生产级并发治理**

推荐优先级：

1. **P0a：执行顺序与隔离**——先建立基线，解决同会话并发、Chat Worker 与长任务争用、取消与迟到写入语义。
2. **P0b：资源预算与背压**——LLM/RAG 全局预算、共享检索执行器、有界队列、入口覆盖与容量看板，在 P0a 验收通过后启动。
3. **P1：状态迁移与扩展准备**——明确权威状态和事务边界，迁移服务型存储并验证 API/Worker 多实例。
4. **P2：受控并行与队列治理**——按耗时数据增加 Action DAG 并行，统一任务治理接口和调度策略。
5. **P3：按需增加 Sub-Agent**——优先 Reviewer，其次 Research；不把 RAG/SQL/Circuit 等已有工具 Agent 化。

质量回归、容量指标和故障测试贯穿所有阶段，不作为 P2 之后才开始的独立工作。

---

## 2. 当前工程定位

### 2.1 当前不是“普通 RAG Chat”

当前真实形态更接近：

```text
User / API
   │
   ▼
Intent / Capability / Action Plan
   │
   ▼
DeepAgents Supervisor
   │
   ├── Document RAG
   ├── Spreadsheet Search / SQL
   ├── Circuit Search
   ├── Memory Search
   ├── Conversation / Attachment
   ├── Document Authoring
   └── Permission / Audit
   │
   ▼
Evidence / Result Aggregation
   │
   ▼
Response / Document Workflow
```

这已经属于成熟的 Supervisor Harness，而不是简单的“LLM + RAG”。

### 2.2 并发安全已经有基础

`runner.py` 使用 request-scoped `ToolRuntime` 和 `ContextVar` 保存每轮执行状态，避免进程级 Runner 在并发会话间共享本轮证据、token 使用量、检索摘要等运行态。

因此：

- 不同用户/请求的运行态隔离方向正确；
- 但这只解决**状态安全**，尚未解决**容量、资源争用与分布式扩展**。

新式 turn 接口提交后进入持久化队列，由 `HardwareWorker` 领取并调用 `_run_turn`；遗留 `POST /query` 仍在 API 进程内同步执行 Agent 与检索。文档任务、解析任务和聊天任务在 Worker 调度侧共享部分资源。因此，API 水平扩展本身不会直接增加队列聊天的执行容量，长时间运行的文档任务也可能推迟聊天任务。

### 2.3 当前 Tool 生态已经足够覆盖多数业务

已具备或已装配的能力包括：

- Document RAG
- document_search_batch
- Spreadsheet Search / SQL
- Circuit Search
- Memory Search
- Conversation Search
- Wiki Search（蒸馏页面回灌检索；2026-09-15 已合并接入 runner）
- Attachment（读/搜/表格/视觉/电路检索，及对话式编辑、候选发布、修订历史）
- Document Authoring
- Catalog / Pipeline
- Permission / Audit
- 资产中心（`assets` / `doc_assets`）、多模态网关、统一模型接入层（`model_gateway` + usage ledger）

因此普通问答、PDF/DOCX 查询、Excel 统计、BOM/电路分析、附件+知识库综合问答、模板分析、文档生成等场景，**不需要为了架构形式拆成多个 Agent**。

### 2.4 文档生成架构方向正确

当前文档生成遵循：

```text
Conversation
   ↓
Requirement
   ↓
Plan Proposal
   ↓
User Confirm / Gate 1
   ↓
Work Order
   ↓
Background Worker
   ↓
Artifact
   ↓
Validation / Release Gate 2
```

推荐继续坚持：

- Agent 负责理解、规划、建议；
- 服务端负责校验、状态与权限；
- Worker 负责正式生成；
- Artifact 可追溯、可修订、不可无审计覆盖；
- 发布保留人工 Gate。

### 2.5 可观测性已有底座，容量治理仍有缺口

`settings.py` 已包含：

- OpenTelemetry / OTLP
- Metrics / Traces / Logs
- Phoenix Project
- Grafana Base URL
- Worker Heartbeat
- Dependency Timeout

`src/observability/metrics.py` 还已有 chat/agent/LLM 时长与调用计数、queue depth/oldest age、worker、authoring、export、eval、memory 等指标。2026-09-15 合并后另有进程级 `llm_governor`（interactive/batch 保留容量、队列超时，active/queued/timeouts/max_wait_ms 快照经 `GET /system/status -> llm` 暴露）与 `model_gateway` usage ledger（分渠道/模型用量）。因此 Grafana / Phoenix 应继续作为现有底座，后续重点是补容量与治理指标、额度等待/取消/SLO/告警，且新增指标优先从 `llm_governor` 快照与 usage ledger 导出。配置存在不等于采集链路与告警已经通过生产验收。

---

## 3. 当前关键缺口

### 3.1 当前不是标准 Multi-Agent，但暂时无需全面改造

`runner.py` 明确禁用了 DeepAgents 的 general-purpose subagent，`task` 委派能力也随之移除。因此当前准确定位是：

> **Single Supervisor + Multi-Tool + Governed Workflow**

这是当前业务下的合理选择。

真正值得未来做 Sub-Agent 的场景只有：

- 深度跨源研究；
- 超长链路推理；
- 独立 Reviewer；
- 大型文档事实校验；
- 需要上下文隔离的复杂任务。

### 3.2 Chat Worker 与长任务共享调度资源

当前 Worker 会在一个循环中处理文档生成、计划提交、聊天 turn 和解析任务。文档任务有批次时间预算（`DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS`，默认 10s，且只在任务边界生效），已经开始的任务会继续执行；2026-09-15 合并后聊天 turn 已可按 `WORKER_TURN_CONCURRENCY`（默认 1）在同一 Worker 内线程池并发，但两类任务仍共享同一循环与进程资源，这不能替代交互任务与 Heavy Job 的资源隔离。P0a 应先明确 Worker 角色、执行池与聊天保留容量；队列优先级（§6.2）在 P2 统一处理，且不能替代正在执行任务的资源隔离。

### 3.3 Action DAG 有依赖语义，但 ready actions 仍串行

`ConversationActionExecutor` 能通过 `depends_on` 找到 ready action，但仍采用：

```python
for action in ready:
    self._execute_action(...)
```

即使多个 ready action 互不依赖，也会逐个执行。

推荐升级为：

```text
             ┌─ Action A ─┐
Start ───────┼─ Action B ─┼─→ Next Stage
             └─ Action C ─┘
```

但必须是 **bounded parallelism**，不能无限 fan-out。

### 3.4 `document_search_batch` 已有局部并行，但缺系统级并发预算

当前最多 4 路文档查询并发，并在每次调用中创建 `ThreadPoolExecutor`。当多个执行中的请求同时调用 batch 时，会出现并发乘法：

```text
1 个执行中 batch × 4 = 最多 4 路
20 个执行中 batch × 4 = 最多 80 路
50 个执行中 batch × 4 = 最多 200 路
100 个执行中 batch × 4 = 最多 400 路
```

这是同时执行的理论上限，不是在线用户数与下游并发的一一对应；当前实际并发还受 Worker 数量、入口和执行路径约束。风险包括：

- 线程数膨胀；
- RAGFlow 瞬时压力；
- 下游连接耗尽；
- latency 抖动；
- timeout → retry storm。

应升级为：

> **请求内并行 + 全局并发预算 + 共享执行器**

---

## 4. 并发与状态治理设计

4.1–4.2 的存量数据迁移属于 P1，其范围清单在 P0a 前确定。若上线目标立即包含跨主机部署，则状态迁移也是上线前置条件。

P0 拆分为两个可独立验收的批次：

- **P0a（顺序与隔离）**：4.3 同会话串行化、4.6 Worker 与 Heavy Job 隔离；目标是先消除状态错乱与调度争用。
- **P0b（容量与背压）**：4.4 资源预算、4.5 准入限流、共享检索执行器与容量看板；目标是在 P0a 稳定后建立可解释的部署级上限。

理由：P0a 修改正确性边界，P0b 修改容量边界。两类变更同时上线会混淆回归归因，一旦出现故障无法判断是顺序语义还是资源限制导致，也无法独立回滚。

### 4.1 Agent Checkpoint：SQLite → 可切换服务型存储

当前 Agent Checkpoint 默认使用 `SqliteSaver`，适合开发和单机低并发，但不适合多 API Worker / 多容器共享会话状态。

建议新增：

```text
src/agents/checkpoints/
├── base.py
├── factory.py
├── sqlite_provider.py
└── postgres_provider.py
```

配置建议：

```env
AGENT_CHECKPOINT_BACKEND=postgres
AGENT_CHECKPOINT_DATABASE_URL=...
```

开发及单机部署可继续使用 SQLite；多主机共享状态以 PostgreSQL 为目标。迁移前必须验证历史恢复、会话删除、版本兼容、连接池生命周期、权限隔离和回滚；仅新增 provider 不能证明多 Worker 执行安全。

Agent Checkpoint 与 `src/document_authoring/harness/checkpointer.py` 中的 Authoring Checkpoint 是两条路径。Authoring 已有 `FencedCheckpointer`，更换底层存储必须保留旧 fencing token 拒写约束。实施时复用官方 saver 能力，通过小型工厂装配，不重写 Checkpoint 协议。

实施依赖：服务型 Checkpoint 需要新增依赖（如目标 LangGraph 版本对应的 PostgreSQL saver 包及其驱动），当前 `pyproject.toml` 只锁定了 `langgraph-checkpoint-sqlite`。新依赖必须锁定版本、确认 saver API/schema 初始化方式、连接池生命周期和离线安装包，并随 PostgreSQL provider 一并交付。

同类缺口：Authoring Checkpoint 也存在“配置可接受、实现未支持”的问题——`settings.py` 的校验允许 `DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=postgres`，但 `build_checkpointer` 对 sqlite/memory 以外的值直接抛错。Agent 与 Authoring 两条路径必须在 P1 同时处理：要么补齐 provider，要么先把校验收紧为 sqlite/memory，禁止保留“配置成功但运行时报错”的中间态。

### 4.2 Memory Store 服务化

当前默认：

```text
MEMORY_STORE_BACKEND=sqlite
MEMORY_SINGLE_WRITER=true
MEMORY_JOB_MAX_CONCURRENCY=1
```

说明目前设计更偏向单机一致性。

建议：

- 短期保留 SQLite 兼容；
- 复用已有 `MemoryWorker`，继续在后台执行 extraction/reflection；
- 保留已有 job lease、generation、outbox 和同意状态校验；
- 中期增加 PostgreSQL 投影后端及权威账本迁移方案。

Memory 的 LangGraph Store 是可重建的检索投影。身份、授权、来源、生命周期、同意状态和幂等信息位于 `src/memory/catalog.py` 的 SQLite 权威账本；该账本支持与 `Conversation.complete_turn` 在同一事务中写入任务和 outbox。两层迁移必须分别定义，不能只切换 Store 后端。

当前 `MEMORY_STORE_BACKEND=postgres` 虽能通过配置值校验，`create_memory_store` 仍会明确报错，不能视作已实现 PostgreSQL 支持。

### 4.3 同一 conversation/thread 串行化

这是高并发下必须补的语义一致性机制。现有 `claim_turn` 原子领取的是单个 turn，尚未阻止其他 Worker 领取同一 session 的另一 turn。

原则：

```text
different thread_id → parallel
same thread_id      → serialized
```

建议新增：

```text
src/core/concurrency/
├── conversation_lock.py
└── keyed_lock.py
```

应在现有持久化 turn 队列上实现以下契约：

1. 顺序以服务端事务内分配的会话轮次序号为准，不用客户端时间或 UUID 字典序定义顺序。
2. 原子领取会话中最早的非终态 turn；前序等待附件时后序保持排队。前序完成、明确失败或取消后才推进；同一轮自动重试沿用原序号。
3. 会话执行所有权从领取、读取历史一直覆盖到 Checkpoint 和业务结果提交；排队前缓存的历史不得作为执行时权威历史。
4. lease 需续约；接管时推进 fencing token。Checkpoint、结果和可见事件提交都必须拒绝旧所有者的迟到写入。
5. 取消入口绕过会话执行队列以便及时通知运行者；已取消任务的后台计算即使迟到返回，也不能再提交有效结果。
6. 排队会带来有意的队头阻塞：前序 turn 处于 `waiting_for_attachments`（常量 `TURN_STATUS_WAITING_FOR_ATTACHMENTS`，如等待附件解析，现实可达分钟级）时，同会话后续 turn 一律不执行。必须为该状态定义等待上限与推进规则（超时后取消还是失败、是否自动推进后序），并在会话列表与 SSE 中暴露“前序未完成、当前排队”的明确状态，避免被误判为卡死。
7. 服务端轮次序号在创建 turn 的同一事务内分配。现有 `chat_turns.user_message_id`（`NOT NULL`）只能作为历史数据回填与排序校验依据；它是消息全局 ID，不应直接当作正式的会话轮次契约。正式实现应增加或明确服务端分配的 `turn_sequence`，旧数据迁移时用 `user_message_id` 校验其一致性，不接受客户端序号。`chat_turns` 表在 2026-09-15 合并后已包含 `wiki_assist` 列，`turn_sequence` 的建表、INSERT 与迁移脚本必须与该列并存。

单进程 keyed lock 可作局部优化，多进程一致性必须由共享状态协调。使用 PostgreSQL advisory lock 时应明确会话级/事务级生命周期及连接池归还规则；使用 Redis lease 时应明确续约、所有者校验和 fencing。互斥、排队顺序和迟到写入防护是三个不同要求。

### 4.4 LLM / RAG 全局 Resource Governor

**职责划分（v1.6 定稿）**：LLM 并发只保留两个权威层级，不允许对同一次调用叠加第三层 semaphore。

- **进程层（已有，直接复用）**：`src/core/llm_governor.py` 已提供进程级 LLM 准入——`interactive` 允许全部额度、`batch` 单独上限，带队列等待告警与超时（`LLM_MAX_CONCURRENCY` / `LLM_BATCH_MAX_CONCURRENCY` / `LLM_QUEUE_TIMEOUT_SECONDS` / `LLM_QUEUE_WARN_SECONDS`），`model_gateway` 的所有模型调用入口（API 与 Worker）都会经过它。本方案不新增 `AGENT_MAX_CONCURRENT_LLM_CALLS` 之类的第二进程级 LLM 上限。
- **部署层（待实施）**：新增 `src/core/concurrency/resource_governor.py` 接入 RAG/TOOL/CHAT/HEAVY 等共享额度；LLM 由 `llm_governor` 扩展共享 limiter，P0b 同主机多进程即必须接入 SQLite 配额库，P1 换为 PostgreSQL。进程限制和部署限制分别计量，一次调用只申请一次对应作用域 permit，不另增 LLM 包装器重复扣减。

配置示例（LLM 额度复用已有名；其余为新增建议）：

```env
AGENT_MAX_CONCURRENT_RUNS=16
RAG_MAX_CONCURRENT_CALLS=8
TOOL_MAX_CONCURRENT_CALLS=16
```

具体数字必须通过压测确定，不应直接照抄为生产值。

预算必须定义作用域：`process` 仅限制本进程；`deployment` 才是跨 API、Chat Worker、Authoring、Memory、Evaluation 的共享额度。每进程 8 路、4 个进程可能形成 32 路，不能命名为“全局 8 路”并据此验收。LLM 的进程层与部署层必须一起纳入验收口径，避免出现未文档化的第三个有效并发上限。

入口覆盖必须显式包含两类执行位置：Worker 进程（持久化 turn、文档/解析/导出任务）与 **API 进程**。遗留 `POST /query` 的 LLM 调用已由进程级 `llm_governor` 覆盖（`model_gateway` 全入口接入），但它在 API 进程内的 RAG/tool 调用没有预算、也不走 Worker。P0b 必须为 API 进程的 RAG/tool 调用提供与 Worker 同源的部署级额度，并明确该入口的处置：保留并接入同一预算，或下线/重定向到 turns 模型。

P0b 单实例开发可使用本地 governor（`RESOURCE_GOVERNOR_SCOPE=process`）；同机多进程生产必须使用部署级共享配额存储（`RESOURCE_GOVERNOR_SCOPE=deployment`，P0b 映射到同主机共享 SQLite 配额库，P1 跨主机换 PostgreSQL，细节见实施方案 §6.3）。所有进程额度之和不得超过部署预算，不允许以“静态分配”替代集中协调；动态扩容前必须具备配额重新分配或集中协调机制。

资源获取放在实际下游调用边界，覆盖所有入口，并约定：

- 同时限制在途调用和等待队列；超时或取消的排队项立即退出。
- LLM 额度持有到流关闭；Future 超时/取消不能证明远端停止，未知结束状态的部署 permit 按实施方案 §6.3.1 隔离。P0b 对 LLM 与 RAG/tool 均验收真实调用和 permit 生命周期，含异步准入被取消后的迟到 permit 回收。
- 每次实际重试重新申请额度，退避期不占执行槽；重试有次数上限、抖动和总 deadline。
- LLM 并发限制与 RPM/TPM 配额分别管理；互动任务保留容量，后台任务不能占满共享池。
- 明确多资源获取顺序，避免父任务占满同一线程池后等待子任务的嵌套死锁。

### 4.5 Admission Control / Rate Limit

至少按以下维度限流：

- per user
- per tenant
- per model
- per resource type

并区分：

- 普通 chat
- deep research
- document generation
- export
- OCR

高成本任务应该有更严格配额，并在饱和时明确返回 `429` / `Retry-After`。

配额超限或队列已满应在接受任务前返回；任务一旦持久化接受，应通过任务状态报告排队、超时和失败，不能在已开始的 SSE 中途改写 HTTP 状态码。额度服务不可用时使用预先定义的有限降级预算或拒绝策略，不能无限放行。

执行默认采用完整实施方案的拒绝/有界等待策略，不新增未验证的降级额度。资源等待队列与持久化 turn/job 队列分别限容；后者在任务创建事务中按部署/租户/用户检查，并保留幂等请求返回原任务的语义。评测 LLM 已接入 `govern_ragas_llm`，但其装配失败的 fail-open 行为需显式检测；生产共享账号不得绕过治理，隔离开发评测可标记 `governance_bypassed`，不计入生产容量验收。

### 4.6 Chat Worker 与 Heavy Job 隔离

复用现有任务存储，先为聊天与文档/解析设置独立消费角色或独立执行池，并为聊天保留容量。2026-09-15 合并后进程内聊天 turn 并发由 `WORKER_TURN_CONCURRENCY`（默认 1）控制，本项直接复用该配置作为 chat 池大小，不再新增 `CHAT_WORKER_MAX_CONCURRENCY`；仍需补的是 Heavy Job（authoring/parse）与 chat 的执行池或角色隔离。仅调整轮询顺序不能中断正在执行的长任务；统一队列是否必要由后续运维需求决定。

Worker 扩并发前核对共享 `AppPipeline`、客户端和可变缓存的并发契约；仅 `ToolRuntime` 使用请求作用域不能证明整个 pipeline 可安全并发。取消、租约续约和控制操作必须具备不被长任务耗尽的执行通道。

验收时在文档、OCR、导出和 Memory 持续运行的混合负载下，分别测量 chat queue wait、heavy-job queue wait、活跃 Worker 与下游配额，确认在线聊天仍满足 SLO。

---

## 5. 吞吐优化与水平扩展前提

共享检索执行器与背压在 P0b 完成；服务型状态迁移与多实例验收属于 P1；Action DAG 并行在 P2 按实际耗时收益实施。

### 5.1 Action DAG Bounded Parallel

修改 `src/agents/orchestration/executor.py`：

- 按依赖层级计算 ready actions；
- 同层只读、无资源冲突 Action 可并行；
- 设置 `ACTION_EXECUTOR_MAX_PARALLEL`；
- 写操作保持串行或按资源键互斥。

建议扩展 ActionSpec：

```text
access_mode = READ_ONLY | WRITE
cost_class = NORMAL | HEAVY
resource_key
```

读写属性与资源成本是独立维度，耗时任务也可以是只读任务。上述字段须由服务端依据动作和实际目标解析或校验，不能直接相信模型提供的值。资源键包含租户/目标身份；未知动作默认串行，同资源写写、读写冲突均需要排序或明确快照读取语义。

现有 `_SUCCESS_STATUSES` 包含 `accepted` 和 `completed`。并行前必须扩展依赖契约，区分“依赖提交成功”和“依赖结果完成”：`accepted` 仅满足前者，依赖后台生成结果的动作须等待任务完成或进入持久化续执行流程。

每批执行使用只读的前序 receipt 快照；子动作返回独立结果，由协调线程按稳定 action 顺序合并。保留能力校验、禁止动作和未解决引用拦截，并覆盖部分失败、取消、证据去重与引用编号一致性。不能只把 `for action in ready` 改成线程池提交。

### 5.2 Shared Retrieval Executor

当前 `document_search_batch` 每次创建线程池。建议改为进程级共享 executor：

```text
Bounded Retrieval Queue → Shared RetrievalExecutor → RAG Resource Governor
```

共享执行器控制本进程线程数量，部署级 RAG governor 控制下游总在途调用，二者不可互相替代。必须同时约定：

- 等待队列上限、每请求 fanout 上限、队列等待 deadline 和满载处理。
- 每次提交使用独立上下文副本；复用已有 `submit_with_current_context`，保留 Python ContextVar 和 OTel 父 Trace。
- 取消时移除未运行的子任务，通知执行中调用；不得关闭其他请求共用的 executor。
- Future 取消不能终止已运行线程；通过底层超时/取消信号结束调用，并保持资源额度与真实在途状态一致。
- 请求结束后迟到结果不得继续写入已结束的 ToolRuntime；工具证据登记与预算仍使用现有同步机制。
- executor 在进程启动时创建、退出时统一关闭；测试时允许注入有界执行器和假后端。

### 5.3 连接池

现有 `RAGFlowClient` 的同步路径已经复用持久化 `httpx.Client`，不应重复实现。异步可取消路径会创建临时 `AsyncClient`，需结合事件循环生命周期评估复用方式。其余客户端先盘点，再统一管理：

- PostgreSQL pool
- RAGFlow HTTP client
- LLM HTTP client
- 其他内部服务客户端

明确连接数、pool wait timeout、重载/关闭时机和实例归属；总连接预算应计入所有 API/Worker 副本。连接池容量、线程池容量和资源额度需要配套压测。

### 5.4 API 与 Worker 水平扩展

完成下述状态边界迁移、资源配额和执行所有权验收后，再扩展 API 和 Worker。API 负责接收、鉴权、持久化请求和订阅结果；Worker 执行容量需要独立扩展。

| 状态 | 当前代码/存储边界 | 多主机前置要求 |
|---|---|---|
| 用户、权限、会话、消息、turn、事件 | `auth.py` / `conversation.py`，`AUTH_DB_PATH` SQLite | 共享权威存储；保留归属校验、轮次顺序、事件恢复与删除语义 |
| Agent Checkpoint | `runner.py`，独立 SQLite saver | PostgreSQL provider、历史/删除兼容及执行所有权校验 |
| Memory 权威账本与 jobs/outbox | `memory/catalog.py` / `jobs.py`；与会话完成事务关联 | 保留 `complete_turn` 与任务/outbox 原子性，或先设计事务 outbox 替代路径 |
| Memory 检索投影 | `memory/store.py`，可重建 SQLite Store | PostgreSQL 投影后端、重建与校验，不能代替权威账本迁移 |
| Authoring 任务、计划、Work Order、Checkpoint | 多个 domain store 与 `FencedCheckpointer` | 逐一迁移权威状态，保留 lease/fencing、幂等、Gate 和恢复语义 |
| 附件、导出、Artifact、资产中心 | 元数据 store 与本地文件路径；资产/Wiki/导出/会话元数据与 `turn_attachments` 关联在 `auth.db`，附件/资产索引与解析状态在 `storage/chat_attachments.db`（`CHAT_ATTACHMENT_INDEX_DB_PATH`），附件文件在 `storage/chat_attachments/` | 共享对象/文件存储、跨节点下载、权限和清理一致性 |
| Wiki 页面与蒸馏结果 | `src/core/wiki.py`（SQLite，co-located 于 `auth.db`） | 纳入共享权威存储迁移范围，保留修订与回灌链路 |
| 其他本地索引、pipeline/circuit/spreadsheet 状态、配置缓存 | 各 domain store、索引和进程缓存 | 实施前补齐资产清单，标明权威/可重建属性及刷新方式 |

该表是迁移的最低范围。每项迁移必须交付源/目标映射、数据校验、切换步骤、备份和回滚说明；不得把“全部 SQLite 文件放到共享目录”直接等同于完成多主机数据库服务化。

数据切换按完整实施方案 §7.4 的事务组进行：会话/权限/turn 与 Memory ledger/jobs/outbox 共同切换，领域剩余状态按事务清单细分，文件在跨主机写入前完成共享，可重建投影随后追平。代码可分 PR，不等于原子数据可拆切换。未实现的 PostgreSQL provider 由 P0-008 补配置保存/启动防护；provider 完成后先在迁移环境验证，再放开生产切换。

迁移先在备份副本演练，再停写排空/转移在途任务、导入并校验、切换和灰度。恢复历史、会话删除、权限隔离、Artifact 可见性与重启接管通过后才扩大流量。目标库已接收新写入时，回滚必须有反向同步/重放或明确的恢复点，禁止直接切回旧库丢弃新状态。

```text
Load Balancer
    │
 ┌──┼──┐
 ▼  ▼  ▼
API API API
 └──┼──┘
    │
Shared Services
```

API 节点尽量 Stateless / Soft-state。增加 API 副本与增加 Worker 吞吐分别验收，不能以 HTTP 接收成功率代替任务完成吞吐。

---

## 6. P2 任务治理接口与调度优化

### 6.1 统一 Heavy Job 治理契约

聊天 turn、Document Authoring、Memory 和 Export 已有持久化任务/Worker 基础。优先统一以下能力的治理接口和观测维度，是否合并底层队列按需要另行决定：

- Document Generation
- OCR
- Large PDF Parse
- Export
- Memory Reflection
- Wiki 蒸馏（批量 LLM，走 `llm_governor` 的 batch 类）
- 大表格处理
- Evaluation

统一流程：

```text
API → enqueue → task_id → Worker → Artifact / Result
```

保留各领域的授权和结果提交边界；异步 API 返回成功仅表示任务已接受。P0a 的执行资源隔离不依赖此处完成通用队列抽象。§13 中的 `src/jobs/` 目录仅为远期可选项：是否落地取决于各领域队列的运维需求，不能因为规划了统一目录就预先合并存储与租约实现。

### 6.2 Queue Priority

推荐：

```text
P0 interactive chat
P1 user-confirmed document job
P2 export / OCR
P3 memory / evaluation
```

以上是队列业务优先级，不是本方案实施优先级。通过独立消费角色、在线保留容量和后台最小配额实现，避免单纯严格优先级导致后台永久饥饿；优先级不能替代对正在执行任务的资源隔离。

### 6.3 Retry + Idempotency

现有文档任务已有幂等、lease、递增 token、资源锁、退避和死信；Memory 有 generation/lease 和 outbox，Export 发布也检查 lease token。统一契约必须保留这些能力，不能降级成仅有重试计数的通用队列。最低语义字段为：

```text
job_id
idempotency_key
attempt
max_attempts
lease_owner
lease_expires_at
lease_token / fencing_token
generation（适用时）
available_at / next_retry_at
status / cancellation state
```

字段可映射现有命名，不为统一名字单独迁表。按至少一次执行设计：幂等键包含租户/业务操作及参数指纹，重复键不同输入应报冲突；所有提交检查有效 lease/token，取消或接管后的旧 Worker 不得发布结果。重试区分可重试错误与权限/校验失败，设置指数退避、抖动、最大次数和总 deadline。

Artifact 写入采用临时对象与受校验的发布记录，明确崩溃后孤儿对象清理；Work Order、Artifact、任务状态和 outbox 的提交关系需要原子性或可恢复协议。Worker crash 后恢复测试必须包括“外部操作已完成，但状态尚未落库”的窗口。

---

## 7. P3：按需 Multi-Agent，而不是全面拆分

### 7.1 Reviewer Agent（优先）

推荐优先于 Research Agent。

职责：

```text
Document / Analysis Result
          ↓
Reviewer Agent
          ↓
Citation Coverage
Numeric Consistency
BOM ↔ Circuit Consistency
Datasheet Verification
Unsupported Claims
Risk Findings
```

推荐返回严格结构：

```json
{
  "status": "pass | warn | fail",
  "issues": [],
  "unsupported_claims": [],
  "evidence_conflicts": [],
  "recommendations": []
}
```

适用于：设计报告、ICD、模板文档、高风险分析结论。

### 7.2 Research Agent（按需）

只用于复杂、多源、长链路研究：

```text
Supervisor
   ↓
research_agent(goal, scope)
   ↓
独立上下文内多轮检索 / 比较 / 冲突消解
   ↓
Compact Research Result
```

返回：

```json
{
  "findings": [],
  "evidence_refs": [],
  "conflicts": [],
  "missing_information": []
}
```

核心收益是 **Context Isolation**，而不是“Agent 越多越智能”。

### 7.3 不建议 Agent 化的能力

以下继续做 Tool：

- RAG
- SQL
- Spreadsheet
- Circuit Search
- Memory Search
- Wiki Search
- Attachment（含对话式编辑/发布/修订）
- Conversation Search
- Permission / Catalog

判断原则：

> 需要一个函数 → Tool；需要独立持续推理上下文 → Agent。

---

## 8. 推荐目标架构

```text
                    Load Balancer / API Gateway
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
               API-1                       API-N
                 │                           │
                 └─────────────┬─────────────┘
                               │
                     Admission Control
                               │
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
       turn/task persistence          legacy POST /query
                 │                       (sync, transitional;
                 │                        disposition: P0b)
                 │                              ┆
                 ▼                              ┆
       Chat Worker Pool / Heavy Worker Pool     ┆
                 │                              ┆
                 ▼                              ┆
             Conversation Lock                  ┆
                 │                              ┆
                 ▼                              ┆
       Supervisor Agent Harness ◄───────────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
        Domain Tools      Research Agent    Reviewer Agent
                              optional          optional
              │
      ┌───────┼────────┬────────┐
      ▼       ▼        ▼        ▼
     RAG     SQL     Circuit   Memory/Wiki
              │
              ▼
         Resource Governor
       ┌──────┼──────┐
       ▼      ▼      ▼
    LLM Pool RAG Pool Heavy Job Pool
      │      │      │
      ▼      ▼      ▼
   Model   RAGFlow  Worker Queue
                    ├─ Authoring
                    ├─ OCR
                    └─ Export

              Shared State (target)
      Session / Auth / Checkpoint / Job State
       Memory Ledger + Projection / Artifact

                   │
                   ▼
          OpenTelemetry Collector
             ┌─────┴─────┐
             ▼           ▼
          Grafana      Phoenix
```

> 图中 `legacy POST /query` 是过渡入口：当前绕过 Worker Pool 与 Conversation Lock 直接进入 Supervisor/检索；其 LLM 调用已过进程级 `llm_governor`，RAG/tool 预算与去留（接入部署级 governor 或下线）由 P0b 决定，图中以虚线 `┆` 表示待决路径。
>
> LLM 并发为两层：进程层复用已合并的 `llm_governor`（interactive/batch 保留容量），部署层由 Resource Governor 管理跨进程共享额度；不得对同一次调用叠加第三层 semaphore。资产中心/多模态网关按 Tool 层接入，不单独成为 Agent。

---

## 9. 文档生成专项优化

继续保持服务端受治理流程，不建议让 Authoring Agent 直接修改正式文件。

推荐增强为：

```text
Conversation
    ↓
Requirement
    ↓
Plan Proposal
    ↓
User Confirm
    ↓
Document Planner
    ↓
Section DAG
    ↓
Parallel Retrieval
    ↓
Section Draft
    ↓
Document Merge
    ↓
Reviewer
    ↓
Artifact
    ↓
Release Gate
```

职责边界：

- Agent：理解 / 规划 / 分析 / 审查；
- Workflow：状态 / 执行 / Gate / Artifact / Retry；
- Worker：长时间计算与文件生成。

---

## 10. 可观测性增强

现有 Grafana + Phoenix + OTel 保留，重点新增容量和治理指标。

### 10.1 Agent Runtime

已有指标使用 `hdb.*` OTel 名称（如 `hdb.agent.runs`、`hdb.agent.duration`、`hdb.chat.turns`、`hdb.chat.queue.wait`）。本节以下指标分为“建议新增的逻辑维度”，实施时必须映射到实际 instrument 名称，避免与已有指标重复：

```text
agent_inflight_requests
agent_queue_depth
agent_step_count
agent_tool_call_count
agent_token_usage
agent_context_tokens
agent_cancel_count
```

### 10.2 LLM

```text
llm_active_requests
llm_queue_depth
llm_queue_wait_ms
llm_timeout_total
llm_retry_total
llm_tokens_per_minute
```

LLM 调用时长已有 `hdb.llm.duration`；进程级 `llm_governor` 已提供 active/queued/timeouts/max_wait_ms 快照（`GET /system/status -> llm`），`model_gateway` usage ledger 提供分渠道/模型用量。新增 active、queue、timeout、retry 和 token 指标应优先从这两处现有状态导出，避免第三套口径，并明确单位与低基数标签。

### 10.3 RAG

```text
rag_active_requests
rag_queue_wait_ms
rag_query_latency_ms
rag_batch_fanout
rag_zero_hit_rate
rag_retry_rate
```

### 10.4 Storage / Conversation

```text
checkpoint_read_latency
checkpoint_write_latency
checkpoint_lock_wait
memory_write_latency
db_pool_active
db_pool_wait
conversation_lock_wait_ms
same_thread_concurrent_requests
```

### 10.5 Worker

```text
job_queue_depth
job_wait_time
job_execution_time
job_retry_count
job_dead_letter_count
worker_active
worker_stale
```

---

## 11. 评测体系

### Retrieval Eval

- Recall / Precision
- MRR / NDCG
- Zero-hit Rate
- Source Routing Accuracy

### Answer Eval

- Groundedness / Faithfulness
- Answer Correctness
- Citation Coverage
- Unsupported Claim Rate

### Agent Eval

- Tool Routing Accuracy
- Tool Call Correctness
- Step Efficiency
- Repeated Tool Call Rate
- Task Completion Rate
- Action Plan Validity

### Document Eval

- Template Compliance
- Section Completeness
- Evidence Coverage
- Numeric Consistency
- Cross-source Consistency
- Unsupported Claims
- Release Readiness

---

## 12. 高并发压测方案

### Stage 1：阶梯并发

```text
1 → 5 → 10 → 20 → 50 并发用户
```

记录：P50 / P95 / P99、错误率、timeout、token throughput、RAG latency、checkpoint latency、queue wait。

### Stage 2：混合负载

建议初始压测模型：

```text
60% ordinary QA
20% multi-source search
10% SQL / spreadsheet
5% document authoring
5% export / OCR
```

### Stage 3：故障注入

至少覆盖：

- LLM timeout
- RAGFlow timeout
- DB connection exhaustion
- Worker crash
- Agent cancellation
- same-conversation concurrent requests
- large attachment
- document retry

---

## 13. 推荐代码结构

```text
src/
├── agents/
│   ├── runner.py
│   ├── checkpoints/
│   │   ├── base.py
│   │   ├── factory.py
│   │   ├── sqlite_provider.py
│   │   └── postgres_provider.py
│   ├── subagents/
│   │   ├── research_agent.py
│   │   └── reviewer_agent.py
│   └── orchestration/
│       ├── executor.py
│       ├── parallel_executor.py
│       └── contracts.py
├── core/
│   └── concurrency/
│       ├── conversation_lock.py
│       ├── rate_limiter.py
│       └── resource_governor.py
├── jobs/
│   ├── queue.py
│   ├── scheduler.py
│   └── worker.py
└── observability/
    ├── metrics.py
    ├── tracing.py
    └── capacity_metrics.py
```

> `src/jobs/` 为远期可选项：仅当 §6.1 决定合并底层队列时才创建。P0/P1 实施不得以该目录存在为前提，也不要为对齐目录迁移现有领域队列的存储与租约实现。

---

## 14. 建议新增配置

```env
# Checkpoint
AGENT_CHECKPOINT_BACKEND=postgres
AGENT_CHECKPOINT_DATABASE_URL=

# Conversation
CONVERSATION_SERIALIZE_SAME_THREAD=true
CONVERSATION_LOCK_TIMEOUT_SECONDS=120
CHAT_TURN_LEASE_SECONDS=120
CHAT_TURN_LEASE_RENEW_SECONDS=15
WAITING_TURN_MAX_SECONDS=600

# Resource limits（LLM 进程级额度复用已合并 llm_governor，不新增同层配置）
AGENT_MAX_CONCURRENT_RUNS=16
RAG_MAX_CONCURRENT_CALLS=8
TOOL_MAX_CONCURRENT_CALLS=16
ACTION_EXECUTOR_MAX_PARALLEL=4

# Worker isolation and queue backpressure
# 聊天 turn 进程内并发复用已合并的 WORKER_TURN_CONCURRENCY（默认 1），不再新增 CHAT_WORKER_MAX_CONCURRENCY
# 单实例开发示例；同机多进程生产必须使用 deployment（共享 SQLite 配额库，见实施方案 §6.3）
RESOURCE_GOVERNOR_SCOPE=process
RESOURCE_QUEUE_MAX_WAIT_SECONDS=30
RESOURCE_QUEUE_MAX_DEPTH=256

# Heavy jobs
HEAVY_JOB_MAX_CONCURRENCY=4
DOCUMENT_JOB_MAX_CONCURRENCY=2
OCR_JOB_MAX_CONCURRENCY=1

# Optional subagents
RESEARCH_AGENT_ENABLED=false
REVIEWER_AGENT_ENABLED=false

# Observability
OBS_CAPACITY_METRICS_ENABLED=true
```

> 已在 2026-09-15 合并基线中、无需重复定义的配置：`LLM_MAX_CONCURRENCY` / `LLM_BATCH_MAX_CONCURRENCY` / `LLM_QUEUE_TIMEOUT_SECONDS` / `LLM_QUEUE_WARN_SECONDS`、`WORKER_TURN_CONCURRENCY`、`DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS`、`AGENT_CHECKPOINT_DB_PATH`、`DOCUMENT_AUTHORING_CHECKPOINTER_*`、`MEMORY_*`、`OTEL_*` / `OBS_*`。
>
> 以上并发值仅作为配置形式示例，生产值必须通过真实服务器压测确定。

---

## 15. 实施顺序

### Phase 0：基线与边界

- [ ] 在合并基线 `01c807e` 上重跑全量测试并冻结回归基线（记录既有失败，不假设全绿）
- [ ] 建立检索、回答、引用、文档和 Action routing 固定回归集
- [ ] 建立 SQLite/文件状态清单，标出权威状态、可重建投影和事务关联（含资产中心/Wiki、`auth.db` 的 `turn_attachments` 关联、`storage/chat_attachments.db` 附件/资产索引与附件文件目录）
- [ ] 记录 Worker 角色、队列深度、并发槽位和共享客户端（含已合并 `llm_governor` 与 `WORKER_TURN_CONCURRENCY`）
- [ ] 固定压测口径、混合负载和初始 SLO

### Phase 1a：顺序与隔离（P0a）

- [ ] Same-thread conversation serialization
- [ ] Turn 顺序领取、lease 续约和 fencing/迟到写入拒绝
- [ ] 服务端 `turn_sequence` 分配与旧数据 `user_message_id` 回填校验
- [ ] `waiting_for_attachments` 队头阻塞的等待上限、推进规则与可见状态
- [ ] Chat Worker 与 Heavy Job 执行池/保留容量隔离

### Phase 1b：容量与背压（P0b，P0a 验收通过后启动）

- [ ] 入口盘点与口径确认：Worker 进程 + API 进程（含遗留 `POST /query` 的去留决策）
- [ ] LLM 配额归一：进程层复用 `llm_governor`，仅新增部署级共享额度/协调，不新增同层上限
- [ ] RAG / tool 部署级 concurrency limit
- [ ] Shared retrieval executor
- [ ] 有界检索队列、取消、超时、重试和上下文传播
- [ ] Capacity dashboard 与 queue/backpressure 告警

### Phase 2：状态迁移与扩展（P1）

- [ ] Checkpoint backend abstraction and PostgreSQL support
- [ ] 新依赖锁定与离线包（`langgraph-checkpoint-postgres` / 驱动）
- [ ] Authoring Checkpointer provider 补齐或校验收紧（消除 postgres 配置假实现）
- [ ] PostgreSQL memory backend
- [ ] Memory 权威账本迁移与事务 outbox 方案
- [ ] 会话/权限/消息/turn 权威存储迁移（按存储域拆分，先只读影子校验再切写）
- [ ] Authoring、附件、Artifact 和本地索引共享化方案
- [ ] API horizontal replicas
- [ ] DB / HTTP connection pool
- [ ] 多 API、多 Chat Worker 的接管、删除和回滚演练

### Phase 3：按数据实施吞吐治理（P2）

- [ ] Action DAG bounded parallel
- [ ] Action access_mode/cost_class/resource_key 服务端契约
- [ ] accepted/completed 依赖语义与部分失败处理
- [ ] Queue priority、retry、idempotency 和 dead-letter 统一观测

### Phase 4：质量与回归（贯穿前述阶段）

- [ ] Agent routing eval
- [ ] Citation coverage eval
- [ ] Document eval
- [ ] Phoenix trace dimension refinement
- [ ] Error dataset / regression suite

### Phase 5：按数据决定（P3）

- [ ] Reviewer Agent
- [ ] Research Agent

只有当 Phoenix / Eval 明确显示上下文膨胀、复杂研究效果下降、文档事实错误较高或需要独立审查时，再启用 Sub-Agent。每阶段先与 Phase 0 基线比较，再扩大并发。

---

## 16. 验收标准

### 并发安全

- 不同用户互不串状态；
- 不同 conversation 可并行；
- 同 conversation 请求按服务端轮次顺序执行；
- 前一轮等待附件、失败或取消时，后一轮行为符合已定义的推进规则；
- lease 接管后旧 Worker 的 Checkpoint、事件、Artifact 和业务写入均被拒绝；
- 取消请求不影响其他用户。

### 服务容量

建立明确 SLO，并在压测前冻结阈值，例如：

```text
普通问答 P95 ≤ 目标值
聊天 Queue wait P95 ≤ 目标值
Heavy Job Queue wait P95 ≤ 目标值
错误率 ≤ 目标值
LLM timeout ≤ 目标值
RAG 429/timeout ≤ 目标值
```

目标值必须记录部署规模、模型、下游版本和负载配比；压测后记录实测值、容量拐点和降级策略，不保留未定义的 `X` 作为发布标准。

### 故障隔离

必须满足：

以下目标仅在 P0a 完成 Chat Worker 与 Heavy Worker 隔离后适用；当前共享 Worker 进程崩溃时不能声称已满足：

```text
RAGFlow 故障 ≠ API 全部不可用
Document Worker 故障 ≠ Chat 不可用
Memory Worker 故障 ≠ 主问答链路失败
```

### Agent 质量

以下阈值全部来自 Phase 0 固定回归集（同一数据集、同一判分脚本、同一模型配置），不接受“人工观察”作为通过依据：

- 重复 Tool Call 率不超过冻结基线 + 回归容差；
- 无单请求步数超过 `AGENT_MAX_RETRIEVAL_ROUNDS` 对应的 recursion budget，即不出现无界循环；
- Tool routing 准确率不低于冻结基线；
- Evidence 引用覆盖率不低于冻结基线；
- Unsupported Claim 检测指标不低于冻结基线。

### 文档生成

- 用户确认后才生成；
- Artifact 可追溯；
- Revision 不覆盖原文件；
- Review 有结构化结果；
- 发布需要 Gate；
- 失败任务可恢复。

### 存储迁移

- 历史 Checkpoint 可恢复，删除和权限边界保持一致；
- Memory 权威账本与可重建投影均完成校验；
- 迁移失败可回滚，切换期间不丢失已接受任务、事件或 Artifact；
- 多实例接管不会产生重复 Work Order、Artifact 或有效迟到写入。

### 运行时治理

- 队列满载会背压或明确拒绝；
- 取消排队项和执行中调用的行为可观察；
- 同一请求的工具调用、Trace、证据和预算在并行子任务中保持一致；
- 资源额度、连接池和 Worker 槽位的总量有可解释的部署级上限。

---

## 17. 与当前工程的核心映射

| 当前实现 | 判断 | 建议 |
|---|---|---|
| DeepAgents Supervisor | 保留 | 继续作为主控制平面 |
| Domain Tools | 保留 | 不拆 Agent |
| Document Workflow | 强项 | 加强 Worker / Review |
| Grafana + Phoenix | 强项 | 补容量治理指标 |
| `llm_governor`（2026-09-15 合并） | 进程级 LLM 准入（interactive/batch、队列超时） | 作为进程层唯一权威复用；部署层协调优先扩展其共享 limiter |
| `WORKER_TURN_CONCURRENCY`（2026-09-15 合并） | 聊天 turn 进程内并发池 | 复用为 chat 池尺寸，不新增 `CHAT_WORKER_MAX_CONCURRENCY` |
| Wiki / 资产中心 / 多模态网关（2026-09-15 合并） | 新工具与新子系统 | 按 Tool 接入，纳入状态清单、工具面与容量指标 |
| ContextVar ToolRuntime | 正确 | 保留 |
| SQLite Agent Checkpoint | 单机可用 | 生产迁移 PostgreSQL |
| SQLite Memory | 权威账本与可重建投影分层；PostgreSQL 配置尚未实现 | 先定义两层迁移和事务 outbox，再迁移投影 |
| Authoring Checkpoint | `FencedCheckpointer` 已有；postgres 配置校验通过但 builder 未实现 | P1 补齐 provider 或收紧校验 |
| 遗留 `POST /query` | API 进程内同步执行 Agent 与检索；LLM 调用已过 `model_gateway → llm_governor`，RAG/tool 无预算 | P0b 为 RAG/tool 接入部署级预算，并决定入口去留 |
| document_search_batch | 已有局部并行 | 共享 executor + 全局限流 |
| Action DAG | 已有依赖语义 | ready layer 改 bounded parallel |
| Chat Worker | 已有持久化 turn 队列、atomic claim 与 `WORKER_TURN_CONCURRENCY` 进程内并发 | 与 Heavy Job 隔离执行容量，补同会话轮次顺序 |
| General Subagent Disabled | 当前合理 | 不建议直接全面开启 |
| Reviewer Agent | 未实现 | P3 优先增加 |
| Research Agent | 未实现 | P3 按需增加 |

---

## 18. 关键代码依据

本方案重点基于以下当前分支代码：

```text
src/agents/runner.py
src/agents/orchestration/executor.py
src/agents/tools/document_rag_tool.py
src/core/llm_governor.py
src/core/model_gateway.py
src/workers/main.py
src/settings.py
```

仓库分支：

`https://github.com/ZuyangYu/Hardware-DataBase/tree/feature/fix-module-matching`

确认的关键事实：

1. `runner.py` 使用 `create_deep_agent`，并显式禁用 general-purpose subagent；
2. Agent Checkpoint 默认使用 `SqliteSaver`；
3. `executor.py` 已支持依赖 DAG，但同层 ready action 当前逐个执行；
4. `document_rag_tool.py` 中 `document_search_batch` 最多 4 路并发并创建线程池；
5. `settings.py` 中 Agent Checkpoint、Memory、Authoring Checkpoint 仍有 SQLite 默认配置；
6. `settings.py` 已包含 OpenTelemetry、Phoenix、Grafana 等观测配置；
7. `settings.py` 校验允许 `DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=postgres`，但 `document_authoring/harness/checkpointer.py::build_checkpointer` 未实现该后端，属“配置可接受、实现未支持”；
8. 遗留 `POST /query` 在 API 进程内同步执行 Agent 与检索，不经 Worker；其 LLM 调用已过进程级 `llm_governor`（`model_gateway` 统一接入），RAG/tool 无预算。`chat_turns.user_message_id` 为 `NOT NULL` 消息全局 ID，可作为历史回填与排序校验依据，正式会话顺序需由服务端 `turn_sequence` 契约定义；`chat_turns` 已含 `wiki_assist` 列。

---

## 19. 最终结论

Hardware-DataBase 更合理的演进路线不是：

```text
Single Agent → Full Multi-Agent
```

而是：

```text
Current Supervisor Harness
        ↓
Baseline + State Boundary Inventory
        ↓
Conversation Ordering + Queue/Worker Isolation
        ↓
Global Resource Budgets + Backpressure
        ↓
Distributed State Migration + Multi-instance Validation
        ↓
Bounded Parallelism (only where measured useful)
        ↓
Evaluation-driven Optimization
        ↓
Optional Reviewer / Research Agent
```

最终目标应定义为：

> **Production-grade Hybrid Supervisor Agent Harness**

核心原则：

1. 业务能力优先做 Tool；
2. 系统行为优先做 Workflow；
3. 真正需要独立上下文时才做 Agent；
4. 并发首先受系统级资源预算控制；
5. 多用户状态必须隔离，同一会话必须有顺序；
6. 文档生成保持可审计、可回滚、可验证；
7. 是否引入 Multi-Agent 由评测数据决定，而不是由架构潮流决定。

落地约束：

1. 本路线图按 Phase（P0a/P0b/P1/P2/P3）拆分为独立变更与独立验收的工程任务，每项任务必须能单独回滚；
2. 本文件不是对 `feature/fix-module-matching` 分支的单次合入方案，模块匹配修复按该分支自身目标单独处理；
3. P0b 不得早于 P0a 验收通过启动，P1 状态迁移不得早于 P0a；每阶段先与 Phase 0 基线比较再扩大范围。

---

## 附录 A：2026-09-15 合并基线对照与待实施落点

> 本附录以合并后干净基线 `01c807e` 为唯一核对对象。A.2–A.5 列出的 P0–P3 能力均为**待实施提案**（当前代码不存在），A.1 记录基线事实，A.3 记录已合并子系统与配额权威边界。

### A.1 基线与状态

| 项 | 内容 |
|---|---|
| 合并基线 | `01c807e`（2026-09-15 合并 PR #49 `feature/asset-center-and-llm-governance`；v1.4 基线 `7fd340a` 已过期） |
| 工作区 | 干净，无未提交实现；本文所有“新增/建议”条目均为待实施提案 |
| 回归状态 | 待重建：当前合并基线没有本方案要求的固定回归基线；Phase 0 必须先重跑全量、记录既有失败清单并冻结初始 SLO |
| 历史实施工件 | `docs/superpowers/implementation/production-hardening/`（记录过一版设计过程，代码落点不在当前基线，仅作参考） |

### A.2 待实施落点（基于 `01c807e` 的代码坐标）

| 路线图条目 | 建议落点 | 现状（`01c807e`） |
|---|---|---|
| P0a 同会话串行化 / turn 顺序 | `src/core/conversation.py`：`turn_sequence` 列、head-of-line claim、lease/fencing、stale-owner 拒写；迁移脚本 `scripts/production_hardening/migrate_chat_turn_sequence.py` | 无 `turn_sequence`；已有 `claim_turn`（`conversation.py:1213`）与 `list_pending_turn_work`（`:1172`）；`user_message_id` 为 `NOT NULL`；新增列需与 `wiki_assist` 并存 |
| P0a waiting 队头阻塞 | 在 `src/attachments/coordinator.py` 上补 600 秒等待上限、单所有者 5 秒扫描、`blocked_by_turn_id/queue_reason/wait_deadline` 与 `hdb.conversation.attachment_wait` | 已有 `TURN_STATUS_WAITING_FOR_ATTACHMENTS`（`conversation.py:85`）、`transition_waiting_turn`、settle/reconcile 协调器（`attachments/coordinator.py`）；缺 claim 队头阻塞、等待超时、可见状态字段与单所有者扫描 |
| P0a Worker 隔离 | `WORKER_ROLE`（`src/workers/main.py`） | 无角色；已有 `WORKER_TURN_CONCURRENCY` 提供聊天 turn 进程内并发 |
| P0b 资源预算 | `src/core/concurrency/resource_governor.py`（RAG/TOOL/HEAVY 等部署级额度；LLM 进程层复用 `llm_governor`） | 无；已有 `src/core/llm_governor.py`（interactive/batch、队列超时）与 `model_gateway` usage ledger |
| P0b 准入限流 | `src/core/concurrency/admission.py` + `src/api/admission.py`，接入创建 turn、遗留 `POST /query`、导出与文档计划确认 | 无 |
| P0b 共享检索执行器 | `src/agents/tools/retrieval_executor.py` + `document_rag_tool.py` 接线 | 无；batch 每次调用新建 `ThreadPoolExecutor`（`document_rag_tool.py:129`） |
| P1 Checkpoint provider | `src/agents/checkpoints/{base,factory,sqlite_provider,postgres_provider}.py`；`AGENT_CHECKPOINT_BACKEND` / `AGENT_CHECKPOINT_DATABASE_URL` | 无；`runner.py::_get_checkpointer`（`:114-125`）固定 `SqliteSaver`；`pyproject.toml` 仅 `langgraph-checkpoint-sqlite==3.1.1` |
| P1 迟到写入闭环 | `src/agents/checkpoints/attempts.py`（attempt namespace + canonical 指针 CAS）、`fork.py` | 无 |
| P1 Authoring Checkpointer | `checkpointer.py::build_checkpointer` 增加 postgres 分支（保持 `FencedCheckpointer` fencing） | `build_checkpointer`（`checkpointer.py:391`）只支持 memory/sqlite；`settings.py:1149` 却允许 `postgres`，属“配置可接受、实现未支持” |
| P1 Memory 投影 | `src/memory/store.py::create_memory_store` 的 postgres 分支 | `store.py:238` 对 postgres 直接抛 `RuntimeError` |
| P1 迁移工具 | `scripts/production_hardening/migrate_sqlite_to_postgres.py` | 无 |
| P2 Action DAG 并行 | `ACTION_DAG_PARALLEL_ENABLED`、`ACTION_EXECUTOR_MAX_PARALLEL`、`ResolvedAction.access_mode/cost_class/resource_key/dependency_requirements` | `contracts.py` 仅有 `ActionSpec.depends_on`；`executor.py:72` 顺序执行 |
| P3 Sub-Agent | `src/agents/subagents/{reviewer_agent,research_agent}.py` | 无 |
| §10 指标 | `src/observability/metrics.py` 新增容量/治理指标 | 已有 `hdb.llm.*` / `hdb.chat.*` / `hdb.queue.*` / `hdb.worker.*` / `hdb.authoring.*` / `hdb.export.*` / `hdb.eval.*` / `hdb.memory.*`；`llm_governor.snapshot()` 已提供 LLM 队列状态 |

### A.3 已合并子系统（PR #49，v1.4 未覆盖）

- `src/core/llm_governor.py`：进程级 LLM 准入（`interactive` 保留容量、`batch` 单独上限、队列超时/告警），`snapshot()` 提供 active/queued/timeouts/max_wait_ms，经 `GET /system/status -> llm` 暴露；模块注释明确多副本需要共享 limiter。
- `src/core/model_gateway.py`：统一模型接入层（governed adapter + 进程级 usage ledger），`model_gateway.py:322` 使用 `get_llm_governor().slot(...)`；`/system/status` 同时返回 usage。
- 资产中心（`src/core/assets.py`、`src/core/doc_assets.py`）、Wiki 蒸馏与回灌检索（`src/core/wiki.py`，`wiki_search` 已接入 runner）、多模态网关（`src/core/multimodal_gateway.py`）、附件对话式编辑与候选发布治理（`make_attachment_edit/release/revision_history`）、导出链路。
- `chat_turns.wiki_assist` 列与 `WORKER_TURN_CONCURRENCY`（聊天 turn 进程内并发池）。
- 数据落点：资产中心（`src/core/assets.py`、`src/core/doc_assets.py`）、Wiki（`src/core/wiki.py`）、导出任务与 `turn_attachments` 关联落在 `auth.db`（连同用户/会话/turn）；附件/资产索引与解析状态在 `storage/chat_attachments.db`（`CHAT_ATTACHMENT_INDEX_DB_PATH`，`src/attachments/store.py:57`），附件文件在 `storage/chat_attachments/`。多主机迁移时三类存储均须并入 §5.4 权威存储清单。

**配额权威边界（v1.8）**：进程级 LLM 准入继续由 `llm_governor` 承担并复用已有配置；P0b 即在该模块扩展同机共享 limiter，P1 迁至 PostgreSQL。资源 governor 与 LLM 适配共享配额存储，各自负责唯一接入边界；不同作用域可组合，同一作用域不得重复扣减。部署 capacity/batch_cap/保留量由配额库版本化配置，环境变量示例只作初始化参考。

### A.4 配置名对照（§14 建议名 → 已合并/现有配置）

| §14 建议 | 已合并/现有配置 | 结论 |
|---|---|---|
| `AGENT_CHECKPOINT_BACKEND` / `AGENT_CHECKPOINT_DATABASE_URL` | 现仅有 `AGENT_CHECKPOINT_DB_PATH` | 新增，保留建议名 |
| `CONVERSATION_SERIALIZE_SAME_THREAD` | 无 | 新增 |
| `CONVERSATION_LOCK_TIMEOUT_SECONDS` | 无 | 新增（会话执行等待上限；租约另用 `CHAT_TURN_LEASE_SECONDS` / `CHAT_TURN_LEASE_RENEW_SECONDS`） |
| `CHAT_TURN_LEASE_SECONDS` / `CHAT_TURN_LEASE_RENEW_SECONDS` | 无 | 新增；与实施方案 §5.2 统一命名（默认 120 / 15 秒），P0-003 冻结 |
| `WAITING_TURN_MAX_SECONDS` | 无 | 新增；与实施方案 §5.3 统一命名（默认 600 秒） |
| `AGENT_MAX_CONCURRENT_RUNS` | 无 | 新增（run 级在途上限；与 LLM 并发不同维度） |
| `AGENT_MAX_CONCURRENT_LLM_CALLS` | `LLM_MAX_CONCURRENCY=4` / `LLM_BATCH_MAX_CONCURRENCY=1`（已合并） | **撤销建议名，复用现有**；部署级总量另按 §4.4 设计 |
| `RAG_MAX_CONCURRENT_CALLS` / `TOOL_MAX_CONCURRENT_CALLS` | 无 | 新增（部署级） |
| `CHAT_WORKER_MAX_CONCURRENCY` | `WORKER_TURN_CONCURRENCY=1`（已合并） | **撤销建议名，复用现有** |
| `ACTION_EXECUTOR_MAX_PARALLEL` | 无 | 新增 |
| `RESOURCE_GOVERNOR_SCOPE` / `RESOURCE_QUEUE_MAX_*` / `HEAVY_JOB_MAX_CONCURRENCY` / `OCR_JOB_MAX_CONCURRENCY` | 无 | 新增提案（命名待实现时定稿） |
| `RESEARCH_AGENT_ENABLED` / `REVIEWER_AGENT_ENABLED` | 无 | 新增（默认 false） |
| `OBS_CAPACITY_METRICS_ENABLED` | 无（`llm_governor.snapshot()` 等常开） | 可选；优先复用现有快照 |

必须复用的已合并配置：`LLM_MAX_CONCURRENCY`、`LLM_BATCH_MAX_CONCURRENCY`、`LLM_QUEUE_TIMEOUT_SECONDS`、`LLM_QUEUE_WARN_SECONDS`、`WORKER_TURN_CONCURRENCY`、`DOCUMENT_AUTHORING_JOB_BATCH_TIME_BUDGET_SECONDS`、`AGENT_CHECKPOINT_DB_PATH`、`DOCUMENT_AUTHORING_CHECKPOINTER_*`、`MEMORY_*`、`OTEL_*` / `OBS_*`。

### A.5 剩余差距（相对 `01c807e`）

- 同会话 turn 无顺序/所有权约束；`turn_sequence`、lease/fencing、stale-owner 拒写均未实现。
- RAG/tool/heavy job 无任何进程级或部署级预算；LLM 仅有进程级 `llm_governor`，跨主机共享协调未实现。
- Worker 无角色隔离；chat 已有 `WORKER_TURN_CONCURRENCY` 进程内并发，Heavy Job 与 chat 仍共享循环与进程。
- Agent Checkpoint 固定 `SqliteSaver`，无 provider 抽象；Authoring `postgres` 配置可校验但 builder 抛错；Memory `postgres` 直接抛错。
- Admission control 不存在；遗留 `POST /query` 的 RAG 路径无预算（LLM 路径已被 `llm_governor` 覆盖）。
- 共享 Artifact/文件服务与本地索引单写者发布未实现；资产中心/Wiki/导出元数据与 `turn_attachments` 在 `auth.db`，附件/资产索引与解析状态在 `storage/chat_attachments.db`，均需纳入多主机迁移清单。
- Action DAG 并行与 Sub-Agent 均未实现，flag/配置尚不存在。
- 回归数据缺口：`evaluation/datasets/hardware_qa_v1.jsonl` 在文档中被引用但仓库缺失（仅存在于 `docs/test_chat/` 与 `storage/evaluations/uploads/`），需补建或更正引用。

### A.6 阅读约定

- §3–§5、§10、§13–§15、§17、§18 为路线图与建议契约，不表示当前代码已支持；其中 v1.6 已按合并基线修订 LLM 配额（§4.4）、Worker 并发（§4.6/§14）与遗留 `POST /query`（§4.4/§17/§18）表述；v1.7 修正附件元数据落点（§5.4/A.3/A.5）、waiting 现状（A.2）、P0b 多进程配额口径（§4.4）与配置命名（§14/A.4）。
- 需要“代码是否已支持”的结论时，以本附录 A 与合并基线 `01c807e` 为准。
- 本附录列出的 P0–P3 落点均为待实施提案，不替代各 Phase 的独立验收与压测要求。
