# 11 · 长期记忆（LangMem）

对应代码：`src/memory/`（约 6.7k 行）：`service.py` / `catalog.py` / `jobs.py` / `store.py` /
`manager.py` / `worker.py` / `schemas.py` / `formatter.py` / `prompts.py` / `retention.py` / `reconcile.py`。

---

## 1. 定位与总体架构

两段式记忆：

| 类型 | 内容 | 授权模型 | 作用域 |
|---|---|---|---|
| **项目记忆（Project Memory）** | 从「部门 + 知识库」会话里提炼的工程事实/决策/约束/问题/经验/待办/上下文 | 无逐事件同意门槛；部门+KB 权限 | `("hdb","department",<dept>,"kb",<kb>,"candidate"\|"verified")` |
| **个人记忆（User Memory）** | **仅限用户显式表达的个人使用偏好** | 需 opt-in **且** 逐消息同意（consent event，不可变） | `("hdb","user",<user_id>,"candidate"\|"verified")` |

```
ConversationService.complete_turn
   └─(同事务) enqueue_project_reflection  → memory_jobs (status=pending, available_at=now+debounce)
MemoryWorker（单写者）
   ├─ claim job（lease_token + generation CAS）
   ├─ LangMemAdapter.extract（模型 = profile "memory"，batch 优先级）
   ├─ memory_reflection_runs 记录输出（run_key 幂等；崩溃后可复用输出）
   ├─ 事务内 apply：prepare_candidate（revision CAS + projection fence + 来源 + outbox）
   └─ projection outbox：写 LangGraph Store → mark_projection_active
MemoryService.search / format_context
   └─ Agent 预取 + memory_search 工具 → <untrusted_memory> 注入
```

**权威与投影分离**：Catalog（`AUTH_DB_PATH`）是唯一权威（身份/生命周期/consent/幂等/ACL）；
LangGraph Store（`memory.db`）是**可重建**的检索投影，由 reconcile 兜底清理。

---

## 2. Catalog 表结构（`catalog.ensure_memory_schema`）

> `ensure_memory_schema(conn)` 幂等；**psycopg 连接直接返回**——PG 下 DDL 必须由
> 控制面版本化 schema 预置（仓库内没有 .sql 迁移文件）。

### 2.1 核心表

```sql
memory_records(
  memory_id PK, scope CHECK IN ('user','project'), user_id, department_id, kb_id,
  status CHECK IN ('candidate','verification_pending','supersede_pending','needs_rebuild',
                  'verified','superseded','rejected','deleted','provenance_missing'),
  current_revision INT>=0 DEFAULT 0, content_hash, memory_type, title, subject,
  content_json, extractor_model, extractor_version, schema_version DEFAULT '1',
  replacement_id, created_at, updated_at, deleted_at)
  INDEX(scope,user_id,department_id,kb_id,status); INDEX(content_hash)

memory_revisions(revision_id PK, memory_id, revision_no>0, projection_id,
  before_content_json, after_content_json, operation, actor_id, reason, created_at,
  UNIQUE(memory_id, revision_no))

memory_sources(source_id PK, memory_id, session_id, turn_id, message_id, consent_event_id,
  source_hash, source_role, contribution_kind DEFAULT 'extracted', source_valid DEFAULT 1,
  invalidated_at, created_at,
  UNIQUE(memory_id,session_id,turn_id,message_id,consent_event_id))
  -- 刻意不做 chat 行级联：会话删除先「失效 + 入队」再删原消息，保留墓碑与来源证据

memory_projections(projection_id PK, memory_id, projection_kind IN ('candidate','verified'),
  store_backend, namespace_json, store_key, current_revision, current_content_hash,
  active DEFAULT 0, manager_writable DEFAULT 0, fence_version DEFAULT 1,
  created_at, retired_at, UNIQUE(store_backend,namespace_json,store_key))
  部分唯一: uq_memory_active_projection(memory_id,projection_kind) WHERE retired_at IS NULL

memory_consent_events(consent_event_id PK, user_id, session_id, turn_id, message_id,
  consent_kind='user_memory_extract', policy_version, consent_revoke_generation DEFAULT 0,
  granted_at, revoked_at, authorized_start_turn_id, authorized_start_message_id,
  authorized_end_turn_id, authorized_end_message_id, authorized_source_hash, created_at)

memory_consent_source_items(consent_event_id, ordinal>=0, turn_id, message_id, role, content_hash,
  PRIMARY KEY(consent_event_id, ordinal), UNIQUE(consent_event_id,turn_id,message_id))

user_memory_settings(user_id PK, opt_in DEFAULT 0, policy_version DEFAULT 'v1',
                     revoke_generation DEFAULT 0, updated_at)
```

