# 08 · 摄取管道与 RAGFlow 检索

对应代码：`src/pipelines/`（registry / ingestion / runtime / runtime_factory / document_store_sqlite /
document_rag / spreadsheet）、`src/ingestion/`、`src/services/`、`src/core/source_group_router.py`。

---

## 1. 文件路由（`pipelines/registry.py`）

### 1.1 常量

```python
DATASET_TABLE / CIRCUIT / CONVERSATION = "table" / "circuit" / "external_conversation"
CONTENT_KIND_{DOCUMENT,SPREADSHEET,CIRCUIT,EXTERNAL_CONVERSATION}
PROCESSOR_KIND_{RAGFLOW=ragflow, SPREADSHEET=spreadsheet_table,
                CIRCUIT=circuit_design, EXTERNAL_CONVERSATION=external_conversation}
PIPELINE_STAGE_{RETRIEVAL, STRUCTURED} = "retrieval" / "structured"
TEXT_ROUTABLE_EXTENSIONS = {".txt", ".md", ".markdown"}
EXTERNAL_SOURCE_GROUP = "外部数据"
```

`normalize_extension`：去空白、小写、补前导点（`"PDF"`/`".PDF"`/`"pdf"` → `".pdf"`）。
`PipelineSpec` 为冻结对象：`key, label, processor_kind, content_kind, supported_extensions,`
`rejected_extensions, stage, dataset_kind, description`。
`PipelineRegistry.register` 拒绝重复 key，并**拒绝两个 spec 的支持扩展名重叠**
（`ValueError("Pipeline extension conflict ...")`）——所以新增扩展名要先想清楚归属。

### 1.2 注册表（唯一事实）

| key | processor_kind | content_kind | supported | rejected | stage | dataset_kind |
|---|---|---|---|---|---|---|
| `document_rag` | `ragflow` | `document_text` | `.doc` `.docx` `.pdf` | — | retrieval | — |
| `spreadsheet` | `spreadsheet_table` | `spreadsheet_table` | `.xlsx` | **`.xls`** | structured | `table` |
| `circuit_design` | `circuit_design` | `circuit_design` | `.edf` `.edif` | — | structured | `circuit` |
| `external_conversation` | `external_conversation` | `external_conversation` | `.txt` `.md` `.markdown` | — | structured | `external_conversation` |

### 1.3 路由算法（`route_file(file_path, source_group)`）

```python
ext = normalize_extension(file_path)
if ext in TEXT_ROUTABLE_EXTENSIONS:                 # .txt/.md/.markdown
    if source_group == "外部数据": return external_conversation
    return document_rag                              # 有意绕过 ragflow 的扩展名集合
命中 supported → 对应 spec
命中 rejected → PipelineRoute(rejected_by=spec, reason=".xls is explicitly rejected ...")
否则 → reason="no configured pipeline"
```

`.xls` 走 rejected 分支：不入库、不归档，返回用户提示
「当前不支持 .xls 归档或解析，请在 Excel 中另存为 .xlsx 后重新上传。」并审计 `upload_unsupported_file`。
`.xlsm` **不在** KB 注册表内（只有附件管道接受，见 [12](12-attachments.md)）。

辅助函数：`route_file` / `pipeline_for_file` / `pipeline_for_processor_kind` /
`configured_extensions()` / `rejected_extensions()`。

---

## 2. `IngestionOrchestrator`（`pipelines/ingestion.py`）

### 2.1 构造与数据对象

```python
IngestionOrchestrator(*, backend_name, store: PipelineDocumentStore, archive,
                      handlers: dict[str, PipelineHandler], audit_callback,
                      content_hash_callback, remote_document_exists_callback)
IngestionScope(kb_name, department_id, kb_id=None, uploaded_by="", source_group=None,
               ctx=None, embed_depth=0, parent_document="")
ArchivedFile(original_path, archived_path, filename, source_group, relative_local_path,
             file_size, content_hash, inspection)
HandlerResult(success, message, document_id="", record_id=None, status="",
              uploaded_to_remote=False, warnings=[], audit_action="", audit_metadata={},
              preserve_failed_record=False)
HandlerDeleteResult(ok, message, errors=[], audit_action="")
HandlerProcessResult(ok, status, message, progress=100, audit_action="",
                     audit_metadata={}, warnings=[])
```

