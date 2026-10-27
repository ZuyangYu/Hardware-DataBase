# 20 · 配置与数据存储参考

对应：`src/settings.py`（1731 行，单一配置事实来源）、`src/storage/control_plane/`、
各域的 `_init_db`、`scripts/production_hardening/`。

---

## 1. 配置系统

### 1.1 机制

- `src/settings.py` 在导入时读取 `.env`（`utf-8-sig`，容 BOM），并把值物化为模块级变量；
  - 路径默认是仓库根 `.env`；容器部署用环境变量 `HDB_ENV_PATH` 指向挂载目录中的可写文件
    （`save_settings_to_env` 用 `mkstemp + os.replace` 原子替换，镜像层与单文件 bind mount
    不允许 rename（EBUSY），故 `.env` 必须落在挂载目录里；见 `deploy/docker/README.md`）；
- 路径类配置若为相对路径，统一相对 `STORAGE_DIR` 解析（默认 `<repo>/storage`，可由 env 配置）；
- 布尔值接受 `1/true/yes/on`；CSV 用 `_env_csv`（casefold 集合）；
- `DEFAULT_VALUES` 镜像全部键，供系统配置页与校验回退；
- `CONFIG_REVISION = sha256(.env)[:16]`（每次保存更新，`/health/ready` 返回）；
- 热重载：`PUT /api/v1/config` → `validate_settings_values` → `validate_provider_configuration`
  → `save_settings_to_env`（原子替换）→ `reload_settings()` → 清模型缓存；
  重启项保持当前运行时和进程环境值，避免 `.env` 覆盖 Docker 注入项或让单例与配置漂移；
- **重启生效清单**（`RESTART_REQUIRED_SETTINGS`）：
  `STORAGE_DIR, AUTH_DB_PATH, CONTROL_PLANE_{BACKEND,DATABASE_URL,POOL_SIZE}, REDIS_URL,`
  `RESOURCE_GOVERNOR_{BACKEND,DATABASE_URL,SCOPE}, AGENT_CHECKPOINT_{BACKEND,DATABASE_URL},`
   `MEMORY_STORE_BACKEND, MEMORY_DATABASE_URL, MEMORY_SQLITE_PATH, WORKER_ROLE, HDB_API_HOST, HDB_API_PORT`；
  还包含 checkpoint 路径/池、authoring checkpoint 和 OTel bootstrap 参数；完整清单以代码为准。
  内容采集策略、探针 timeout 等继续支持热重载；
- **provider 交叉校验**（fail-closed，启动与保存时都跑）：选 postgres 必须给显式 DSN
  （`AGENT_CHECKPOINT_*`、`DOCUMENT_AUTHORING_CHECKPOINTER_*`、`MEMORY_*`、
  `RESOURCE_GOVERNOR_*` 且 `SCOPE=deployment`、`CONTROL_PLANE_*`），**绝不静默回落本地**。

### 1.2 全量配置清单（按域）

> 「默认」列给出 `settings.py` 的默认值。标 ⚠️ 表示**定义但未被代码读取**或语义与直觉不同。

#### 路径与存储

| 变量 | 默认 |
|---|---|
| `STORAGE_DIR` | `<repo>/storage`（支持 env 覆盖，重启生效） |
| `PIPELINE_ARCHIVE_ROOT` / `RAGFLOW_FILE_ROOT` | `storage/pipeline_archives` / 同左 |
| `LOG_DIR`（派生） | `storage/logs` |
| `AUTH_DB_PATH` | `storage/auth.db` |
| `AGENT_CHECKPOINT_DB_PATH` | `storage/agent_checkpoints.db`（`AGENT_CHECKPOINT_BACKEND=postgres` 时退休） |
| `MEMORY_SQLITE_PATH` | `storage/memory.db`（`MEMORY_STORE_BACKEND=postgres` 时退休） |
| `RESOURCE_GOVERNOR_DB_PATH` | `storage/resource_quotas.db`（`RESOURCE_GOVERNOR_BACKEND=postgres` 时退休） |
| `DOCUMENT_AUTHORING_CHECKPOINTER_PATH` | `storage/document_authoring_checkpoints.sqlite`（切 postgres 时退休） |
| `DOCUMENT_AUTHORING_COMPATIBILITY_DB_PATH` | `storage/document_authoring_compatibility.sqlite` |
| `CHAT_ATTACHMENT_STORAGE_DIR` / `_INDEX_DB_PATH` | `storage/chat_attachments` / `storage/chat_attachments.db` |
| `RESULT_EXPORT_STORAGE_DIR` | `storage/exports` |
| （非 env）`storage/projects.db` | `ProjectStore` 默认 |