**SQL 触发器强制不可变**：

- `trg_memory_consent_immutable_update`：任何授权字段变化 → `RAISE(ABORT,'memory consent authorization fields are immutable')`；
  **只有 `revoked_at` 可改**；
- `trg_memory_consent_items_no_update` / `_no_delete`：manifest 行**永不**可改可删。

### 2.2 任务与运行表

```sql
memory_jobs(job_id PK, session_id FK chat_sessions ON DELETE CASCADE,
  job_kind CHECK IN ('project_reflection','user_reflection'),
  scope_fingerprint, consent_event_id FK, consent_policy_version, consent_revoke_generation,
  target_turn_id, target_message_id, source_start_turn_id/message_id,
  authorized_start/end_turn_id/message_id, authorized_source_hash,
  generation DEFAULT 1, status CHECK IN ('pending','running','retrying','completed','cancelled','dead_letter'),
  requested_at, available_at, next_retry_at, lease_owner, lease_token, lease_expires_at,
  claimed_at, completed_at, retry_count DEFAULT 0, last_error)
  -- CHECK 约束：project job 不得带 consent/authorized 列；user job 必须有且无 source_start_*
  -- UNIQUE uq_memory_jobs_project_session(session_id) WHERE job_kind='project_reflection'
  -- UNIQUE uq_memory_jobs_user_consent(consent_event_id) WHERE consent_event_id IS NOT NULL

memory_checkpoints(job_id PK FK CASCADE, committed_turn_id, committed_message_id,
                   committed_generation, source_fingerprint, updated_at)

memory_reflection_runs(run_id PK, job_id, generation, job_kind, consent_event_id,
  run_key UNIQUE, consent_revoke_generation, authorized_*, source_snapshot_hash,
  encrypted_source_snapshot_ref, extractor_model, extractor_version, schema_version,
  output_payload_json, output_hash,
  status CHECK IN ('prepared','output_persisted','catalog_committed','projected','failed'),
  created_at, committed_at)

memory_run_items(run_id, item_ordinal, output_item_hash, planned_action, memory_id,
  revision_no, projection_id, expected_fence, status DEFAULT 'planned', error,
  PRIMARY KEY(run_id,item_ordinal))

memory_projection_outbox(operation_id PK, memory_id, projection_id,
  operation CHECK IN ('put','delete'), expected_revision, expected_fence,
  idempotency_key UNIQUE, payload_json,
  status CHECK IN ('pending','running','retrying','completed','dead_letter'),
  retry_count, next_retry_at, last_error, created_at, completed_at)

memory_deletion_outbox(同上; operation CHECK IN ('delete_projection','rebuild','redact');
  source_or_consent_id 取代 projection_id)

memory_audit_events(audit_event_id PK, memory_id, operation_id, event_type, actor_id,
  evidence_refs_json, request_id, metadata_json, created_at)
```

审计事件类型（代码实际写入）：
`candidate_revision_prepared, projection_retired, projection_active, projection_delete_completed,`
`verify_requested, reject_requested, delete_requested, supersede_requested, draft_updated,`
`consent_granted, consent_revoked, user_memory_settings_changed, source_invalidated,`
`deleted_purged, retention_expired, project_reflection_requested, project_reflection_already_covered`。

