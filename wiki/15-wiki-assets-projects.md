# 15 · Wiki、资产中心与项目治理

对应代码：`src/core/wiki.py`（1938 行）、`src/core/doc_assets.py`（823 行）、
`src/core/assets.py`（573 行）、`src/projects/`（1075 行）、`src/api/routes/{wiki,doc_assets,assets}.py`。

三块「知识沉淀」能力：

| 子系统 | 把什么沉淀成什么 | 权威性 |
|---|---|---|
| **Wiki** | 文档/表格/电路解析结果 → 结构化知识页（summary/entity/concept/index） | 蒸馏知识；**不是引文权威** |
| **文档资产（doc_assets）** | 上传文件 → 逻辑文档 + 版本链 + 关系 + 生命周期 | 与解析台账严格同步 |
| **硬件资产（assets）** | 解析文本 → AI 候选 → 人工接受 → 受信资产记录 | 候选与资产严格分离 |
| **项目（projects）** | 项目/基线/来源版本/区域策略 → 冻结的 source set snapshot | 项目作用域治理 |

---

## 1. Wiki 子系统（`src/core/wiki.py`）

### 1.1 模型与常量

```python
PAGE_TYPES    = ("summary", "entity", "concept", "index")
PAGE_STATUSES = ("draft", "published", "archived")
EDIT_SOURCES  = ("pipeline", "agent", "user", "revert")
GRANULARITIES  = ("focused", "standard", "exhaustive")
MAX_REVISIONS_PER_PAGE = 50
MAX_CHUNK_CHARS_PER_BATCH = 12000
WIKI_MAX_BLOCKS_PER_DOC = 6 ; WIKI_MIN_CELL_DENSITY = 24
WIKI_JOB_STATE: dict[int, dict] + _WIKI_JOB_LOCK    # 进程内实时任务态
```

Slug 规则（`slugify`）：小写、空格转 `-`、仅保留 `[\w\u4e00-\u9fff-]`、折叠连字符、≤80 字符、
兜底 `untitled`。页面种类前缀 `entity/…`、`concept/…`、`summary/{doc_id}`、`index`。

### 1.2 表（`AUTH_DB_PATH`）

```sql
wiki_pages(id PK, department_id FK, kb_id FK, kb_name, slug, title,
  page_type DEFAULT 'entity', status DEFAULT 'published', content, summary,
  aliases_json, source_refs_json, chunk_refs_json, in_links_json, out_links_json,
  version DEFAULT 1, last_edit_source DEFAULT 'pipeline', created_at, updated_at,
  UNIQUE(kb_id, slug), INDEX(kb_id, page_type, status))

wiki_page_revisions(id PK, page_id FK CASCADE, version, title, page_type, status,
  content, summary, aliases_json, edit_source, editor_id, edited_at, created_at,
  UNIQUE(page_id, version))

wiki_jobs(kb_id PK, department_id, running DEFAULT 0, stage, error, stats_json,
  started_at, finished_at, updated_at)
```

### 1.3 蒸馏管道（`ingest`）

输入：`{"doc_id", "name", "chunks", "chunk_refs"}` 列表 + `chat_fn(prompt) -> str`（batch 优先级模型）。

```
Pass 0  每文档 CANDIDATE_PROMPT（附 granularity 提示）→ 实体/概念（name/slug/aliases/description）
        slug 归一（末段 + 类型前缀）；用 _page_name_index（title/slug 尾段/别名，忽略
        大小写/空格/-/_/·/.，仅同 page_type 前缀）做实体消解（_resolve_slug）
Map     引用回填：chunks 打包成 ≤12000 字符批次，渲染为 <c000>…</c000>；
        CITATION_PROMPT 询问每批实质性讨论的候选 + new_slugs（带 source_chunks）；
        未知句柄直接丢弃（防幻觉）；批次索引经 _batch_chunk_ranges 映射回精确 chunk_ref；
        slug_updates 累积 chunks/chunk_texts/chunk_refs/docs；
        每文档摘要页 summary/{doc_id}（SUMMARY_PROMPT：前 8 chunks × 1600 字符）；
        用户锁定页跳过
Reduce  每 slug PAGE_PROMPT 生成 markdown（[[slug]] 链接仅限已知 slug）；
        材料预算 = max(1500, 12000 // len(chunk_texts)) 每条（≤8 条）；无材料不建 stub；
        合并既有内容；aliases/source_refs/chunk_refs 并集；内容变化才 version++
Post    rebuild_links → cleanup_dead_links → _ensure_index_page
```