#### 鉴权

| 变量 | 默认 |
|---|---|
| `AUTH_DEFAULT_ADMIN_USERNAME` / `_PASSWORD` | `admin` / `""`（必须设置且 ≠ `admin123`） |
| `AUTH_SESSION_TTL_HOURS` | `24` |

#### RAGFlow 与检索

| 变量 | 默认 |
|---|---|
| `RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY` | `http://localhost:9380` / `""` |
| `RAGFLOW_GOVERNANCE_DATASET_NAME` | `department_governance`（`.env.example` 里是 `ADAS_new_develop`，注意差异） |
| `RAGFLOW_DESIGN_DATASET_NAME` | `project_design_assets`（同上差异） |
| `RAGFLOW_TIMEOUT_SECONDS` | `120` |
| `RAGFLOW_STATUS_REFRESH_MIN_SECONDS` | `60` |

#### 本地台账域后端（存储收敛）

| 变量 | 默认 |
|---|---|
| `PROJECT_STORE_BACKEND` / `PROJECT_STORE_DATABASE_URL` | `""`（跟随 `CONTROL_PLANE_BACKEND`）/ 同左 |
| `PIPELINE_DOCUMENT_STORE_BACKEND` / `PIPELINE_DOCUMENT_DATABASE_URL` | `""`（跟随 `CONTROL_PLANE_BACKEND`）/ 同左 |
| `AUTHORING_STORE_BACKEND` / `AUTHORING_STORE_DATABASE_URL` | `""`（跟随 `CONTROL_PLANE_BACKEND`）/ 同左 |
| `AUTH_STORE_BACKEND` / `AUTH_STORE_DATABASE_URL` | `"sqlite"`（**不跟随控制面**）/ 同左 |
| `COMPATIBILITY_STORE_BACKEND` / `COMPATIBILITY_STORE_DATABASE_URL` | `"sqlite"`（同上）/ 同左 |

- 域表在 PG 侧用**机械类型映射**（TEXT 时间戳保真 '' 哨兵），建表结构由 store 自己
  的 SQLite bootstrap 生成（`src/storage/domain_pg.py::provision_domain_schema`）；
- 空值跟随控制面，意味着 `CONTROL_PLANE_BACKEND=postgres` 时这些域自动切 PG ——
  **切换前必须先迁移数据**（`migrate_sqlite_to_postgres.py --group projects|pipeline`），
  store 启动时若发现"PG 空 + SQLite 有行"会 fail-closed 并给出迁移命令；
- 显式 `_BACKEND=sqlite` 可以让单个域在迁移完成前留在本地文件；
- 认证域特殊：PG 里 `users`/`departments`/`audit_events` 早期按控制面语义建过
  （TIMESTAMPTZ/BOOLEAN），必须先跑 `scripts/production_hardening/rebuild_auth_domain.py`
  重建为机械形状（TEXT 时间戳、BIGINT 标志）再切 `AUTH_STORE_BACKEND=postgres`；
  形状不对时 `AuthService`/`AppLogService` 构造会 fail-closed 并提示该命令。
| `RAGFLOW_SIMILARITY_THRESHOLD` / `RAGFLOW_VECTOR_WEIGHT` | `0.25` / `0.4` |
| `FINAL_TOP_K` | `5` |
| `AGENT_MAX_RETRIEVAL_ROUNDS` | `3` |
| `AGENT_EVIDENCE_BUDGET` / `AGENT_ATTACHMENT_EVIDENCE_BUDGET` | `30` / `80` |
| `EVIDENCE_SOURCE_AWARE_ENABLED` | `false`（附件+知识库来源切片灰度） |
| `EVIDENCE_GLOBAL_CONTEXT_MAX_BYTES` / 系统提示/回答预留 | `32000` / `4000` / `6000` |
| `EVIDENCE_ATTACHMENT_MAX_BYTES` / `EVIDENCE_KNOWLEDGE_BASE_MAX_BYTES` | `12000` / `16000` |
| `EVIDENCE_ATTACHMENT_REQUIRED_RESERVE_BYTES` / `_KNOWLEDGE_BASE_...` | `4000` / `4000` |
| `CIRCUIT_SEMANTIC_QUERY_ENABLED` | `true` |
| `SPREADSHEET_SKIP_BOILERPLATE_SHEETS` | `true` |

#### Agent / LLM