`PipelineHandler` ABC：`existing_record_dataset_kind` / `submit`（抽象），
`can_reuse_existing` / `reuse_message` / `on_stale_existing` / `rollback` /
`delete_record` / `process_record` / `cleanup_failed_process`（可覆写）。

### 2.2 `upload_files(files, scope, default_dataset_kind, default_dataset_id, progress_callback)`

逐文件（均在上锁范围外做审计/消息汇总，上锁范围内做去重+归档+提交）：

1. `route_file` + handler 查找；`.xls` → 跳过 + 审计；无 handler → 跳过 + 审计；
2. 流式 SHA-256（1 MiB 块）；
3. **进程内锁**：`_ingest_lock(scope, content_hash)`（键 `dept:kb:hash`，引用计数 RLock）；
4. **去重复用**：`store.find_by_hash(kb, dataset_kind, hash, dept)` 命中且状态非 `failed/deleted`：
   - `_existing_record_is_reusable` 三态判定：
     - `shared_read_only` → RAGFlow 远端存在性必须为 True（或 `can_reuse_existing`）；
     - 本地归档必须存在；RAGFlow 远端存在性三态：True/False/None（None → 抛错保留映射，绝不破坏性操作）；
   - `True` → 复用（嵌入子文件补父引用；按需重新展开历史父文件的嵌入子项）；
   - `False` → `on_stale_existing` + 删旧归档 + 删旧行，继续走全新提交；
5. **归档**：`archive.archive_source_file(...)` → 写 `storage/pipeline_archives/...`；
   `inspect_container_file` 统计 OOXML 嵌入/媒体对象；`file_size`；
6. `handler.submit(scope, archived, default_dataset_kind, default_dataset_id, progress_callback)`；
7. 成功后：嵌入子项补 `update_embedding_meta`；`embed_depth==0` 时展开 OOXML 嵌入对象（见 §5）；
8. 失败：默认清理（rollback 远端 + 删归档 + 删行）；`preserve_failed_record=True` 时保留；
9. 异常：rollback + 删除归档 + 审计 `pipeline_upload_failed` + `[failed] {file}: {exc}`；
10. 表格任务被触及时做 **worker 存活告警**（`worker_registry` 心跳过期 → 提示启动 worker）。

返回值 `IngestResult(success_count, total_count, messages, backend, failed_count, skipped_count)`，
`status ∈ {success, partial, failed}`。

### 2.3 幂等键汇总

| 键 | 位置 |
|---|---|
| `UNIQUE(kb_name, department_id, document_name, dataset_kind)` | `pipeline_documents` |
| `(kb_name, dataset_kind, content_hash, department_id)` | `find_by_hash` 去重查询 |
| `document_id = table:{hash[:16]} / circuit:{hash[:16]} / conversation:{hash[:16]}` | 本地处理器 |
| `document_id = <RAGFlow 文档 id>` | RAGFlow 处理器 |
| 归档文件名冲突 | 最多重试 20 次：`{base}_{time_ns}_{uuid4[:8]}{ext}` |
| 嵌入子项 | 父内 SHA-256 去重；同名追加 `#<hash[:6]>` |
| parse claim | `UPDATE ... WHERE id=? AND worker_id=''` 后 `SELECT ... WHERE worker_id=?` |

---

## 3. 四个 Handler 的行为

### 3.1 `RAGFlowDocumentHandler`（`ingestion.py:587-723`）

- dataset kind 取调用方（由 source_group 映射，见 §6.2）；
- `submit`：`submit_remote(...)` → 台账 upsert（`status=parsing`, progress 5，
  `parse_stage="已提交到 RAGFlow 解析队列"`）→ 返回记录；
  DB 异常时远端清理 + 删台账行（不泄漏半成品）；
- `rollback`：远端删 + 台账删；
- `delete_record`：共享只读 → 拒绝（审计 `shared_document_mutation_denied`）；
  否则远端删 + 归档删 + 台账删（审计 `ragflow_delete_document`）；
- 无 `process_record`：解析在 RAGFlow 侧异步进行，状态由 `refresh_parse_statuses` 与
  按需刷新对齐。

### 3.2 `SpreadsheetPipelineHandler`（`ingestion.py:1241-1470`）

