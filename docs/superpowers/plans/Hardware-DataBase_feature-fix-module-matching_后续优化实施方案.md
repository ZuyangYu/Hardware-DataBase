# Hardware-DataBase `feature/fix-module-matching` 后续优化实施方案

> 版本：v1.0  
> 日期：2026-09-16  
> 适用分支：`feature/fix-module-matching` 及其生产化实施分支  
> 关联文档：  
> - [修改与优化方案](./Hardware-DataBase_feature-fix-module-matching_修改优化方案.md)  
> - [完整实施方案](./Hardware-DataBase_feature-fix-module-matching_完整实施方案.md)  
> - [实施记录](../implementation/production-hardening/implementation-log.md)

## 1. 方案定位

本方案用于收敛当前工作区已经出现的 P0–P3 生产化代码，并补齐上线前仍缺少的稳定性、容量、质量和交付治理。

当前代码以 `01c807e` 为 HEAD，但生产化实现仍保留在未提交工作区。因此，本方案把这些实现视为候选实现，先完成稳定性核验和分阶段提交，再允许灰度启用。

模块匹配算法本身继续作为独立专项推进。本方案只规定专项的入口、数据、验收和交付约束，不把模块匹配算法修改与生产化基础设施变更合并为一个不可回滚的大变更。

## 2. 当前问题与实施原则

### 2.1 必须先解决的问题

1. 工作区实现没有形成可复现的提交和版本边界。
2. 多进程 SQLite 竞争测试存在间歇性 `database is locked` 和子进程无法 ready 的问题。
   - 实测（2026-09-16）：`tests/test_multiprocess_sqlite_contention.py` 连续 20 次运行中 5 次失败，失败轮耗时约 31.3 秒（ready 等待超时）。
   - 已定位根因：`SqliteQuotaStore` / `CoordinatorLeaseStore` 在构造阶段执行 `BEGIN IMMEDIATE` 初始化 schema 与身份行，配额连接 busy timeout 仅 1 秒且无有界重试；4 个进程同时启动时，未获得写锁的子进程在写 ready 标记前退出，父进程等待 30 秒超时。另 `ConversationService` 每次构造都会重复执行 7 条 DDL 与回填 UPDATE，放大竞争。
   - 已在上一轮修复：`start_governor_maintenance` 自死锁、配额短事务忙错误重试、`try_acquire` 忙时视为未准入；剩余“初始化阶段无重试 + 构造器重复 DDL”由 G1 §6.1 关闭。
3. 方案已有确定性测试，但目标环境容量、Heavy Job 排队、过载恢复和绝对 SLO 尚未完成。
4. 回归集尚未达到 200 条；完整 RAGAS 质量基线尚未执行。
5. PostgreSQL、共享文件和跨主机配额已有 provider、脚本或演练，但没有完成生产切换。
6. Reviewer/Research 和 Action DAG 并行已有代码，但默认关闭，尚未以正式评测证明上线收益。

### 2.2 实施原则

- 先提交和稳定正确性，再验证容量，最后开启可选并行能力。
- 每个阶段独立提交、独立验收、独立回滚。
- 旧入口、旧数据和旧 Worker 在协议兼容前不得与新协议混合写入。
- 所有生产结论必须绑定 commit、工作区 diff、数据集 hash、配置快照和测试命令。
- 未通过验收的功能保持关闭；不能用“代码已存在”替代上线证据。

## 3. 最终目标

最终系统应达到以下状态：

```text
用户/API
   ↓
统一准入与权限校验
   ↓
有序会话 Turn + 可恢复 Checkpoint
   ↓
Chat / Authoring / Parse / Memory / Export 独立 Worker
   ↓
共享资源 Governor + 有界检索执行器
   ↓
PostgreSQL 权威状态 + 共享 Artifact 存储
   ↓
可观测指标、质量评测、告警、灰度与回滚
```

最终目标不是全面拆分 Agent，而是：

> 一个受治理的 Supervisor Harness，配合确定性 Workflow、可恢复任务、部署级资源预算和按评测结果启用的 Specialist Agent。

## 4. 实施阶段与依赖

