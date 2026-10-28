# 12 · 对话附件与文档修订

对应代码：`src/attachments/`（约 9.0k 行）、`src/api/routes/attachments.py`、
`src/agents/tools/attachment_tools.py`、`src/core/session_cleanup.py`。

定位：**会话级、临时、私有**的数据源。附件不进 RAGFlow、不进知识库；只能通过作用域受控的
附件工具读取。原始文件永远不可变，修订只产出「候选副本」。

---

## 1. 模型与状态机

| 生命周期 | 取值 | 说明 |
|---|---|---|
| 附件 `chat_attachments.status` | `active → deleted`（软删）/ `expired`（到期） | 只有 active 且未过期可读 |
| 解析 `chat_attachment_assets.parse_status` | `queued → running → ready \| degraded \| failed` | `degraded` 表示可用但有降级原因 |
| 解析任务 `chat_attachment_jobs.status` | `queued → running → completed \| failed`（失败可回 queued） | 每个 asset 同时最多一个活任务 |
| 修订 `chat_attachment_revisions.status` | 恒为 `review_candidate`；`release_state: not_requested → pending → approved \| rejected` | 拒绝后可重新请求 |
| 编辑提案 `chat_attachment_edit_proposals.status` | `pending → confirmed \| rejected` | 仅 docx `replace_section` |
| 会话清理 `session_cleanup_outbox.status` | `pending → running → completed \| retrying → dead_letter` | 最多 5 次 |

其它常量：

```python
PARSER_VERSION = "local_doc_v2"     # 升版会让存量资产变"陈旧"，无需重新上传即可重排队
SUPPORTED_EXTENSIONS = {.pdf,.docx,.txt,.md,.xlsx,.xlsm,.edf,.edif}
REJECTED_EXTENSIONS = {.xls}        # 提示另存为 .xlsx
part_type ∈ {text, table, image, circuit, ocr_text, visual_evidence}
source_scope ∈ {auto, attachment_only, knowledge_base_only, attachment_and_knowledge_base}
```

作用域解析（只能收窄）：

- `resolve_auto_scope`：通用对话 → 有附件 `attachment_only` 否则 `knowledge_base_only`；
  KB 对话 → 有附件 `attachment_and_knowledge_base` 否则 `knowledge_base_only`；
- `resolve_source_scope`：`attachment_only` 无 id → 报错；组合作用域在通用对话下收窄为
  `attachment_only`；KB-only 忽略附件 id。

错误层级：`AttachmentError`(400) → `AttachmentNotFound`(404)、
`AttachmentPermissionError`(404，不泄漏存在性)、`AttachmentUnsupportedType`(400)、
`AttachmentQuotaExceeded`(413)。

---

## 2. 表结构（`storage/chat_attachments.db`）

```sql
chat_attachments(attachment_id PK, session_id, user_id, tenant_id DEFAULT 'default',
  asset_id, client_request_id, filename, media_type, extension, size_bytes, sha256,
  usage_hint DEFAULT 'reference', status DEFAULT 'active',
  created_at, updated_at, expires_at, deleted_at)
  UNIQUE idx_chat_attachments_session_request(session_id, client_request_id) WHERE client_request_id IS NOT NULL
  INDEX idx_chat_attachments_session_status(session_id, status, created_at)

chat_attachment_assets(asset_id PK, session_id, user_id, tenant_id, sha256, media_type,
  extension, size_bytes, storage_key, parse_status DEFAULT 'queued', parser_version,
  manifest_json, error_code, error_message, created_at, updated_at)
  UNIQUE idx_chat_attachment_assets_content(tenant_id,user_id,session_id,sha256)   -- 每用户每会话去重

chat_attachment_parts(part_id PK, asset_id, ordinal, part_type, text_content,
  locator_json, metadata_json, content_hash, parser_version)
  INDEX(asset_id, ordinal)

chat_attachment_embeddings(asset_id, part_id, provider, model, content_hash,
  vector_json, created_at, updated_at, PK(asset_id,part_id,provider,model))

chat_attachment_visual_cache(asset_id, page_number, provider, model, question_hash,
  content, request_id, created_at, updated_at, PK(asset_id,page_number,provider,model,question_hash))

chat_attachment_jobs(job_id PK, tenant_id, user_id, session_id, asset_id, job_type,
  status DEFAULT 'queued', attempt DEFAULT 0, max_attempts DEFAULT 3, payload_json,
  result_json, lease_owner, lease_expires_at, parser_version, error_code, error_message,
  created_at, updated_at, completed_at)
  UNIQUE idx_chat_attachment_jobs_live(asset_id, job_type) WHERE status IN ('queued','running')

chat_attachment_revisions(revision_id PK, parent_attachment_id, child_attachment_id UNIQUE,
  session_id, user_id, tenant_id, operation, before_text, after_text,
  parent_content_hash, child_content_hash, status DEFAULT 'review_candidate',
  release_state DEFAULT 'not_requested', idempotency_key UNIQUE, details_json,
  release_requested_by/at/comment, reviewed_by/at/comment, released_attachment_id,
  created_at, updated_at)

chat_attachment_edit_proposals(proposal_id PK, session_id, user_id, tenant_id,
  parent_attachment_id, operation, heading_path_json, after_text, before_text,
  evidence_refs_json, status DEFAULT 'pending', note, confirmed_revision_id,
  created_at, updated_at)

session_cleanup_outbox(outbox_id PK, session_id, user_id, tenant_id,
  status DEFAULT 'pending', targets_json, idempotency_key UNIQUE,
  retry_count DEFAULT 0, max_retries DEFAULT 5, lease_owner, lease_expires_at,
  last_error, next_retry_at, created_at, updated_at, completed_at)

coordinator_leases(scope PK, owner_id, fencing_token DEFAULT 0, lease_expires_at, updated_at)
```

