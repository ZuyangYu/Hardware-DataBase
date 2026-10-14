# 18 · 可观测性、后台进程与运维

对应代码：`src/observability/`（1.8k 行）、`src/workers/main.py`（728 行）、
`src/core/{logger.py, app_logs.py, session_cleanup.py, notifications.py}`、
`src/maintenance/cli.py`、`deploy/observability/`、`scripts/`、`benchmarks/`、`tests/`。

---

## 1. 可观测性

### 1.1 OTel 引导（`bootstrap.py`）

`init_observability(service_name, *, service_version, environment, span_exporter=None, metric_exporter=None)`
——进程级一次性（`_LOCK`）；`OBS_ENABLED=false` 时标记已初始化但**不建 provider**。

Resource 属性：`service.name, service.version, deployment.environment.name,`
`openinference.project.name=OBS_PHOENIX_PROJECT, service.instance.id="{hostname}-{pid}",`
`host.name, process.pid`。

- Traces：`TracerProvider(resource, sampler=ParentBased(TraceIdRatioBased(OBS_TRACE_SAMPLE_RATIO)))`
  + `BatchSpanProcessor(RedactingSpanExporter(OTLPSpanExporter(...)))`；
- Metrics：`MeterProvider` + `PeriodicExportingMetricReader(OTLPMetricExporter)`；
- Logs：`LoggerProvider` + `BatchLogRecordProcessor(OTLPLogExporter)`；
  `RedactingLoggingHandler` 在副本上脱敏 body、extras、异常事件后导出；挂到 `RAG` logger；
- 客户端插桩：`HTTPXClientInstrumentor`、`RequestsInstrumentor`（各一次）；
- `instrument_fastapi(app)` 幂等（`app.state.hdb_fastapi_instrumented`）；
- `shutdown_observability()`：2s 超时强制 flush traces/metrics/logs 并摘除日志 handler；
- **所有导出失败 fail-open/异步**——遥测永不阻塞业务。

配置（`config.py` 读取当前 settings；provider 初始化参数重启生效）：`enabled, service_name, endpoint, environment,`
`service_version, sample_ratio(0..1), capture_content, capture_query, capture_evidence,`
`capture_llm_content, content_max_chars(≥1000), log_format, metrics_enabled, traces_enabled,`
`logs_enabled, phoenix_project`。

### 1.2 Span 约定（`tracing.py` / `semantic.py`）

- `OPENINFERENCE_SPAN_KIND = "openinference.span.kind"`；`SpanKind`：
  `AGENT, CHAIN, EVALUATOR, LLM, RETRIEVER, RERANKER, TOOL, PROMPT, EMBEDDING`；
- 工厂：`observe.agent/chain/evaluator/llm/retriever/reranker/tool/prompt(name, context=None, **attrs)`；
  `.llm()` 自动映射 `model→llm.model_name`、`provider→llm.provider`；
- `Observation` 上下文管理器：`.start()/.set(k,v)`（隐私过滤）`/.set_content/.set_input/.set_output`
  `/.outcome(status)`（success/completed/ok→OK；failed/error/cancelled→ERROR）`/.event/.error(exc)`
  （脱敏 exception event/message/stacktrace + `error.type`）`/.tokens(...)`（`llm.token_count.*` + `gen_ai.usage.*`）
  `/.set_token_usage(summary)` `/.hit_count(n)` `/.score(s)`；
- `QUERY_ATTRIBUTES = {hdb.query.mode, hdb.query.source, hdb.turn.id, hdb.session.id}`；
- 所有失败都被吞掉——遥测不是业务依赖。

### 1.3 指标目录（`metrics.py`）

开关：`OBS_ENABLED and OBS_METRICS_ENABLED`；属性键受 `METRIC_LABEL_KEYS ∪ {"format"}` 白名单限制、
值截断 80 字符（保证低基数）。