- 来源引用格式 `<doc_id>#<chunk>`；`_is_source_ref` 拒绝含 `/` 的旧式 `<slug>#chunk-n` 占位；
- 样板/模板表由 `_APPROVAL_RE`、`_TEMPLATE_SHEET_RE`、`_BOILERPLATE_TEXT_RE` 排除；
- 冲突合并 `merge_pages(source→target)`：并集别名/来源/chunk，合并正文（去源页 `## 来源`），
  重写 `[[source]] → [[target]]`，归档源页并重建链接；
- `_is_user_locked`（`last_edit_source == "user"`）→ 再蒸馏对该页只读；
- `stats` 键：`documents, candidates, cited, pages_written, uncited, skipped_user_edited,`
  `skipped_user_edited_slugs, errors`；`max_pages_per_ingest` 保留引用 chunk 最多的 slug。

### 1.4 检索排序（`search_pages`）

输入 `kb_id, department_id, query, limit=10(clamp 1..30), status='published',`
`expand_links=True, expand_limit=8`。

1. 用 `tokenize_hardware_query(needle, max_tokens=8)` 分词；无 token 且长度 ≥2 则用原文；
2. WHERE：`kb_id=? AND department_id=? AND page_type != 'index'`（+status），
   token OR 匹配 title/slug/summary/aliases/content（`LIKE ... ESCAPE '\'`）；
3. 每 token 的 `match_rank` 贡献：

```sql
CASE WHEN title LIKE ? THEN 3
     WHEN slug LIKE ? OR summary LIKE ? OR aliases_json LIKE ? THEN 2
     WHEN content LIKE ? THEN 1 ELSE 0 END
```

`match_rank` 为各 token 之和；`ORDER BY match_rank DESC, updated_at DESC LIMIT limit`；
4. `expand_links` 时从命中集 `out_links_json` + `in_links_json` 收集一跳邻居（排除命中），
   作用域内取出并标 `hop=1`、`via=<source slug>`，上限 `expand_limit`；
5. 评分：`base = min(1.0, 0.25 + 0.25*match_rank)`（rank 为 0 时 0.25），
   `score = base * (0.5 if hop else 1.0)`，保留 4 位；
6. **index 页永不参与检索**。

### 1.5 其它操作与 API

| 方法 | 说明 |
|---|---|
| `list_pages` | 默认只列 published；title/summary/aliases LIKE |
| `get_page` | 附带 `backlinks`（`out_links_json LIKE '%"slug"%'`）与修订列表 |
| `get_stats` | 各类型计数 + 孤儿数（`in_links_json='[]'` 且非 index） |
| `get_graph(view="pages"\|"documents")` | 节点带 `link_count`，边只连活 slug，最多 200 |
| `pages_for_source` | 某来源（doc/chunk）出现在哪些页 |
| `lint_pages` | 质量问题清单（带严重级别） |
| `upsert_manual` / `update_page` / `archive_page` / `revert_page` | 人工编辑族 |
| `gather_documents` | 构建蒸馏输入（解析分块 + 表格块 + 电路块，样板过滤） |
| `rebuild_links` / `cleanup_dead_links` | `[[slug]]` 优先，其次标题/别名子串（≥2 字符）；死链降级为纯文本 |
| `_ensure_index_page` | 按类型构建/更新 index 页（`[[slug]] title — summary`） |
| `_snapshot_revision` / `_prune_revisions` | 每页最多 50 个修订 |