---

## 3. 生命周期

### 3.1 状态

`candidate → verification_pending → verified`；旁支 `supersede_pending → superseded`、
`needs_rebuild`、`rejected`、`deleted`、`provenance_missing`。
**可读/可放行集合**：`ACTIVE_MEMORY_STATUSES = {candidate, verified}`。

### 3.2 抽取（worker）

1. claim job → 重载授权来源窗口 → `save_run`（`run_key = sha256({job_kind, consent_event_id,`
   `scope_fingerprint, target_turn/message, generation, authorized_*, source_snapshot_hash,`
   `schema_version, extractor_version})`）；
2. `LangMemAdapter.extract` → `persist_output_for_job`（同事务复核 status/lease/generation 与
   consent）→ `status='output_persisted'`；
3. `_apply_job` 单事务：
   - 复核 lease/generation/consent、校验 output hash；
   - 逐条输出：schema 校验（`extra="forbid"`）→ **按 content_hash 去重**（同域已有相同 hash
     的 candidate 则复用）→ `prepare_candidate(...)`；
   - run → `catalog_committed`、推进 checkpoint、job → `completed`。

`prepare_candidate` 在调用方事务内完成：命名空间派生 + 作用域不可变校验 →
`memory_records.current_revision` CAS（`WHERE current_revision=? AND status IN ('candidate','needs_rebuild')`）
→ 投影 fence 校验（`manager_writable` + `expected_fence`）→ 写 revision →
写 provenance（**`source_hash` 缺失即抛错**）→ staging payload + `INSERT OR IGNORE` 投影 outbox
（`idempotency_key = put:{projection_id}:{revision}:{content_hash}`）→ 审计。

### 3.3 投影（outbox → Store）

- worker claim outbox（`pending/retrying` 且 `next_retry_at <= now`）→ 校验
  （`orphan_projection/orphan_record/stale_fence/retired_projection/invalid_payload/stale_content_hash`）
  → `runtime.put(namespace, store_key, {"kind","content","schema_version"})`
  → 新事务 `mark_projection_active`；
- `mark_projection_active` 复核：outbox 是 put、投影 revision/fence、双侧 content hash、
  记录状态、`retired_at IS NULL`；置 `active=TRUE`；
  verified 投影激活后把记录从 `verification_pending` 升为 `verified`，再 retire 候选投影
  （**顺序不可颠倒**）；
- 物理写入期间 Catalog fence 变化 → 回滚并以 `catalog_fence_changed` 完成
  （fail-closed，由后续删除 outbox 清掉陈旧对象）。

### 3.4 治理操作（`MemoryService`）

| 操作 | 语义 |
|---|---|
| `verify(memory_id, expected_revision, evidence_refs, reason)` | 要求 ≥1 证据引用；fence 候选 → `verification_pending` + revision+1 + 新 verified 投影（`manager_writable=FALSE`）+ outbox；审计 `verify_requested` |
| `reject(...)` | retire 全部活投影（fence++）→ `rejected` + revision+1 |
| `delete(...)` | 同上 + `scrub=True`：内容置 `{}`、标题「已删除记忆」、`deleted_at`；revision 前后 JSON 置空；run 输出/快照引用清空；随后物理删除 `run_items/sources/revisions/records`（审计保留）；清理失败降级保留墓碑 |
| `supersede(memory_id, replacement_id, ...)` | 禁止自替代；两条记录必须同域且 active；retire 旧投影 → `supersede_pending` + `replacement_id`；投影删完后转 `superseded` |
| `update_draft(content, expected_revision)` | 仅 candidate；Pydantic 严格校验；max-revision CAS；fence 候选后重走 `prepare_candidate(revision_operation="draft_update")` |

### 3.5 来源失效

`apply_source_invalidation` 分类：