| 指标 | 类型 | 标签 | 记录点 |
|---|---|---|---|
| `hdb.queue.depth` / `hdb.queue.oldest_age` | gauge | `queue` | `set_queue_state` |
| `hdb.resource.active/queue.waiting/capacity/reserved/uncertain` | gauge | `resource` | `set_resource_state` |
| `hdb.chat.reserved_capacity` | gauge | — | 同上 |
| `hdb.memory.index_health` | gauge | `backend`,`semantic_index` | 记忆运行时 |
| `hdb.memory.projection_outbox_pending` / `projection_lag_seconds` | gauge | `queue=memory_projection` | 同上 |
| `hdb.chat.turns` | counter | `status`,`mode` | `record_chat_turn` |
| `hdb.chat.turn.duration` / `queue.wait` / `ttft` | histogram | `mode` | 同上 |
| `hdb.chat.queue_timeout` | counter | — | 排队超时收敛 |
| `hdb.agent.runs` / `duration` / `retrieval_rounds` / `step_count` / `cancel_count` | counter/histogram | `status`,`mode` | `record_agent*` |
| `hdb.agent.stage.completed` / `stage.duration` | counter/histogram | `stage`,`status` | `record_agent_stage` |
| `hdb.agent.tool_call_count` | counter | `tool`,`status` | `record_agent_tool_call` |
| `hdb.llm.calls` / `duration` / `ttft` / `timeout_total` / `tokens` / `active_requests` | counter/histogram/gauge | `provider`,`status`,`streaming`,`kind` | chat runtime |
| `hdb.worker.task.processed` / `failed` / `duration` | counter/histogram | `status` | `record_worker` |
| `hdb.attachment.events` / `duration` | counter/histogram | `event`,`status`,`scope` | `record_attachment` |
| `hdb.conversation.claims` / `lock.wait` / `concurrent_claim_blocked` / `attachment_wait` / `stale_write_rejected` | counter/histogram | `status`,`operation` | 会话服务 |
| `hdb.governor.lease_lost` | counter | `resource`,`reason` | 租约失守 |
| `hdb.rag.batch_fanout` / `rag.zero_hit` | histogram/counter | — | RAGFlow 客户端 |
| `hdb.job.wait_time` / `execution_time` / `dead_letter_count` | histogram/counter | `kind` | worker/job |
| `hdb.action.parallel.layers` / `layer_size` / `duration` | counter/histogram | `status` | action DAG |
| `hdb.retrieval.executor.attempts` / `queue_wait` | counter/histogram | `status` | 共享检索池 |
| `hdb.eval.runs` / `sample.duration` / `sample.failed` / `score.observations` / `score` | counter/histogram | `status`,`mode`,`metric` | 评估 |
| `hdb.authoring.runs` / `run.duration` / `unit.outcome` / `unit.duration` | counter/histogram | `status`,`operation` | 文档生成 |
| `hdb.document.authoring.compatibility.events` | counter | `kind`,`mode` | 兼容性遥测 |
| `hdb.authoring.agent.runs/duration` / `agent.tool.calls/duration` | counter/histogram | `status`,`mode`,`tool` | agent 模式 |
| `hdb.document.planning.shadow.runs/blocking_issues/issue_codes/validity/units/tables/duration` | counter/histogram | `status` | 影子计划（问题码只计数、不作标签） |
| `hdb.export.jobs` / `job.duration` / `queue.wait` / `render.duration` / `artifact.bytes` / `downloads` | counter/histogram | `format`,`status` | 导出 |
| `hdb.checkpoint.fork_latency` / `commit_latency` / `write_latency` | histogram | `reason` | attempt/观察器 |
| `hdb.memory.*`（jobs_total、job_lease_expired、job_replayed、jobs_failed、outbox_dead_letter、`reflection_*`、`search_*`、`context_*`、`write_latency`、`sqlite_busy_retries`、`postgres_retries`、`reconcile_*`、`injected`） | counter/histogram | — | 记忆 |
| `hdb.eval.governance_bypassed` | counter | — | 评估治理绕过 |

`set_active_state(name, description, count, unit)` / `incr_active(...)` 提供线程安全的动态活动 gauge。

### 1.4 隐私与脱敏（`privacy.py`）

- **永远脱敏的键名**：`authorization, api_key, apikey, cookie, password, secret, set-cookie, token`；
- 内容类别：`_QUERY_KEYS`（query/original_query/rewritten_query/question/user_query）、
  `_EVIDENCE_KEYS`（content/evidence/retrieved_text/document(s)/datasheet/spreadsheet/cell）、
  `_LLM_KEYS`（prompt/completion/input/output/messages/response）、`_INPUT_OUTPUT_KEYS`（`input.value`/`output.value`）；