FTS5 虚表（`index.py`，可用性探测失败则静默停用该索引）：

```sql
attachment_parts_fts USING fts5(text_content, part_id UNINDEXED, asset_id UNINDEXED,
  ordinal UNINDEXED, part_type UNINDEXED, locator_json UNINDEXED, metadata_json UNINDEXED)
attachment_parts_trigram USING fts5(text_content, tokenize='trigram', part_id UNINDEXED, asset_id UNINDEXED)
```

会话侧（`AUTH_DB_PATH`）：`turn_attachments` 冻结快照 + `chat_turns.required_attachment_ids/source_scope`。

---

## 3. 磁盘、配额与上传流程

### 3.1 磁盘

```
CHAT_ATTACHMENT_STORAGE_DIR/
├── sources/<session_id>/<asset_id>/source<ext>          # 0600，原子改名
├── circuits/<session_id>/...                            # 附件私有 CircuitStore
└── table_indexes/<session_id>/<asset_id>/table_indexes.db
```

- 写入：`mkstemp(.upload-)` → 1 MiB 分片 → **边写边查** `CHAT_ATTACHMENT_MAX_BYTES` →
  fsync → chmod 0600 → `os.replace` 原子落地；SHA-256 对最终字节计算；
- 路径全部经 `safe_child_path`；`resolve_storage_key` 只接受根内相对/绝对路径；
- **没有物理「隔离区」**：上传即落地，可读性由行状态 + 过期时间控制。

### 3.2 上传配额检查顺序（`AttachmentService.upload`）

1. `client_request_id` 幂等查询（**绕过配额**，浏览器重试不会因会话满而失败）；
2. 文件名消毒（去路径/NUL，≤200 字符；拒绝 `~$` Office 锁文件）；
3. 扩展名白名单/黑名单；
4. 声明的 MIME 必须与扩展名推导一致（空/octet-stream 放过；`.xlsm` 大小写不敏感）；
5. 会话文件数 ≥ `CHAT_ATTACHMENT_MAX_FILES_PER_SESSION`(20) → 413；
6. 流入 staging，单文件硬上限 50 MiB；0 字节拒绝；
7. 会话字节超 500 MiB → 删 staging + 413；
8. 租户字节超 5 GiB → 删 staging + 413；
9. 魔数校验：PDF `%PDF-`；docx/xlsx/xlsm zip（OLE2 → 「旧版/加密 Office」提示）；edf/edif `(edif` 前缀；
10. `(tenant,user,session,sha256)` 去重：命中则删 staging；新资产把字节移到规范路径并回填 `storage_key`；
    然后建 attachment（幂等）+ 入队解析任务；失败清理孤儿资产。