| 情形 | 结果 |
|---|---|
| user 域 | `deleted` + 全量 payload 擦除 |
| verified project | `provenance_missing` |
| 其它 | 有有效来源 → `needs_rebuild`；否则 `deleted` |

同时 fence 全部活投影（`active=FALSE, manager_writable=FALSE, fence+1, retired_at`）+ 入队 `delete_projection`。
调用方：`invalidate_message_memory_in_connection`（消息编辑/脱敏）、
`invalidate_session_memory_in_connection`（清空/删除会话，另 revoke 该会话全部同意）。

### 3.6 版本与栅栏

- `memory_revisions` 每 `(memory_id, revision_no)` 追加，`revision_no` 从 1 起；
- 任何变更都在 CAS 谓词下 `current_revision + 1`，陈旧操作 → `RuntimeError("candidate revision CAS failed")`；
- `projections.fence_version` 是物理写栅栏；`fence_projection` / `retire_projection` 都是 CAS；
- `needs_rebuild` 的记录**不能复用已 retired 的物理 key**（唯一约束），因此用
  `"{memory_id}:candidate:{revision_no}"` 作为新 key。

---

## 4. Consent 与 opt-in（个人记忆）

两道**独立**门：

1. **opt-in**（`user_memory_settings.opt_in`，默认 0）：
   关闭时 `revoke_generation += 1` 并执行 `_revoke_user_consent_in_transaction`：
   revoke 全部活 consent → 取消 pending/running 的 user job（generation+1）→ 清 run 输出与快照引用 →
   失效来源 → 抹除派生 user 记忆（fence + 删除 outbox）→ 逐条审计。**开启不 bump generation**。
2. **逐事件 consent**（`create_user_consent(actor, session_id, message_ids)`）：
   - 1~200 个唯一 message id；会话属于调用者；消息必须属于**已完成** turn 且角色是 user/assistant；
   - 按 id 排序，`message_content_hash(role, content)`（`schema_version="message-v1"`）；
   - `MemoryConsentManifest` 校验 ordinal 连续、`(turn_id,message_id)` 唯一；
     `MemoryConsentEvent` 校验边界与 `authorized_source_hash == manifest_hash`；
   - 写入前复核 opt-in 与 `policy_version`/`revoke_generation` 未过期，
     否则 `PermissionError("consent policy or revoke generation is stale")`。

读取期门控：`_user_record_is_active()` 要求 opt-in + ≥1 有效来源 + **每个来源都有同用户的未 revoke consent**；
`search()` 在 user 域 Store 读取**前后各采样一次 `revoke_generation`**，变化即丢弃全部 user 结果（fail-closed）。

--- 

## 5. 任务：去抖、租约、单写者

### 5.1 项目记忆去抖/水位

- 每个 session **最多一个** project job（部分唯一索引）；
- 水位比较用 `_message_order(turn_id, message_id) = (int(message_id), str(turn_id))`
  （数字优先，避免 `"10" < "2"` 的字典序事故）；
- 目标不变 → 保留；`force=True` → generation+1、重置 pending、清租约；目标更新 → 更新水位 +
  generation+1（fence 在飞 worker）；
- `available_at = now + MEMORY_DEBOUNCE_SECONDS`（默认 300s）。

### 5.2 领取与租约

`claim_next(worker_id, lease_seconds=180, max_retries=5)`：

- 候选：`(pending|retrying) AND available_at<=now AND (next_retry_at IS NULL OR <=now)`
  ∪ `running AND lease_expires_at<=now`；
- 排序：**user_reflection 优先**，再 `requested_at, job_id`；
- 超预算 → `dead_letter`（保留真实错误）+ 指标 + 告警日志；
- 领取：`lease_token=uuid4`、`retry_count=attempts+1`、清 `next_retry_at`；
- `heartbeat` 需 token+generation 匹配；`mark_completed` / `mark_retry` 同；
- `requeue_dead_letter(job_id)` 重置。

### 5.3 Worker（`MemoryWorker`）