- `submit` **只入队**：台账 `status=archived`, progress 5, `parse_stage="已归档，等待表格结构化解析"`，
  调用 `ensure_worker()`（在 `PipelineRuntimeFactory` 中绑定到 `PipelineRuntime.ensure_worker_running()`，
  **当前是有意为之的 no-op**：上传请求绝不内联解析）；
- `can_reuse_existing`：`dead_letter` → False（销毁重建）；可重置状态 → 重置为 `archived` + progress 5
  + 清错误 + `ensure_worker()` → True；
- `process_record`（worker 路径）：解析归档路径 → `parse_index(SpreadsheetIndexRequest(...))`
  → 进度 10「Preparing Excel structure parsing」→ 结果写回（`status=indexed|degraded`，progress 100）
  → `release_parse_claim`；审计 `spreadsheet_upload_indexed|processed`；
- `cleanup_failed_process` → `delete_index(record)`；
- `delete_record`：先删索引（失败则保留行可重试）→ 归档 → 台账行。

### 3.3 `CircuitPipelineHandler`（`ingestion.py:726-906`）

- `submit`：台账 upsert（`archived`）→ **同步** `CircuitIndexService.index_file(...)` →
  状态仅允许 `indexed|degraded`（否则降级）→ progress 100；
- 失败：`HandlerResult(success=False, preserve_failed_record=True)`——**保留行与归档**供排查；
- `rollback` → 删台账行；`delete_record` → 先删索引，再归档 + 行。

### 3.4 `ExternalConversationHandler`（`ingestion.py:908-1239`）

写入顺序（契约 C4）：`archive（编排器）→ conversation.json → index.db → 台账 upsert`；删除逆序。

- `submit`：台账 `parsing`/progress 10 → 解析（`parse_external_conversation`）→
  `conversation_store.save(...)` → `index_conversation` → 向量索引（fail-soft）→
  **后台单飞** LLM 结构化/摘要（`_spawn_postprocess`，模块级锁 + inflight 集合）→
  把 `document_id` 改指向真实 `conversation_id`，状态 `indexed`；
- 失败：`preserve_failed_record=True` + 审计 `external_conversation_failed`；
- `delete_record`：向量 → 索引 → 目录 → 归档 → 台账行，逐级收集错误，任一失败 → `ok=False`。

---

## 4. 解析台账（`pipelines/document_store_sqlite.py`）

### 4.1 表

```sql
pipeline_datasets(kind PK, dataset_id, dataset_name, updated_at)         -- 物理数据集映射
pipeline_documents(... 见下)                                            -- 文档/任务台账
pipeline_document_profiles(record_id PK, kb_name, department_id, remote_document_id,
    parse_status, content_hash, source_version_id, revision, mpn_values JSON,
    manufacturer, identity_origin JSON, confirmed_at, updated_at)
pipeline_profile_outbox(id PK, record_id, event_kind, payload JSON, created_at, processed_at)
```

`pipeline_documents` 关键列（全量见源码 147-183）：

```
id, kb_id, kb_name, document_name, original_file_name,
dataset_kind, dataset_id, document_id, source_group, department_id, uploaded_by,
status, content_kind, processor_kind, shared_read_only, local_path, file_size,
content_hash, upload_status, error_message, ragflow_error, last_status_checked_at,
parse_started_at, parse_completed_at, parse_progress, parse_stage,
worker_id, worker_started_at, worker_heartbeat_at, retry_count,
asset_id, logical_document_id, source_version_id, project_id, document_role,
module_scope_json, revision, approval_status, effective_from, effective_to, usage_type,
embed_depth, parent_document, created_at, updated_at,
UNIQUE(kb_name, department_id, document_name, dataset_kind)
索引: ix_pipeline_documents_claim(processor_kind,status,worker_id,retry_count,updated_at,id)
      ix_pipeline_documents_lease(processor_kind,worker_id,worker_heartbeat_at)
```

常量：`WORKER_STALE_SECONDS=1800`、`WORKER_MAX_RETRIES=3`、
`TERMINAL_PARSE_STATUSES={"parsed","failed","deleted","indexed","degraded"}`。
**所有 department 参数必填**（`_require_department_id`，缺失抛 `ValueError`）。

### 4.2 状态机与归一化

`normalize_parse_status(raw, processor_kind)`：