路由（`routes/wiki.py`）：`POST /kbs/{kb}/wiki/ingest`、`GET .../wiki/status`、
`GET/POST .../wiki/pages`、`GET/PATCH .../wiki/pages/{slug:path}`、
`POST .../pages/{slug}/archive`、`POST .../pages/{slug}/revert`、`GET .../wiki/graph`、
`GET .../wiki/stats`、`GET .../wiki/lint`。

Ingest 语义：守护线程 + 进程内 `WIKI_JOB_STATE[kb_id]` + 持久化 `wiki_jobs`；
同一 KB 并发 ingest → 409；重启后持久化行 `running=true` 会显示为「中断（服务重启）」；
LLM 用 `create_chat_model(priority=PRIORITY_BATCH)`（与交互流量隔离）。

Agent 工具：`wiki_search(query, top_k, include_linked)` → 证据
`Evidence(id="wiki:{slug}", content_kind="wiki_page", processor_kind="wiki", score,`
`locator={slug,hop,via}, metadata={page_type,summary,out_links,version})`；
仅在 `wiki_assist=True` 时挂载。文档资产生成入口：`GET /kbs/{kb}/doc-assets/{id}/wiki`。

---

## 2. 文档资产（`src/core/doc_assets.py`）

定位：一个逻辑文档一行，部门+KB 作用域；每次上传成为版本；**「登记即生效」**——
新版本立即成为唯一 `effective`，旧版本转 `superseded`；生命周期严格绑定解析台账
（文件离开解析库，账目随之更新）。

### 2.1 常量

```python
LIFECYCLE_STATES = ("draft", "effective")
VERSION_STATES   = ("draft", "effective", "superseded", "deleted")
DOC_CATEGORIES   = (design_doc, schematic, bom, netlist, test_report, requirement, standard, other)
RELATION_TYPES   = ("derived_from", "companion", "references", "verified_by")
LINK_SOURCES     = ("manual", "ai", "structure");  LINK_STATUSES = ("suggested", "confirmed", "dismissed")
```

### 2.2 表

```sql
doc_assets(id PK, department_id, kb_id, kb_name, project, doc_no, title, category,
  description, tags_json, lifecycle_status, current_version_id, owner_user_id,
  created_by_user_id, created_at, updated_at;  INDEX(kb_id, department_id, updated_at DESC))

doc_asset_versions(id PK, asset_id, version_no, file_id, file_name, content_hash,
  parse_status, note, state, uploaded_by_user_id, uploaded_at, UNIQUE(asset_id, version_no))

doc_asset_events(id PK, asset_id, version_id, event, actor_user_id, comment, created_at)

doc_asset_links(id PK, department_id, from_asset_id, to_asset_id, rel_type, note,
  source, status, created_by_user_id, created_at,
  UNIQUE(from_asset_id, to_asset_id, rel_type))
```

库内迁移：把 draft/in_revision 行提升为 effective、删除 obsolete/archived 资产、
剥离 `file_id` 的 `ragflow:` 前缀。

### 2.3 方法与钩子

| 方法 | 语义 |
|---|---|
| `create_asset` | 类别校验；可选初始版本 |
| `list_assets` | 搜索 title/doc_no/project；状态/类别/项目过滤；带 version_count 与 effective_version_no |
| `get_asset` | 版本 + 最近 100 事件 + links_out/links_in |
| `update_info` | 字段白名单 `{title,doc_no,category,project,description,tags}`；改名/改类别/改项目会清除「影子档」标签 |
| `add_version` | 拒绝 obsolete/archived；重复 file_id 或 content_hash 拒绝；分配 `next_no`；旧 effective 转 superseded；资产置 effective |
| `add_link` / `remove_link` | 同部门、无自链、按 rel_type 唯一 |
| `backfill_shadow_assets` | 为「尚未成为任何版本」的解析文件建影子资产（按 file_id/hash 幂等，标签「影子档」） |
| `sync_file_deleted` | 版本标 deleted；若是 effective 则回退到最新可用版本，否则删除资产——**「账随物走」** |
| `absorb_shadow_for_file` | 文件已被别处登记为版本后，删除纯影子资产 |