- 门控：`allow_query_content() = OBS_CAPTURE_CONTENT or OBS_CAPTURE_QUERY`；
  evidence/llm 同理；`safe_span_attributes(attrs, content_kind=...)` 丢密钥、
  内容类未开启则丢弃、`input.value/output.value` 必须有 `content_kind`、
  内容截断到 `max(1000, OBS_CONTENT_MAX_CHARS)`、其它字符串 2000、列表 50×200；
- `redact_text(value)`：脱敏形如 `key=value` 的凭据并截断 8000 字符（日志与错误消息用）。

### 1.5 trace 上下文传播（`context.py`）

W3C `propagate.inject/extract`；`TraceIdentity(trace_id, span_id)`；`use_trace_context`；
`thread_with_current_context` / `start_thread_with_current_context`（把 contextvars 复制进线程）；
`submit_with_current_context(executor, fn, ...)`。
**持久化 turn 的 `trace_context_json` 就是 W3C carrier**——worker 用它把
`hdb.chat.turn` span 挂到原请求之下。

### 1.6 结构化日志

- `TraceContextFilter` 注入 `trace_id/span_id/service`；
- `StructuredJsonFormatter`：`{timestamp, level, service, trace_id, span_id, event, message(脱敏)}`
  + 可选 `retriever, duration_ms, hit_count, status, worker_id, run_id`；
- `core/logger.py`：logger 名 `RAG`；级别 `LOG_LEVEL`（默认 INFO，`apply_log_level()` 热重载）；
  JSON 控制台 + 每日文件 `storage/logs/rag_YYYY-MM-DD.log`；
- `query_error` ContextVar（`set/get/clear_query_error`）让后端传真实错误而不是靠文本嗅探。

### 1.7 健康检查（`health.py`）

| 函数 | 内容 |
|---|---|
| `check_live()` | `{status:"live", service}` |
| `check_ready()` | 依赖 `{database: AUTH_DB_PATH SELECT 1, control_plane: check_control_plane(), storage: STORAGE_DIR 存在且可写}`；全 up 才 ready；返回 `config_revision` 与 `restart_required_settings` |
| `check_dependencies()` | 追加 `worker`（注册表）、`ragflow`（`GET {url}/api/v1/datasets`，<500 视为 up）、`llm`（Ollama `/api/tags` 或 custom `/models`）；任一下线 → down；worker 为 0 → degraded |

`check_control_plane()`：sqlite 时 `SELECT 1`；postgres 时要求 DSN + `assert_schema_compatible`，
返回 `{status, backend, schema_version, latency_ms}` 或 `down`。
探测超时 `OBS_DEPENDENCY_TIMEOUT_SECONDS=5`；错误只暴露类型名。

### 1.8 Worker 注册表（`worker_registry.py`）

表 `worker_instances(worker_id PK, worker_type, hostname, pid, started_at, heartbeat_at,`
`current_task_kind, current_task_id, service_version)` + 心跳索引。
`register` / `heartbeat(task_kind, task_id)` / `unregister` / `list_workers(stale_after_seconds=OBS_WORKER_STALE_SECONDS=30)`。
**全部 fail-soft**。语义：只登记「活着」的 worker，过期判定基于心跳；
供 `/health/dependencies` 与系统状态页使用。

---

## 2. 后台 worker（`src/workers/main.py`）

### 2.1 角色

`WORKER_ROLE ∈ {all, chat, authoring, parse}`（默认 `all`，**仅开发**）：

| 角色 | 循环内容 |
|---|---|
| `chat` | claim 可执行的 chat turn（按顺序/租约）并执行 |
| `authoring` | 文档生成 job（`document_authoring_jobs`）+ 计划确认 submission 排空 |
| `parse` | `PipelineRuntime.run_once()` × `WORKER_PARSE_BATCH_SIZE` + `refresh_parse_statuses` |
| `all` | 以上全部 |

### 2.2 循环与节拍