| 归一 | 归一前 |
|---|---|
| `queued` | `0, queued, pending, uploading, uploaded, ready, archived` |
| `running` | `1, running, parsing, processing, started` |
| `completed` | `2, done, success, parsed, completed, complete, finish, finished, indexed, degraded, 已完成` |
| `failed` | `3, fail, failed, error, exception, unsupported` |
| `cancelled` | `cancel, cancelled, canceled, stopped, stop, deleted, removed` |
| `paused` | `paused` |
| `unknown` | 其它 |

实际写入的原始状态：

| 阶段 | status / upload_status | stage | progress |
|---|---|---|---|
| RAGFlow 提交 | `parsing` / `parsing` | 已提交到 RAGFlow 解析队列 | 5 |
| RAGFlow 远端完成/失败 | `parsed` / `parsed`（或 `failed`） | RAGFlow 解析完成 / 错误 | 100 / — |
| 表格提交 | `archived` / `archived` | 已归档，等待表格结构化解析 | 5 |
| 表格 claim | `processing` / `processing` | 准备解析 Excel | ≥10 |
| 表格成功 | `indexed`（或 `degraded`） | 结果消息 | 100 |
| 表格失败 | `failed` | 解析失败 | 100 |
| 重试耗尽 | `dead_letter` | Exceeded maximum background parse retries | — |
| 租约过期回收 | `queued` | Worker timeout; queued for retry | 不变 |
| 重新排队 | `queued` | 重新排队等待解析 | — |
| 电路成功/失败 | `indexed`/`degraded`；`failed` | — | 100 |
| 外部对话 | `parsing`→`indexed` | 解析外部对话记录 | 10→100 |

> `parse_completed_at` 只在 `{parsed, failed, deleted, indexed, degraded}` 时写；
> `cancelled`/`completed`/`dead_letter` **不会**更新该时间戳（历史遗留）。

### 4.3 关键方法

| 方法 | 语义 |
|---|---|
| `claim_next_parse_record(worker_id, processor_kinds=None, stale_after_seconds, max_retries)` | ① 过期租约回收（清 worker 字段 → `queued`）；② dead-letter 扫描（`retry_count>=max_retries` → `dead_letter`）；③ 挑最老可领取行；④ CAS 领取（`WHERE id=? AND worker_id=''`），确认后返回记录 |
| `release_parse_claim(record_id)` | 仅清 worker 字段 |
| `mark_document_failed_by_id` | progress 100 + `failed` + 释放 claim |
| `update_document_progress_by_id` | clamp 进度；**滑动** `parse_started_at/worker_heartbeat_at/updated_at`（避免慢解析被静态超时窗口误杀）；终态补 `parse_completed_at`；`completed` 时触发影子资产钩子 |
| `update_document_status[_by_id]` | 状态位 + 错误字段 + `last_status_checked_at`；写 outbox `parse_status`；首次 `completed` 触发影子资产 |
| `list_status_refresh_candidates` / `mark_status_checked` | 远端状态对齐节流（默认 ≥60s 间隔） |
| `requeue_dead_letter_records` | 重置为 `queued`、`retry_count=0`、清错误、stage「重新排队等待解析」 |
| `delete_document[_by_id[_scoped]]` / `delete_document_by_remote_id` / `delete_documents_by_kb[_unscoped]` | 删除路径 + outbox `deleted` |
| `find_by_hash` / `get_document[_unscoped]` / `list_documents[_unscoped]` | 查询（unscoped 仅治理/维护用；同名多行抛 `ValueError`） |
| 嵌入子项 | `update_embedding_meta` / `add_parent_reference` / `list_embedded_children` / `remove_parent_reference`（返回是否孤儿） |
| profiles | `confirm_document_identity`（冻结 MPN/厂商/来源）、`get_document_profile`、`rebuild_document_profiles`、`record_profile_event`、`list_pending_profile_events`、`mark_profile_event_processed` |
| 治理统计 | `document_stats_by_kb(department_id)` / `document_stats_by_kb_identity()` |

---

## 5. 归档、嵌入对象与资产清理

### 5.1 归档布局

```
PIPELINE_ARCHIVE_ROOT/
└── departments/<safe(dept) or "unknown">/kbs/<validate_kb_name(kb)>/<safe(source_group)>/<file>
（兼容旧路径：PIPELINE_ARCHIVE_ROOT/<kb>/<source_group>/<file>）
```

`archive_source_file` 使用 **`open(..., "xb")` 独占创建**避免覆盖；冲突时换名重试 20 次。
台账里存的是相对 KB 归档目录的路径（`<source_group>/<filename>`）。