| 机制 | 实现 |
|---|---|
| 单写者 | `MEMORY_SINGLE_WRITER=true` 时 `flock(LOCK_EX\|LOCK_NB)` 锁 `memory-worker.lock`；第二个 worker 直接 `RuntimeError` |
| 反思预算 | 单线程池跑 adapter，墙钟超时 `MEMORY_REFLECTION_TIMEOUT_SECONDS=120`；心跳线程间隔 `max(1, min(30, lease/3))`；`MEMORY_REFLECTION_MIN_INTERVAL_SECONDS` 节流 |
| 超时不可强杀 | 超时后 `_reflection_in_flight` 保持直到回调触发（防止共享状态被复用），晚到的结果被 lease/generation CAS 拒绝 |
| 熔断 | 连续失败 `MEMORY_REFLECTION_CIRCUIT_FAILURES=5` → 打开 `MEMORY_REFLECTION_CIRCUIT_COOLDOWN_SECONDS=60` |
| 错误分类 | `InvalidMemorySource`/`PermissionError` → 取消 job（重试无意义）；`StaleMemoryJob` → 视为陈旧成功；其它 → `mark_retry` |
| 资源许可 | `RESOURCE_GOVERNOR_ENABLED` 时拿 `memory` 许可（1.0s 超时，`RESOURCE_MEMORY_MAX_CONCURRENT=1`） |
| run_once 顺序 | retention（每 `max(30, MEMORY_RECONCILE_INTERVAL_SECONDS=3600)`）→ deletion outbox → projection outbox → reconcile（同节拍）→ `has_claimable()` → 许可 → job |
| 崩溃恢复 | `_requeue_stuck_outbox()` 把 `running` 翻回 `retrying`；`run_key` + output hash 让 catalog 提交后崩溃可无重复模型调用地重放 |

---

## 6. LangMem 适配与写入门（`manager.py` / `store.py`）

`LangMemAdapter`：

- 模型：`create_chat_model(profile="memory", priority=PRIORITY_BATCH)` +
  `instrument_chat_model(operation="memory_reflection", profile="memory")`；
- Manager 只用一个 schema（`UserMemory` 或 `ProjectMemory`）、固定指令（`prompts.py`）、
  `enable_inserts=True, enable_deletes=False`、namespace 末段必须是 `candidate`；
- 消息前置服务端自有的来源提示 `[source turn_id=... message_id=... role=...]`——**模型永远不掌握 ID**；
- 输出两遍 Pydantic 校验（`extra="forbid"`），按 `(output_key, output_item_hash)` 去重，
  `output_hash` 对有序条目哈希。

`CatalogAwareStore(BaseStore)`（关键安全闸）：

- 拒绝 namespace 末段非 `candidate`、或与服务器作用域不完全一致的写；
- `search()` 分页扫描（`page_size = wanted * MEMORY_STORE_OVERSAMPLE_FACTOR`，上限 `MEMORY_STORE_MAX_SCAN`），
  逐条校验 namespace、投影允许性（kind=candidate、`manager_writable`、未 retired、记录 status=candidate）、
  语义对象存在、`content_hash(semantic) == projection.current_content_hash == record.content_hash`；
- `put/delete` **只捕获不写**（`CapturedPut/CapturedDelete` 含 projection_id + expected_fence）；
- `get()` 未经允许返回 `None`；`batch/abatch` 实现 BaseStore 契约以支持异步调用方。

---

## 7. 投影存储、reconcile 与保留

### 7.1 `create_memory_store`

- `MEMORY_STORE_BACKEND ∈ {sqlite, postgres}`，其它值 `ValueError`；
- 索引仅在 `MEMORY_EMBEDDING_DIMS > 0` 且能构造 embedding 时启用：
  provider `openai|custom` → `OpenAIEmbeddings(..., check_embedding_ctx_length=False)`；
  `ollama` → `OllamaEmbeddings`；字段来自 `MEMORY_INDEX_FIELDS`（默认 `content.content,content.title,content.subject`）；
