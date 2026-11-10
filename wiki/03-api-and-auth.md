# 03 · API 层与鉴权治理

对应代码：`src/api/`（10.7k 行）、`src/core/auth.py`（1246 行）、`src/core/app_logs.py`（1011 行）。

---

## 1. FastAPI 应用构造

### 1.1 入口与生命周期

```python
# src/api/app.py
def create_app() -> FastAPI          # 工厂函数（uvicorn --factory 可用）
def main()                            # console script hardware-database-server
```

`lifespan` 启动序列（顺序重要）：

1. `validate_settings_values` + `validate_provider_configuration`（fail-closed，见 §5.2）；
2. `init_observability(...)` → `instrument_fastapi(app)`（幂等，标记 `app.state.hdb_fastapi_instrumented`）；
3. 域 schema 初始化：`AuthService._init_db`（含角色迁移/KB 共享迁移）、`ConversationService._init_db`
   （含 `turn_sequence` 回填）、`AppLogService._init_db`、`WikiService`、`DocumentAssetService`、
   `ensure_memory_schema`、附件 store；
4. `_prune_query_traces_once()`：默认清理 30 天前的 `query_traces`/`retrieved_evidence`；
5. `_harden_storage_permissions()`：`.env` 与 `storage/*.db(-wal/-shm)` chmod 600；
6. 资源治理维护线程 + 可选共享检索池；
7. `_spawn_children()`：按 `HDB_API_SPAWN_{WORKER,EXPORT_WORKER,MEMORY_WORKER}` 启动子进程
   （`python -m src.workers.main` / `src.result_exports.worker` / `src.memory.worker`）；
8. shutdown：先子进程（5s 宽限后 SIGKILL）→ governor → `reset_pipeline()` → `shutdown_observability()`。

### 1.2 中间件与横切

| 项 | 实现 |
|---|---|
| CORS | `CORSMiddleware`，来源来自 `HDB_API_CORS_ORIGINS`（逗号分隔；`.env.example` 给 `http://localhost:5176,http://127.0.0.1:5176`）。**生产必须改为真实前端域名** |
| 异常映射 | `install_error_handlers`：`PermissionError→403`、`ValueError→400`、`RuntimeError→500`，body `{"detail": str(exc)}` |
| 请求日志 | uvicorn access log + OTel server span + JSON 日志（`trace_id/span_id` 注入） |
| 请求 ID | 无 `X-Request-ID` 中间件；关联性依赖 OTel trace（`otel_trace_id/otel_span_id` 落库到 query trace） |
| 探针 | `/health`、`/health/live`、`/health/ready`、`/health/dependencies` 挂在根路径 |

### 1.3 路由注册与端点清单

所有业务路由挂 `/api/v1`。下表按模块列出**全部端点**（`auth` 依赖列标注权限要求）。

#### 认证 `routes/auth.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/login` | `{username,password}` → `{token,user}`；失败锁定见 §2.4 |
| POST | `/api/v1/logout` | 撤销当前 token（审计 `logout`） |
| GET | `/api/v1/whoami` | 返回当前 `AuthUser`（含部门名） |

#### 会话与轮次 `routes/conversations.py` / `routes/query.py`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/v1/conversations` | 列出/创建会话（`kb_name` 必填，`__general__` 为通用对话） |
| GET/DELETE | `/api/v1/conversations/{id}` | 详情（含记忆设置）/ 删除（先写附件清理 outbox） |
| PATCH | `/api/v1/conversations/{sid}/messages/{mid}` | 编辑/脱敏消息（触发记忆失效重建） |
| GET | `/api/v1/conversations/{id}/messages` | 消息列表（含 footer/turn summary/附件快照） |
| GET | `/api/v1/conversations/{id}/turns` | 轮次列表（`include_terminal` 用于历史回填） |
| POST | `/api/v1/conversations/{id}/turns` | **创建 turn**（幂等：`client_request_id`） |
| POST | `/api/v1/turns/{turn_id}/start` | 幂等触发 worker（生产可 no-op） |
| POST | `/api/v1/turns/{turn_id}/cancel` | 请求取消（协作式） |
| GET | `/api/v1/turns/{turn_id}` | 轮次状态 |
| GET | `/api/v1/turns/{turn_id}/events` | **SSE**事件流（`Last-Event-ID`/`after` 续传） |
| GET | `/api/v1/conversations/{id}/memory-summary` | 记忆抽取统计 |
| PUT | `/api/v1/conversations/{id}/memory-settings` | 会话级自动抽取开关 |