### 5.2 OOXML 嵌入对象（`src/ingestion/embedded_extractor.py`）

- 白名单：`.docx/.xlsx/.pptx`；扫描 `word/embeddings/`、`xl/embeddings/`；
  载荷本身必须是 zip（OLE `.bin` 跳过）；
- 内容哈希去重（父内）；docx 标题取首段文本（`word/document.xml` 段落拆分 + 所有 `w:t` 拼接），
  截断 60 字符；子文件名 `{父stem} [嵌入] {title}.{ext}`，冲突加 `#<hash[:6]>`；
- 编排：写入临时目录 → 以 `embed_depth+1`、`parent_document=父名` 递归 `upload_files`；
  `embed_depth>0` 禁止再展开；
- 台账：父引用计数（`add_parent_reference`）；父删除时 `_delete_embedded_children` 仅删除孤儿；
- `container_inspector.inspect_container_file` 只做 zip 成员计数（不解析内容），
  用于告警与审计 metadata。

### 5.3 资产清理（`services/pipeline_asset_cleanup.py`）

`cleanup_knowledge_base(kb, department_id)` 依次 rmtree（缺路径跳过、错误收集）：
部门归档 → 表格索引目录 → 外部对话目录 → 旧归档根 → `RAGFLOW_FILE_ROOT`（若不同）。
由 `DocumentManager.delete_knowledge_base_documents` 在 backend 删除成功**之后**调用；
部分失败返回 `partial=True` + 文案「知识库已删除，部分归档资产清理失败。」

### 5.4 生命周期与保留

| 对象 | 规则 |
|---|---|
| KB 归档 / 表索引 | **无 TTL**，生命周期绑定台账行（删文档/删库即删） |
| 解析失败 | circuit/external conversation 保留；RAGFlow/表格提交失败回滚删除；表格后台失败保留 `failed` 行 |
| dead_letter | 需显式 `POST /kbs/{kb}/parse-tasks/requeue-dead-letters` |
| 租约 | 心跳过期 1800s 由下次 claim 回收 |
| RAGFlow 解析看门狗 | 远端确认 running 超 `RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS=3600` → 本地 `failed` + 删远端文档（**UTC 解析关键**，否则 +8h 误判） |

---

## 6. RAGFlow 后端

### 6.1 `RAGFlowClient` HTTP 面

构造：`base_url` 去尾斜杠；`RAGFLOW_API_KEY` 为空时**构造即抛错**；
进程级 `httpx.Client(timeout=RAGFLOW_TIMEOUT_SECONDS, follow_redirects=True)`。
`request()` 对 JSON 响应检查 `code ∉ {None, 0}` → `RAGFlowAPIError`（`code==102` = 非 owner）。

| 方法 | HTTP | 路径 |
|---|---|---|
| `list_datasets(name=None)` | GET | `/api/v1/datasets` |
| `create_dataset(name)` | POST | `/api/v1/datasets` |
| `ensure_dataset(name)` | GET+POST | 存在则复用 |
| `upload_document(dataset_id, file_path)` | POST | `/api/v1/datasets/{id}/documents`（multipart） |
| `update_document_metadata(dataset_id, doc_id, metadata)` | PUT | `.../documents/{doc_id}`（`{"meta_fields": ...}`） |
| `parse_documents(dataset_id, ids)` | POST | `/api/v1/datasets/{id}/chunks` |
| `stop_parse_documents(...)` | DELETE | 同上 |
| `wait_document_ready(dataset_id, doc_id)` | 轮询 | 0.5s 间隔，超 `RAGFLOW_DOCUMENT_READY_TIMEOUT_SECONDS=10` |
| `delete_documents(dataset_id, ids)` | DELETE | `.../documents` |
| `list_documents(dataset_id, document_id=None)` | GET | `.../documents` |
| `list_chunks(dataset_id, doc_id)` | GET | `.../documents/{id}/chunks` |
| `retrieve(question, dataset_ids, top_k, metadata_condition, should_cancel)` | POST | `/api/v1/retrieval` |
| `health()` | GET | `/api/v1/datasets` |

`retrieve` 载荷：

```json
{"question":"...","dataset_ids":[...],"page":1,"page_size":top_k,
 "similarity_threshold":0.25,"vector_similarity_weight":0.4,"top_k":top_k,
 "metadata_condition":{...}}
```