- 心跳：`heartbeat(worker_id, task_kind=...)`，间隔 `OBS_WORKER_HEARTBEAT_INTERVAL_SECONDS=10`；
- 空闲睡眠 `WORKER_POLL_INTERVAL_SECONDS=0.5`；批大小 `WORKER_CLAIM_BATCH_SIZE=8`；
- **`.env` 监听**：启动时记录 mtime，任务边界检测到变化即 `reload_settings()`（worker 无需重启即可生效）；
- 优雅退出：SIGTERM/SIGINT → 完成当前任务后退出（未完成任务靠租约过期被其他 worker 接管）。

### 2.3 租约与恢复

- turn：`claim_turn_result` + 心跳续租（TTL 90s / 租约 120s）；
- 解析：`claim_next_parse_record`（`WORKER_STALE_SECONDS=1800` 回收）；
- 导出：`claim` + 心跳线程（见 [16](16-exports-and-external-conversations.md)）；
- 记忆：`claim_next` + 心跳线程 + 熔断（见 [11](11-memory.md)）；
- 附件：`claim_job` + watchdog + 协调器租约（见 [12](12-attachments.md)）；
- 文档生成：`claim_harness_run` + fencing（见 [13](13-document-authoring.md)）；
- 全局兜底：`requeue_stale_turns`（会话）、`_requeue_stuck_outbox`（记忆）、
  死信重排接口（解析）。

---

## 3. Redis（默认必需的实时平面）

**项目自管实例**：`bash scripts/redis.sh up` 起 `deploy/redis/docker-compose.yml`
（`redis:8-alpine`，仅宿主 `127.0.0.1:6380`，必填随机密码，AOF everysec，
内存 256mb + volatile-lru），健康后自动把 `REDIS_URL` 写入应用 `.env`；
随后 `bash scripts/hdb.sh restart`（重启生效项）。自检 `bash scripts/redis.sh test`。
详见 `deploy/redis/README.md`。

| 用途 | Key / 机制 | 配置 |
|---|---|---|
| 取消快路径 | `hdb:turn:cancel:{turn_id}`，TTL 7200 | `CANCEL_FAST_PATH_ENABLED=true` + `REDIS_URL` |
| 跨进程 SSE | `hdb:turn:events:{turn_id}` Stream，TTL 7200，maxlen 5000 | `TURN_EVENT_STREAM_TTL_SECONDS` / `_MAXLEN` |
| 分布式准入 L2 | 计数器 + TTL 120（**失败 fail-closed**） | `DISTRIBUTED_ADMISSION_ENABLED` |
| 提供商配额 | RPM/TPM 预算（0=关；开启后 Redis 不可用即拒绝） | `PROVIDER_RPM_LIMIT` / `PROVIDER_TPM_LIMIT` |

`REDIS_REQUIRED=true`（默认）时未配置/不可达会拒绝启动并使 `/health/ready` 报 not_ready。
仅当测试显式设为 false 时，取消可退回 DB 轮询、SSE 退回进程队列 + DB 重放。
`/health/dependencies` 的 Redis 项仍报告 `up/degraded/not_configured` 供诊断。

---

## 4. 部署与运行手册

### 4.1 单机

```bash
uv sync --all-groups                      # dev 组：pytest + ruff
uv run hardware-database-server           # API 8001（默认自动拉起 worker + export worker）
cd frontend && npm ci && npm run dev      # 5176，/api 代理到 8001
```

可选 `bash scripts/hdb.sh start|stop|restart|status|logs`：
白名单读取 `.env` 的 `HDB_API_HOST/PORT`、`HDB_FRONT_PORT`、`HDB_WORKER_ROLES`；
强制 `HDB_API_SPAWN_WORKER=0` / `HDB_API_SPAWN_EXPORT_WORKER=0` / `HDB_API_SPAWN_MEMORY_WORKER=0`；
按角色启动 `worker-chat` / `worker-authoring` / `worker-parse` / `memory-worker` / `export-worker`；
pid 在 `storage/run/`，日志在 `storage/logs/`；前端用 `storage/nodeenv`（便携 Node）。

实时面（必需）：`bash scripts/redis.sh up` 起项目自管 Redis（`127.0.0.1:6380`），
健康后自动写 `REDIS_URL` 到 `.env`，再 `bash scripts/hdb.sh restart` 生效；见 §3。
`REDIS_REQUIRED=true`（默认）时 Redis 未配置/不可达会拒绝启动（API 与 worker），
`/health/ready` 也判 not_ready —— 不提供降级成 DB 轮询的运行形态。

