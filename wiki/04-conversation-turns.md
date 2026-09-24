# 04 · 会话与 Turns 执行模型

对应代码：`src/core/conversation.py`（2973 行）、`src/api/routes/query.py`（1563 行）、
`src/storage/control_plane/`（PG 控制面）。

这是整个系统**最核心的持久化协议**：回答不是「一个 HTTP 请求」，而是一个可恢复、可取消、
可多实例执行的**轮次（turn）状态机**。

---

## 1. 数据模型

### 1.1 核心记录

```python
ChatSession(id, user_id, kb_name, department_id, kb_id, title, created_at, updated_at,
            document_context, auto_memory, next_turn_sequence)
ChatMessage(id, session_id, turn_id, role, content, footer, created_at, edited_at,
            redacted, memory_context, document_context, citations)
ChatTurn(id, session_id, user_message_id, assistant_message_id,
         kb_name, department_id, kb_id,
         query, query_mode, wiki_assist, status, client_request_id, cancel_requested,
         last_event_seq, answer, summary, footer, metrics, trace_context,
         error_message, worker_id, worker_heartbeat_at, retry_count,
         created_at, started_at, finished_at,
         document_context, export_plan, source_scope, required_attachment_ids, attachments,
         turn_sequence, fencing_token, session_generation, attempt_id)
ChatTurnEvent(turn_id, seq, event_type, payload, created_at)
TurnClaimResult(status, turn=None, blocker_turn_id="")
StaleTurnOwner(RuntimeError)
```

turn 状态（与 PG schema 的 CHECK 单一来源一致）：

```
pending | streaming | cancelling | waiting_for_attachments | completed | failed | cancelled
TURN_STATUSES_NON_TERMINAL = ("pending","streaming","cancelling","waiting_for_attachments")
```

### 1.2 DDL（SQLite；PG 版见 §7）

```sql
chat_sessions(id PK, user_id FK CASCADE, kb_name, department_id, kb_id, title,
              document_context_json DEFAULT '{}', next_turn_sequence DEFAULT 1, auto_memory,
              created_at, updated_at)
  INDEX idx_chat_sessions_user_kb(user_id, kb_name, updated_at)

chat_messages(id PK, session_id FK CASCADE, role, content, created_at)
  INDEX idx_chat_messages_session(session_id, id)

chat_turns(id TEXT PK, session_id FK CASCADE, user_message_id FK, assistant_message_id FK,
           kb_name, department_id, kb_id, query, query_mode DEFAULT 'fast', status,
           client_request_id, turn_sequence, cancel_requested DEFAULT 0,
           last_event_seq DEFAULT 0, answer DEFAULT '', summary_json DEFAULT '{}',
           footer DEFAULT '', metrics_json DEFAULT '{}', trace_context_json DEFAULT '{}',
           document_context_json DEFAULT '{}', export_plan_json DEFAULT '{}',
           source_scope DEFAULT 'auto', required_attachment_ids DEFAULT '[]',
           error_message DEFAULT '', worker_id DEFAULT '', worker_heartbeat_at,
           retry_count DEFAULT 0, created_at, started_at, finished_at)
  UNIQUE idx_chat_turns_session_request(session_id, client_request_id) WHERE client_request_id IS NOT NULL
  UNIQUE ux_chat_turns_session_sequence(session_id, turn_sequence)
  INDEX ix_chat_turns_session_status_sequence(session_id, status, turn_sequence)
  INDEX idx_chat_turns_status_created(status, created_at)

chat_turn_events(turn_id, seq, event_type, payload_json, created_at,
                 PRIMARY KEY(turn_id, seq), FOREIGN KEY turn_id CASCADE)

chat_message_edits(id PK, message_id FK CASCADE, editor_user_id, action CHECK IN ('edit','redact'),
                   previous_content, previous_content_hash, reason, request_id, created_at)

turn_attachments(turn_id, attachment_id, ordinal, filename_snapshot, media_type_snapshot,
                 usage_hint_snapshot DEFAULT 'reference', content_hash_snapshot,
                 parser_version_snapshot, asset_id, parse_status_snapshot DEFAULT 'queued',
                 PRIMARY KEY(turn_id, attachment_id))
  INDEX idx_turn_attachments_asset(asset_id)

conversation_execution_state(session_id PK, active_turn_id DEFAULT '', active_attempt_id DEFAULT '',
                             lease_owner DEFAULT '', lease_expires_at,
                             fencing_token DEFAULT 0, generation DEFAULT 0,
                             recoverable_checkpoint_ref DEFAULT '', committed_checkpoint_ref DEFAULT '',
                             revision DEFAULT 0, updated_at)
```