包裹 `governor_acquire("rag", interactive=True)` + `hdb.rag.active_requests`；零命中记
`hdb.rag.zero_hit`。取消：`request_cancellable` 在工作线程跑 asyncio 任务，取消事件触发时
取消在飞 HTTP（抛 `QueryCancelled`）。

### 6.2 两个物理数据集

| dataset kind | 配置键 | 默认名 | source_group 映射 |
|---|---|---|---|
| `governance` | `RAGFLOW_GOVERNANCE_DATASET_NAME` | `department_governance` | 项目管理数据、人员与组织数据 |
| `design` | `RAGFLOW_DESIGN_DATASET_NAME` | `project_design_assets` | 文档资料、外部数据、设计数据、物料数据、测试数据、未分类；网表/原理图/未知 → design |

映射表 `SOURCE_GROUP_DATASET_KIND`（`ragflow_backend.py:100-109`）。
`_ensure_physical_datasets`：从 `pipeline_datasets` 读映射；保存的名字与当前配置名不一致时
重新 `ensure_dataset`（重命名即重建）；类级 `_DATASET_LOCK` 保护创建。

文档 metadata（`update_document_metadata` 写入的 `meta_fields`）：

```json
{"kb_name", "logical_kb_id", "kb_id", "department_id", "source_group",
 "uploaded_by", "original_file_name"}
```

### 6.3 提交序列（`_submit_archived_document`）

```
1. upload_document
2. update_document_metadata
3. wait_document_ready（≤10s）；code 102 → 记 warning（当前 API key 读不到进度），其它错误 = 失败
4. sleep(RAGFLOW_PARSE_START_DELAY_SECONDS = 2.0)
5. parse_documents([doc_id])
6. progress(5, "已提交到 RAGFlow 解析队列")
异常 → _cleanup_remote_document（尽力删远端）后重新抛出
```

### 6.4 `retrieve()` 算法（`ragflow_backend.py:1132-1389`）

```
Step 0 权限：_check_kb_access(kb, ctx, "read") + KbScope.require_department()
Step 1 本地授权记录：store.list_documents(kb, dept) → 仅 processor=ragflow 且
       normalize_parse_status == "completed" 的记录进入 records_by_remote_id
Step 2 严格链接模式（filters 提供 allowed_record_ids 时）：
       - 非法/非列表 → []（fail-closed 什么都不给）
       - 授权集合必须与请求集合完全一致，否则 []
       - link_stamps（content_hash/source_version_id/revision）不一致 → []
       - 该模式下禁用所有回退
Step 3 source_group 路由：route_source_groups(query)（见 §7）；
       balanced_route=true → 不做硬过滤；否则 confidence ≥ 0.7 才生成硬过滤组
Step 4 数据集扇出：有 shared_read_only 记录 → 逐数据集调用后合并按分数降序；
       否则一次性调用
Step 5 硬 metadata 过滤：
       {"logical_operator":"and","conditions":[
          {"name":"kb_name","comparison_operator":"=","value":kb},
          [{"name":"department_id","comparison_operator":"=","value":dept}]
          [source_group 单个 "=" / 多个 "in"]
          [source_names 单个 "=" / 多个 "in" 于 original_file_name]
       ]}
Step 6 首次检索；非严格模式且 0 命中 → 去掉全部 metadata_condition 重试
       （标记 metadata_condition_fallback）
Step 7 本地校验（_filter_chunks）：chunk → 本地记录映射；kb/department/source_group
       一致性；source_names 过滤；构造 Evidence
       （metadata 带 route 原因/置信度、fallback 标记、local_record_id 等）
Step 8 文件名回退：仍无证据且提供 source_names 且此前是因 source_name 丢弃过 chunk →
       再次无过滤检索 + 重过滤
返回 evidences[:top_k]
```

**分数**：原样使用 RAGFlow 的 `similarity|score|vector_similarity`（不做归一化/重排）；
阈值与向量权重由服务端参数生效（0.25 / 0.4）。
`SourceGroupRoute.weights` 会被计算但**没有任何消费者**（只有 `source_groups` 与 `confidence` 生效）。

### 6.5 错误与状态处理

