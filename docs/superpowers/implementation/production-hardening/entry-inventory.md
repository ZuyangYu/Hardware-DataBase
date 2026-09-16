# 资源治理入口清单（entry-inventory）

> 基线：`feature/fix-module-matching` commit `01c807e`
> 依据：实施方案 §6.2/§6.4 与 `src/core/concurrency/` 实现
> 用途：证明所有真实下游调用边界都接入了治理；新增入口必须更新本表。

## 1. LLM 调用边界

| 入口 | 客户端边界 | 资源/作用域 | 取消信号 | 重试策略 | permit 释放 |
|---|---|---|---|---|---|
| Chat/Agent `/conversations/{id}/turns` 执行 | `_GovernedChatOpenAI/_GovernedChatOllama._generate/_stream` → `llm_governor.slot(priority)`（**单一 LLM 准入权威**：内部先取进程槽、再取部署共享 permit） | llm（两层，同一作用域不重复申请） | turn cancel signal | LangChain `max_retries`（每次底层请求重新进入 `_generate` → `slot()`，两层 permit 都重新获取） | 请求结束（流式=流关闭） |
| 遗留 `POST /query` | 同上 | 同上 | 请求 cancel event | 同上 | 同上 |
| Wiki 蒸馏 / Memory 抽取 / 评测 judge | `govern_ragas_llm` / `hdb_priority=batch` 的 governed 模型 | 进程 batch 槽 + 部署非互动 permit | 任务取消 | 同上 | 调用结束 |
| Evaluation judge LLM | `govern_ragas_llm` → `llm_governor.slot(batch)` | 进程 batch 槽（部署级不重复申请，方案 §6.2 明确不重复限流） | 评测取消 | RAGAS/instructor 自带 | 调用结束 |
| `EVAL_EMBEDDING_*` | 未接入 governor | — | — | — | **范围外**：embedding 非 LLM，已在 `baseline-report.md` 与 `metric-catalog.md` 标注；纳入前不得宣称评测入口全覆盖 |

## 2. RAG / TOOL / 其他下游

| 入口 | 客户端边界 | 资源 | 取消 | 释放 |
|---|---|---|---|---|
| `document_search` / `document_search_batch` | `RAGFlowBackend.retrieve` → `governor_acquire("rag")` | rag | ToolRuntime cancel + RetrievalExecutor cancel_check | 调用结束（迟到结果由 epoch 拒收） |
| agent 工具调用 | `runtime.timed_tool_call` → `governor_acquire("tool")` | tool | QueryCancelled | 调用结束 |
| Chat Worker turn | `_run_turn` → `get_governor().acquire("chat")` | chat | turn cancel | turn 结束 |
| Document authoring/plan jobs | `HardwareWorker._acquire_heavy_permit` | heavy_job | job cancel 标志 | job 结束 |
| Attachment OCR | `governor_acquire("ocr")` | ocr | parse deadline | 调用结束 |
| Export worker | `get_governor().acquire("export")` | export | job lease 失效 | job 结束 |
| Memory worker | `get_governor().acquire("memory")` | memory | worker stop | job 结束 |
| Attachment parse / RAGFlow ingest | parse 角色（进程隔离）；远端 RAGFlow 自带配额 | — | 任务取消 | 远端完成 |
| 附件协调扫描 | `coordinator_leases` 单所有者，非下游调用 | — | 线程 stop | lease 释放 |

## 3. 任务准入（HTTP 层）

| 入口 | 资源桶 | 检查 | 说明 |
|---|---|---|---|
| `POST /conversations/{id}/turns` | `chat`（通用）/`deep`（KB 问答） | per user/tenant/model RPM + 队列深度 + 未终态 turn 数（会话/用户）+ `client_request_id` 幂等重放 | 重放返回原 turn，不计数；超限 429 + Retry-After |
| `POST /query`（遗留） | 同上 | 同上（无持久化 turn，跳过未终态检查） | LLM 走 `llm_governor`；RAG 走 `RAGFlowBackend.retrieve` 的部署 permit |
| Export 创建 | `export` | per user/tenant/model RPM | 空配置继承 heavy 桶 |
| 文档计划确认 | `document` | 同上 | 同上 |
| Evaluation 创建 | `evaluation` | 同上 | system_admin 专属入口 |

准入维度说明（§4.5）：per user、per tenant、per model（`ADMISSION_MODEL_RPM_*`，空=关闭）、per resource type（chat/deep/document/export/ocr/evaluation，空=继承 chat 或 heavy）。OCR 无 API 入口，由部署级 OCR permit 约束。

## 3.1 队列业务优先级（§6.2）

| 优先级 | 工作 | 实现方式 |
|---|---|---|
| P0 交互聊天 | chat turns | 独立 `--role chat` 进程 ×2 + `RESOURCE_CHAT_*` 保留容量 |
| P1 用户确认的文档任务 | authoring jobs / plan submissions | 独立 `--role authoring` 进程 |
| P2 导出 / OCR | result exports / attachment OCR | 独立 export worker 进程 + `RESOURCE_EXPORT_/OCR_*` 预算 |
| P3 Memory / Evaluation | memory projection / eval runs | 独立 memory worker 进程 + `RESOURCE_MEMORY_*` 预算 |

## 3.2 Governor 一等接口（§6.1.1）

- `acquire(request_id=...)` 注册可取消等待；`cancel_wait(request_id)` 取消本进程排队项（跨进程等待由 deadline 有界，无持久化排队行）；
- `GovernorPermit.finish(outcome, evidence)`：`completed`/`cancelled` 需服务端认可的结束证据（客户端异常不算）；`uncertain` 保持隔离；
- `GovernorPermit.reconcile(evidence, operator)` / `Governor.reconcile(...)`：对 uncertain permit 做证据化 CAS 释放并写审计；
- `snapshot()` 返回按资源的活动/等待/不确定计数；恢复工具见 `governor_recovery.py`；
- 维护循环多实例选主：`resource_maintenance_lease`（lease 15s/每 5s 续约/接管递增 token），仅持有者扫描并升级 uncertain。

## 3.3 连接池与客户端复用（路线图 §5.3）

- PostgreSQL Agent Checkpoint：`PostgresCheckpointerProvider` 使用 `psycopg_pool.ConnectionPool`
  （`AGENT_CHECKPOINT_POOL_SIZE`，默认 4；并发读写与关闭已实测），避免多线程共享单连接；
- RAGFlow HTTP：同步路径复用持久化 `httpx.Client`（既有实现，不重复）；异步可取消路径仍为临时 `AsyncClient`，待事件循环生命周期评估后统一；
- LLM HTTP：由 `model_gateway` governed adapter 持有官方客户端，重试在适配器边界重新获取 permit；
- SQLite（会话/附件/配额）：直连 + 短事务（无池），busy 错误有界重试/拒绝。

## 4. 已知范围外（需在报告中显式标注，不得宣称全覆盖）

- `EVAL_EMBEDDING_*` embedding 调用（见 §1）；
- 供应商 RPM/TPM 限额未在客户端登记（`RateLimiter` 已有实现，但未接入真实 provider 配额；当前以并发 permit 为主要预算）；
- LangChain SDK 内部重试不单独计数（`hdb.llm.retry_total` 未实现）；重试会重新获取进程与部署两级 permit（`_generate` 边界），但重试次数无法从现成指标区分。