| 变量 | 默认 |
|---|---|
| `AGENT_LLM_PROVIDER` | `ollama`（`ollama\|custom`；`openai` 归一为 custom） |
| `AGENT_OLLAMA_BASE_URL` / `_MODEL` | `http://localhost:11434` / `qwen2.5:32b` |
| `AGENT_CUSTOM_API_KEY` / `_BASE_URL` / `_MODEL` | `""` |
| `AGENT_CUSTOM_MAX_TOKENS` | `4096` |
| `AGENT_MODEL_MAX_INPUT_TOKENS` | `65536`（0=关闭上下文压缩） |
| `AGENT_TEMPERATURE` / `AGENT_TIMEOUT_SECONDS` | `0.2` / `120` |
| `AGENT_RATE_LIMIT_MAX_RETRIES` | `4`（仅 custom） |
| `AGENT_RATE_LIMIT_INITIAL_DELAY_SECONDS` / `_MAX_DELAY_SECONDS` | `1` / `16` ⚠️（声明但未使用） |
| `LLM_MAX_CONCURRENCY` / `LLM_BATCH_MAX_CONCURRENCY` | `4` / `1` |
| `LLM_QUEUE_TIMEOUT_SECONDS` / `LLM_QUEUE_WARN_SECONDS` / `LLM_STREAM_IDLE_TIMEOUT_SECONDS` | `180` / `10` / `0` |
| `SYSTEM_PROMPT` / `NO_CONTEXT_PROMPT` | 内置中文 |
| `AGENT_CHECKPOINT_BACKEND` / `_DATABASE_URL` / `_POOL_SIZE` | `sqlite` / `""` / `4` |

#### 会话 / 控制面

| 变量 | 默认 |
|---|---|
| `CONVERSATION_SERIALIZE_SAME_THREAD` | `false` ⚠️（`claim_turn_result` 写的是 `getattr(..., True)`，但键总存在 → 实际 false） |
| `CHAT_TURN_HEARTBEAT_TTL_SECONDS` / `_INTERVAL_SECONDS` | `90` / `10` |
| `CHAT_TURN_LEASE_SECONDS` / `_RENEW_SECONDS` | `120` / `15` |
| `WAITING_TURN_MAX_SECONDS` / `_TIMEOUT_SCAN_SECONDS` | `600` / `5` |
| `CHAT_QUEUE_DEADLINE_SECONDS` | `600` |
| `CANCEL_FAST_PATH_ENABLED` / `CANCEL_KEY_TTL_SECONDS` | `true` / `7200` |
| `TURN_EVENT_STREAM_TTL_SECONDS` / `_MAXLEN` | `7200` / `5000` |
| `REDIS_REQUIRED` | `true`（硬依赖：Redis 未配置/不可达则 API 与 worker 拒绝启动，`/health/ready` 判 not_ready；仅测试隔离置 false） |
| `REDIS_URL` | `""`（项目自管实例由 `bash scripts/redis.sh up` 写入） |
| `CONTROL_PLANE_BACKEND` / `_DATABASE_URL` / `_POOL_SIZE` | `sqlite` / `""` / `8`（自管实例：`bash scripts/postgres.sh up` + `write-env`） |

#### 准入与资源治理