可选账本库：`bash scripts/postgres.sh up` 起项目自管 PostgreSQL
（`deploy/postgres`，pgvector 镜像，`127.0.0.1:55433`），
`bash scripts/postgres.sh write-env` 一次写入控制面/记忆/检查点/配额五组后端与 DSN，
再 `bash scripts/hdb.sh restart`；首次切换先跑 `cutover_rehearsal.py` 预演
（见 `deploy/postgres/README.md`）。切换后 `/health/ready` 的 `control_plane`
应报 `backend=postgres` + `schema_version`。

### 4.2 多实例（检查清单）

1. `HDB_API_SPAWN_*=0`（全部）；
2. `CONTROL_PLANE_BACKEND=postgres` + DSN（池 `CONTROL_PLANE_POOL_SIZE=8`）；
3. `AGENT_CHECKPOINT_BACKEND=postgres`、`DOCUMENT_AUTHORING_CHECKPOINTER_BACKEND=postgres`、
   `MEMORY_STORE_BACKEND=postgres`；
4. `RESOURCE_GOVERNOR_SCOPE=deployment` + `RESOURCE_GOVERNOR_BACKEND=postgres`；
5. `REDIS_URL`（`bash scripts/redis.sh up`；跨机多实例需改指向所有副本可达的外部实例）
   + `DISTRIBUTED_ADMISSION_ENABLED=true`；
6. **只跑一个** memory worker（`MEMORY_SINGLE_WRITER=true`）；
7. 所有实例共享同一 `storage/`（附件/归档/导出/索引均为文件 + SQLite）；
8. 先跑 `connection_budget.py` 校验 PG 连接预算；
9. `HDB_API_CORS_ORIGINS` 设为真实前端域名。

### 4.3 可观测栈

```bash
docker compose -f deploy/observability/docker-compose.yml up -d
```

服务：`otel-collector`(4317/4318/9464)、`prometheus`(9090，15s 抓取 + 规则)、
`tempo`(168h)、`loki`(168h)、`grafana`(3000，预置 2 个 dashboard)、`phoenix`(6006)+`phoenix-db`。
告警：6 条 SLO（chat 错误率 >10%、p95 >8s、队列 >100、worker 失败、检索零命中 >30%、LLM p95 >20s）
+ 9 条 hardening（陈旧权威写入=critical、资源拒绝/队列/不确定/升级、附件等待超时、
重任务队列饥饿 >600s、检索执行器 shedding、action 并行失败）。
包含 `alertmanager`（9093）。默认 receiver 展示告警；真实通知需要配置收件端。
错误率只计 `failed/(completed+failed)`，用户取消不算服务错误，也不稀释错误率；
检索零命中率直接以检索调用数为分母。无流量时不触发比例告警。

### 4.3.2 通知接入

提供 `alertmanager/alertmanager.webhook.yml` 和 `docker-compose.alerting.yml`。
收件端须接受 Alertmanager webhook 格式。部署前将通知系统提供的 URL/token 写入部署机
专用目录中的 `alert-webhook-url`、`alert-webhook-token`（文件来自部署环境，勿写入仓库）。
导出两个绝对路径环境变量：

```bash
export ALERTMANAGER_CONFIG="$PWD/deploy/observability/alertmanager/alertmanager.webhook.yml"
export ALERTMANAGER_SECRETS_DIR="/etc/hdb/alertmanager-secrets"
docker run --rm --entrypoint /bin/amtool \
  -v "$ALERTMANAGER_CONFIG:/work/alertmanager.yml:ro" \
  -v "$ALERTMANAGER_SECRETS_DIR:/run/secrets:ro" \
  prom/alertmanager:v0.28.1 check-config /work/alertmanager.yml
docker compose -f deploy/observability/docker-compose.yml \
  -f deploy/observability/docker-compose.alerting.yml up -d alertmanager
```