模块钩子（fail-soft 调用）：`sync_file_deleted(kb_id, file_id)` 由 RAGFlow 删除路径触发；
`ensure_shadow_asset_for_record(record_id)` 在解析首次 `completed` 时由台账触发
（`_guess_category` 猜类别）。权限：所有读写要求 `(id, kb_id, department_id)` 作用域；
不做逐用户 ACL（信任路由层的 KB 权限）。

路由（`routes/doc_assets.py`）：`GET /kbs/{kb}/doc-assets`、`GET .../{asset_id}`、
`GET .../{asset_id}/wiki`、`PATCH .../{asset_id}`、`POST .../{asset_id}/versions`、
`POST .../{asset_id}/links`、`DELETE .../{asset_id}/links/{link_id}`。

---

## 3. 硬件资产（`src/core/assets.py`）

原则：**AI 输出与可信记录分离**——抽取器产出 `pending` 候选（带证据），用户接受后才成为资产。

```python
ASSET_TYPES = {device, board, component, firmware, other}
CANDIDATE_STATES = {pending, accepted, rejected}
```

### 3.1 表

```sql
hardware_assets(id PK, department_id, kb_id, asset_type, name, model, manufacturer,
  serial_number, version, status, owner_user_id, attributes_json,
  created_from_candidate_id, created_by_user_id, created_at, updated_at)

asset_candidates(id PK, department_id, kb_id, kb_name, file_id, file_name, source_kind,
  source_fingerprint, extraction_method, asset_type, name, model, manufacturer, version,
  attributes_json, evidence_excerpt, evidence_locator, confidence, status, asset_id,
  resolved_by_user_id, created_at, resolved_at, UNIQUE(kb_id, file_id))

asset_evidence(id PK, asset_id, file_id, file_name, locator, excerpt, metadata_json, created_at)
```

### 3.2 语义分派（模型调用之前）

| 来源 | category | extraction_target | asset_eligible |
|---|---|---|---|
| 电路处理器 / `.edf` | `circuit_design` | `board_components_topology` | True |
| 名称含「架构/architecture」 | `hardware_architecture` | `assets_relations` | True |
| 需求/requirement/specification | `hardware_requirement` | `requirements_constraints` | **False** |
| 表格/`.xlsx/.xls/.csv` | `structured_table` | `bom_assets_parameters` | True |
| 默认 | `document_rag` | `document_assets` | True |

`generate_candidate(...) -> (candidate, used_llm)`：LLM 抽取（`create_chat_model().invoke`，
max_tokens 600、temperature 0、30s 超时、严格 JSON）+ 启发式回退 `_rule_extract`
（类型猜/正则抓型号与版本；有型号 0.42，否则 0.25）；`source_fingerprint = sha256({payload, excerpt[:12000]})`；
`(kb_id, file_id)` 唯一 pending（有则更新，无则插入，IMMEDIATE 事务）。
`accept_candidate` 创建资产 + 证据行并标记接受（重复接受幂等）；`reject_candidate` 只允许 pending。
`list_file_links` 把解析文件投影为 `{linked, pending_review, ignored, unprocessed}`。
路由：`GET/POST /kbs/{kb}/assets…`、`/asset-candidates…`、`/asset-sources`。

> 注意：`generate_candidate` 自身**不复查** `asset_eligible`；资格是调用方契约
> （`list_file_links` 会返回这些字段）。

---

## 4. 项目治理（`src/projects/`）

DB：`ProjectStore` 默认 `storage/projects.db`（独立于 auth.db）。

### 4.1 模型（`models.py`，全部 pydantic 严格校验）