| 变量 | 默认 |
|---|---|
| `ADMISSION_CONTROL_ENABLED` | `true` |
| `ADMISSION_CHAT_RPM_PER_USER` / `_PER_TENANT` | `30` / `120` |
| `ADMISSION_HEAVY_RPM_PER_USER` / `_PER_TENANT` | `10` / `40` |
| `ADMISSION_RETRY_AFTER_SECONDS` | `5` |
| `ADMISSION_{DEEP,DOCUMENT,EXPORT,OCR,EVALUATION}_MODEL_RPM_PER_USER/_PER_TENANT` | `""`（继承） |
| `ADMISSION_MAX_UNTERMINAL_TURNS_PER_SESSION` / `_PER_USER` | `8` / `32` |
| `DISTRIBUTED_ADMISSION_ENABLED` / `_TTL_SECONDS` | `false` / `120` |
| `PROVIDER_RPM_LIMIT` / `PROVIDER_TPM_LIMIT` | `0`（关闭） |
| `RESOURCE_GOVERNOR_ENABLED` | `false` ⚠️（调用点用 `getattr(..., True)`，但键存在 → 实际 false，门是惰性的） |
| `RESOURCE_GOVERNOR_SCOPE` / `_BACKEND` / `_DATABASE_URL` / `_REQUIRE_PRECONFIGURED` / `_DB_PATH` / `_DEPLOYMENT_ID` / `_MAINTENANCE_SECONDS` | `process` / `sqlite` / `""` / `false` / `storage/resource_quotas.db` / `local` / `5` |
| `RESOURCE_QUEUE_MAX_WAIT_SECONDS` / `_MAX_DEPTH` | `30` / `256` |
| `RESOURCE_PERMIT_LEASE_SECONDS` / `_UNCERTAIN_ESCALATE_SECONDS` | `300` / `600` |
| `RESOURCE_LLM_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `2` |
| `RESOURCE_RAG_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `8` / `4` |
| `RESOURCE_TOOL_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `16` / `0` |
| `RESOURCE_CHAT_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `0` |
| `RESOURCE_HEAVY_JOB_MAX_CONCURRENT` / `_RESERVED_INTERACTIVE` | `4` / `1` |
| `RESOURCE_OCR_*` / `RESOURCE_EXPORT_*` / `RESOURCE_MEMORY_*` | `1/0` / `4/0` / `1/0` |
| `SHARED_RETRIEVAL_EXECUTOR_ENABLED` / `RETRIEVAL_EXECUTOR_MAX_WORKERS` / `_QUEUE_MAX_DEPTH` / `_PER_REQUEST_FANOUT` / `_QUEUE_MAX_WAIT_SECONDS` / `_SHUTDOWN_DEADLINE_SECONDS` | `false` / `8` / `256` / `4` / `30` / `10` |
| `ACTION_DAG_PARALLEL_ENABLED` / `ACTION_EXECUTOR_MAX_PARALLEL` | `false` / `4` |
| `REVIEWER_AGENT_ENABLED` / `RESEARCH_AGENT_ENABLED` | `false` / `false`（未接线） |

#### Worker

| 变量 | 默认 |
|---|---|
| `WORKER_ROLE` | `all`（`all\|chat\|authoring\|parse`） |
| `WORKER_POLL_INTERVAL_SECONDS` | `0.5` |
| `WORKER_PARSE_BATCH_SIZE` / `WORKER_CLAIM_BATCH_SIZE` / `WORKER_TURN_CONCURRENCY` | `1` / `8` / `1` |

#### 附件（`CHAT_ATTACHMENT_*`）

`CHAT_ATTACHMENTS_ENABLED=true`；单文件 50MiB / 每会话 20 个 / 会话 500MiB / 租户 5GiB；
`MAX_PAGES=300`、`MAX_ROWS=200000`、`MAX_CELLS=2000000`、`MAX_SHEETS=100`、
`MAX_SHARED_STRINGS=1000000`、`MAX_CELL_TEXT_LENGTH=100000`、`MAX_UNCOMPRESSED_BYTES=200MiB`；
`PARSE_TIMEOUT_SECONDS=120`、`RETENTION_SECONDS=2592000`；
`CONTEXT_MAX_TOKENS=8000`、`CONTEXT_RATIO=0.3`；
`FTS_ENABLED/TRIGRAM_ENABLED=true`；`DENSE_ENABLED=false`、`RETRIEVAL_MODE=lexical`、
`EMBEDDING_PROVIDER=openai_compatible`（base_url/model/api_key 空）、`DENSE_MAX_PARTS=4000`；
`OCR_ENABLED=false`、`OCR_MAX_PAGES=10`；
`VISUAL_ENABLED=false`、`REMOTE_INFERENCE_ALLOWED=false`、
`VISUAL_PROVIDER=volcengine_ark`、`VISUAL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`、
`VISUAL_MODEL=""`、`VISUAL_MAX_PAGES_PER_TURN=3`、`VISUAL_MAX_IMAGE_BYTES=8388608`、
`VISUAL_TIMEOUT_SECONDS=60`、`ARK_API_KEY=""`；
`JOB_LEASE_SECONDS=300`、`JOB_MAX_ATTEMPTS=3`。

#### 记忆（LangMem）