- **版本兼容补丁**：`langgraph-checkpoint-sqlite==3.1.1` 的 `SqliteStore` 读 `text_fields` 而非 `fields`，
  运行时会重命名，避免静默退化为「整对象索引」（`tests/test_memory_store_index_contract.py` 锁定）；
- 索引构造失败 → 重开无语义检索的 Store，`index_error` + `hdb.memory.index_health` 置不健康，
  聊天/控制面继续可用（search 返回空，outbox 稍后重试）；
- Postgres 必需 DSN，缺失抛错（**绝不隐式空库**）；busy 重试：SQLite 3 次 × 50ms×attempt，
  PG 重试 `OperationalError/InterfaceError`。

表名（SQLite/PG 同）：`store(prefix,key,value,created_at,updated_at)`、`store_vectors(...)`。

### 7.2 reconcile

`reconcile_store(runtime, catalog, max_scan)`：遍历 `("hdb",)` 前缀、末段为 `candidate|verified` 的命名空间，
分页扫描物理对象；仅当投影缺失/未激活/已 retired、记录缺失或非 active、语义对象缺失、
或任一 hash 不匹配时删除物理对象。返回删除计数（`hdb.memory.reconcile_removed` / `_error`）。

### 7.3 保留

`expire_memory_records`：`MEMORY_RETENTION_DAYS` 为正整数才启用；条件
`updated_at < cutoff AND (scope='user' OR status IN ('deleted','rejected','superseded','provenance_missing'))`，
单批 ≤5000（默认 500）；执行失效 + 删除 outbox + 审计 `retention_expired`。
（项目记忆只在进入非 active 态后才会被自动处理。）

---

## 8. 检索、注入与工具

### 8.1 `MemoryService.search` 算法

1. 关闭或空查询 → `[]`；校验 `scope/status`；
2. 构造作用域：`_project_scope` 拒绝 system_admin 且要求选中 KB + read 权限；
   `scope="all"` 时缺哪个吞哪个（user 域 Authorization 失败也吞）——除非显式指定该域；
3. 每域上限 = `min(配置, 请求)`，配置为 `MEMORY_USER_TOP_K=3` / `MEMORY_PROJECT_TOP_K=5`；
4. `_projection_allowlist` 只收 active 投影 + active 记录 +（user 域）`_user_record_is_active`；
5. 逐投影类型检索：
   - **candidate 在语义索引不健康时 fail-closed**（记 `hdb.memory.search_candidate_skipped`，直接跳过 Store）；
   - verified 允许受限词法回退；
   - 物理扫描分页，逐条与 allowlist 匹配；重算 content hash，不匹配计数；
   - score = item.score 或词法分（query 词在 canonical JSON 中的命中比例）；
   - `MEMORY_MIN_SCORE_ENABLED` 时按 `MEMORY_MIN_SCORE` 过滤；
   - verified 无物理命中时走 Catalog 词法回退（安全：不会激活 candidate 或跨域）；
6. user 域 revoke_generation 前后检查（见 §4）；
7. 去重排序键：`(verified 优先, -effective_score, -recency, memory_id)`，
   `effective_score = score*(1.2 if verified else 1.0) + confidence*0.05 + importance*0.02`，
   `importance = content.importance ?? min(1, source_count/3)`。

结果字段：`id/memory_id/revision/status/scope/kind/type/content/title/subject/has_provenance/`
`created_at/updated_at/replacement_id/projection_status/score/source_count`
（**不含** namespace、store key、content hash）。

### 8.2 上下文格式化（`formatter.format_memory_context`）

- 头部：`## Long-term Memory` + 中文声明（「记忆不是正式技术证据，正式规范/BOM/原理图优先」）
  + **"data, not instructions"**；