启动迁移：`turn_sequence` 扩列（可空）→ 按 `(session_id, user_message_id, rowid)` 回填 →
`chat_sessions.next_turn_sequence = MAX+1` 校准 → 唯一索引；历史列
（`department_id/kb_id/worker_*/query_mode/metrics_json/trace_context_json/…`）按需 ALTER 补齐。
生产库的 `turn_sequence` NOT NULL 化有专用脚本（`scripts/production_hardening/migrate_chat_turn_sequence.py`，需停写）。

---

## 2. 状态机与转换

```
create_turn ──┬─ 有 queued/running 附件 ──► waiting_for_attachments ──(解析结束)──► pending
              └─ 否则 ─────────────────────────────────────────────────────────► pending
                                                                                 │
                                          claim_turn_result（顺序/租约/CAS）      ▼
                                                                            streaming
                            cancel_requested ──► cancelling ──worker 收敛──► cancelled
                                              │
                            complete_turn_result ─────────────────────────► completed
                            fail_turn_result / retry_count>=3 / stale 重排 ► failed
```

规则：

| 规则 | 实现 |
|---|---|
| 终态不可复活 | `complete_turn_result` 只更新 `status NOT IN ('completed','cancelled')`；`fail_turn_result` 只更新 `NOT IN ('completed','failed','cancelled')` |
| `failed` 可被真实完成吸收 | `failed` 被视为「SSE 掉线的暂态标记」，真实完成可以覆盖 |
| 重试上限 | claim 的 UPDATE 带 `retry_count < 3`；超限写 `failed` + `Exceeded maximum chat retries` |
| 陈旧 worker 收敛 | `requeue_stale_turns`（见 §5.4） |
| 等待超时 | `expire_waiting_turns`（≤600s，事件 `attachment_wait_timeout`） |
| 排队超时 | `expire_queued_turns`（≤600s，事件 `chat_queue_timeout`，占位文案「生成失败：排队等待超时」） |

---

## 3. 创建 turn（`create_turn`）

**单个 `BEGIN IMMEDIATE` 事务**内完成以下所有动作：

1. 幂等：`(session_id, client_request_id)` 已存在 → 直接返回原 turn（含附件快照）。
2. 作用域：`resolve_source_scope(requested_scope, has_attachments, is_general_chat)`；
   附件引用由 `AttachmentService.resolve_refs_for_turn(user_id, session_id, ids)` 解析，
   缺失/越权/失效 → `blocked`，任一 blocked → `ValueError("attachment_unavailable: ...")`
   （HTTP 400 + `{"error_code":"attachment_unavailable","attachment_ids":[...]}`）。
   注意 `source_scope="attachment_only"` 但无附件 id 是 400；KB-only 会忽略附件 id。
3. 插入 user 消息与**空的 assistant 消息**（终态时回填内容）。
4. 更新会话：`updated_at`、`next_turn_sequence`、标题（仅当标题是「新对话」时取 query 前 32 字）、
   `document_context_json`。
5. `allocate_turn_sequence`：`BEGIN IMMEDIATE` 下读 `next_turn_sequence`，
   `allocated = max(counter if >0 else 1, MAX(turn_sequence)+1)`，写回 `allocated+1`；会话不存在抛 `KeyError`。
   **单调、不复用**（`clear_session` 也不重置）。