解析限额（解析过程中强制）：`MAX_PAGES=300`（PDF 硬失败 `page_limit_exceeded`）、
`MAX_ROWS=200000`、`MAX_CELLS=2000000`、`MAX_SHEETS=100`、`MAX_SHARED_STRINGS=1000000`、
`MAX_CELL_TEXT_LENGTH=100000`、`MAX_UNCOMPRESSED_BYTES=200MiB`（zip-bomb 防护 + TXT/MD 读取上限）、
`PARSE_TIMEOUT_SECONDS=120`、`RETENTION_SECONDS=2592000`(30 天)。

---

## 4. 解析管道

### 4.1 路由（`router.AttachmentProcessorRouter`，构造后冻结）

| 扩展名 | 处理器 | 实现 |
|---|---|---|
| `.pdf` `.docx` `.txt` `.md` | `LocalDocumentProcessor` | `LocalDocumentParser` |
| `.xlsx` `.xlsm` | `SpreadsheetProcessor` | 复用 `SpreadsheetPipeline` + 附件私有 `TableIndexStore`（record_id = asset_id 哈希前 12；`kb_name=att_<16 alnum>`；department=`session_<id>`） |
| `.edf` `.edif` | `CircuitProcessor` | 复用 `CircuitIndexService` + `CircuitStore(root=circuits/<session>)` |

`ParseOutcome(ok, parse_status, parts, manifest, error_code, error_message, degraded_reasons)`。

### 4.2 本地文档解析（`local_document_parser.py`）

**PDF**：`pypdf.PdfReader`；页数超限硬失败；逐页 `extract_text()`；
密度 <24 字符的页标记 `low_text_density`；文本按空行切成 ≤1200 字符块 →
`text` 分块（locator `{"page","block"}`）；分块文本截断 8000 字符（`content_hash` 用**截断前**原文）。
OCR 开启时对前 10 个低密度页识别 → `ocr_text` 分块；失败只记 `degraded_reasons`（绝不编造文本）。
Manifest：`format, page_count, indexed_pages, low_text_density_pages[:50], ocr_candidate, ocr_enabled,`
`ocr_attempted_pages, ocr_indexed_pages, ocr_failed_pages, part_count, rough_token_count`。

**DOCX**：先 `_check_ooxml_limits`（总解压 ≤200 MiB、单成员压缩比 ≤200）。
`iter_inner_content()` 保序遍历段落/表格（旧版退化 `document.paragraphs`）。
标题层级来自 `docx_revision.heading_level`（`w:outlineLvl` → 样式名正则
`(?:heading|标题|標題)\s*([0-9]+)` → 继承样式链）；标题维护 `heading_path` 栈。
表格逐行转换（单元格 `\n` 连接），超限行按二分切成单元格片段再打包为合法 JSON 数组
（≤8000 字符），locator 带 `heading_path/table/table_index/row_start/row_end`。
内嵌工作簿（`word/embeddings/*.xlsx|.xlsm`）用 `parse_xlsx` 重解析为 table 分块；
超链接转 `"链接: <target>"` 文本（**从不抓取**）；图片转空文本 `image` 分块（只带 partname/content_type）。
Manifest 统计分块/token/链接/图片/表格/降级表格/内嵌工作簿/sheet。

**TXT/MD**：文件 ≤ 解压上限；UTF-8 `errors="replace"`；按 ~1800 字符或空行切块；
locator `{"line_start","line_end"}`。

### 4.3 执行器（`AttachmentParseExecutor.process_asset`）

```
1. get_asset（缺失 → asset_not_found）；前后各查一次 deadline
2. router.resolve(ext)（不支持 → failed/unsupported_attachment_type）
3. storage.resolve_storage_key（失败 → source_unavailable）
4. processor.parse（异常 → parse_failed）
5. outcome 失败 → asset failed（记 error_code/message）
6. 成功：合并 degraded_reasons → replace_parts（原子删+插；同时清 embeddings/visual cache）
        → 重读拿持久 part_id → _reindex_asset（FTS/trigram，fail-soft）
        → update_asset_status(parse_status, parser_version, manifest)
```

`parser_version` 记为 `f"{PARSER_VERSION}:{processor.kind}"`（除 job 指定）。

---

## 5. 检索

### 5.1 词法索引（`index.py`）

- 一个共享 DB（`CHAT_ATTACHMENT_INDEX_DB_PATH`），**不是每附件一个**；
- `replace_asset`：单事务删+插（非空文本才入索引）；
- `search`：`build_fts_match` 构造安全 MATCH，`asset_id IN (...)` 约束，
  `bm25()` 排序 → `score = max(0, 1/(1+|rank|))`，backend `fts5`；