| 阶段 | 目标 | 主要交付 | 前置条件 | 退出条件 |
|---|---|---|---|---|
| G0 | 交付固化与基线重建 | 分支、提交、工件纳管、干净基线 | 当前工作区盘点 | 可从新 commit 重放全部测试 |
| G1 | P0a 顺序与隔离 | turn 顺序、fencing、附件等待、Worker role | G0 | 多进程正确性稳定通过 |
| G2 | P0b 容量与背压 | Governor、Admission、共享检索执行器 | G1 | 所有入口预算可观测且过载行为明确 |
| G3 | P1 状态服务化 | PostgreSQL provider、迁移、共享文件、多副本 | G2 | 迁移、接管、回滚和权限校验通过 |
| G4 | 质量与容量验收 | 固定数据集、压测、SLO、告警 | G1，可与 G2/G3 部分并行 | 目标环境满足硬性门槛 |
| G5 | P2 受控并行 | Action DAG 并行和 receipt 恢复 | G4 证明串行瓶颈 | 并行收益显著且无质量退化 |
| G6 | P3 可选 Agent | Reviewer/Research 灰度 | G4、专项评测 | 评测收益超过不确定性，否则保持关闭 |
| M0–M3 | 模块匹配专项 | 样例、算法修复、回归集、灰度 | 可与 G0/G1 并行 | 匹配质量和兼容性门槛通过 |

## 5. G0：交付固化与基线重建

### 5.1 任务

1. 创建独立实施分支，例如 `feature/production-hardening-p0`。
2. 保存当前工作区清单：tracked diff、untracked 文件、配置快照、依赖锁文件和忽略目录中的实施工件。
3. 将需要共享的实施文档显式纳入版本管理；不能依赖 `/docs/` 被忽略后的本地文件。
4. 将代码按主题拆成独立提交：
   - `p0-turn-sequence-and-fencing`
   - `p0-waiting-attachments-and-worker-roles`
   - `p0-resource-governor-and-admission`
   - `p0-shared-retrieval-executor`
   - `p1-checkpoint-and-migration`
   - `p2-action-receipts`
   - `p3-optional-subagents`
5. 在干净提交上重跑基线，记录 commit SHA 和工作区状态，不允许只记录 `git rev-parse HEAD`。
6. 统一回归样本统计。当前文档中出现 118 和 143 两个样本数，必须以数据集清单和 hash 重新计算为准。

### 5.2 交付物

- `baseline-report.md`
- `state-inventory.md`
- `entry-inventory.md`
- `regression-set.md`
- `implementation-log.md`
- `workspace-manifest.json`
- 基线 commit、依赖 lock hash、数据集 hash 和配置快照

### 5.3 G0 验收

- 可以在干净 checkout 中重放全部确定性测试。
- 实施文档、测试和新增脚本不会只存在于 ignored/untracked 状态。
- 基线报告不会把未提交代码误标为干净 commit 的能力。

## 6. G1：P0a 顺序与隔离加固

### 6.1 SQLite 多进程初始化和竞争修复（G1 第 1 项）

按以下顺序实施，完成后再执行 20 次稳定性门槛：

1. **单一初始化**：`ConversationService` 增加进程内初始化缓存（同一 `db_path` 只初始化一次），并只在需要时执行回填 UPDATE（先计数再决定是否写）。
2. **有界重试**：`SqliteQuotaStore`、`CoordinatorLeaseStore` 的构造/schema 初始化包在有界忙重试中（最多 2 秒退避，超时给出明确失败原因，不静默通过）。
3. **忙超时**：配额连接保持短 busy timeout（1 秒，超时按未准入处理）；会话/租约库使用较长 busy timeout 等待初始化完成。
4. **启动自检**：进程启动前检查数据库路径、WAL 状态、文件可写性和数据库身份。
5. **测试**：至少 4 个子进程同时初始化协调租约、配额库和会话库；20 次连续运行 0 失败、0 超时（失败需保留子进程日志）。

验收命令与门槛见 §6.4；该修复必须先于其他 G1 内容合入，否则多进程门槛不稳定。

### 6.2 会话正确性