6. 初始状态：任一附件 `parse_status ∈ {queued,running}` → `waiting_for_attachments`，否则 `pending`。
7. 冻结附件快照（`turn_attachments`），此后历史展示不依赖活行（`deleted` 状态由读取时叠加）。
8. `export_plan_json = infer_export_intent(query)`：**服务端**识别「导出/生成 Excel/PDF」意图，
   在 turn 完成事务中据此创建导出任务（前端不参与判定）。

---

## 4. Claim：租约、顺序与栅栏（`claim_turn_result`）

```python
stale_after = max(30, stale_after_seconds or CHAT_TURN_HEARTBEAT_TTL_SECONDS)   # 90
serialize   = CONVERSATION_SERIALIZE_SAME_THREAD       # 默认 False（见排障）
lease       = max(30, CHAT_TURN_LEASE_SECONDS)         # 120
```

`BEGIN IMMEDIATE` 内的步骤：

1. 载入 turn（join session，校验 `user_id`）；不存在 → `noop`。
2. **心跳过期回收**：`streaming/cancelling` 且 `worker_heartbeat_at` 早于 `now - stale` →
   重置为 `pending`、清 worker 字段（原 worker 后续写入会因 owner 校验失败）。
3. 状态非 `pending`：若已取消 → `cancelled`；否则 `noop`。
4. `pending + cancel_requested` → 收敛为 `cancelled`：写入占位回答「已停止生成」，
   追加 `error{cancelled:true}` + `turn_end{cancelled}`，返回 `cancelled`。
5. **顺序串行化（开启时）**：确保 `conversation_execution_state` 存在；
   同会话存在更小 `turn_sequence` 的非终态 turn → `blocked(blocker_turn_id)`；
   其它非终态 turn 持有有效租约 → `blocked`；终态 turn 的残留租约 → 清除后继续。
6. **CAS 领取**：
   `UPDATE chat_turns SET status='streaming', started_at=?, worker_id=?, worker_heartbeat_at=?,`
   `retry_count=retry_count+1 WHERE id=? AND retry_count<3`；
   行被别人抢走 → `retry_exhausted`（并置 failed）或 `noop`。
7. **执行态更新（开启串行化时）**：生成 `attempt_id="attempt-"+uuid4hex`，
   单条 UPDATE 原子完成：`active_turn_id/active_attempt_id/lease_owner/lease_expires_at` +
   `fencing_token+1` + `revision+1`，然后回读 token/generation 装饰返回的 turn。

### 4.1 Owner 谓词与心跳

```python
_verify_turn_owner(turn_id, worker_id, fencing_token)
  == active_turn_id == turn_id AND lease_owner == worker_id AND fencing_token == token

touch_turn_worker(turn_id, worker_id, fencing_token=None, ...)
  # 有 token：校验 owner + 续租（延长 CHAT_TURN_LEASE_RENEW_SECONDS=15）
  #           失租记录 hdb.governor.lease_lost{resource="chat",reason="stale_owner"}
  # 无 token：仅更新心跳（legacy 路径）
release_turn_ownership(...)   # 仅当 active_turn_id == turn_id 时清理；崩溃后由下次 claim 回收
```

### 4.2 checkpoint 引用（CAS 注册）

- `get_checkpoint_refs(session_id)` → `{recoverable, committed, generation, fencing_token}`；
- `update_recoverable_checkpoint_ref(turn_id, worker_id, fencing_token, ref, expected_generation=None)`：
  **CAS**，owner 或 generation 变化 → `StaleTurnOwner`；