- `trigram_search`：词长 ≥3，固定 0.9，backend `trigram`；
- `exact_scan`：≥2 字符，`LIKE ... ESCAPE '\'`，0.8，backend `scan`（短位号如 `R1`/`FB` 用）；
- `normalized_identifier_scan`：分隔符不敏感，默认扫 4000 个分块，0.85。

### 5.2 Dense（`dense.py` / `embedding.py`，默认关闭）

- `OpenAICompatibleEmbeddingGateway`：`POST {base_url}/embeddings`，Bearer，超时 = 解析超时；
  校验返回条数、非空向量、顺序；`default_embedding_gateway()` 需 base_url+key+model 齐备，
  provider ∈ `{openai_compatible, openai}`，**绝不回落到记忆的 embedding 凭据**；
- `DenseAttachmentRetriever.search`：需 `CHAT_ATTACHMENT_DENSE_ENABLED`；
  载入允许资产的分块（上限 `DENSE_MAX_PARTS=4000`，硬顶 10000）；
  缓存键 `(asset_id,part_id,provider,model)` + content_hash 校验；未命中批量嵌入后 upsert；
  余弦相似度（分母 0 → 0），排序 `(-score, ordinal, part_id)`，最多 `min(limit,50)`，backend `dense`；
  错误降级（`dense_unavailable`/`dense_failed`）。

### 5.3 混合检索（`retrieval.py`）

`AttachmentRetrievalService.search(query, asset_ids, limit=8, context_token_budget=None)`：

1. 空查询/空资产 → 空；
2. 预算：默认 `CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS=8000`，并与
   `AGENT_MODEL_MAX_INPUT_TOKENS * CHAT_ATTACHMENT_CONTEXT_RATIO`(0.3) 取更小；调用方给值优先；
3. 标识符抽取（≤16 个，用 6 个）→ `_exact_lookup`：raw / 去分隔符 / 前缀归一形式，
   经 trigram（归一长度 ≥3）/ exact_scan / normalized_identifier_scan；按 part_id 去重；
4. 稀疏：FTS5 可用则 `search`；否则对前 4 个 `\w{2,}` 词做 exact_scan；
5. 稠密：仅在开启且 `RETRIEVAL_MODE != "lexical"`；
6. **RRF 融合**：`k=60`，`score += 1/(k + position + 1)`，取每键最佳分块，
   融合上限 `max(limit, len(identifiers)*2)`，分数保留 6 位；
7. 上下文打包：硬 token 上限（`rough_tokens = max(1, len*2//7)` ≈ 3.5 字符/token），
   超限即停并标 `truncated_by_budget`；
8. 返回 `{query, chunks, degraded_reasons, matched_backends, truncated_by_budget}`。

### 5.4 查询归一化（`query_normalizer.py`）

- `_IDENTIFIER_RE` 命中硬件 token（`STM32H743ZI`、`VDD_3V3`、`+12V`、`R1`、带单位数字）；
- `_REFDES_RE`：`R|C|U|D|Q|J|TP|FB|L|SW|Y|K|X|RN|LED|TVS` + 1~4 位数字；另有 `_NETNAME_RE`/`_PARTNO_RE`；
- `fts_safe_term`：去 `(){}\[]"'^*&,:!?` 后加引号转义——**用户输入永不直接拼进 MATCH**；
- `build_fts_match(query, max_terms=12)`：标识符优先 → CJK 2-gram（连续段 ≤16 字）→ 其余拉丁词，OR 组合；
- `normalize_identifier`：仅保留 `[a-z0-9]`；`identifier_variants` 给 raw/归一/截断形式。

### 5.5 视觉与 OCR

**视觉（`visual.py`）**：需 `CHAT_ATTACHMENT_VISUAL_ENABLED` **且**
`CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED`；本地 `pdftoppm` dpi 160 渲染；
页面默认 `[1]`，尝试次数上限 3（硬顶 20，含失败页），单图 ≤8 MiB；
缓存键 `(asset_id,page,provider,model,sha256(question))`，内容 ≤12000 字符；
产出 `visual_evidence` 证据；所有失败只加降级原因
（`visual_page_limit/visual_unsupported_type/visual_source_unavailable/visual_page_out_of_bounds/`
`visual_image_too_large/visual_failed`）。

