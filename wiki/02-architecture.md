# 02 · 总体架构

## 1. 分层视图

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 表现层  frontend/ (React 18 + TS + Vite)                                  │
│   LoginPage / ChatPage(useKbChat) / KbFilesPage / MemoryPage / WikiPage  │
│   DocumentAssetsPage / admin/* / 组件库(ui/*)                             │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ HTTPS + Bearer；SSE(fetch stream) / multipart / 轮询
┌───────────────┴──────────────────────────────────────────────────────────┐
│ API 层  src/api/  (FastAPI，唯一后端入口)                                  │
│   app.create_app → lifespan(DB init/迁移/子进程/遥测) → routers(/api/v1)  │
│   deps(current_user/require_*/reject_system_admin_kb_access)              │
│   admission(请求准入) · errors(统一错误映射) · schemas(DTO)               │
└───────────────▲──────────────────────────────────────────────────────────┘
                │ Python 调用（不重写业务，只包一层 HTTP）
┌───────────────┴──────────────────────────────────────────────────────────┐
│ 应用编排层  src/core/app_pipeline.py  (AppPipeline 单例)                   │
│   ├─ backend        : RAGFlowBackend（唯一检索/摄取后端）                  │
│   ├─ documents      : DocumentManager（治理门面）                          │
│   ├─ agent          : MultiSourceAgentRunner（查询引擎）                   │
│   ├─ projects       : ProjectService / ProjectEvidenceRetrievalService    │
│   ├─ document_generation : DocumentGenerationService                      │
│   └─ 域服务：AuthService / WikiService / DocumentAssetService / AppLog…    │
└───────┬──────────────┬───────────────┬──────────────┬────────────────────┘
        │              │               │              │
┌───────▼──────┐ ┌─────▼──────┐ ┌──────▼──────┐ ┌─────▼───────────────┐
│ Agent 运行时 │ │ 摄取管道   │ │ 结构化索引  │ │ 受控文档生成        │
│ src/agents/  │ │ src/pipeline│ │ spreadsheet│ │ document_authoring/ │
│ runner/tools │ │ ingestion  │ │ circuit    │ │ harness/planning/   │
│ orchestration│ │ registry   │ │ external_  │ │ writers/renderers   │
│ checkpoints  │ │ runtime    │ │ conversations│ │ migrations         │
└───────┬──────┘ └─────┬──────┘ └──────┬──────┘ └─────┬───────────────┘
        │              │               │              │
┌───────▼──────────────▼───────────────▼──────────────▼───────────────────┐
│ 基础层                                                                   │
│  model_factory / chat_model_runtime / llm_governor / model_gateway       │
│  auth / conversation(控制面) / app_logs / notifications / wiki / assets  │
│  concurrency(resource_governor/admission/rate_limiter/distributed)       │
│  observability(OTel/metrics/health/worker_registry) + settings           │
└──────────────────────────────────────────────────────────────────────────┘
        │                    │                        │
