# 状态清单（state-inventory）

> 基线：`feature/fix-module-matching` commit `01c807e`（2026-09-15，PR #49 合并）
> 依据：`src/settings.py`、各领域 store 构造函数与 `docs/superpowers/plans/Hardware-DataBase_feature-fix-module-matching_完整实施方案.md` §7
> 用途：Phase 1 迁移范围的最低清单；迁移前须用 `scripts/production_hardening/state_fingerprint.py` 生成指纹并归档。

## 1. 权威状态（必须迁移、必须校验）

| 状态域 | 存储位置 | 生产者 | 事务/一致性关联 |
|---|---|---|---|
| 身份、权限、登录会话、审计、查询日志 | `storage/auth.db`（`AUTH_DB_PATH`） | `src/core/auth.py`、`src/core/app_logs.py` | 与下方同库表共享 `BEGIN IMMEDIATE` 边界 |
| 对话、消息、turn、turn 事件、附件快照、编辑记录 | `storage/auth.db`（`src/core/conversation.py`） | `ConversationService` | `create_turn`/`claim_turn`/`complete_turn` 单事务 |
| 会话执行权威状态（lease/fencing/generation/checkpoint 指针） | `storage/auth.db` → `conversation_execution_state`（P0-003） | `ConversationService` | 与 turn、事件、最终答案、领域 outbox 同一事务；session 删除级联 |
| Action 计划 receipt 账本 | `storage/auth.db` → `action_receipts`（P2-001） | `ConversationActionExecutor` / `ActionReceiptStore` | 稳定键 `(tenant,session_generation,turn_id,plan_version,action_id)`；与领域提交无法同事务时靠幂等键回查 |
| 附件协调选主 | `storage/chat_attachments.db` → `coordinator_leases`（P0-004） | `src/attachments/coordinator_lease.py` | 行级 lease；扫描结果仍按 turn 原状态条件迁移 |
| 部署级资源配额与 uncertain 审计 | `storage/resource_quotas.db`（`RESOURCE_GOVERNOR_DB_PATH`，deployment 作用域） | `src/core/concurrency/resource_governor.py` | 短事务 lease 行；身份行 `deployment_id/schema_version/config_revision`；upgrade 前须排空在途 permit |
| 文档生成任务队列 | `storage/auth.db`（`document_authoring/job_store.py`） | `DocumentAuthoringJobStore` | lease/幂等/重试字段 |
| 文档生成计划提交、生成会话、工单、评审、Artifact 元数据 | `storage/auth.db`（`document_authoring/` 领域 store，pipeline 注入同一库） | `AppPipeline` 文档生成域 | 与 turn 的 lineage 字段关联；跨表发布须原子或可恢复 |
| Memory 权威账本、jobs、projection/deletion outbox、同意状态 | `storage/auth.db`（`memory/catalog.py`、`memory/jobs.py`、`memory/service.py`） | `MemoryService`、`MemoryWorker` | `complete_turn` 与任务/outbox 同事务写入 |
| 文档兼容性审计账本 | `storage/document_authoring_compatibility.sqlite`（`DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH`） | `document_authoring/compatibility.py` | 独立库；迁移按 §7.4 事务清单划组 |
| Agent Checkpoint（会话图状态） | `storage/agent_checkpoints.db`（`AGENT_CHECKPOINT_DB_PATH`） | `src/agents/runner.py`（进程级 `SqliteSaver` 单例） | 无 fencing；P0-003 增加 attempt 隔离与指针 CAS |
| Authoring Checkpoint | `storage/document_authoring_checkpoints.sqlite`（`DOCUMENT_AUTHORING_CHECKPOINTER_PATH`） | `FencedCheckpointer` | 已有 fencing token 拒写 |
| 附件元数据 | `storage/chat_attachments.db`（`CHAT_ATTACHMENT_INDEX_DB_PATH`） | `AttachmentStore` | 与文件目录成对校验 |
| 附件文件 | `storage/chat_attachments/`（`CHAT_ATTACHMENT_STORAGE_DIR`） | 上传/解析流程 | 路径与元数据一致性 |
| Pipeline 文档与解析任务台账 | `storage/pipeline_documents.db` | `PipelineDocumentStore` | 部门+KB 作用域 |
| Pipeline 归档源文件 | `storage/pipeline_archives/`（`PIPELINE_ARCHIVE_ROOT`） | `IngestionOrchestrator` | 内容 hash 去重 |
| Circuit 设计状态 | `storage/circuits/{kb}/{design_id}/circuit_state.json` + `index.json` + `pipeline_metadata.json` | `CircuitIndexService` | JSON 文件，非事务 |
| 测试数据域 | `storage/test_data/` | `src/test_data/` | 域数据，未见迁移需求 |
| Result Export 任务与产物 | `storage/auth.db`（`result_exports/store.py`）+ `storage/exports/`（`RESULT_EXPORT_STORAGE_DIR`） | `ResultExportWorker` | lease token 发布校验 |