**OCR（`ocr.py`）**：`local_cpu_tesseract`，`pdftoppm`+`tesseract` 由 `shutil.which` 发现；
`--psm 3 -l eng+chi_sim`，dpi 72~400（默认 200），无 shell 子进程，
超时 = 解析超时，包裹 `governor_acquire("ocr", interactive=False)`；缺二进制 → `OcrUnavailableError`（降级）。

---

## 6. 修订机制（永不改原件）

### 6.1 服务级门（`create_attachment_revision`）

支持操作：`replace_text, replace_section, append_paragraphs, set_cell, replace_cell_text,`
`annotate_text, reflow_to_docx, insert_rows, delete_rows`。

校验：

- 扩展名门：`.txt/.md/.docx/.xlsx/.xlsm/.pdf/.edf/.edif`；
  section/append 仅 docx；cell/row 仅 xlsx/xlsm；annotation/reflow 仅 pdf；
  **PDF `replace_text` 明确拒绝**（要求用 `annotate_text`）；
- 必填参数：`after` 非空（行操作/reflow 除外）；`before` 必填且必须与 `after` 不同；
  section 需 `heading_path`；cell 需 `sheet`+`cell`；insert 需 `sheet` + `after_row ≥0`；
  delete 需 `sheet` + `row ≥1`；
- 幂等：`request_fingerprint = sha256({parent_id, parent_hash, operation, before, after,`
  `heading_path, sheet, cell, after_row, row, count, values, client_request_id})`，
  `idempotency_key = "attachment-revision:<fp>"`；
- 子文件走**正常上传路径**（文件名 `<stem>.revision-<fp10><ext>`），
  因此 ACL/保留/解析全部适用；`child_bytes == source_bytes` → 拒绝「未修改」；
- 修订 id `attachment-revision-<fp24>`；`status=review_candidate`；`release_state=not_requested`。

### 6.2 DOCX（`docx_revision.py`）

- `heading_level(paragraph)`：`w:outlineLvl` → 样式名正则 → 沿 `base_style` 链查 outline level；
- `replace_text_in_docx`：扫描正文 + 表格单元格段落（按 XML 元素身份去重）；
  出现 0 次 → 报错；>1 次 → 报错（**歧义不产出候选**）；
  多行 `after` 要求目标是整段（`whole_block`）；整段替换含 `w:drawing|w:object|w:pict|w:oleObject`
  的段落被拒；
  span 模式只重写受影响 run 区间，替换继承起始 run 的格式；多行走
  `_replace_block_with_lines`（首行进 run0，后续行插入并带原样式与首 run 的 `w:rPr`；
  剥离 `w:sectPr`/`w:pageBreakBefore`）；
- `replace_section_in_docx`：定位唯一标题（末段精确优先，再子串；前缀必须精确匹配）；
  区间 = 标题后同级/更高级标题前；**先采样格式**（首段样式/净化 `w:pPr`/首 run `w:rPr`）再拆；
  `preserve_embedded=True` 时保留含嵌入对象的段落与全部表格，移到新正文末尾；
  `[[embed]]` / `[[embed:N]]` 占位符可精确摆放（越界/重用/跨行 → 报错）；
- `append_paragraphs_in_docx`：插到节末（文档级插在末尾 `w:sectPr` 之前），继承锚点样式；
- 审计：`heading_path/heading_level/embedded_policy/removed_*/preserved_*/placed_objects/`
  `inserted_paragraphs/removed_text`（`removed_text` 会被服务层回填为 `before`）。

### 6.3 XLSX（`xlsx_revision.py`）

高保真 OOXML 手术：**只重写 worksheet XML 成员**，其余 zip 成员逐字节复制
（`_rewrite_sheet` 保留 `ZipInfo`）；子文件用项目自己的 `parse_xlsx` 重新解析并断言编辑生效后才接受。

- `set_cell_value`：校验 A1 引用；按序查找/创建单元格与行；拒公式单元格；写 inlineStr
  （必要时 `xml:space="preserve"`）；保留单元格样式；
- `replace_cell_text`：读共享字符串 + inline 字符串；目标文本必须**恰好出现在一个单元格**；
  拒公式；
- `insert_rows(after_row, count∈[1,100], values)`：下移并**重编所有单元格地址**；
  简单 A1 公式位移（跳过 `$` 绝对行）；合并区域扩展/移动；刷新 `<dimension>`；
  跨表引用（`!`）、`INDIRECT(`、`OFFSET(` → **拒绝**而不是改写；