`MEMORY_ENABLED=true`、`STORE_BACKEND=sqlite`、`DATABASE_URL=""`、`SQLITE_PATH=storage/memory.db`、
`SQLITE_BUSY_TIMEOUT_MS=30000`、`SINGLE_WRITER=true`、`EXTRACTION_ENABLED=true`、
`DEBOUNCE_SECONDS=300`、`JOB_LEASE_SECONDS=180`、`JOB_MAX_RETRIES=5`、
`REFLECTION_TIMEOUT_SECONDS=120`、`REFLECTION_MIN_INTERVAL_SECONDS=0`、
`REFLECTION_CIRCUIT_FAILURES=5`、`REFLECTION_CIRCUIT_COOLDOWN_SECONDS=60`、
`PROJECTION_MAX_RETRIES=5`、`RECONCILE_INTERVAL_SECONDS=3600`、
`USER_TOP_K=3`、`PROJECT_TOP_K=5`、`STORE_OVERSAMPLE_FACTOR=4`、`STORE_MAX_SCAN=100`、
`CONTEXT_MAX_TOKENS=1800`、`ITEM_MAX_TOKENS=350`、`MIN_SCORE_ENABLED=false`、`MIN_SCORE=""`、
`MODEL_*=""`、`MODEL_REQUIRE_STRUCTURED_TOOLS=true`、`EMBEDDING_*=""`、
`INDEX_FIELDS=content.content,content.title,content.subject`、`RETENTION_DAYS=""`。
⚠️ 未被读取：`MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION`、`MEMORY_USER_MEMORY_OPT_IN`、
`MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT`。

#### 导出与外部对话

`RESULT_EXPORT_ENABLED=true` + 每格式开关 `true`；`ORGANIZE_ENABLED=true`、`ORGANIZE_MAX_CHARS=40000`、
`MAX_BYTES=26214400`、`JOB_LEASE_SECONDS=300`、`JOB_BATCH_SIZE=8`、`RETENTION_DAYS=30`、
`MAX_RUNNING_JOBS=4`；`RESULT_EXPORT_PDF_FONT`（未设置）。
`EXTERNAL_CONVERSATION_LLM_STRUCTURE=true`、`_SUMMARY=true`、`_MAX_CHARS=12000`、`_TIMEOUT_SECONDS=60`。

#### 文档生成

内置默认开启：`DOCUMENT_AUTHORING_AGENT_MODE_ENABLED`（external_agent，逐字段可降级）、
`AGENT_DOCUMENT_TOOLS_ENABLED`（chat 文档工具）。
灰度（默认关闭）：`DOCUMENT_REQUIREMENT_RESOLUTION_ENABLED`、`DOCUMENT_PLANNING_SHADOW_ENABLED`、
`DOCUMENT_PLANNING_V2_ENABLED`、`DOCUMENT_RISK_BASED_PLAN_GATE_ENABLED`、
`DOCUMENT_PLAN_DAG_EXECUTION_ENABLED`、`DOCUMENT_TEMPLATE_FREE_EXECUTION_ENABLED`、
`DOCUMENT_AUTHORING_COMPATIBILITY_CLOSURE_ENABLED`、`DOCUMENT_AUTO_ACTIVATE_SAFE_TEMPLATES`、
`DOCUMENT_AUTO_ACCEPT_AI_TEMPLATE_RECOMMENDATIONS`、`DOCUMENT_AUTO_PUBLISH_VERIFIED`；
默认开启：`DOCUMENT_TASK_WRITE_ENABLED/READ/STREAM=true`、`DOCUMENT_TASK_ASSOCIATION_REQUIRED=false`；
白名单 CSV：`DOCUMENT_PLAN_DAG_ALLOWLIST_{TENANTS,DOCUMENT_TYPES,FORMATS}`、
`DOCUMENT_TEMPLATE_FREE_ALLOWLIST_{TENANTS,DOCUMENT_TYPES,FORMATS,RECIPES}`；
其它：`DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=sqlite`、`_DATABASE_URL=""`、
`DOCUMENT_PREVIEW_RETRIEVAL_ATTEMPTS=3`、`_BACKOFF_SECONDS=0.5`、
`DOCUMENT_AUTHORING_JOB_LEASE_SECONDS=300`、`_BATCH_SIZE=4`、`_BATCH_TIME_BUDGET_SECONDS=10`；
`CONVERSATION_ACTION_PLAN_MODE=off`、`CONVERSATION_UNIFIED_TOOLS_ENABLED=true`（平台内置管线，非用户开关）。

#### 可观测性

生产默认开启观测：`OBS_ENABLED=true`、`OTEL_SERVICE_NAME=hardware-database-api`、
Docker 同网络 endpoint=`http://otel-collector:4317`（宿主进程使用 `http://127.0.0.1:4317`）、
`OBS_ENVIRONMENT=development`、
`OBS_SERVICE_VERSION=0.1.0`、`OBS_TRACE_SAMPLE_RATIO=1.0`、
`OBS_CAPTURE_CONTENT/QUERY/EVIDENCE/LLM_CONTENT=false`、`OBS_CONTENT_MAX_CHARS=50000`、
`OBS_LOG_FORMAT=json`、`OBS_{METRICS,TRACES,LOGS}_ENABLED=true`、
`OBS_PHOENIX_PROJECT=hardware-database`、`OBS_GRAFANA_BASE_URL=""`、`OBS_PHOENIX_BASE_URL=""`、
`OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS=10`、`OBS_WORKER_STALE_SECONDS=30`、
`OBS_DEPENDENCY_TIMEOUT_SECONDS=5`。