应用一体化部署时把第一个 compose 文件替换为 `deploy/docker/docker-compose.yml`，
并同时加载 `deploy/docker/docker-compose.observability.yml` 和上述 alerting 覆盖文件。
用通知系统测试 URL 验证 firing 与 resolved 均送达；Alertmanager UI 同时检查 receiver。

规则验证和低流量/取消回归（CI 自动执行）：

```bash
docker run --rm --entrypoint /bin/promtool \
  -v "$PWD/deploy/observability/prometheus:/work:ro" -w /work \
  prom/prometheus:v3.5.0 test rules tests/rules.test.yml
```

秒级直方图使用 10ms 至 600s 的专用桶。HTTP 依赖探针只把 2xx 视为成功，
禁止跟随重定向；custom LLM `/models` 使用配置的 Bearer 鉴权。

### 4.4 维护命令

```bash
uv run hardware-database-maintenance reparse-attachments --dry-run --limit 200
uv run hardware-database-maintenance compatibility-readiness
uv run hardware-database-export-worker          # 多实例时单独运行
uv run hardware-database-memory-worker
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
```

### 4.3.1 探针缓存

`/health/ready` 带短 TTL 缓存（`OBS_READINESS_CACHE_SECONDS`，默认 1 秒）：
K8s/负载均衡每几秒探一次时，无缓存会每次都真查 SQLite + PG + storage；
缓存以 `CONFIG_REVISION` 为键，改配置立即重探，真实故障仍在 ~1 秒内暴露。
`/health/live` 与 `/health/dependencies`（管理员页）不缓存。

### 4.4.1 备份/恢复（`scripts/backup.sh`）

数据端是两个物理对象（PG 控制面 + `storage/`），备份合成一条命令：

```bash
bash scripts/backup.sh create                     # 全量：pg_dump(-Fc) + storage tar，默认留 7 份
bash scripts/backup.sh create --lean              # 排除可重建数据（索引/checkpoint/投影）
bash scripts/backup.sh create --skip-pg           # 纯 SQLite 部署
bash scripts/backup.sh restore backups/storage-20260922-120000.tar.gz   # 破坏性，交互确认
bash scripts/backup.sh list
```

- `create` 先停止正在运行的应用写入服务，保持 PG/Redis 运行；PG dump 与 storage
  在同一停写窗口生成，完成后恢复原运行服务；校验文件随备份生成。
- `restore` 会把现有 `storage/` 挪到 `storage.pre-restore-<ts>`，并用
  `pg_restore --clean --if-exists --single-transaction --exit-on-error` 恢复控制面库。
  解包、复制 dump 或 PG 恢复失败时回滚 storage 并保持应用停止；成功才恢复原服务。
  连接中断时 PG 结果可能不确定，启动前须确认 PG 与 storage 属于同一批次。
- 可重建数据（`table_indexes/`、`circuits/`、`agent_checkpoints.db` 等）`--lean`
  时不备份，恢复后随首次解析重建；`auth.db`/`pipeline_documents.db`/
  `chat_attachments.db`/`document_authoring.db`/`projects.db`/`exports` 永远是全量。

### 4.5 生产硬化脚本（`scripts/production_hardening/`）