- `delete_rows(row, count∈[1,100])`：引用被删行的公式 → 报错（**不静默 `#REF!`**）；
  下方公式上移；合并区域收缩/移除；刷新 dimension。

### 6.4 PDF（`pdf_revision.py` / `pdf_reflow.py`）

- `annotate_text(before, after)`：pypdf 文本抽取（含坐标/字号访问器）；全文必须唯一匹配；
  加密 PDF 拒绝；在近似矩形处**追加 Text 注记**（"建议替换为：{after}"），**原字形不动**；
- `reflow_to_docx`：把 PDF 文本层抽成干净的 DOCX（每页 `—— 第 N 页 ——` 标记 + 每非空行一段）；
  加密/无文本层 → `PdfReflowUnavailable`；审计明确警告版式/图片/表格不保留；
  曾经评估 LibreOffice headless 因「文本重复成双文本框」导致唯一匹配失效而放弃。

### 6.5 EDF（`edf_revision.py`）

EDIF 是文本，替换机械可行；但候选只有在**项目自己的 `EdfParser` 报告
`(instance_count, net_count)` 与父文件一致**时才被接受——语法破坏或连通性改变一律拒绝。

### 6.6 受治理编辑提案

`create_attachment_edit_proposal`：仅 docx `replace_section`（非空 heading + 内容 ≤200k 字符）；
重复 pending 提案幂等返回；`confirm_attachment_proposal` 执行修订
（`client_request_id=attachment-proposal:<id>`）后标记 `confirmed`；
`reject_attachment_proposal` → `rejected`。
runner 对批准/取消走**确定性路径**（不调模型，`_direct_attachment_proposal_answer`），
发 `attachment_revision` + `document_card`。

### 6.7 放行治理

- `request_attachment_release(revision_id, session, user, comment)`：owner 作用域；
  必须是 `review_candidate`；已批准 → 报错；已 pending → 幂等返回；
  CAS `release_state ∈ {not_requested, rejected} → pending`（comment ≤2000）；
- `review_attachment_release(revision_id, session, reviewer_id, approve, comment)`：
  reviewer 不按 owner（走 KB write 权限）；批准时**复制子文件为新的已放行附件**
  （`<stem>.released<ext>`，`client_request_id=attachment-release:<revision_id>`），
  归属原上传者；只允许从 `pending` CAS；拒绝保留候选可下载且可重新请求；
  发站内通知 + 指标；
- 候选与放行**永不同体晋升**。

---

## 7. Job / Worker / 协调器 / 会话清理

### 7.1 Job

只有 `job_type='parse'`；`run_job(job, executor)` 调 `executor.process_asset(job.asset_id)`。

### 7.2 `AttachmentWorker.run_once()` 顺序

1. 保留期扫描 `expire_due_attachments()`；
2. `SessionCleanupCoordinator.run_once(limit=2)`；
3. `AttachmentCoordinatorLoop.run_once()`（**仅当持有协调器租约**）；
4. 解析任务：`list_pending_jobs(limit=4)` → `claim_job`。

`claim_job` CAS：`queued` 或过期 `running`，且 `attempt < max_attempts`；
置 `running/lease_owner/lease_expires_at/attempt+1` 并把 asset 置 `running`。

`_process_with_watchdog`：超时 `CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS`；
心跳线程间隔 `min(lease/3, timeout/3, ≥0.05s)`；执行器跑在单线程池；
超时返回 `parse_timeout` 且**不 join 线程池**（执行器也拿到 deadline，过期后不得落盘）。

`fail_job`：attempt 用尽 → `failed`，否则回 `queued`；asset 仅在 `queued|running` 时被改写
（执行器记录的确定性失败被保留）。每个 job 结束后 `coordinator.notify_asset_settled(asset_id)`。

### 7.3 `AttachmentTurnCoordinator`

解析 waiting turn：

- `notify_asset_settled(asset_id)` / `notify_attachment_deleted(attachment_id)` → 找附件 → 找等待中的 turn；
- `_resolve_turn`：任一必需附件 `failed`（含被删除/过期映射）→
  `transition_waiting_turn(to_status="failed", error_message="附件解析失败，本轮请求已取消；可重试解析后重新发送。")`
  + 事件 `attachment_failed`；否则全部脱离 `{queued,running}` → 转 `pending` + 事件 `attachment_ready`（带降级列表）；