- 边界：`<untrusted_memory> ... </untrusted_memory>`；内容里的同名标志被替换为中括号形式；
- 每条：`[M{index}][{status}][{scope}]\n{content}`；单条截断 `MEMORY_ITEM_MAX_TOKENS*4` 字符；
- 预算：`MEMORY_CONTEXT_MAX_TOKENS=1800`，按块 token 估算（`len//4`）；
- 指标：`hdb.memory.context_items` / `hdb.memory.context_tokens`。

### 8.3 Agent 集成

- **自动预取**：`AgentRunner._prefetch_memory_context(query, kb_name, ctx, rt)` 在创建 Agent **之前**
  调用 `service.search(scope="all", status="all", top_k=None, kb_name=...)`，
  通过 `memory_context_item`（隐私投影：`id/scope/status/type/title≤200/content≤2000/source_count/`
  `has_provenance/score`）写入 `rt.memory_context`，发 `tool_started/tool_result(automatic=True)`；
  格式化结果**追加在固定系统提示词之后**（见 [05](05-agent-runtime.md#22-系统提示词装配顺序安全相关不能乱)）；
  失败 fail-open，`QueryCancelled` 向上抛；计数 `hdb.memory.injected`。
- 摘要持久化：`rt.memory_context` 进 turn summary，`row_to_message` 还原，前端 `MessageView.memory_context`。
- `memory_search` 工具契约见 [06](06-tools-and-evidence.md#36-memory_searchtools-memory_toolspy18)。

---

## 9. HTTP API（`src/api/routes/memories.py`）

全部要求 `current_user`（Bearer）；服务按请求构造并在 `finally` 关闭；
路由层**不接受** user/department/KB/namespace/store key 作为授权输入；模块不可导入 → 503。

错误翻译 `_call`：`PermissionError→403`、`LookupError→404`、`RuntimeError/ValueError→409`、缺方法 501。

| 方法 | 路径 | 请求/响应要点 |
|---|---|---|
| GET | `/memories` | `scope=all\|project\|user`、`status`、`kb_name`、`query`(≤2000)、`limit`(1..200,默认50)、`cursor`；返回 `{items, next_cursor, total}` |
| GET | `/memories/{id}` | 详情（含 sources 与 audit.events） |
| PATCH | `/memories/{id}/draft` | `{content, expected_revision≥1, reason≥1, request_id}`；202；陈旧 revision → 409 |
| POST | `/memories/{id}/verify` | 要求 ≥1 `evidence_refs`；202 `status=verification_pending` |
| POST | `/memories/{id}/reject` | 202 |
| POST | `/memories/{id}/supersede` | `{successor_memory_id}`；自替代/跨域/非 active → 409 |
| DELETE | `/memories/{id}` | **body** `MemoryActionRequest`；202（擦除 + 物理清理，审计保留） |
| POST | `/conversations/{sid}/extract-memory` | `{reason, request_id, message_ids≤20}`；必须恰属一个已完成 turn；已被最新项目反思覆盖 → `already_covered` |
| GET/PUT | `/memory-settings` | 仅本人；关闭即撤销同意并删除派生个人记忆 |
| POST | `/conversations/{sid}/memory-consents` | `{message_ids 1..200, reason, request_id}`；需 opt-in；202 |
| GET | `/memory-consents` | 可按 `session_id` 过滤；脱敏视图（无原文） |
| DELETE | `/memory-consents/{id}` | 撤销 |

`MemoryView` **刻意不暴露** namespace/store key/content hash。

---

## 10. 配置清单

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_ENABLED` | `true` | 总开关 |
| `MEMORY_STORE_BACKEND` / `MEMORY_DATABASE_URL` / `MEMORY_SQLITE_PATH` | `sqlite` / `""` / `storage/memory.db` | 投影存储（重启生效） |
| `MEMORY_SQLITE_BUSY_TIMEOUT_MS` | `30000` | SQLite 忙等 |
| `MEMORY_SINGLE_WRITER` | `true` | 单写者 flock |
| `MEMORY_EXTRACTION_ENABLED` | `true` | 抽取总开关 |
| `MEMORY_DEBOUNCE_SECONDS` | `300` | 入队去抖 |
| `MEMORY_JOB_LEASE_SECONDS` / `MEMORY_JOB_MAX_RETRIES` | `180` / `5` | 任务租约/重试 |
| `MEMORY_REFLECTION_TIMEOUT_SECONDS` | `120` | 反思墙钟 |
| `MEMORY_REFLECTION_MIN_INTERVAL_SECONDS` | `0` | 反思节流 |
| `MEMORY_REFLECTION_CIRCUIT_FAILURES` / `_COOLDOWN_SECONDS` | `5` / `60` | 熔断 |
| `MEMORY_PROJECTION_MAX_RETRIES` | `5` | 投影 outbox 重试 |
| `MEMORY_RECONCILE_INTERVAL_SECONDS` | `3600` | retention/reconcile 节拍（地板 30） |
| `MEMORY_USER_TOP_K` / `MEMORY_PROJECT_TOP_K` | `3` / `5` | 检索上限 |
| `MEMORY_STORE_OVERSAMPLE_FACTOR` / `MEMORY_STORE_MAX_SCAN` | `4` / `100` | 扫描边界 |
| `MEMORY_CONTEXT_MAX_TOKENS` / `MEMORY_ITEM_MAX_TOKENS` | `1800` / `350` | 注入预算 |
| `MEMORY_MIN_SCORE_ENABLED` / `MEMORY_MIN_SCORE` | `false` / `""` | 分数阈值 |
| `MEMORY_MODEL_PROVIDER/MODEL/BASE_URL/API_KEY` | `""` | 反思模型覆盖 |
| `MEMORY_MODEL_REQUIRE_STRUCTURED_TOOLS` | `true` | 要求结构化工具 |
| `MEMORY_EMBEDDING_PROVIDER/MODEL/BASE_URL/API_KEY/DIMS` | `""` | 记忆嵌入 |
| `MEMORY_INDEX_FIELDS` | `content.content,content.title,content.subject` | 索引字段 |
| `MEMORY_RETENTION_DAYS` | `""` | 保留期（空=永久） |

**存在但未被读取的配置**（保留/未接线）：`MEMORY_ALLOW_GENERAL_CHAT_EXTRACTION`、
`MEMORY_USER_MEMORY_OPT_IN`、`MEMORY_USER_MEMORY_REQUIRE_PER_EVENT_CONSENT`
（实际 opt-in 是 DB 行；逐事件同意是无条件强制）。

---

## 11. 测试与不变量

`tests/test_memory.py`（最大，覆盖以下不变量）、`test_memory_security_contract.py`、
`test_memory_store_index_contract.py`、`test_control_plane_memory_*_postgres.py`、
`test_control_plane_memory_invalidation_postgres.py`。

关键不变量：

- 会话完成 → 恰好一个项目 job；worker 产出 Catalog candidate；
- revision CAS + projection fence CAS；verified 激活顺序不可颠倒；
- `needs_rebuild` 使用新投影 key（不复用已退休 key）；
- 陈旧 deletion outbox 是 no-op（身份/fence 复核）；
- 删除的记忆默认不可见且控制面被物理清理（审计保留）；
- 个人同意需 opt-in；反思只重载 manifest 内的消息；
- 命名空间/上下文边界 fail-closed；
- retention 擦除 payload 并 fence 投影；
- Phase 1 单写者锁；
- 分页可越过孤儿（`MEMORY_STORE_MAX_SCAN` 内）；
- 消息编辑/脱敏触发来源失效与重建；journal 只保留哈希；
- 租约到期接管且旧 worker 无法提交；
- 去抖 upsert 单 job 并推进 generation；
- catalog 提交后崩溃可重放且无新模型调用；
- consent 撤销与 worker 输出持久化竞态安全；
- 重复 candidate 内容复用不重复。