| 脚本 | 用途 |
|---|---|
| `state_fingerprint.py` | 迁移前后：7 个状态 DB + 5 个数据目录的行数与 SHA-256 指纹 |
| `migrate_sqlite_to_postgres.py` | 分组迁移（authority/projection/attachments/pipeline/compatibility），类型映射、幂等、行数+哈希校验 |
| `load_sqlite_into_postgres.py` | 控制面停机窗口加载（生成 schema + 权威性断言） |
| `cutover_rehearsal.py` | 7 步演练：preflight → `VACUUM INTO` → migrate → verify → store smoke → rollback cycle → JSON 报告 |
| `migration_rollback_drill.py` | 证明源库哈希不变、二次执行幂等、派生表跳过；支持 `--kill-after` SIGKILL 恢复 |
| `migrate_chat_turn_sequence.py` | 扩列→回填→校验→NOT NULL（生产需停写） |
| `verify_checkpoint_postgres.py` | PG checkpointer setup/put/get 一致性、fork 带 pending writes、删除隔离 |
| `multi_instance_drill.py` | 双 API 副本：单 owner claim/fencing、共享会话、跨副本恢复、附件字节共享 |
| `fault_isolation_drill.py` | 杀 authoring/parse/chat worker + 指向不可达 RAGFlow，断言聊天仍可完成 |
| `concurrency_compare_drill.py` | A/B（串行化+governor+共享执行器 关/开）跨用户负载、隔离、同会话突发顺序、429 过载 |
| `control_plane_benchmark.py` | PG claim 吞吐/延迟、8 路单 owner 竞争、热会话 SKIP LOCKED 隔离 |
| `connection_budget.py` | 汇总每副本池大小 vs `max_connections`/`pg_stat_activity` |
| `load_probe.py` | 固定到达率探测（health 或完整 turn），输出延迟分位与 shedding 计数 |
| `governor_recovery.py` | `list/show/reconcile/release` 部署许可（release 需服务端验证证据 + 审计） |
| `regression_runner.py` | 固定 pytest 集合 + 冻结数据集哈希 + git commit → 基线 JSON（无 LLM/网络） |
| `reviewer_eval.py` | reviewer 子代理的确定性 precision/recall/状态准确率（`--model` 加语义） |
| `prepare_adas_new_eval.py` | 幂等注册远端 ADAS_new 文档并索引本地 EDF 供 RAGAS 用 |

`scripts/import_evaluation_history.py`：历史评估运行目录的 dry-run-first 原子导入
（目标必须属当前 euid 且非组/世界可写）。

---

## 5. 测试策略

### 5.1 规模与约定

- `tests/` 共 **307 个 Python 测试模块**（281 个扁平 `test_*.py` + `tests/core/` 3 个 +
  `tests/evaluation/` 23 个）+ 1 个 shell 测试；扁平平铺、多为 `unittest.TestCase`，pytest 作为 runner；
- `norecursedirs = ["build", "src/circuit/vendor/spydrnet"]`；
- `tests/conftest.py` 在模块收集之前使用临时 storage、SQLite 与空配置，保证类级 API
  启动不触碰本地部署；测试级 fixture 自动关闭准入控制并重置单例；
  `document_gating_env.py` 固定治理类自动发布开关为安全默认；
- 运行：`uv run python -m pytest`（全量）、单文件、`::Class::test` 单测；
  `uv run ruff check .` 静态检查（vendor 与 `.claude` 排除）。
- `.github/workflows/ci.yml`：锁文件、ruff、全量后端（一次性 PG/Redis）、前端测试与构建、告警规则；
  真实模型、浏览器和容量验收入口见 [23](23-reliability-improvement-plan.md)。

### 5.2 外部依赖的门控

| 依赖 | 门控 |
|---|---|
| Postgres | `HDB_TEST_POSTGRES_DSN` 存在才跑（约 14 个文件：控制面 schema/store/health/factory/read-parity、记忆 enqueue/worker/e2e/invalidation、governor、checkpointer） |
| Redis | `HDB_TEST_REDIS_URL`（默认 `redis://127.0.0.1:6379/15`）可达才跑；否则 skip（分布式准入、redis events/cancel、provider quota）。自管实例用 `bash scripts/redis.sh test` 直接指定 |
| PostgreSQL | `HDB_TEST_POSTGRES_DSN` 存在才跑；**必须指向一次性库**（如 `hdb_rehearsal`），测试会建库/删 schema。注意 `tests/conftest.py` 会把五个后端强制回落 sqlite（settings + `os.environ`，子进程同样继承），**防止 `.env` 指向生产 PG 时测试写入真实数据** |
| LLM/RAGFlow | 一律 fake：monkeypatch `build_governed_model`、`_Model`/`RunnableLambda` 替身、`FakeLLM`；真实 smoke 需 `AGENT_REAL_SMOKE_OLLAMA=1` / `AGENT_REAL_SMOKE_OPENAI=1` |
| RAGAS | 需安装 `ragas`/`openai`/`langchain_openai` 且配置 `EVAL_*`，否则 skip |

### 5.3 关键测试与保护的不变量（精选）