| 情形 | 行为 |
|---|---|
| `code==102`（非 owner） | 特殊处理：提交阶段记 warning 继续；删除阶段本地清理并返回成功；存在性判定返回 True（保留映射） |
| 404 / "not found" | `_is_ragflow_not_found_error` → 远端确定不存在 |
| 状态不可读（102） | 用户可见 warning「realtime progress is not readable by the current API key」，错误字段不显示 |
| 传输异常 | 原样抛出；`refresh_parse_statuses` fail-soft |
| 解析超时 | 见 §5.4 看门狗；共享只读记录豁免 |

### 6.6 其它 `RAGFlowBackend` 方法

`list_knowledge_bases`（AuthService 注册表）、`create_kb_storage`（no-op）、
`upload_files`（dataset kind 选择 + IngestionScope）、`refresh_parse_statuses`（节流对齐）、
`delete_document`（权限 + 共享检查 + `runtime.delete_record` + 影子资产同步 + 嵌入子项级联）、
`delete_knowledge_base`（拒绝含共享只读记录；部分失败 `ok=False`）、
`requeue_dead_letter_parse_tasks`、`list_parse_tasks` / `delete_parse_task`（**合成任务视图**）、
`list_documents` / `get_parse_result`（分块）/ `health_check`。

`_wait_archived_document_result` / `_wait_parse_progress` 是**死代码**（无调用者）。

---

## 7. Source group 分类与查询路由

### 7.1 10 个分组（`src/ingestion/source_groups.py`）

| # | 常量 | 标识 | 用户可选 |
|---|---|---|---|
| 1 | `DOCS_GROUP` | `文档资料` | ✅ |
| 2 | `MATERIAL_GROUP` | `物料数据` | ✅ |
| 3 | `DESIGN_GROUP` | `设计数据` | ✅（伞形） |
| 4 | `NETLIST_GROUP` | `网表数据` | 仅内部 |
| 5 | `SCHEMATIC_GROUP` | `原理图数据` | 仅内部 |
| 6 | `TEST_GROUP` | `测试数据` | ✅ |
| 7 | `PROJECT_GROUP` | `项目管理数据` | ✅ |
| 8 | `EXTERNAL_GROUP` | `外部数据` | ✅ |
| 9 | `PEOPLE_GROUP` | `人员与组织数据` | ✅ |
| 10 | `UNKNOWN_GROUP` | `未分类` | 兜底 |

其它：`USER_SELECTABLE_SOURCE_GROUPS`、`SOURCE_GROUP_DESCRIPTIONS`、
`SOURCE_GROUP_DISPLAY_NAMES`（netlist/schematic/design 统一显示为「设计数据」）、
`DESIGN_UMBRELLA_SUBGROUPS`、`IMPLEMENTED_PARSE_GROUPS`、
`safe_source_group`（非法 → 未分类）、`classify_source_group(filename)`（启发式置信度）、
`resolve_design_subgroup`、`expand_source_group_for_file`。

> 现状：`classify_source_group` / `expand_source_group_for_file` / `display_source_group`
> 在 `src/` 下**无调用者**；前端直接传具体分组，后端只做 `safe_source_group`。

### 7.2 查询意图路由（`src/core/source_group_router.py`）

按关键词给 source group 打分：

- 接口文档短路（`hsi/hardware software interface/接口文档`）→
  权重 `{各组 0.65, DOCS 1.25, DESIGN 1.2, UNKNOWN 0.5}`，分组 `(文档资料, 设计数据)`，confidence 0.85；
- 高置信（`best_hits ≥ 2` 且严格最大）→ 权重 `{各组 0.65, best 1.3, UNKNOWN 0.5}`，confidence 0.85；
- 低置信（恰好 1 命中）→ 分组保留但 confidence 0.55 → `should_filter=False`（**不硬过滤**）；
- 均衡 → 全 1.0，UNKNOWN 0.75，confidence 0.0。

`should_filter = bool(source_groups) and confidence >= 0.7`。
消费者只有 `RAGFlowBackend.retrieve`。

---

## 8. 解析任务面板（`src/ingestion/parse_tasks.py` + `routes/parse_tasks.py`）

`ParseTask` DTO 字段：`id, kb_name, source_path, original_name, source_group, created_by,`
`status, progress, stage, message, result, document_id, created_at, updated_at, started_at, finished_at`。
（旧的进程内 `ParseTaskManager` 已删除；现在由 `RAGFlowBackend.list_parse_tasks` 从台账合成。）