| 模型 | 关键字段 |
|---|---|
| `Project` | `project_id, tenant_id='default', department_id, name, product_type, lifecycle_stage, current_hardware_revision, current_bom_revision, status∈{active,archived,draft}` |
| `ProjectKnowledgeBinding` | `binding_type∈{project_private, shared_standard, component_library, template_library}, priority, allowed_source_roles, status` |
| `ProjectPrincipalBinding` | `principal_type∈{user,group,department,service_account}, project_role∈{viewer,author,reviewer,approver,project_admin}, capabilities, valid_from/to, status` + `is_valid_at` |
| `SourceAsset` | `pipeline_record_id, original_file_name, content_hash, content_kind, parser_kind, processing_status, storage_ref, data_classification∈{public,internal,confidential,restricted}` |
| `LogicalDocument` | `document_id, title, document_role, owner_department_id, metadata` |
| `SourceVersion` | `version_id, document_id, asset_id, revision, approval_status∈{draft,reviewing,approved,released,obsolete}, effective_from/to, predecessor_version_ids` |
| `ProcessingArtifact` | `artifact_id, asset_id, processor_kind/version, backend_locator, content_fingerprint, status∈{processing,ready,failed,superseded}` |
| `ProjectSourceBinding` | `version_id, module_scope, usage_type∈{project_fact,shared_reference,template_only,historical_reference}, status` |
| `ProjectBaseline` | `baseline_version, content_hash（重算校验，不匹配拒绝）, product_variant, hardware_revisions, items[BaselineItem], effective_at, status∈{draft,approved,released,obsolete}` |
| `SourceRegionPolicy` | `region_type∈{project_fact,template_instruction,example,definition,change_history,formula_result,hidden_internal}, allowed_evidence_uses, decision∈{allow,deny}, priority, classification_confidence, approved_by/at, processing_artifact_id, policy_version`（`allow` 必须有批准人） |
| `SourceSetSnapshot` | `work_order_id, project_id, baseline_id, baseline_content_hash, baseline_item_ids, source_version_ids, shared_reference_version_ids, processing_artifact_ids, region_policy_versions, authorization_snapshot_id, content_hash` |
| `ProjectSourceCatalogEntry` | 目录投影（含 `current_for_context = approval_status ∈ {approved, released}`） |

### 4.2 Store（`store.py`）

迁移账本：`schema_migrations(migration_id PK, applied_at)` + 迁移 `001_project_scope`。
表：`projects, project_knowledge_bindings, project_principal_bindings, source_assets,`
`logical_documents, source_versions, processing_artifacts, project_source_bindings,`
`project_baselines(UNIQUE(project_id,baseline_version)), source_region_policies,`
`source_set_snapshots(work_order_id UNIQUE)`。写入 `[identifier, *values, payload_json]`，
读取时重新 pydantic 校验。

关键方法：`create/get/list_project(s)`、`add/list_knowledge_bindings`、
`add/list_principal_bindings`、`create_source_asset`、`create/get_logical_document`、
`create/get_source_version`、`create/get_processing_artifact`、`ready_artifacts_for_version`、
`add/list_project_source_bindings`、`create/get/list_baselines(approved_only)`、
`add_region_policy`、`allowed_region_policies`（decision=allow）、`create/get_source_set_snapshot`。

### 4.3 服务（`service.py`）

- `create_project(ctx, project)`：租户必须匹配 ctx；
- `list_accessible_projects`：按 `view_project` 能力过滤；
- `list_source_catalog`：要求 `read_project_sources`；
- **`create_source_set_snapshot(ctx, *, work_order_id, project_id, baseline_id, processing_artifact_ids=None)`**
  （要求 `create_work_order`）：
  基线必须属于该项目且 `approved|released`；只用 `usage_type != template_only` 的活绑定；
  逐基线项：绑定有效 → 版本 approved/released → `_require_kb_intersection` →
  选 ready 产物 → **fail-closed P2a：每个产物至少一条已批准的 `allow` 区域策略** →
  记录 policy 版本 → 生成快照（含 `authorization_snapshot_id`）；
- `_require_kb_intersection`：若逻辑文档声明了 `kb_id`/`kb_name`，该 KB 必须绑定到项目且
  调用方有 read 权限；无 KB 身份的旧来源放过（项目绑定仍然管辖）。

### 4.4 访问服务（`access_service.py`）

角色→能力表：