- `reconcile_waiting_turns(limit)` 扫描补偿漏事件；`expire_waiting_turns` 走会话服务。

### 7.4 协调器租约（`coordinator_lease.py`）

- `CoordinatorLeaseStore` 在共享附件 DB 上行锁 `coordinator_leases`（默认 scope `attachment-waiting-turns`）；
  `try_acquire`：同 owner 续租；他人有效租约 → 拒绝；否则 `fencing_token+1` 接管（表初始化有 5s 忙重试）；
- `AttachmentCoordinatorLoop`：`lease_seconds=15`、`renew_seconds=5`、
  间隔 `WAITING_TURN_TIMEOUT_SCAN_SECONDS=5`；先续租再 `reconcile + expire`；续租失败立即停止扫描。

### 7.5 会话清理

`SessionCleanupCoordinator`：claim `session_cleanup_outbox` 行 →
删附件行/parts/embeddings/visual cache/jobs/assets + FTS 行 + 电路/表索引存储 +
（`require_session_absent` 时）确认会话已消失；失败指数退避
`min(600, 30*2**min(retry,4))`，`max_retries=5` 后 dead_letter。

### 7.6 保留与清理

- `expires_at = now + RETENTION_SECONDS`；≤0 → 立即到期；
- 读取过滤 `status='active' AND (expires_at IS NULL OR expires_at > now)`；
- `expire_due_attachments(limit)`：批量置 `expired`，零引用资产回收
  （删存储、FTS/trigram 行，再删 jobs/parts/embeddings/visual cache）；
- `delete_attachment` 软删；最后一个引用消失时回收资产并**同步通知等待中的 turn**。

---

## 8. 附件 HTTP API（`/api/v1`）