合成规则：
- **共享只读行整体跳过**；
- 表格行：已完成则隐藏；否则状态归一 + 进度（queued→10, running→65）；
- RAGFlow 行：命中 `RAGFLOW_HIDDEN_TASK_STATUSES` 隐藏；否则先刷新远端状态；
  刷新成功且 running 且超时 → 标记失败并清理远端；
  `_parse_task_state_from_ragflow_status` 映射进度/阶段（completed 100 / failed 100 / cancelled 100 /
  running 65 / queued 20 / unknown 40）。

端点契约：

| 端点 | 语义 |
|---|---|
| `GET /kbs/{kb}/parse-tasks` | 活动任务（需 write，拒绝 sysadmin） |
| `DELETE /kbs/{kb}/parse-tasks/finished` | 清理已完成（对 RAGFlow 实为 no-op） |
| `POST /kbs/{kb}/parse-tasks/requeue-dead-letters` | 重排队 |
| `DELETE /kbs/{kb}/parse-tasks/{task_id}` | 停止/删除（非终态先 `stop_parse_documents`；id 必须 `ragflow-<digits>`） |

---

## 9. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `PIPELINE_ARCHIVE_ROOT` | `storage/pipeline_archives` | 归档根 |
| `RAGFLOW_FILE_ROOT` | `= PIPELINE_ARCHIVE_ROOT` | RAGFlow 文件根 |
| `RAGFLOW_BASE_URL` / `API_KEY` | `http://localhost:9380` / `""` | 后端地址与密钥 |
| `RAGFLOW_GOVERNANCE_DATASET_NAME` / `DESIGN_DATASET_NAME` | `department_governance` / `project_design_assets` | 物理数据集名 |
| `RAGFLOW_TIMEOUT_SECONDS` | `120` | HTTP 超时 |
| `RAGFLOW_STATUS_REFRESH_MIN_SECONDS` | `60` | 状态对齐节流 |
| `RAGFLOW_SIMILARITY_THRESHOLD` / `VECTOR_WEIGHT` | `0.25` / `0.4` | 检索参数 |
| `FINAL_TOP_K` | `5` | 兜底 top_k |
| `SPREADSHEET_SKIP_BOILERPLATE_SHEETS` | `true` | 跳过模板/封面/变更历史表 |
| `WORKER_POLL_INTERVAL_SECONDS` / `WORKER_PARSE_BATCH_SIZE` / `WORKER_CLAIM_BATCH_SIZE` | `0.5` / `1` / `8` | worker 节拍 |
| `WORKER_ROLE` | `all` | `all|chat|authoring|parse` |
| `EXTERNAL_CONVERSATION_LLM_*` | `true/12000/true/60` | 外部对话 LLM 后处理 |

硬编码常量：`RAGFLOW_PARSE_START_DELAY_SECONDS=2.0`、`RAGFLOW_DOCUMENT_READY_TIMEOUT_SECONDS=10.0`、
`RAGFLOW_PARSE_PROGRESS_TIMEOUT_SECONDS=3600.0`、`WORKER_STALE_SECONDS=1800`、`WORKER_MAX_RETRIES=3`。

---

## 10. 已知坑（改代码前必读）

1. `route_file` 对 `.txt/.md/.markdown` 的 RAGFlow 重定向是**有意绕过扩展名表**的；
   若把这三个扩展名加进 `document_rag` 的 supported，注册表会因扩展名冲突直接抛错。
2. `.xlsm` 只被附件管道接受；KB 通道会走「无配置管道」跳过。
3. `SourceGroupRoute.weights` 无消费者。
4. `PipelineRuntime.ensure_worker_running()` 是有意 no-op；上传不会内联解析。
5. `PipelineRuntime.background_processor_kinds()` **只有** `spreadsheet_table`：
   RAGFlow/电路/外部对话的行永远不会被统一 worker 领取。
6. `_INGEST_LOCKS` 是进程内锁；跨进程幂等依赖 SQLite 唯一约束与每 KB 文件锁
   （并发上传同一内容理论上可能双双到达 RAGFlow 提交）。
7. `clear_finished_parse_tasks` 后端未覆写 → 对 RAGFlow 是 no-op（已完成行本来就被隐藏）。
8. 终端状态集合不含 `cancelled/completed/dead_letter`，这些状态不会更新 `parse_completed_at`。