| 角色 | 能力 |
|---|---|
| `viewer` | `view_project, read_project_sources, read_evidence, download_review_candidate` |
| `author` | viewer + `create_work_order, run_deterministic_work_order, submit_draft` |
| `reviewer` | viewer + `submit_human_event, confirm_result` |
| `approver` | reviewer + `approve_artifact, download_approved_release` |
| `project_admin` | author + reviewer + approver + `manage_project` |

`_binding_capabilities = 角色能力 ∪ 绑定能力`；`require(ctx, project_id, capability)`
先查项目在租户内且 `status=="active"`，再查能力；
`authorization_snapshot_id` = `sha256(canonical({tenant, project, user, bindings}))[:32]`。
**全局管理员不是项目内容的自动读者**。

### 4.5 项目作用域检索（`retrieval.py`）

`ProjectEvidenceRetrievalService.retrieve(ctx, requirement, snapshot_id, retrieve_one) → RetrievalOutcome`：

1. 载入快照（租户内），缺失 → `KeyError`；`access.require(snapshot.project_id, "read_evidence")`；
2. requirement 的 project 不匹配 → `PermissionError`；
3. `requested_versions ⊆ snapshot.source_version_ids ∪ shared_reference_version_ids`，
   否则 `PermissionError("requirement expands beyond frozen source versions")`；
4. 按资产身份把产物映射到版本；
5. 逐版本：无产物 → `source_unavailable/no_frozen_artifact`；调 `retrieve_one(...)`；
   捕获 `PermissionError → access_denied`、`SourceUnavailableError → source_unavailable(retryable)`、
   其它 → `retrieval_failed`；
   **适配器作用域过滤**：每条证据必须匹配 `project_id`、`source_version_id`、允许的产物 id，
   否则 `filter_unsupported/out_adapter_returned_out_of_scope_evidence`（不静默接受、不放宽重试）；
6. 聚合 `_aggregate_status`：`access_denied` 优先；`retrieval_failed`/`filter_unsupported` →
   仅此类时 `retrieval_failed` 否则 `partial_failure`；`source_unavailable` 同理；
   否则 `success_with_hits|success_empty`；
7. 结果带 `query_fingerprint = sha256(f"{requirement.model_dump_json()}|{snapshot.content_hash}")`、
   `applied_source_set_snapshot_id`、`applied_region_policy_versions`。

---

## 5. 前端对应页面（详见 [19](19-frontend.md)）

| 页面 | 路由 | 能力 |
|---|---|---|
| 资产中心 | `/assets?tab=documents\|wiki\|graph` | 文档资产列表/详情/版本/关系/时间线；Wiki 内嵌；图谱视图 |
| 知识 Wiki | `/wiki`（或 `/assets?tab=wiki&open=<slug>`） | 页面列表/详情/编辑/归档/回滚/质量报告/蒸馏任务 |
| 记忆 | `/memory` | 见 [11](11-memory.md) |
| KB 内容工作台 | `/kbs/:kb/files` | 文件/Excel 台账/电路/模块树/测试数据/原理图/外部对话 7 个标签页 |

---

## 6. 配置与运维要点

| 项 | 默认 | 说明 |
|---|---|---|
| `WIKI_*` 常量 | 见 §1.1 | 无独立 env，常量在代码中 |
| Wiki 蒸馏模型 | `create_chat_model(priority=PRIORITY_BATCH)` | 与用户交互流量隔离（共享 `LLM_BATCH_MAX_CONCURRENCY`） |
| `ProjectStore` 路径 | `storage/projects.db` | 无 env 覆盖 |
| `DOCUMENT_AUTO_PUBLISH_VERIFIED` | false | 与文档资产无关（属文档生成） |

**数据一致性**：
- Wiki 页引用的 `source_refs` 指向解析 chunk；文件删除后 link 会变死链（由 `cleanup_dead_links` 清理）；
- doc_assets 与解析台账通过 `sync_file_deleted` / `ensure_shadow_asset_for_record` 双向对齐；
- 硬件资产是人工确认闭环，不参与自动检索注入。