| 测试 | 不变量 |
|---|---|
| `test_chat_turn_sequence.py` | `turn_sequence` 的 schema/分配/幂等/旧库回填；每会话单调 |
| `test_chat_turn_claim_fencing.py` | 每 turn 单一 owner；租约续期；陈旧写入拒绝 |
| `test_multiprocess_sqlite_contention.py` | 真子进程竞争：只有一个能 claim、token=1、协调器单一 owner、配额许可互斥 |
| `test_resource_governor.py` / `_postgres.py` | 治理/拒绝/配额租约/令牌桶/批量保留；PG 上同契约 |
| `test_control_plane_postgres.py` | 真 PG schema + loader；失败迁移可通过重建目标回滚 |
| `test_control_plane_store_contract.py` | SQLite 与 PG 执行存储的行为一致性（有序 claim、fencing、终态事务 + domain writer、陈旧重排） |
| `test_memory_security_contract.py` | 记忆始终在 `<untrusted_memory>` 内；注入载荷无法逃逸 |
| `test_auth_kb_scope.py` | 两级角色：sysadmin 不能访问 KB 内容；共享/所有权；旧角色迁移与权限表删除 |
| `test_document_authoring_safety.py` + `test_document_generation_*` | 声明单元之外不得有模板/脚本执行；治理评审门 |
| `test_provider_config_guard.py` / `test_settings_env_loading.py` | backend=postgres 无 DSN 拒绝启动；`.env` 重载正确 |
| `test_migration_validation.py` | 迁移哈希/类型工具正确（派生表跳过、哈希稳定） |
| `test_result_export.py` / `_followup.py` | 快照→产物的持久承诺在队列/渲染器重构后仍成立（真实渲染器 + OOXML 校验） |

其它重点：`test_waiting_turn_timeout.py`、`test_worker_roles.py`、`test_shared_retrieval_executor.py`、
`test_distributed_admission.py`、`test_conversation_led_document_*_e2e.py`、`test_memory_postgres_runtime.py`。

### 5.4 基准（与 RAGAS 独立，`benchmarks/spreadsheet/`）

- 数据集 `golden_qa.json` v1.1：30 题（point_lookup 8 / filter 7 / comparison 5 / aggregation 5 /
  multi_condition 5），答案对照本地 7 个 `AAA/*.xlsx`（gitignored）人工核验；
- `run_eval.py`：解析保真度 + 检索 recall@k/MRR（与表格工具同一条 token/like/rank 路径）；
  产出 `latest_report.json`，冻结基线 `baseline_report.json`；
- `run_answer_eval.py`：端到端 A/B（arm a = 禁用 SQL 工具，arm b = 全工具），
  确定性 `must_include`/`REGEX:` 评分，temperature=0，可续跑；
- `verify_sql_path.py`：18 条参考 SQL 走真实只读校验/执行路径；
- `wait_and_probe.py` / `wait_and_resume_a.py`：限流窗口等待与自动续跑。

`.venv/bin/python benchmarks/spreadsheet/<script>.py`。

---

## 6. 运维排障速查

| 症状 | 检查 |
|---|---|
| `/health/ready` 不 ready | `dependencies` 里哪一项 down：`database`（SQLite 锁/权限）、`control_plane`（PG DSN/schema 版本）、`storage`（可写） |
| `/health/dependencies` worker 为 0 | 没起 worker 或心跳过期（`OBS_WORKER_STALE_SECONDS=30`）；看 `storage/logs/` |
| 日志里 `hdb.governor.lease_lost` 频繁 | 任务超过租约且心跳失败：看 worker 是否被 SIGKILL、时钟是否漂移、DB 是否被锁 |
| Prometheus 告警 `HdbQueueOldestJobWaiting` | 队列最老任务 >600s：看导出/文档生成 worker 是否在跑、是否有永久失败任务 |
| PG 连接打满 | `connection_budget.py`；检查 checkpoint 池在 worker 上被计两次 |
| `.env` 改动未生效 | 重启项清单（`/health/ready`）；worker 需要任务边界才 reload |
| 遥测无数据 | `OBS_ENABLED`、endpoint 可达性；导出失败是 fail-open，业务日志里通常只有 debug |
| 存储权限告警 | 启动会 chmod 600 `.env` 与 `storage/*.db`；若失败说明目录权限不允许 |
| 迁移后行为异常 | `state_fingerprint.py` 对比；`migration_rollback_drill.py` 验证可回滚 |