┌───────▼──────┐   ┌────────▼────────┐   ┌───────────▼──────────────────┐
│ 本地状态      │   │ 外部服务        │   │ 后台进程                     │
│ storage/*.db │   │ RAGFlow         │   │ worker(all|chat|authoring|   │
│ 文件归档/索引 │   │ LLM(Ollama/API) │   │ parse) / export / memory     │
│              │   │ PG / Redis      │   │                              │
└──────────────┘   └─────────────────┘   └──────────────────────────────┘
```

### 1.1 依赖方向与边界

- **跨层数据只走 schema**：`src/pipelines/document_rag/schemas.py`（`Evidence`/`IngestResult`/`RequestContext`）、
  `src/agents/schemas.py`、`src/circuit/models.py`、`src/evaluation/schemas.py`。
  禁止用临时 dict 跨边界传递。
- **API 层不重写业务**：路由只做参数解析、权限依赖、DTO 映射、错误码翻译，
  真正逻辑在 `AppPipeline` 与各 domain service。
- **`AppPipeline` 是单例**（`api/deps.py` 双检锁）；`reset_pipeline()` 在配置热重载时停止
  `backend.runtime` 并关闭 backend，避免旧解析 worker 竞争。
- **部分域服务不经 AppPipeline**：`WikiService`、`DocumentAssetService`、`AssetService`、
  `AppLogService`、`NotificationService` 由路由/工具闭包直接构造（共享 `AUTH_DB_PATH`）。
  这是有意的（避免 AppPipeline 继续膨胀），但意味着权限检查必须在各自路由内完成。

---

## 2. 请求生命周期（以「知识库问答」为例）

```
1. 鉴权
   Authorization: Bearer <token>
   → AuthService.get_user_by_token(sha256(token)) → AuthUser(username, role, department_id)
   → build_context_for_user → RequestContext(roles, allowed_kbs, kb_permissions, metadata)
2. 准入
   enforce_admission(...)：ADMISSION_CONTROL_ENABLED 时按 (user, tenant, resource) 记 RPM + 未终态 turn 数
3. turn 创建（一个 BEGIN IMMEDIATE 事务）
   ConversationService.create_turn
     ├─ 解析 source_scope / attachment_ids，冻结 turn_attachments 快照
     ├─ 写 user 消息 + 空 assistant 消息
     ├─ 分配单调 turn_sequence（每会话唯一、不复用）
     ├─ 依据附件解析状态决定初始 status：waiting_for_attachments | pending
     └─ 解析查询意图，落 export_plan_json（服务端导出意图）
4. 执行（worker）
   claim_turn_result：租约 + fencing_token + 顺序串行化（可选）
   _run_turn：
     ├─ AgentRunner.stream：作用域 → 计划 → 工具 → deepagents 循环 → 回答
     ├─ 事件写入 chat_turn_events（seq 单调，SSE 可断点续传）
     └─ complete_turn_result：同事务写回答 + 终态事件 + 导出任务 + 记忆 outbox
5. 传输
   GET /turns/{id}/events?after=<seq>（SSE）
     ├─ 进程内队列（同进程 worker）或 Redis Stream（跨进程）或 DB 轮询（兜底 0.4s）
     └─ 事件：delta / narration / stage / tool_started / tool_result / degraded / done / error
6. 落账
   query_traces + retrieved_evidence（审计中心可查、带 OTel trace 链接）
```

关键点：

- **HTTP 请求不等待模型**。除了「创建 turn」「启动 turn」这类毫秒级操作，其余全部异步；
  浏览器只订阅事件。刷新/切换会话不丢任务（见 [04-conversation-turns.md](04-conversation-turns.md)）。
- **同一 turn 只有一个 owner**（worker_id + attempt_id + fencing_token）。
  过期 worker 的写入会被拒绝（`StaleTurnOwner`），避免双写。

---

## 3. 进程模型

### 3.1 进程清单

| 进程           | 入口                                                         | 职责                                          | 备注                                                |
| -------------- | ------------------------------------------------------------ | --------------------------------------------- | --------------------------------------------------- |
| API            | `hardware-database-server` → `src/api/app.py:main`      | HTTP + SSE + 子进程管理                       | 默认`127.0.0.1:8001`；单机可自动拉起 worker       |
| Worker（统一） | `hardware-database-worker --role all\|chat\|authoring\|parse` | turn 执行 / 文档生成 job / 解析 claim         | `WORKER_ROLE` 默认 `all`（仅开发）              |
| Export worker  | `hardware-database-export-worker`                          | 导出渲染（不可变快照 → 文件）                | 有独立运行配额与保留期清理                          |
| Memory worker  | `hardware-database-memory-worker`                          | 记忆抽取/投影/删除 outbox/retention/reconcile | `MEMORY_SINGLE_WRITER=true` 时用 flock 保证单写者 |
| Eval CLI       | `hardware-database-eval`                                   | RAGAS validate/collect/score/run              | 也可由 API 页面驱动                                 |
| E2E runner     | `hardware-database-e2e`                                    | 附件编辑端到端演练（需`HDB_E2E=1`）         | 会产生真实模型调用                                  |
| Maintenance    | `hardware-database-maintenance`                            | 附件补齐解析、兼容性就绪检查                  | —                                                  |

### 3.2 单机自动拉起（lifespan）

`src/api/app.py::lifespan` 启动阶段：

1. `validate_provider_configuration()`——**fail-closed**：选了 `postgres` 但没给 DSN 直接拒绝启动
   （checkpoint / memory / 资源治理 / 控制面全部适用）。
2. 初始化各域 schema（`AuthService._init_db`、`ConversationService._init_db`、
   `ensure_memory_schema`、`WikiService`、`AppLogService`、附件 store…）。
3. 启动资源治理维护线程、共享检索线程池（若开启）。
4. `_harden_storage_permissions()`：`.env` 与 `storage/*.db` chmod 600。
5. 启动时执行一次 query trace 清理（默认 30 天）。
6. 按开关 spawn 子进程：worker / export worker / memory worker。
7. 注册 OTel（`init_observability`）与 FastAPI instrumentation。
8. shutdown：先停子进程（5s 宽限后 kill），再停 governor、关闭 pipeline、flush 遥测。

### 3.3 多实例部署要求

| 维度                | 单机                                                                                               | 多实例                                                                                           |
| ------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 控制面              | `CONTROL_PLANE_BACKEND=sqlite`（或 `bash scripts/postgres.sh up` + `write-env` 单机也走 PG） | `postgres` + `CONTROL_PLANE_DATABASE_URL`（池 `CONTROL_PLANE_POOL_SIZE=8`）                |
| 会话 checkpoint     | `AGENT_CHECKPOINT_BACKEND=sqlite`                                                                | `postgres`                                                                                     |
| 文档生成 checkpoint | `DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=sqlite`                                                 | `postgres`（注意：attempt/pointer 表目前仅 SQLite，见 [21](21-gotchas-and-extension-guide.md)） |
| 记忆投影            | `MEMORY_STORE_BACKEND=sqlite`                                                                    | `postgres`（pgvector）                                                                         |
| 资源配额            | `RESOURCE_GOVERNOR_SCOPE=process`                                                                | `deployment` + `RESOURCE_GOVERNOR_BACKEND=postgres`                                          |
| 实时平面            | 可选 Redis（`bash scripts/redis.sh up`，缺失按 fail-soft 退化）                                  | `REDIS_URL`：取消快路径 / 跨进程 SSE / 分布式准入                                              |
| 子进程              | `HDB_API_SPAWN_*` 默认                                                                           | 全部设 0，独立起 worker（含**唯一的** memory worker）                                      |
| 文件                | 本地`storage/`                                                                                   | 所有实例共享同一`storage/`（附件、归档、导出、索引都是文件+SQLite）                            |

扩容前必跑 `scripts/production_hardening/connection_budget.py` 校验 PG 连接数预算。

---

## 4. 存储全景

### 4.1 SQLite 数据库（默认单机）

| 数据库           | 路径（默认）                                                           | 主要表                                                                                                                                                                                                                                                                                              | 权威性                             |
| ---------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| 认证/治理库      | `storage/auth.db` (`AUTH_DB_PATH`)                                 | `users` `departments` `knowledge_bases` `auth_sessions` `login_attempts` `audit_events` `query_traces` `retrieved_evidence` `wiki_pages(+revisions/jobs)` `doc_assets(+versions/events/links)` `hardware_assets(+candidates/evidence)` `notifications` `worker_instances` | **权威**（不可重建）         |
| 会话库           | 同`auth.db`                                                          | `chat_sessions` `chat_messages` `chat_turns` `chat_turn_events` `chat_message_edits` `turn_attachments` `conversation_execution_state` `action_receipts`                                                                                                                            | 权威                               |
| 记忆库           | `storage/auth.db`（Catalog）+ `storage/memory.db`（投影）          | Catalog：`memory_records/revisions/sources/projections/consent_*/jobs/runs/outboxes/audit`；投影：LangGraph `store`/`store_vectors`                                                                                                                                                           | Catalog 权威；投影**可重建** |
| 解析台账         | `storage/pipeline_documents.db`                                      | `pipeline_documents` `pipeline_datasets` `pipeline_document_profiles` `pipeline_profile_outbox`                                                                                                                                                                                             | 权威                               |
| 表格索引         | `storage/table_indexes/departments/<dept>/kbs/<kb>/table_indexes.db` | `table_documents/sheets/rows/cells/text_blocks/semantic_rows` + `sql_table_registry` + 物化表 `t_<rec>_<seq>`                                                                                                                                                                                 | 权威（可重解析）                   |
| 附件库           | `storage/chat_attachments.db`                                        | `chat_attachments/assets/parts/revisions/edit_proposals/embeddings/visual_cache/jobs` + FTS5 虚表 + `session_cleanup_outbox` + `coordinator_leases`                                                                                                                                           | 权威                               |
| 文档生成库       | `storage/document_authoring.db`                                      | 30+ 表（模板/分析/区域/绑定/计划/任务/run/receipt/事件/artifact…）                                                                                                                                                                                                                                 | 权威                               |
| 生成 checkpoint  | `storage/document_authoring_checkpoints.sqlite`                      | LangGraph checkpoint                                                                                                                                                                                                                                                                                | 可重建                             |
| 项目库           | `storage/projects.db`                                                | `projects` `project_knowledge_bindings` `source_assets` `source_versions` `project_baselines` `source_set_snapshots` …                                                                                                                                                                 | 权威                               |
| Agent checkpoint | `storage/agent_checkpoints.db`                                       | LangGraph`checkpoints`/`writes` + `hdb_checkpoint_attempts`/`pointers`                                                                                                                                                                                                                      | 可重建（attempt 指针表除外）       |
| 资源配额         | `storage/resource_quotas.db`                                         | 许可租约/队列/额度                                                                                                                                                                                                                                                                                  | 可重建但影响限流                   |
| 导出库           | 同`auth.db`                                                          | `result_snapshots` `result_export_jobs` `result_export_outbox` `result_artifacts`                                                                                                                                                                                                           | 权威（快照不可变）                 |
| 评估             | 文件系统（`storage/evaluations/<run_id>/`）                          | `run_state.json`/`snapshot.jsonl`/报告                                                                                                                                                                                                                                                          | 权威（运行目录）                   |

### 4.2 文件系统布局（关键路径）

```
storage/
├── pipeline_archives/departments/<dept>/kbs/<kb>/<source_group>/<file>
├── table_indexes/departments/<dept>/kbs/<kb>/table_indexes.db (+ .index-locks/)
├── circuits/{index.json, <kb>/<design_id>/{circuit_state.json, connectivity_graph.gpickle, ...}}
├── external_conversations/departments/<dept>/kbs/<kb>/{index.db, <conversation_id>/...}
├── chat_attachments/{sources,circuits,table_indexes}/<session_id>/...
├── exports/artifact-<hex>.<ext>            # 0700 目录 / 0600 文件
├── document_authoring/                      # artifact/template 字节
├── evaluations/[{uploads/,<run_id>/,e2e/}]
├── logs/rag_YYYY-MM-DD.log                  # JSON 日志
└── run/                                     # hdb.sh 的 pid 文件
```

### 4.3 PostgreSQL（控制面）

`CONTROL_PLANE_BACKEND=postgres` 时接管：

- 会话执行协议：`chat_sessions` `chat_messages` `chat_turns` `chat_turn_events`
  `conversation_execution_state` `turn_attachments` `chat_message_edits` `action_receipts`；
- 版本化 schema：`control_plane_schema` 表记录 `schema_version`（当前 `"1"`）；
  `/health/ready` 用 `assert_schema_compatible` 校验，**不匹配即 not-ready**；
- SQLite → PG：`loader.load_sqlite_snapshot()` 逐表复制 + 行数/内容哈希校验 + 权威性断言；
  事件必须停机窗口（目标表非空即拒绝）；
- 域写入（导出快照/任务/outbox、记忆 reflection outbox）通过 `domain_writer` 与 turn 完成同事务提交。

**刻意留在 SQLite 的域**：认证/审计/文档生成/附件字节。迁移文档明确：
PostgreSQL 只拥有「会话执行 + 记忆账本 + 导出账本」，其它域不参与控制面事务。

---

## 5. 横切机制

### 5.1 权限与作用域

- `RequestContext`（`document_rag/schemas.py`）承载 `user_id`（chat 路径下是**用户名**）、
  `roles`、`allowed_kbs`、`kb_permissions`（键为 `kb_scope_key(name, department_id)`）、
  `metadata`（`department_id`/`kb_id`/`actor_department_id`/`resource_department_id`）。
- `KbScope`（`services/kb_scope.py`）把「department_id + kb_name」固化为检索/存储路径的统一入口，
  `require_department()` 是所有后端方法的强制前置。
- 附件是例外：附件作用域是 `(session_id, user_id)` 而非 KB；工具每次调用都重新校验冻结的 refs。

### 5.2 事件与实时

| 机制         | 通道                                                          | 用途                                             |
| ------------ | ------------------------------------------------------------- | ------------------------------------------------ |
| 进程内队列   | `_TURN_EVENT_QUEUES`                                        | 同进程 worker → SSE                             |
| Redis Stream | key`hdb:turn:events:{turn_id}`，TTL 7200，maxlen 5000       | 跨进程/跨实例 SSE                                |
| DB 轮询      | `chat_turn_events` 每 0.4s                                  | 无 Redis 兜底                                    |
| Redis 取消键 | `hdb:turn:cancel:{turn_id}`，TTL 7200                       | 取消快路径（权威仍是 DB 列`cancel_requested`） |
| 文档任务流   | `GET /document-generation/chat-tasks/events`（SSE，可重放） | 文档生成卡片更新                                 |
| 通知         | `notifications` 表 + 30s 轮询 + 可选 webhook                | 附件放行审批等                                   |

### 5.3 幂等与去重

三个层次：

1. **上传幂等**：内容 SHA-256 + `UNIQUE(kb_name, department_id, document_name, dataset_kind)`，
   `find_by_hash` 命中且远端存在 → 复用。
2. **业务幂等键**：`client_request_id`（turn/附件/导出/文档 job/计划确认）、
   `idempotency_key`（revision/proposal/receipt/consent）、`run_key`（记忆 reflection）。
   冲突但载荷不同 → 明确报错而不是覆盖。
3. **执行幂等**：`node_execution_receipts`（run+node+unit+input_fingerprint）、
   `harness_runs` 租约、`t_h...`—「重复执行返回已提交结果」。

### 5.4 失败策略矩阵

| 层                    | 失败对象                 | 策略                                                 | 用户可见表现                              |
| --------------------- | ------------------------ | ---------------------------------------------------- | ----------------------------------------- |
| 检索工具              | RAGFlow 超时/5xx         | fail-open，返回空                                    | `degraded` 事件 + footer 诊断           |
| 模型调用              | 429/超时/不可用          | 重试（custom 最多 4 次）+ 友好文案                   | turn 失败，`friendly_error_message`     |
| Agent 循环            | 递归上限（工具步数用尽） | 停止检索，输出已收集证据的说明                       | `degraded{stage:"agent_loop"}` + 说明段 |
| LLM 准入              | 队列超时                 | `LLMQueueTimeoutError`                             | turn 失败（排队超时）                     |
| 附件解析              | 解析器异常               | 资产`failed`，可 retry；waiting turn 收敛为 failed | 卡片提示 +`attachment_failed`           |
| 附件/记忆/导出 worker | 任务失败                 | 退避重试 → dead_letter + 指标/告警日志              | 任务中心可见失败状态，可手动重试          |
| 文档生成              | 单元检索/校验失败        | 单元`requires_human`，run `waiting_human`        | 需要人工决策卡                            |
| 文档生成              | 迁移/DDL 冲突            | 中止且不产生部分写                                   | 明确错误码                                |
| 权限/作用域           | 任何越权                 | fail-closed 拒绝                                     | 403/404/409                               |
| OTel/指标/审计        | 导出或写入失败           | 完全 fail-soft                                       | 无感                                      |

### 5.5 配置热重载

- `PUT /api/v1/config` → `AppPipeline.apply_settings`：
  `validate_settings_values` → `validate_provider_configuration` → `save_settings_to_env`（原子替换）
  → `reload_settings()` → `invalidate_chat_model_cache()`。
- 校验先于落盘：非法值不会污染 `.env` 导致下次启动失败。
- **重启才能生效**的键（`settings.RESTART_REQUIRED_SETTINGS`，`/health/ready` 会返回）：
  `STORAGE_DIR`、`AUTH_DB_PATH`、`CONTROL_PLANE_*`、`REDIS_URL`、`RESOURCE_GOVERNOR_{BACKEND,DATABASE_URL,SCOPE}`、
  `AGENT_CHECKPOINT_{BACKEND,DATABASE_URL}`、`MEMORY_STORE_BACKEND/DATABASE_URL/SQLITE_PATH`、
  `WORKER_ROLE`、`HDB_API_HOST/PORT`。

---

## 6. 部署拓扑与运行手册

### 6.1 单机（默认）

```bash
uv sync --all-groups                       # dev 组含 pytest/ruff；eval 组需 --group eval
uv run hardware-database-server            # API 8001；默认自动拉起 worker + export worker
cd frontend && npm ci && npm run dev       # 5176，代理 /api → 8001
# 可选：bash scripts/hdb.sh start|stop|restart|status|logs
```

生产化进程管理（推荐）：`scripts/hdb.sh` 白名单读取 `.env` 中 `HDB_API_HOST/PORT`、`HDB_FRONT_PORT`、`HDB_WORKER_ROLES`，
强制 `HDB_API_SPAWN_WORKER=0`/`HDB_API_SPAWN_EXPORT_WORKER=0`/`HDB_API_SPAWN_MEMORY_WORKER=0`，
分别启动 `worker-chat`、`worker-authoring`、`worker-parse`、`memory-worker`、`export-worker`，
pid 写 `storage/run/`、日志写 `storage/logs/`。

### 6.2 可观测性栈

```bash
docker compose -f deploy/observability/docker-compose.yml up -d
# OTel Collector(4317/4318) → Prometheus(9090) / Tempo / Loki / Grafana(3000) / Phoenix(6006)
# 应用侧：OBS_ENABLED=true  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4317
```

内置 2 个 Grafana dashboard 与 15 条告警规则（6 条 SLO + 9 条 hardening）。
**没有 Alertmanager**：告警止于 Prometheus 评估。

### 6.3 健康探针

| 端点                          | 语义                                                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `GET /health`               | 存活 + 状态摘要                                                                                                                            |
| `GET /health/live`          | `{status:"live", service}`                                                                                                               |
| `GET /health/ready`         | `database` / `control_plane`（schema 版本）/ `storage` 三项全 up 才 ready；返回 `config_revision` 与 `restart_required_settings` |
| `GET /health/dependencies`  | 仅系统管理员：worker 注册表、RAGFlow、LLM 可达性                                                                                           |
| `GET /api/v1/system/status` | 治理页：整体状态、依赖、LLM 并发、token 用量、24h 任务计数                                                                                 |

### 6.4 迁移与备份（生产硬化脚本）

```
state_fingerprint.py        # 迁移前/后：行数 + 7 个状态 DB 与 5 个数据目录的 SHA-256
migrate_sqlite_to_postgres.py  # 分组迁移（authority/projection/attachments/pipeline/compatibility）
load_sqlite_into_postgres.py   # 控制面停机窗口加载（含 schema 生成 + 权威性断言）
cutover_rehearsal.py        # 7 步演练：preflight → VACUUM INTO → migrate → verify → smoke → rollback → 报告
migration_rollback_drill.py # 证明源库哈希不变、二次执行幂等、派生表跳过
migrate_chat_turn_sequence.py  # turn_sequence 扩列→回填→校验→NOT NULL（生产需停写）
verify_checkpoint_postgres.py / multi_instance_drill.py / fault_isolation_drill.py
concurrency_compare_drill.py / control_plane_benchmark.py / connection_budget.py
load_probe.py / governor_recovery.py / regression_runner.py
```

---

## 7. 为什么这样设计（关键权衡）

| 决策                                          | 备选                                     | 理由                                                                         |
| --------------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------- |
| RAGFlow 固定为文档后端                        | 本地 Chroma+BM25+RRF+rerank（已删除）    | 解析/嵌入/混合检索全部下沉到 RAGFlow，应用侧不再维护向量索引；代码量大幅下降 |
| Agent 用`deepagents` 通用循环，不写死 graph | 手写「拆解→规划→补检」有界图（已删除） | 多跳由模型自主决定，泛化更好；预算用`recursion_limit` 而非硬节点           |
| 表格/电路走独立结构化索引                     | 全部塞进 RAGFlow                         | 精确数字/引脚/位号查询需要确定性检索，语义检索不可靠                         |
| 会话执行持久化（turns + SSE）                 | 请求内同步流式                           | 刷新/切换不丢任务；worker 可水平扩展；带租约天然抗双写                       |
| «治理角色不能看内容»                        | 超管全权                                 | 防止平台管理员静默窥视部门私有数据（合规要求）                               |
| 文档生成哈希绑定 + 收据 + 人工放行            | 直接让模型写模板                         | 工程文档是受控交付物，必须可复现、可审计、可回滚                             |
| 记忆 Catalog 权威 + 投影可重建                | 直接写向量库                             | consent/revoke/来源失效必须强一致，向量库只是可重建的检索副本                |
| 配置单文件 + 热重载                           | 各模块自读 env                           | 避免配置漂移；系统配置页可在线改；重启项显式暴露                             |

---

## 8. 下一步

- 查询链路细节：→ [04-conversation-turns.md](04-conversation-turns.md)、[05-agent-runtime.md](05-agent-runtime.md)
- 数据摄取细节：→ [08-ingestion-and-ragflow.md](08-ingestion-and-ragflow.md)
- 运维与扩容：→ [18-observability-and-ops.md](18-observability-and-ops.md)、[20-config-and-storage.md](20-config-and-storage.md)