- `committed_checkpoint_ref` 只在 `complete_turn_result` 中推进。
- 引用内容由 `src/agents/checkpoints/attempts.py` 生成（见 [05](05-agent-runtime.md#62-attempt尝试隔离语义)）。

---

## 5. 事件、取消与收敛

### 5.1 事件追加

```python
_append_turn_event_in_connection(conn, turn_id, event_type, payload)
    seq = last_event_seq + 1 → INSERT chat_turn_events → UPDATE last_event_seq
append_turn_event(turn_id, event_type, payload, *, worker_id="", fencing_token=None)
    # IMMEDIATE 事务 + 可选 owner 校验；StaleTurnOwner →
    # 指标 hdb.conversation.stale_write_rejected{operation="append_event"}
list_turn_events(user_id, turn_id, after_seq=0)
```

### 5.2 SSE 事件目录（前端消费契约）

| 事件 | 载荷要点 | 前端行为 |
|---|---|---|
| `delta` | `{text}` | 追加流式文本（64ms 节流合并渲染） |
| `narration` | `{text}` | 从累积回答中剥离「计划叙述」段落 |
| `stage` | `{stage, status, message, ...}` | 轨迹步骤（含 blocked/error 控制路径） |
| `tool_started` | `{tool, args, seq, automatic?}` | 插入活动轨迹项 |
| `tool_result` | `{tool, status, hit_count, ...}` | 结束轨迹项；失败标红 |
| `degraded` | `{stage, reason}` | 降级横幅 |
| `document_card` | 文档生成卡片快照 | 渲染/更新状态卡 |
| `document_clarification_question` / `_ready` | 澄清问答 | 快捷回复 |
| `done` | `{answer, footer, summary{evidence,memory_context}, ...}` | **权威回答**（覆盖流式文本） |
| `error` | `{message, cancelled?}` | 错误展示 |
| `attachment_ready` / `attachment_failed` | 等待收敛 | 队列提示条 |

`purpose`/`footer` 之外的细节由 `src/api/schemas.py` 的 `TurnEventView` 定义。

### 5.3 取消（协作式）

`request_turn_cancel(user_id, turn_id)`：

1. 终态直接返回；
2. `streaming/cancelling` 先 **bump session generation**（让在飞的提交失效）；
3. 单条 UPDATE（条件更新，天然与 worker 收敛竞争）：
   - `pending` → `cancelled` + `finished_at` + 错误文案「已停止生成」；
   - `streaming` → `cancelling`；
   - `waiting_for_attachments` → `cancelled`；
4. 对未开始的 turn 释放残留租约。

Agent 侧的取消：`should_cancel` 回调（由 API 传入）让 RAGFlow 请求可被真正取消
（`request_cancellable` 竞速 asyncio task）；`QueryCancelled` 在 runner 内被静默处理
（不产出回答、不视为错误）。

### 5.4 收敛后台任务

| 任务 | 语义 | 指标 |
|---|---|---|
| `requeue_stale_turns(stale_after_seconds=90)` | 过期 `streaming/cancelling`：已请求取消→`cancelled`；`retry_count>=3`→`failed`；否则回 `pending`；随后清理失效执行态 | — |
| `expire_waiting_turns()` | 附件等待超 `WAITING_TURN_MAX_SECONDS`(600) → `failed` + `attachment_wait_timeout` | `hdb.conversation.attachment_wait` |
| `expire_queued_turns()` | `pending` 且未请求取消、超 `CHAT_QUEUE_DEADLINE_SECONDS`(600) → `failed` + `chat_queue_timeout` | `hdb.chat.queue_timeout` |
| `pending_turn_queue_state()` / `turn_queue_context(turn_id)` | 队列深度/最老时长/阻塞原因（`waiting_for_attachments` / `queued_behind_previous_turn`） | — |
| `task_metrics_summary(department_id, hours=24)` | 完成/失败/取消计数与失败率，`queue_ms/first_token_ms/total_ms` 均值 | — |

---

## 6. 消息、编辑与会话级操作

| 方法 | 行为 |
|---|---|
| `add_message(user_id, session_id, role, content)` | role ∈ {user,assistant,system}；user 消息在标题仍为「新对话」时取前 32 字做标题 |
| `list_messages` | LEFT JOIN turns 补 footer/turn_id/turn_summary/turn_document_context |
| `history_before_turn(user_id, turn_id, limit=5)` | 最近 N 个 **completed** 轮（user+assistant），按时间正序 |
| `edit_message(...)` | 校验归属；`redact` → `[已脱敏]`；写 `chat_message_edits`（含旧内容与 sha256）；**失效相关记忆**；session touch；可选重新入队项目反思（`force=True`，带 guard） |
| `clear_session` | 删消息、重置标题、`_reset_execution_state`（generation++）、会话记忆失效 |
| `delete_session` | **先**写附件清理 outbox（`require_session_absent=True`），再事务删除会话（FK 级联清 turns/messages/events） |
| `get_session_memory_settings` / `set_session_auto_extract` | 会话级记忆开关；关闭时取消 pending `project_reflection` job 并写 `memory_audit_events` |
| `list_session_document_cards` | 从 turn 事件提取 `document_card` 卡（历史回填） |
| `list_turn_attachments` / `for_session` / `_mark_deleted_attachment_snapshots` | 冻结快照 + 当前删除态/解析态的叠加视图 |
| `list_waiting_turns_for_asset(asset_id)` | 附件结算回调入口 |

---

## 7. PostgreSQL 控制面

### 7.1 版本化 schema（`src/storage/control_plane/schema.py`）

- `SCHEMA_VERSION = "1"`，记录表 `control_plane_schema(key PK, value, updated_at)`；
  `assert_schema_compatible` 在 `/health/ready` 与启动时校验，不匹配即 not-ready。
- 核心表：`chat_sessions, chat_messages, chat_turns, chat_turn_events, conversation_execution_state,`
  `turn_attachments, chat_message_edits, action_receipts`。
- 与 SQLite 的差异：整型 PK → `BIGINT GENERATED BY DEFAULT AS IDENTITY`；`*_json` → `JSONB`；
  `*_at` → `TIMESTAMPTZ`；0/1 → `BOOLEAN`；turn status 用 `CHECK (status IN (...))`。
- 部分索引覆盖（`PARTIAL_INDEX_OVERRIDES`）：记忆相关唯一索引（`uq_memory_jobs_*`）必须显式列出，
  否则生成器改名会「响亮失败」；FK 在数据装载后添加。

### 7.2 并发与锁顺序

- 连接池：`psycopg_pool.ConnectionPool`（min 1 / max `CONTROL_PLANE_POOL_SIZE`），
  `options="-c TimeZone=UTC -c statement_timeout=30000"`，`row_factory=dict_row`。
- **所有时间来自 DB 时钟**（`clock_timestamp()`），租约用 `make_interval(secs => %s)`，
  避免多实例时钟漂移；`_LEASE_SECONDS = 120`。
- 锁顺序：`conversation_execution_state → chat_turns → chat_turn_events`；
  `_load_state(..., skip_locked=True)` 用 `FOR UPDATE SKIP LOCKED`，
  热点会话不会阻塞其它会话的 claim。
- 事件追加用单条 CTE：`WITH bump AS (UPDATE chat_turns SET last_event_seq = last_event_seq + 1 ... RETURNING)`
  `INSERT INTO chat_turn_events ...`。
- `claim_turn`：拿执行态（busy → `blocked`）→ 回收自身过期租约 → 取消收敛 → 顺序/租约检查 →
  重试上限 → 生成 `attempt_id` 并 bump token → 条件领取 `chat_turns`（失败抛 `RuntimeError`，说明并发被抢）。
- `complete_turn`：owner + `expected_generation` 校验 → 写回答 → 条件 UPDATE
  （`status NOT IN ('completed','cancelled')`，None → `StaleTurnOwner`）→ 同事务写终态事件 →
  committed checkpoint ref 的 CAS → `domain_writer(conn, turn)`。

### 7.3 域写入必须同事务

`terminal_domain.write_terminal_domains(conn, turn, owner_user_id=...)` 在**完成 turn 的同一事务**里写：

- 结果导出快照 + 导出任务 + outbox（受 `RESULT_EXPORT_ENABLED` 与 `turn.export_plan` 控制）；
- 记忆 reflection outbox（项目记忆入队）。

失败则答案、状态、事件三者一并回滚——**不存在「回答成功但导出任务丢失」的中间态**。

### 7.4 迁移装载器（`loader.py`）

`load_sqlite_snapshot(source, conn, *, tables=None, batch_size=500, dry_run=False, ...) → LoadReport`：

- 只读打开 SQLite（`mode=ro`）；跳过派生表（`_fts*`）与瞬态表（`worker_instances`）；
- 目标非空 → 拒绝（必须停机窗口）；
- 逐表批量插入 + 行数校验 + **内容 SHA-256 校验**，状态 `ok|count_mismatch|hash_mismatch|skipped|error|verify_error|dry_run`；
- 装载后：补 `conversation_execution_state`、建索引与 FK、推进 identity 序列、
  执行 `_authority_checks`（状态合法、`turn_sequence` 非空且唯一、`last_event_seq == COUNT(events)`、
  每个 session 有执行态、无孤儿 `active_turn_id`）。

---

## 8. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `CONVERSATION_SERIALIZE_SAME_THREAD` | `false` | 严格同会话串行 + 租约/栅栏；关闭时仍允许并发多轮（fencing_token 恒 0） |
| `CHAT_TURN_HEARTBEAT_TTL_SECONDS` | `90` | worker 心跳过期阈值（claim 回收判据） |
| `CHAT_TURN_HEARTBEAT_INTERVAL_SECONDS` | `10` | 心跳间隔 |
| `CHAT_TURN_LEASE_SECONDS` | `120` | 会话租约时长（下限 30） |
| `CHAT_TURN_LEASE_RENEW_SECONDS` | `15` | 续租步长 |
| `WAITING_TURN_MAX_SECONDS` / `WAITING_TURN_TIMEOUT_SCAN_SECONDS` | `600` / `5` | 附件等待上限与扫描间隔 |
| `CHAT_QUEUE_DEADLINE_SECONDS` | `600` | 排队超时 |
| `CANCEL_FAST_PATH_ENABLED` / `CANCEL_KEY_TTL_SECONDS` | `true` / `7200` | Redis 取消键 |
| `TURN_EVENT_STREAM_TTL_SECONDS` / `MAXLEN` | `7200` / `5000` | Redis 事件流 |
| `REDIS_URL` | `""` | 为空则禁实时平面（重启生效） |
| `CONTROL_PLANE_BACKEND` / `_DATABASE_URL` / `_POOL_SIZE` | `sqlite` / `""` / `8` | 控制面后端（重启生效） |
| `WORKER_TURN_CONCURRENCY` | `1` | 单 worker 的 turn 并发 |

---

## 9. 排障与已知不一致

| 现象 | 原因/处理 |
|---|---|
| turn 永远停在 `pending` | worker 没起（`/health/dependencies` 看 worker 注册表）；或串行化下被前一个非终态 turn 阻塞（`turn_queue_context` 的 `queue_reason`） |
| turn 卡在 `streaming` 但无输出 | worker 崩溃：等 90s 心跳过期后 `requeue_stale_turns` 回收；也可直接取消 |
| 重复提交产生两个 turn | 没带 `client_request_id`；带的话唯一索引保证幂等 |
| 「回答完成」但前端没收到 | SSE 断了会重连并带 `Last-Event-ID`；若事件已过期（Redis TTL），从 DB 按 `after` 重放 |
| `CONVERSATION_SERIALIZE_SAME_THREAD` 默认值陷阱 | `settings` 默认 `false`，而 `claim_turn_result` 里读的是 `getattr(..., True)`；因为键总是存在，实际生效值是 **false**。生产如需严格同会话顺序必须显式打开 |
| `lease_lost` 标志 | 心跳线程会 set 但当前无消费者；真正生效的是它同时设置的 cancel event |
| 多实例下 PG 未启用 | 会话权威漂移；必须 `CONTROL_PLANE_BACKEND=postgres` 且所有实例共享（见 [18](18-observability-and-ops.md)） |