- `turn_sequence` 由服务端生成，客户端不得传入。
- 同一 session 只允许最早可执行的非终态 turn 被领取。
- lease、fencing token、generation、attempt 和 checkpoint 指针必须在权威事务内更新。
- 旧 Worker 的事件、Checkpoint、Artifact、答案和失败状态写入必须被拒绝，并记录 stale write 指标。
- 清空或删除会话后，旧 Worker 不能重新创建执行状态或恢复旧结果。

### 6.3 附件等待与 Worker 隔离

- `waiting_for_attachments` 队头阻塞、600 秒默认超时和后序推进行为必须稳定。
- 只有有效 coordinator lease 持有者可以扫描和迁移等待状态。
- `chat`、`authoring`、`parse` 角色必须有明确队列归属。
- Chat Worker 与 Heavy Worker 进程故障不能相互阻塞。

### 6.4 G1 验收命令和门槛

```bash
uv run pytest -q tests/test_chat_turn_sequence.py \
  tests/test_chat_turn_claim_fencing.py \
  tests/test_waiting_turn_timeout.py \
  tests/test_coordinator_lease.py \
  tests/test_worker_roles.py \
  tests/test_multiprocess_sqlite_contention.py
```

要求：

- 多进程专项连续运行至少 20 次，0 次失败、0 次超时。
- 同 session 并发提交始终按 `turn_sequence` 完成。
- 跨 session 可以并行。
- stale owner 有效写入数为 0；重复终态事件和重复 Artifact 为 0。
- 健康 coordinator 下附件超时后 10 秒内进入终态；协调者故障时不超过 lease 过期后两个扫描周期。

## 7. G2：P0b 容量与背压

### 7.1 Resource Governor

- LLM 进程级治理继续由 `llm_governor` 负责。
- 同机多进程通过共享 SQLite 配额库协调 LLM、RAG、TOOL、CHAT、HEAVY、OCR、EXPORT、MEMORY。
- 跨主机部署的 PostgreSQL 配额存储属于 G3（状态服务化），不在 G2 验收范围内；G2 只验收同机多进程。
- permit 生命周期必须覆盖 acquire、renew、finish、cancel、uncertain 和 evidence-backed reconcile。
- 远端调用结束未知时进入 `uncertain`，不能因客户端 timeout 直接释放容量。

### 7.2 入口覆盖

必须覆盖：

- 新式 Chat Turn；
- 遗留 `POST /query`；
- Document Authoring；
- Memory、Export、OCR 和 Parse Worker；
- Evaluation judge LLM；
- 每个实际 RAG 调用；
- SDK 重试路径。

`EVAL_EMBEDDING_*` 和供应商 RPM/TPM 若仍属于范围外，必须在入口清单、指标目录和发布报告中明确标注。

### 7.3 G2 验收

- 资源总量不超过部署配置，不能由每个进程的本地 semaphore 叠加放大。
- 队列达到上限时返回明确 429/503 或持久化失败状态。
- 已接受任务不再伪装成未接受请求。
- 取消、超时、重试不会双重释放 permit。
- uncertain permit 有告警、维护选主和证据化恢复。
- Retrieval Executor 共享池有界，停机时未确认远端请求不被错误清零。

## 8. G3：状态服务化与多实例

### 8.1 Provider

- Agent Checkpoint、Authoring Checkpoint 和 Memory provider 的配置与实现必须一致。
- 未交付 provider 不得被配置保存或启动流程静默接受。
- PostgreSQL saver、驱动和连接池版本写入 lock 文件并在 CI/部署镜像中验证。

### 8.2 迁移分组

按事务依赖实施：

1. 会话、权限、turn、事件、执行指针和 Memory 权威账本；
2. Authoring、Attachment、Export、Wiki 和资产元数据；
3. Artifact、附件和归档文件；
4. 可重建检索投影。

同一原子事务中的表必须使用同一数据库连接和事务提交。迁移报告中的任意 `hash_mismatches` 或 `count_mismatches` 都阻止切换，不能只依据顶层 `ok=true`。

### 8.3 G3 验收