#### API / 通知 / 其它

`HDB_API_HOST=127.0.0.1`、`HDB_API_PORT=8001`、`HDB_API_CORS_ORIGINS=http://localhost:5176,http://127.0.0.1:5176`、
`HDB_API_SPAWN_WORKER=1`、`HDB_API_SPAWN_EXPORT_WORKER=1`、`HDB_API_SPAWN_MEMORY_WORKER=0`、
`HDB_API_MAX_UPLOAD_BYTES=536870912`；`NOTIFY_WEBHOOK_URL/TOKEN/TIMEOUT_SECONDS=""`/`""`/`2`；
`LOG_LEVEL=INFO`（直接读 env）；`HDB_WIKI_SEARCH_EXPAND`（未设置）；
`HDB_OPENCODE_SESSION_ID`（llm_headers）。

#### 评估（`src/evaluation/config.py`）

`EVAL_LLM_PROVIDER`（回落 `AGENT_LLM_PROVIDER`）、`EVAL_LLM_{BASE_URL,API_KEY,MODEL}`、
`EVAL_EMBEDDING_BASE_URL`/`_MODEL`（**必填**）、`EVAL_EMBEDDING_API_KEY`/`_DIMS`、
`EVAL_LLM_MAX_TOKENS=4096`、`EVAL_MAX_CONTEXTS_PER_SAMPLE=4`、`EVAL_MAX_CONTEXT_CHARS=4000`、
`EVAL_MAX_CONTEXT_CHARS_PER_ITEM=1200`、`EVAL_SCORING_MAX_BUDGET_ATTEMPTS=3`、
`EVAL_SCORING_CONTEXT_SHRINK_FACTOR=0.5`、`EVAL_TIMEOUT_SECONDS=60`、`EVAL_MAX_WORKERS=4`、
`EVAL_MAX_RETRIES=2`、`EVAL_OUTPUT_ROOT=storage/evaluations`、
`EVAL_LLM_FALLBACK_{BASE_URL,API_KEY,MODEL}`、`EVAL_COLLECT_RETRIES=1`。

---

## 2. SQLite 数据库与表