## 2. 可重建投影（先重建、后切换，不参与权威切换组）

| 投影 | 存储位置 | 来源 | 重建方式 |
|---|---|---|---|
| Memory 检索投影（LangGraph Store/LangMem） | `storage/memory.db`（`MEMORY_SQLITE_PATH`） | Memory 权威账本 + outbox | 权威组切换后按 outbox 重放/重建，再开放检索 |
| Circuit 向量索引（ChromaDB per-KB） | RAGFlow/本地 Chroma 客户端管理 | `circuit_state.json` | 重新 reindex（fail-soft） |
| Spreadsheet 结构化索引 | `storage/table_indexes/{kb}/table_indexes.db` | 归档 `.xlsx` | 重新解析 |
| RAGFlow 解析结果（文档 chunks/embedding） | RAGFlow 服务侧 | 归档源文件 | 重新上传/解析 |
| 查询日志/评测运行产物 | `storage/auth.db`（审计表）、`storage/evaluations/` | 运行过程 | 观测数据，不迁移（按保留策略清理） |
| Worker 心跳注册表 | `src/observability/worker_registry.py` | 进程心跳 | 瞬态，多实例启用后按新注册表重建 |
| 进程内配置缓存、解析任务瞬态 | `storage/parse_tasks/tasks.json` + 内存 | 上传流程 | 瞬态；重启后由台账恢复 |

## 3. 迁移分组（与实施方案 §7.4/§13.6 对齐）

1. **AUTH_DB 原子组**：身份/权限/会话/turn/消息/事件/审计 + 文档生成任务 + 计划提交/生成会话/工单/评审/Artifact 元数据 + Memory 权威账本/jobs/outbox + Export 任务。这些表可能跨表事务，必须整组切换（可跨多个 PR 实现，但切换点唯一）。
2. **Checkpoint 组**：Agent Checkpoint 与 Authoring Checkpoint 跟随各自执行所有权协议迁移；attempt 指针与数据始终成对校验。
3. **文件组**：附件、归档、Artifact、导出产物迁移到共享文件服务；数据库只保存逻辑 storage key。
4. **投影组**：Memory 投影、Circuit 向量、Spreadsheet 索引、RAGFlow 解析结果，按 §7.3 在权威组之后重建。
5. **非迁移组**：日志/评测/心跳/配置缓存按保留策略处理，不进迁移范围。

## 4. 校验要求

- 每个迁移组交付：源/目标映射、行数与 hash 校验、外键与权限校验、切换步骤、回滚说明；
- 使用 `state_fingerprint.py` 在迁移前后各生成一次指纹（表行数、关键列 max/校验和、文件 hash）；
- 记录未解释差异；差异未归零前不得扩大流量；
- 禁止把文件直接复制到共享目录或把整个 `storage/` 挂载共享替代数据库迁移。