- 任意 API 副本可恢复同一会话。
- 同角色 Worker 可接管过期任务，旧 Worker 无有效迟到写入。
- 权限、删除、租户隔离和 Memory 账本/投影一致。
- Artifact 和附件可跨节点访问，文件 hash 与元数据一致。
- 迁移失败可恢复到明确时间点。
- 目标库开始写入后，不允许直接切回旧库；回滚必须使用反向同步或明确恢复点。

## 9. G4：质量、容量和观测验收

### 9.0 资源预算与责任（执行前提）

| 项 | 估算/要求 | 责任角色 |
|---|---|---|
| RAGAS 全量 25×6，单轮 | 当前判分模型约 3.6 分钟/指标项 → 单轮约 9 小时，三轮约 27 小时 | 测试负责人 + 模型预算审批 |
| 200 条人工确认样本 | 需领域标注（现有 143 条，再补 57+） | 后端 + 业务标注人 |
| 30 分钟×3 + 2× 过载 + 恢复 | 目标环境至少一个完整业务高峰窗口；含 Chat/Heavy/RAG/LLM/DB/接管 | 运维 + 测试 |
| 绝对 SLO | 由业务/服务负责人登记，本文不虚构数值 | 业务负责人 |

缺少预算或标注责任人时，G4 只能标记“确定性回归通过，质量/容量未验收”。

### 9.1 固定质量集

- 回归集至少 200 条人工确认样本。
- 覆盖普通问答、多源检索、Spreadsheet/SQL、Circuit/BOM、附件、文档生成、Action routing 和权限边界。
- RAGAS 全量评测至少运行 3 次并报告波动区间。
- Document Eval、Agent routing、Citation coverage 和 Unsupported Claim 必须有可复现报告。

### 9.2 容量测试

目标环境必须执行：

- 预热 5 分钟；
- 固定混合负载测量至少 30 分钟，重复 3 次；
- 2 倍目标到达率过载 5 分钟；
- 恢复负载后观察队列是否在 5 分钟内恢复到过载前水平；
- 单独测量 Chat、Heavy Job、RAG、LLM、DB pool 和 Worker 接管。

### 9.3 默认发布门槛

- 跨会话 Chat P95 和 TTFT P95：不超过对应基线的 1.10 倍，且同时满足已登记绝对 SLO。
- 接受后内部失败率：不超过 1%，且较基线增加不超过 0.2 个百分点。
- routing、citation、正确率下降不超过 1 个百分点。
- Unsupported Claim rate 上升不超过 1 个百分点。
- 重复 Work Order、Artifact、终态事件和有效 stale write 为 0。
- 权限越权为 0。
- RAG 429、LLM timeout 和队列拒绝分别统计，不得合并掩盖。

## 10. G5/G6：可选并行和 Sub-Agent

### 10.1 Action DAG

只有 G4 证明串行确实是瓶颈后，才允许开启 `ACTION_DAG_PARALLEL_ENABLED`。

必须先通过：

- 只读、不同 resource key 的动作可并行；
- 同资源写写、读写冲突保持串行；
- `accepted` 与 `completed` 依赖语义准确；
- receipt 可恢复，已完成动作不重复执行；
- 部分失败、取消、依赖环和迟到结果可重放或进入待协调状态。

### 10.2 Reviewer/Research

- Reviewer 先以离线评测和影子模式运行，不修改正式 Artifact，不自动放行发布 Gate。
- Research 只允许一层委派，权限范围由父请求冻结。
- 必须比较现有 Supervisor 与 Sub-Agent 的正确率、引用、完成率、延迟和 token 成本。
- 收益落在统计不确定性内时保持关闭。

## 11. M0–M3：模块匹配专项

模块匹配专项独立于生产化基础设施实施，但可以与 G0/G1 并行。

### M0：样例和基线

建立脱敏样例表，至少包含：

| 字段 | 要求 |
|---|---|
| 输入 | 原始模块名、上下文和来源 |
| 期望结果 | 人工确认的模块/器件标识 |
| 当前结果 | 当前算法输出及候选排名 |
| 错误类型 | 漏匹配、误匹配、歧义、格式归一化或上下文缺失 |
| 证据 | 来源文件、行号/坐标、解析版本 |
| 版本 | 算法、规则、模型和数据版本 |