| DB | 路径 | 表 |
|---|---|---|
| 认证/治理 | `AUTH_DB_PATH` | `departments, users, auth_sessions, login_attempts, knowledge_bases, audit_events, query_traces, retrieved_evidence, wiki_pages, wiki_page_revisions, wiki_jobs, doc_assets, doc_asset_versions, doc_asset_events, doc_asset_links, hardware_assets, asset_candidates, asset_evidence, notifications, worker_instances, llm_usage_ledger`（+ 记忆 Catalog 表，见下） |
| 会话（同库） | `AUTH_DB_PATH` | `chat_sessions, chat_messages, chat_turns, chat_turn_events, chat_message_edits, turn_attachments, conversation_execution_state, action_receipts` |
| 记忆 Catalog（同库） | `AUTH_DB_PATH` | `memory_records, memory_revisions, memory_sources, memory_projections, memory_consent_events, memory_consent_source_items, user_memory_settings, memory_jobs, memory_checkpoints, memory_reflection_runs, memory_run_items, memory_projection_outbox, memory_deletion_outbox, memory_audit_events` |
| 记忆投影 | `MEMORY_SQLITE_PATH` | LangGraph `store`, `store_vectors`, `store_migrations`, `vector_migrations` |
| 解析台账 | `storage/pipeline_documents.db` | `pipeline_documents, pipeline_datasets, pipeline_document_profiles, pipeline_profile_outbox` |
| 表格索引 | `storage/table_indexes/departments/<dept>/kbs/<kb>/table_indexes.db` | `table_documents, table_sheets, table_rows, table_cells, table_text_blocks, table_semantic_rows, sql_table_registry` + 物化表 `t_<record>_<seq>` |
| 附件 | `storage/chat_attachments.db` | `chat_attachments, chat_attachment_assets, chat_attachment_parts, chat_attachment_embeddings, chat_attachment_visual_cache, chat_attachment_jobs, chat_attachment_revisions, chat_attachment_edit_proposals, session_cleanup_outbox, coordinator_leases` + FTS5 虚表 `attachment_parts_fts`, `attachment_parts_trigram` |
| 文档生成 | `storage/document_authoring.db` | 30+ 表（见 [14](14-authoring-render-and-api.md#41-work_order_storepydocument_authoringdb)） |
| 生成 checkpoint | `storage/document_authoring_checkpoints.sqlite` | LangGraph checkpoint |
| 项目 | `storage/projects.db` | `projects, project_knowledge_bindings, project_principal_bindings, source_assets, logical_documents, source_versions, processing_artifacts, project_source_bindings, project_baselines, source_region_policies, source_set_snapshots, schema_migrations` |
| Agent checkpoint | `storage/agent_checkpoints.db` | LangGraph `checkpoints`, `writes`, … + `hdb_checkpoint_attempts`, `hdb_checkpoint_pointers` |
| 资源配额 | `storage/resource_quotas.db` | 许可/队列/额度（governor） |
| 导出（同认证库） | `AUTH_DB_PATH` | `result_snapshots, result_export_jobs, result_export_outbox, result_artifacts` |
| 外部对话索引 | `storage/external_conversations/departments/<dept>/kbs/<kb>/index.db` | `conversations`, `messages` |
| 文档生成兼容性 | `storage/document_authoring_compatibility.sqlite` | 兼容性遥测/闭包 |
| 评估 | 文件系统 `storage/evaluations/` | JSON/JSONL/CSV/HTML（无 DB） |

**PRAGMA 约定**：几乎全部 `journal_mode=WAL` + `synchronous=NORMAL`；
`foreign_keys=ON` 视域而定（会话库、表格库、文档生成库开启；部分库为性能关闭）。
写事务统一 `BEGIN IMMEDIATE`（避免升级锁竞争）。

---

## 3. PostgreSQL 控制面

### 3.1 范围

启用 `CONTROL_PLANE_BACKEND=postgres` 后接管：会话执行协议（8 张核心表）+ 记忆账本 +
导出账本；**auth/审计/文档生成/附件字节刻意留在 SQLite**。

### 3.2 Schema 管理

- `SCHEMA_VERSION="1"`，`control_plane_schema(key PK, value, updated_at)`；
- `apply_schema(conn, include_indexes=False)` + `create_core_indexes`；
- `assert_schema_compatible(conn)` 在启动与 `/health/ready` 调用，**不匹配即 not-ready**；
- 核心表 DDL 手写（`CORE_TABLES`）：SQLite 整型 PK → `BIGINT GENERATED BY DEFAULT AS IDENTITY`；
  `*_json` → `JSONB`；`*_at` → `TIMESTAMPTZ`；0/1 → `BOOLEAN`；
  `chat_turns.status` 用 `CHECK` 约束；`chat_sessions` 折叠执行态列（`active_turn_id/lease_owner/`
  `lease_expires_at/fencing_token/generation`）；
- `PARTIAL_INDEX_OVERRIDES` 显式列出生成器无法翻译的部分唯一索引（记忆相关）；
- FK 在数据装载后添加（`CORE_FOREIGN_KEYS`）；identity 序列用 `CORE_IDENTITY_COLUMNS` 推进；
- 非核心表（导出、记忆等）由 loader 反射 SQLite 结构**生成 DDL**（`generated_table_ddl`）。

### 3.3 并发语义

- 时间一律用 DB 时钟 `clock_timestamp()`；租约 `make_interval(secs => %s)`；`_LEASE_SECONDS=120`；
- 锁顺序 `conversation_execution_state → chat_turns → chat_turn_events`；
- `FOR UPDATE SKIP LOCKED` 保证热点会话不阻塞其它会话；
- 事件追加单 CTE；claim/complete/fail/requeue 的实现见 [04](04-conversation-turns.md#7-postgresql-控制面)。

### 3.4 迁移

- `load_sqlite_snapshot(source_path, conn, *, tables=None, batch_size=500, dry_run=False, ...)`
  → 逐表复制 + 行数 + 内容哈希校验 + 权威性断言（状态合法、`turn_sequence` 非空唯一、
  `last_event_seq == COUNT(events)`、每会话有执行态、无孤儿 active turn）；
- 停机窗口强制（目标表非空即拒绝）；跳过 `_fts*` 派生表与 `worker_instances` 瞬态表；
- 演练与回滚工具见 [18](18-observability-and-ops.md#45-生产硬化脚本scriptsproduction_hardening)。

---

## 4. 磁盘布局（完整）

```
storage/
├── auth.db, auth.db-wal, auth.db-shm
├── pipeline_documents.db
├── memory.db
├── resource_quotas.db
├── agent_checkpoints.db
├── document_authoring.db
├── document_authoring_checkpoints.sqlite
├── document_authoring_compatibility.sqlite
├── projects.db
├── chat_attachments.db
├── pipeline_archives/departments/<dept>/kbs/<kb>/<source_group>/<file>
│   └── <kb>/<source_group>/<file>                     # 旧路径兼容
├── table_indexes/departments/<dept>/kbs/<kb>/
│   ├── table_indexes.db
│   └── .index-locks/root-<sha256>.lock
├── circuits/index.json + <kb>/<design_id>/{circuit_state.json, pipeline_metadata.json,
│   connectivity_graph.gpickle, datasheet_links.json, module_screenshots/, pdf_cache/}
├── external_conversations/departments/<dept>/kbs/<kb>/{index.db, <conversation_id>/{conversation.json, original.<ext>}}
├── test_data/<kb>/<report_id>/report.json
├── chat_attachments/{sources,circuits,table_indexes}/<session_id>/...
├── exports/artifact-<hex>.<ext>
├── document_authoring/                    # 模板与产物字节
├── evaluations/{uploads/, <run_id>/, e2e/}
├── chroma/                                # 仅在安装 chromadb 且绑定嵌入时
├── logs/rag_YYYY-MM-DD.log
├── run/                                   # hdb.sh pid 文件
├── excel_indexes/…                        # 旧版表格索引（删除文档时清理）
└── nodeenv/                               # hdb.sh 的便携 Node
```

权限：启动时 `_harden_storage_permissions()` 对 `.env` 与 `storage/*.db(-wal/-shm)` chmod 600；
导出目录 0700/文件 0600；附件源文件 0600。

---

## 5. 迁移、备份与保留

### 5.1 启动自动迁移

`AuthService._init_db`（角色归一、KB 共享模型、删旧权限表）、`ConversationService._init_db`
（`turn_sequence` 回填 + 唯一索引）、`AppLogService._init_db`（加列）、
`PipelineDocumentStore._init_db`（加列 + 部门唯一约束重建）、`TableIndexStore._ensure_columns`、
`DocumentAuthoringStore` 的库内迁移、`DocumentPlanningStore`、`ProjectStore`（`001_project_scope`）。
全部幂等，可重复执行。

### 5.2 备份/恢复

- SQLite 冷备：`VACUUM INTO 'backup.db'`（脚本已用）；
- `DocumentAuthoringStore.create_sqlite_backup` / `restore_sqlite_backup`：校验文件哈希 +
  `PRAGMA integrity_check`；
- 迁移前后用 `state_fingerprint.py` 记录 7 个状态 DB + 5 个数据目录的指纹；
- 大规模迁移走 `cutover_rehearsal.py`（含回滚循环）与 `migration_rollback_drill.py`。

### 5.3 保留策略汇总

| 对象 | 保留 | 机制 |
|---|---|---|
| `query_traces` / `retrieved_evidence` | 30 天 | 启动时一次性 prune（无配置键） |
| `audit_events` | **永久**（哈希链，无清理路径） | append-only |
| 导出产物字节 | `RESULT_EXPORT_RETENTION_DAYS=30` | 到期清字节、保留元数据与审计 |
| 附件 | `CHAT_ATTACHMENT_RETENTION_SECONDS=2592000` | 到期扫描 + 清理 outbox |
| 记忆 | `MEMORY_RETENTION_DAYS`（空=永久） | retention 扫描（保守策略） |
| 电路/表格/归档 | 无 TTL | 生命周期绑定台账行 |
| 日志文件 | 按日滚动（`rag_*.log`） | 无自动删除 |
| Tempo/Loki | 168h | 容器配置 |

---

## 6. 数据一致性要点

1. **一个事务内完成的事**：turn 回答 + 终态事件 + 导出快照/任务 + 记忆 outbox；
   文档生成的计划确认（plan/OutputSpec/submission 一起）；记忆的 candidate 提交（record/revision/
   sources/outbox 一起）。
2. **投影可重建**：记忆 Store、Agent checkpoint、文档生成 checkpoint、资源配额；
   其余为权威数据。
3. **文件与行强绑定**：附件 / 导出 / 电路 / 表格索引都遵循「先删行后删文件或反之有明确顺序」，
   失败会留下可重试状态而不是静默丢失。
4. **跨库无分布式事务**：附件清理用 outbox；会话删除先写 outbox 再删会话；
   删 KB 是「逐记录 + 目录清理」并上报 partial。