所有端点要求 `current_user`；`CHAT_ATTACHMENTS_ENABLED=false` → **404**「chat attachments are not enabled」；
`_require_session` 按 owner 载入（他人 → 404；system_admin 访问 KB 内容 → 403）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/conversations/{sid}/attachments` | multipart + `client_request_id` + `usage_hint`；201 |
| GET | `/conversations/{sid}/attachments` | 列表 |
| GET | `/conversations/{sid}/attachments/{aid}` | 详情（manifest/degraded_reasons） |
| GET | `/conversations/{sid}/attachments/{aid}/download` | 真 MIME + RFC 5987 文件名 |
| DELETE | `/conversations/{sid}/attachments/{aid}` | 软删 |
| POST | `/conversations/{sid}/attachments/{aid}/retry` | 先入队再重置状态 |
| GET | `/conversations/{sid}/attachments/revisions/{rid}` | owner 或 KB reviewer；before/after ≤8000 |
| GET | `.../revisions/{rid}/candidate-download` | reviewer（KB write；非 owner 作用域） |
| POST | `.../revisions/{rid}/release-request` | owner；`{comment≤2000}` |
| POST | `.../revisions/{rid}/release-review` | reviewer；`{"decision":"approve"\|"reject","comment"}` |
| POST | `/attachments/reparse-stale` | 员工；`limit` + 重复 `extension`；返回 `{scanned,queued,queued_asset_ids}` |
| GET | `/attachment-releases/pending` | 待放行队列（按 KB write 过滤） |

附件视图字段：`attachment_id, asset_id, filename, media_type, extension, size_bytes, sha256,`
`usage_hint, status, parse_status, error_code, error_message, manifest, degraded_reasons,`
`created_at, updated_at, expires_at`。

解析器回填：`list_stale_assets(current_version_prefix=PARSER_VERSION, extensions)` →
`reparse_asset` 入队（`parser_version=f"{PARSER_VERSION}:{ext}"`）；`reparse_stale_assets` 返回统计。

---

## 9. Agent 集成（详见 [06](06-tools-and-evidence.md#38-附件工具族toolsattachment_toolspy)）

- **turn 创建**：`resolve_refs_for_turn` 冻结快照；`parse_status ∈ {queued,running}` →
  turn `waiting_for_attachments`；blocked id → 400 `attachment_unavailable`；
- **系统提示词**：作用域行（≤5 个附件点名 + 只能用 `attachment_*` 工具读取 + 可写时的完整操作规则）；
- **按需读取**：服务端不再在模型第一轮前自动列出/检索附件；模型通过
  `attachment_list` / `attachment_search` / `attachment_read` 自己决定读取时机；
- **预算**：有附件时 `max(AGENT_EVIDENCE_BUDGET=30, AGENT_ATTACHMENT_EVIDENCE_BUDGET=80)`；
- **capability**：`attachment.search`（读族）/ `attachment.revise`（编辑族）；
  `filter_tools_by_capability` 对未映射工具 fail-closed；`attachment_only` 时移除全部 `knowledge_base.*`，
  `knowledge_base_only` 时移除全部 `attachment.*`；
- **叙述白名单**不含 `attachment_edit` / `request_attachment_release`（只有受治理执行器能写）。

---

## 9.1 服务端预取已移除（2026-10-23）

模型第一轮前不再执行 `attachment_list` 或附件检索预览。附件内容只能由模型显式调用
`attachment_*` 工具取得；正式证据、引用编号和预算仍由 `timed_tool_call` 统一处理。

---

## 10. 配置清单

| 变量 | 默认 |
|---|---|
| `CHAT_ATTACHMENTS_ENABLED` | `true` |
| `CHAT_ATTACHMENT_STORAGE_DIR` / `_INDEX_DB_PATH` | `storage/chat_attachments` / `storage/chat_attachments.db` |
| `CHAT_ATTACHMENT_MAX_BYTES` / `_MAX_FILES_PER_SESSION` / `_MAX_SESSION_BYTES` / `_MAX_TENANT_BYTES` | 50MiB / 20 / 500MiB / 5GiB |
| `CHAT_ATTACHMENT_MAX_PAGES/ROWS/CELLS/SHEETS/SHARED_STRINGS/CELL_TEXT_LENGTH/UNCOMPRESSED_BYTES` | 300 / 200000 / 2000000 / 100 / 1000000 / 100000 / 200MiB |
| `CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS` / `_RETENTION_SECONDS` | 120 / 2592000 |
| `CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS` / `_CONTEXT_RATIO` | 8000 / 0.3 |
| `CHAT_ATTACHMENT_FTS_ENABLED` / `_TRIGRAM_ENABLED` | true / true |
| `CHAT_ATTACHMENT_DENSE_ENABLED` / `_RETRIEVAL_MODE` | false / `lexical` |
| `CHAT_ATTACHMENT_EMBEDDING_PROVIDER/BASE_URL/MODEL/API_KEY` | `openai_compatible` / `""` |
| `CHAT_ATTACHMENT_DENSE_MAX_PARTS` | 4000（硬顶 10000） |
| `CHAT_ATTACHMENT_OCR_ENABLED` / `_OCR_MAX_PAGES` | false / 10 |
| `CHAT_ATTACHMENT_VISUAL_ENABLED` / `_REMOTE_INFERENCE_ALLOWED` | false / false |
| `CHAT_ATTACHMENT_VISUAL_PROVIDER/BASE_URL/MODEL` | `volcengine_ark` / `https://ark.cn-beijing.volces.com/api/v3` / `""` |
| `CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN` / `_MAX_IMAGE_BYTES` / `_TIMEOUT_SECONDS` | 3 / 8388608 / 60 |
| `ARK_API_KEY` | `""` |
| `CHAT_ATTACHMENT_JOB_LEASE_SECONDS` / `_JOB_MAX_ATTEMPTS` | 300 / 3 |
| `WAITING_TURN_MAX_SECONDS` / `_TIMEOUT_SCAN_SECONDS` | 600 / 5 |

---

## 11. 测试与不变量

`tests/`：`test_attachment_*`（上传/配额/解析/索引/检索/修订/放行/保留/回填）、
`test_embedded_ingestion.py`、
`test_waiting_turn_timeout.py`、`test_document_shadow_planner.py`。

关键不变量：

1. **fail-closed 授权 + fail-open 可用性**：作用域越权 403/404；索引/解析失败降级不阻断聊天；
2. **会话隐私 ACL 每次调用重验**：工具重新读活行，校验 asset/session，丢弃外来/已删/失败附件；
3. **不可变原件 + 可审计派生物**：编辑只产子附件；放行再产一份副本；before/after/hash/details 全存；
4. **幂等与租约**：job/asset 状态 CAS、协调器 fencing token、修订/上传幂等键；
5. **两阶段清理**：逻辑过期在请求路径，物理回收走 outbox + 到期扫描，均带重试与 dead letter；
6. 没有 PG 对应实现（附件 store 仅 SQLite）。