冻结 Top-1 accuracy、Top-k recall、误匹配率、歧义保留率和解析失败率基线。

### M1：根因和算法修改

- 每个错误样例必须关联根因和修改文件。
- 规则、归一化、别名、层级上下文和候选排序变化分别提交。
- 算法修改不得直接改变会话、配额、迁移等生产化协议。

### M2：回归和兼容

- 全量历史样例回归。
- 新增 hard cases 和反例。
- 旧格式、大小写、连字符、版本号和同名模块必须覆盖。
- 匹配失败时返回可解释的候选和证据，不能静默猜测。

### M3：灰度

- 先影子计算，比较新旧结果，不改变正式写入。
- 再按知识库或租户灰度。
- 匹配质量下降、误匹配率上升或证据缺失时自动回滚算法版本。

## 12. 发布、回滚和配置要求

### 12.1 配置开关

以下开关在对应阶段验收前保持关闭：

```env
CONVERSATION_SERIALIZE_SAME_THREAD=false
RESOURCE_GOVERNOR_ENABLED=false
SHARED_RETRIEVAL_EXECUTOR_ENABLED=false
ACTION_DAG_PARALLEL_ENABLED=false
REVIEWER_AGENT_ENABLED=false
RESEARCH_AGENT_ENABLED=false

# 已有开关，按阶段说明：
ADMISSION_CONTROL_ENABLED=true            # 入口 RPM/backlog；默认开启，属 P0b 强制项
AGENT_CHECKPOINT_BACKEND=sqlite           # postgres 需 URL + 版本验证
DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=sqlite
```

测试环境可以显式开启；生产按租户或固定 session 分组灰度，不能让同一 session 的不同 turn 随机落入新旧 claim 协议。

### 12.2 回滚规则

- G1：停止新 turn，排空或撤销在途执行，再切回兼容单消费者模式。
- G2：暂停新准入和新 lease，保留已有 lease 的释放及 uncertain 处理；禁止关闭 Governor 后无限放行。
- G3：目标库有新写入后不能直接切回旧库，先执行反向同步或保持目标库前向修复。
- G5：关闭并行开关，保留串行和 receipt 恢复路径。
- G6：关闭 Sub-Agent，未完成审查的发布继续保持未完成。
- M3：按算法版本回滚，不回滚会话、配额或存储 schema。

## 13. 最终交付定义

方案完成必须同时满足：

1. 所有实现已进入可审计提交，工作区不存在未说明的生产化代码。
2. 全量测试、专项多进程测试和静态检查稳定通过。
3. P0a 正确性、P0b 背压、P1 迁移和 G4 质量/容量门槛全部通过。
4. 关键配置、数据库版本、数据集 hash、依赖版本和启动命令已冻结。
5. Grafana/Phoenix/Prometheus 可以定位队列、锁、额度、下游、Worker 和 stale write。
6. 所有阶段都有灰度步骤、回滚步骤、验收报告和责任角色。
7. 模块匹配专项有独立的错误样例、算法提交、准确率报告和回滚版本。
8. Reviewer、Research 和 Action DAG 并行只有在评测证明收益后才开启。

## 14. 立即执行顺序

1. 固化当前工作区清单并创建生产化实施分支。
2. 按 §6.1 修复初始化竞争（单一初始化 + 有界重试 + 忙超时），再连续 20 次运行多进程 SQLite 专项测试，要求 0 失败 0 超时。
4. 重新生成干净 commit 基线，统一样本数和迁移报告判定。
5. 提交并验收 G1，再开启测试环境的 P0a 开关。
6. 完成 G2 入口覆盖和部署级容量测试。
7. 在临时 PostgreSQL 和共享文件环境完成 G3 迁移/回滚演练。
8. 补齐 200 条质量集并完成 G4 目标环境压测。
9. 根据 G4 结果决定 G5/G6 是否开启。
10. 独立推进 M0–M3 模块匹配专项，并将通过的样例纳入最终回归集。