#### 知识库与文件 `routes/kbs.py` / `files.py` / `upload.py` / `kb_assign.py`

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/kbs` | 任意登录 | 可访问 KB 列表 + 权限 + 所有者/共享状态 |
| POST | `/api/v1/kbs` | employee | 创建 KB（默认私有，owner=创建者） |
| DELETE | `/api/v1/kbs/{kb}` | 仅 owner | 删库（先删文档，再清理本地资产；部分失败会如实上报） |
| PATCH | `/api/v1/kbs/{kb}/sharing` | 仅 owner | 部门共享开关（审计 `update_kb_sharing`） |
| PUT | `/api/v1/kbs/{name}/assign` | system_admin | KB 挂载到部门/指定 owner；**禁止跨部门迁移** |
| GET | `/api/v1/kbs/{kb}/files` | read | 文件列表（合并解析状态） |
| POST | `/api/v1/kbs/{kb}/files` | write | **上传**（multipart，分片写盘，上限 `HDB_API_MAX_UPLOAD_BYTES`=512MiB，超限 413） |
| DELETE | `/api/v1/kbs/{kb}/files/{file_id}` | admin | 删除文档（远端 + 归档 + 台账；shared 库记录 `is_shared/actor_is_owner`） |
| GET | `/api/v1/kbs/{kb}/files/{file_id}/chunks` | read | 解析分块预览 |
| GET/DELETE | `/api/v1/kbs/{kb}/parse-tasks[/{id}]` | write | 解析任务面板；`/finished` 需注册在 `{id}` 之前避免路径吞并；`/requeue-dead-letters` 重排队 |

#### 结构化数据 `routes/structured.py`

`GET /kbs/{kb}/structured/{spreadsheets, circuit-designs, circuit-designs/{id}, circuit-designs/{id}/parse-log, modules, schematics, schematics/{id}/pages/{page}, test-reports, test-measurements}`
——只读视图，全部要求 KB read。外部对话 4 个端点见 [16](16-exports-and-external-conversations.md)。

#### Wiki / 资产 `routes/wiki.py` / `doc_assets.py` / `assets.py`

见 [15-wiki-assets-projects.md](15-wiki-assets-projects.md)。要点：Wiki 的 `{slug:path}` 路由支持多段 slug。

#### 记忆 `routes/memories.py`

见 [11-memory.md](11-memory.md)（13 个端点，含 consent 与 settings）。

#### 附件 `routes/attachments.py`

见 [12-attachments.md](12-attachments.md)（11 个端点）。

#### 导出 `routes/exports.py`

见 [16](16-exports-and-external-conversations.md)（12 个端点，含 `/artifacts/document/{id}/*` 统一协议）。

#### 管理 `routes/{users,departments,config,logs,governance,status,evaluation,notifications}.py`

| 模块 | 端点要点 |
|---|---|
| `users` | `GET/POST /users`，`PUT /users/{id}/active`、`PUT /users/{id}/password`（全部 system_admin；不能操作自己） |
| `departments` | `GET/POST /departments`、`DELETE /departments/{id}`（可删性：无用户/无 KB 引用） |
| `config` | `GET/PUT /config`（PUT 校验→写 `.env`→重载→清模型缓存；审计 `change_settings`）、`GET /health/ragflow`、`GET /health/llm` |
| `logs` | `GET /logs/audit[+stats/actions]`、`GET /logs/query[+stats]`、`GET /logs/query/{id}/evidence`（scoped + 非 owner 脱敏） |
| `governance` | `GET /governance/kb-summaries` |
| `status` | `GET /system/status`（依赖 + LLM 并发 + token 用量 + 24h 任务） |
| `evaluation` | 11 个端点，全部 `require_system_admin`，见 [17](17-evaluation.md#16-apiroutesevaluationpy) |
| `notifications` | `GET /notifications`、`POST /notifications/{id}/read`、`POST /notifications/read-all` |

### 1.4 错误映射约定

- **全局**：`PermissionError→403`、`ValueError→400`、`RuntimeError→500`。
- **路由级覆盖**（语义不同处显式捕获）：
  - 导出：非 completed turn → **409** `only completed turns can be exported`；格式关闭 → 409；
    options 过大/未知键 → 400；artifact 不存在 → 404；
  - 附件：NotFound/Permission → 404、UnsupportedType → 400、Quota → **413**；
  - 记忆：PermissionError→403、LookupError→404、RuntimeError/ValueError→**409**（CAS/版本冲突不进 500）；
  - 评估：参数/状态 → 400/409、运行不存在 → 404、`run_state.json` 损坏 → 500、
    严格基线不可比 → **409** 并携带 `{message,warnings,compatibility}`。
- **turn 内部错误不是 HTTP**：`_run_turn` 捕获异常 → `friendly_error_message` → turn 标记 `failed`，
  同时发 `error` + `turn_end{failed}` 事件；原始异常进日志/OTel。

`friendly_error_message`（`src/core/error_friendly.py`）文案映射：

| 匹配模式 | 返回文案 |
|---|---|
| `429\|rate limit\|usage limit` | 模型服务调用过于频繁或额度受限，请稍后重试。 |
| `streaming response failed` 等上游 5xx 文案 | 模型服务暂时不可用，请稍后重新发送本问题。 |
| `timeout\|timed out` | 模型服务响应超时，请稍后重试。 |
| `connection\|unavailable` | 无法连接模型服务，请检查网络或稍后重试。 |
| `PermissionError` | 没有执行该操作的权限。 |
| 其它 | 系统错误，请稍后重试；若持续出现请联系系统管理员。 |

准入相关 429 原因码：`rate_limit_user` / `rate_limit_tenant` / `rate_limit_model` /
`queue_full` / `queue_state_unavailable` / `admission_unavailable`；
积压 429：`session_backlog` / `user_backlog`；turn 事件原因：`chat_queue_timeout` / `attachment_wait_timeout`。

### 1.5 上传实现（`routes/upload.py`）

1. `_require_kb_permission(user, kb, "write")` + 拒绝 system_admin；
2. 分片读 `UploadFile`（1 MiB/片）写临时文件，累计超 `HDB_API_MAX_UPLOAD_BYTES`（512 MiB）→ 413；
3. 组装文件列表交给 `AppPipeline.upload_files` → 逐文件返回 `[success]/[warning]/[failed]/[skipped]` 消息；
4. 上传路由本身不解析文件——解析是 worker 的职责。

### 1.6 知识库目录树与文件夹上传

目录属于 Pipeline Domain，根目录使用虚拟 `directory_id=0`；目录权限继承
知识库权限，不增加目录级 ACL：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/v1/kbs/{kb}/directories?parent_id=0` | read | 查询直接子目录，返回派生 `path` 和计数 |
| POST | `/api/v1/kbs/{kb}/directories` | write | 创建目录，body 为 `{parent_id, name}` |
| PATCH | `/api/v1/kbs/{kb}/directories/{directory_id}` | admin | 重命名；带 `parent_id` 时移动目录 |
| DELETE | `/api/v1/kbs/{kb}/directories/{directory_id}` | admin | 默认仅删除空目录；`recursive=true&confirm=true` 递归删除 |

文件列表支持：

```text
GET /api/v1/kbs/{kb}/files?directory_id=0&recursive=false&query=&limit=100&offset=0
```

`FileView.name` 仍是 basename；`directory_id`、`relative_path`、`display_name` 是
新增字段。路径由目录树动态派生，目录重命名不会改文件记录 ID 或物理归档。

上传接口继续使用 `POST /kbs/{kb}/files`，新增 multipart 字段：

```text
directory_id: 目标目录，默认 0
manifest: {"entries":[{"index":0,"relative_path":"docs/spec.pdf","source_group":"文档资料"}]}
files: repeated UploadFile
```

服务端验证 manifest 条目顺序、路径穿越、目录归属和 source group；每个文件独立
路由、归档、去重和报告。未知扩展名默认返回 `unsupported_file_type`；启用
`DIRECTORY_RAW_ONLY_UNKNOWN=true` 后先归档为 `raw_file`（`raw_archived`）但不解析。
同目录同路径不同内容返回 `file_path_conflict`，不同目录允许同名文件。

持久化文件夹导入批次（P1）：

```text
POST /api/v1/kbs/{kb}/directory-imports
POST /api/v1/kbs/{kb}/directory-imports/plan
PUT  /api/v1/kbs/{kb}/directory-imports/{job_id}/files/{file_id}/content
GET  /api/v1/kbs/{kb}/directory-imports
GET  /api/v1/kbs/{kb}/directory-imports/{job_id}
POST /api/v1/kbs/{kb}/directory-imports/{job_id}/retry[?file_id=]
POST /api/v1/kbs/{kb}/directory-imports/{job_id}/cancel
```

`/plan` 先创建空批次（只登记相对路径），随后可逐个 `PUT .../content` 上传文件，
实现文件级断点恢复；中断后重新上传未完成文件即可，无需重传整个文件夹。

创建接口接收与同步上传相同的 `files/directory_id/source_group/manifest`，先把
字节暂存到 `storage/directory_imports/<uuid>/`，再持久化批次和逐文件行；解析 worker
逐个文件调用正常摄取链路，失败文件保留暂存字节以支持单独重试。批次状态为
`queued/running/completed/partial/failed/cancelled`。

---

## 2. 鉴权与治理（`src/core/auth.py`）

### 2.1 常量与初始化

```python
PBKDF2_ITERATIONS = 260_000
ROLE_SYSTEM_ADMIN = "system_admin"; ROLE_EMPLOYEE = "employee"
LOGIN_MAX_FAILURES = 5; LOGIN_BASE_LOCK_SECONDS = 30; LOGIN_MAX_LOCK_SECONDS = 15*60
```

`AuthService.__init__(db_path=None)`：默认 `settings.AUTH_DB_PATH`；
按绝对路径做进程级一次性初始化（`_AUTH_INIT_LOCK` + `_AUTH_INITIALIZED_DBS`），
执行 `_init_db()` + `_ensure_default_admin()`。连接 PRAGMA：`WAL`、`synchronous=NORMAL`、
`foreign_keys=ON`、`isolation_level=None`（自动提交，事务由代码显式控制）、`timeout=30`。

### 2.2 DDL（`_init_db`）

```sql
departments(id PK, name UNIQUE, created_at)
users(id PK, username UNIQUE, password_hash, role DEFAULT 'employee',
      department_id FK, is_active DEFAULT 1, managed_by_env DEFAULT 0,
      created_at, updated_at)
auth_sessions(id PK, token_hash UNIQUE, user_id FK, created_at, expires_at, revoked_at)
login_attempts(key PK, username, client_ip DEFAULT '', failed_count DEFAULT 0,
               locked_until, last_failure_at)
knowledge_bases(id PK, name, department_id FK, owner_user_id FK,
                is_shared DEFAULT 0, created_at,
                UNIQUE(department_id, name))
```

迁移（幂等，启动自动执行）：
- 角色：`'admin'→system_admin`；`'dept_admin'/'user'→employee`；
- `_migrate_kb_scope_schema`：**删除旧的 `kb_permissions` 表**；当 `knowledge_bases` 缺少
  `UNIQUE(department_id,name)` 时重建（rename→create→copy→drop，临时关 FK）；
- `is_shared` 默认 0：存量库迁移时也置为私有；
- `_ensure_default_department`：`INSERT OR IGNORE departments(name='system')`。

### 2.3 默认管理员引导（fail-closed）

- 必须设置 `AUTH_DEFAULT_ADMIN_PASSWORD` 且 ≠ `admin123`，否则进程启动即 `RuntimeError`。
- 已存在 admin 行时：仅当角色为 `system_admin` 且（`managed_by_env=1` 或旧口令仍是 `admin123`）
  才同步新口令，并置 `managed_by_env=1`。
- 否则插入 `(username, pbkdf2_hash, system_admin, department_id=<system id>, is_active=1, managed_by_env=1)`。

### 2.4 口令与令牌

```python
hash_password(pw)  # pbkdf2_sha256$260000$<b64 salt16>$<b64 dk>
verify_password(pw, encoded)   # hmac.compare_digest
hash_token(token)              # sha256 hex（库里只存哈希）
```

登录：`secrets.token_urlsafe(32)`，TTL `AUTH_SESSION_TTL_HOURS`（默认 24h）。
爆破防护键 `"{username.lower()}|{client_ip}"`：失败累计 ≥5 后按
`min(30 * 2**(count-5), 900)` 秒锁定；错误文案不区分「用户不存在」与「密码错误」。

### 2.5 KB 注册、共享与权限判定

| 方法 | 语义 |
|---|---|
| `register_knowledge_base(kb, owner=..., is_shared=False)` | `INSERT OR IGNORE`；owner 的部门即 KB 部门 |
| `get_knowledge_base_sharing(kb)` | → `(owner_user_id, is_shared)` |
| `set_knowledge_base_sharing(kb, actor, is_shared)` | 仅 owner（employee）；审计 `update_kb_sharing` |
| `list_accessible_kbs(user, existing_kbs)` | sysadmin → `[]`；employee → `department_id = user.dept AND (is_shared=1 OR owner_user_id IS NULL OR owner_user_id = user.id)` ∩ 物理存在的 KB |
| `get_kb_permissions_for_user(user, existing)` | 同上谓词，值恒为 `"admin"`；键为 `kb_scope_key(name, department_id)` |
| `assign_knowledge_base_as(actor, kb, department_id, owner_user_id, source_kb_id)` | 仅 sysadmin；目标部门非 system；owner 必须是目标部门员工；**跨部门迁移拒绝** |
| `list_knowledge_base_summaries(existing)` | 治理面板用：owner/部门/员工数/注册与物理存在标记 |

> 注意：`RequestContext.user_id` 在 chat 路径下是**用户名**；`AppPipeline._resolve_actor_user_id`
> 会把纯数字当 id、否则查 `users.id`。附件 ACL 用数字 id，由 runner 显式传 `attachment_user_id`。

### 2.6 RequestContext 构造

```python
build_request_context(session_state)   # 旧 Streamlit 风格，仍被单测覆盖
build_context_for_user(user: AuthUser, kb_name="")  # API 路径（src/api/context.py）
```

- 已登录：实时回读 `users` 表（**不是** session 快照），角色变更立即生效；账号停用→匿名上下文。
- 匿名：`user_id = anonymous-<sha256(session_id)[:12]>`，`roles=["anonymous"]`。
- `metadata` 携带 `department_id` / `kb_id` / `actor_department_id` / `resource_department_id`。

### 2.7 两级角色迁移与前端对齐

| 角色 | 前端入口 | 后端强制 |
|---|---|---|
| `system_admin` | 员工/部门/挂载/配置/评估/治理/日志/状态 | KB 内容路由全部 403（`deps.reject_system_admin_kb_access`）；`has_kb_permission` 恒 False |
| `employee` | 资产中心/全部知识库/对话/记忆/治理/日志/状态 | 自己的库 + 部门共享库；共享库内危险操作前端二次确认 |

---

## 3. 审计与查询日志（`src/core/app_logs.py`）

### 3.1 表结构

```sql
audit_events(id PK, actor_user_id, actor_username, actor_role, department_id,
             action, target_type, target_id, kb_name, success, error_message,
             metadata_json, previous_hash, integrity_hash, created_at)
query_traces(id PK, user_id, username, department_id, chat_session_id,
             user_message_id, assistant_message_id, kb_name,
             original_query, rewritten_query, backend, retriever_type,
             vector_top_k, bm25_top_k, final_top_k, latency_ms,
             status, error_message, metadata_json,
             otel_trace_id, otel_span_id, turn_id, created_at)
retrieved_evidence(id PK, trace_id FK, rank, file_name, document_id, chunk_id,
                   vector_score, bm25_score, rrf_score, rerank_score,
                   text_preview, metadata_json, created_at)
```

### 3.2 审计哈希链

- `record_audit` 在 `BEGIN IMMEDIATE` 中串行化「读上一条哈希 → 计算 → 插入」。
- 哈希输入：`actor_user_id, actor_username, actor_role, department_id, action, target_type,`
  `target_id, kb_name, success(0/1), error_message, metadata_json, created_at` + `previous_hash`，
  规范化 JSON（`sort_keys`、无空格）后 SHA-256。
- `audit_integrity_status(limit=5000)` 逐行重算：`previous_hash` 不匹配或哈希不匹配即 `broken_id`；
  旧行（空哈希）单独计入 `legacy` 且不打断链。
- **调用方全部 fail-soft**（`AuthService._audit` / `AppPipeline._audit` / `RAGFlowBackend._audit`），
  唯一例外是 `change_settings` 由路由层显式记一次。
- `audit_events` **没有清理路径**（唯 append-only）；`query_traces`/`retrieved_evidence` 启动时清理 30 天。

### 3.3 查询 trace 的状态推导

```python
query_trace_status(response, retrieval_summary)
  # summary 中有映射状态：success/partial_failure→partial/failed/no_evidence
  # 否则按回答前缀：'Error:' / '系统错误:' / 'RAGFlow retrieved relevant context, but answer generation failed:'
```

`failure_category(message)` 归一化为安全类别（六种友好文案 + `retrieval failed`/`no evidence`/
`model_rate_limited`/`model_timeout`/`model_unavailable`/`model_unreachable`/`permission_denied`/
`system_error`/异常类名/`[unclassified]`），**绝不返回原始报文**。

### 3.4 作用域与脱敏

| 函数 | 规则 |
|---|---|
| `scoped_where(viewer)` | sysadmin→`1=1`；employee→`department_id = viewer.department_id`；未知角色→仅自己 |
| `_audit_where` | 叠加 action/kb_name/success/keyword（actor_username/target_id/error_message/metadata LIKE） |
| `_query_where` | 员工可见本部门同事的 **状态**，但 keyword 只覆盖 `username/kb_name/backend/retriever_type`（防止用查询文本侧信道） |
| `redact_query_trace` | 非 owner：query/error → `[redacted]`；metadata 只保留白名单键（阈值/权重/数据集名/agent_status/rounds…），`tool_diagnostics` 压缩为 `{tool_name,status,hit_count,top_k,scoped}` |
| `redact_evidence` | 非 owner：保留来源与分数，`text_preview="[redacted]"` |

---

## 4. 数据访问与治理要点

- **所有 KB 级方法都必须 `KbScope.require_department()`**（`RAGFlowBackend` 全部入口、spreadsheet、
  circuit、external conversation、KB 路径）。
- **删除是分散的**：删文档 = 远端删除 + 归档删除 + 台账删除（RAGFlow handler）/ 索引删除 / 电路目录删除 /
  对话目录+索引删除；删库 = 逐文档删除 + `PipelineAssetCleanupService.cleanup_knowledge_base` 清理目录。
  部分失败会上报 `partial=True` 并保留可重试状态。
- **共享只读记录（`shared_read_only=1`）**：observability 实例写入的记录，develop 实例可复用远端映射但
  不重建本地归档、不修改、不删除（`PermissionError` 文案明确）。
- **软删除与孤儿**：附件行软删；资产在最后一个引用消失时回收；嵌入对象（OOXML 内嵌子文件）按父引用计数
  决定是否级联删除。

---

## 5. 配置项（本层相关）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HDB_API_HOST` / `HDB_API_PORT` | `127.0.0.1` / `8001` | 监听（重启生效） |
| `HDB_API_CORS_ORIGINS` | `http://localhost:5176,http://127.0.0.1:5176` | 逗号分隔；生产必须显式设置 |
| `HDB_API_MAX_UPLOAD_BYTES` | `536870912`（512 MiB） | 单次上传上限 |
| `HDB_API_SPAWN_WORKER` | `1` | API 内拉起统一 worker |
| `HDB_API_SPAWN_EXPORT_WORKER` | `1` | API 内拉起导出 worker |
| `HDB_API_SPAWN_MEMORY_WORKER` | `0` | 记忆 worker（单写者，默认不自动拉起） |
| `AUTH_DB_PATH` | `storage/auth.db` | 重启生效 |
| `AUTH_DEFAULT_ADMIN_USERNAME` / `_PASSWORD` | `admin` / `""`（必须设置） | 引导管理员 |
| `AUTH_SESSION_TTL_HOURS` | `24` | 会话有效期 |
| `ADMISSION_CONTROL_ENABLED` | `true` | 请求准入总开关 |

## 6. 排障清单

| 现象 | 排查顺序 |
|---|---|
| 401 循环 | token 是否过期/被 `revoke_session`；`auth_sessions` 是否被停用用户连带撤销；前端是否在无 token 请求上触发全局登出 |
| 403「system_admin 是治理角色」 | 用员工账号；sysadmin 账号改配置/治理可，读内容不可 |
| 上传 413 | 请求体超 `HDB_API_MAX_UPLOAD_BYTES`，或走附件通道时超附件配额（见 [12](12-attachments.md)） |
| 上传后搜不到 | RAGFlow 可达性/Key/dataset 名 → 解析任务是否 completed → KB 权限 |
| 429 | 看 reason：`rate_limit_*` 是 RPM，`queue_full` 是准入队列，`session_backlog/user_backlog` 是未终态 turn 超限 |
| 配置改了不生效 | `/health/ready` 的 `restart_required_settings` |
