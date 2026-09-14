# Hardware-DataBase 会话附件、知识库与文档模板统一改造实施方案

- 日期：2026-09-03
- 目标分支：`feature/fix-module-matching`
- 状态：Final Recommended Implementation Plan
- 适用仓库：`https://github.com/ZuyangYu/Hardware-DataBase.git`
- 目标：指导代码修改、迁移、测试、灰度和验收，完成“知识库长期资产 + 会话临时附件 + 文档模板生成”统一协作能力。

## 实施状态（截至 2026-09-04）

本方案已按分阶段策略在当前工作树完成核心闭环，现状如下：

- 已落地并通过回归：会话附件生命周期、附件作用域工具、附件证据桥接、模板附件化、模板填充工单、通用 Markdown/Excel/Word/PDF/PowerPoint 导出及其权限/生命周期基础设施。
- 本轮已完成统一的服务端 `IntentPlan`：比较请求优先于模板生成；显式格式请求才创建通用导出；“按推荐执行”仅在存在有效模板上下文时进入模板流程；路由事件会记录 intent/action/target/reason_codes。
- 前端已对多个可用模板附件阻断自动猜选，要求用户明确点击“作为模板”；缺少合法模板时会在发送前提示上传或选择；服务端仍是最终权限和路由事实源。
- 模板输出格式契约已加入：模板原生格式进入真实填充工单；请求 PDF/PPTX 时先生成原生模板成品，再由独立 `convert_artifact` worker 做语义排版、产物校验、哈希绑定、重试和候选/发布状态继承，不会把聊天 Markdown 冒充为模板成品。
- OCR、视觉和 Dense/Hybrid Retrieval 已实现为独立可替换能力：均有硬资源上限、失败降级、证据元数据和低基数观测；默认仍按部署策略关闭，缺少 `tesseract`、远程凭据或 embedding 配置时不会影响本地附件检索。
- 清理与观测已覆盖转换工单、转换子产物、会话删除和队列阶段；生成任务与转换任务按同一工单合并投影，避免前端重复卡片。

本轮验证包括服务端意图/文档/导出/转换回归、前端聊天与转换入口测试、静态检查；重启后再次检查 API readiness、前端入口和后台 worker。

---

## 1. 最终目标（Target State）

Hardware-DataBase 最终将同时支持两类一等数据源：

1. **Knowledge Base Asset**
   - 长期保存、可共享；
   - 继续使用现有知识库权限体系；
   - PDF/DOCX 等文档继续进入 RAGFlow；
   - Excel/EDF 继续使用现有结构化解析、索引和查询能力。

2. **Chat Attachment**
   - 当前用户、当前会话私有；
   - 临时、可删除、可过期；
   - 不自动进入 RAGFlow，不自动归档到知识库；
   - PDF/DOCX/TXT/MD 本地解析和检索；
   - Excel/EDF 复用现有领域 parser/service，只增加附件作用域；
   - OCR、视觉理解、Dense Retrieval 作为后续可开关增强能力。

Deep Agent 不直接访问文件系统，也不把附件原路径暴露给模型，而是通过：

```text
SourceScope
+ ToolRuntime
+ ScopeResolver
+ Domain Tools
+ Evidence
```

访问两类数据源。

最终用户应能直接通过自然语言完成：

```text
“只分析这个附件”
“只查知识库”
“结合附件和知识库比较”
“查这个 Excel 的参数”
“检查这个 EDF 的网络连接”
“分析 PDF 第 8 页的原理图”
“把这个 DOCX 作为模板，根据附件和知识库生成设计说明书”
“把分析结果导出为 Word / PDF / Excel / PPT”
```

系统负责完成：来源选择、权限校验、工具编排、证据定位、后台任务、模板生成和结果交付。

> 核心目标不是“给聊天增加文件上传按钮”，而是“给 Deep Agent 增加一个受权限控制、可追溯、可生命周期治理的会话级数据源”。

---

## 2. 当前代码基线与必须保持的不变量

本方案以 `feature/fix-module-matching` 当前实现为基线。

### 2.1 `PipelineRegistry` 不改为附件二维路由

当前 `src/pipelines/registry.py` 的注册逻辑明确禁止两个 `PipelineSpec` 声明相同扩展名：

```python
overlap = spec.supported_extensions & existing.supported_extensions
if overlap:
    raise ValueError("Pipeline extension conflict ...")
```

因此当前不变量是：

```text
extension -> one PipelineSpec
```

而不是：

```text
(source_type, extension) -> PipelineSpec
```

**决策：保留现有 `PipelineRegistry` 作为知识库 ingestion router，不为附件改写其核心语义。**

新增独立：

```text
AttachmentProcessorRouter
```

负责会话附件路由。

### 2.2 General Chat 当前绕过 KB Agent

当前 `CreateTurnRequest` 已有：

```text
query
client_request_id
query_mode
document_context
document_flow
```

但没有通用附件字段；代码注释还明确说明 general chat 绕过 knowledge-base agent。

因此：

```text
General Chat + Attachment
```

必须新增明确的 Agent 路由，不能只在 KB Chat 中挂附件工具。

### 2.3 当前 turn worker 只消费 `pending`

当前 active 状态主要为：

```text
pending
streaming
cancelling
```

Worker 只取 `pending`；stale recovery 只处理 `streaming/cancelling`。

因此如果新增：

```text
waiting_for_attachments
```

必须同时补齐状态机、取消、恢复、SSE、前端显示和附件就绪后的原子转移。

### 2.4 Excel / Circuit 当前强制 Knowledge Base department scope

现有 spreadsheet 和 circuit Agent tools 都依赖 KB/department 上下文。

**决策：不复制 parser，不复制核心查询逻辑；抽象数据作用域解析。**

### 2.5 CircuitStore 已支持自定义 root

`CircuitStore(root=...)` 已经存在，并且 `design_dir()` 会执行 `validate_kb_name()` 和路径逃逸检查。

因此附件 EDF 不应通过：

```text
kb_name="__chat_attachments__/42"
```

伪造路径；应使用独立 `CircuitStore(root=...)`。

### 2.6 `parser_registry.py` 不是当前 live parser dispatcher

当前代码已经注明 factories 不在 live path 使用，Agent 只消费 capability 描述。

**决策：附件实现优先复用具体 parser/service，不强制经由 `src/ingestion/parser_registry.py`。**

### 2.7 现有文档模板链路必须保留

当前模板链路已经形成：

```text
Upload
 -> sanitize_template
 -> immutable TemplateVersion
 -> TemplateAnalysis
 -> review/activation
 -> DocumentContext
 -> Document Authoring Tools
 -> renderer / worker
```

**Attachment 只是模板来源，不是 TemplateVersion 的替代品。**

---

## 3. 核心架构决策

### 3.1 顶层架构

```text
                                  Deep Agents
                                      |
                         SourceScope / ToolRuntime
                                      |
               +----------------------+----------------------+
               |                                             |
        Knowledge Base Scope                           Attachment Scope
               |                                             |
      +--------+---------+                       +-----------+------------+
      |        |         |                       |           |            |
 RAGFlow   Spreadsheet  Circuit            Local Docs   Spreadsheet    Circuit
 docs      Service      Service             Search        Adapter       Adapter
                                                   |           |            |
                                                   +----- shared domain ----+
                                                           services
                                      |
                                  Evidence
                                      |
                         +------------+-------------+
                         |                          |
                   Chat Answer                Document / Export
                                                   |
                                    TemplateVersion / DocumentContext
```

### 3.2 三层职责必须分离

不要把“来源路由、权限和 parser”混成一个 registry。

```text
AttachmentProcessorRouter
    决定文件怎么解析

ScopeResolver
    决定本轮允许读什么

Domain Service
    真正执行文本、Excel、Circuit 查询

Deep Agent Tool
    只做薄适配和 Evidence 转换
```

### 3.3 不修改知识库默认行为

知识库仍为：

```text
PDF/DOCX -> RAGFlow
XLSX     -> SpreadsheetIndexService
EDF/EDIF -> CircuitIndexService
```

现有 KB 上传 API 和 RAGFlow 数据集逻辑不因附件功能改变。

---

## 4. 数据模型最终设计

为了真正实现“同一会话相同文件只解析一次”，必须把“用户上传记录”和“解析资产”分开。

### 4.1 `chat_attachments`：用户可见附件记录

```text
attachment_id TEXT PRIMARY KEY
session_id INTEGER NOT NULL
user_id INTEGER NOT NULL
tenant_id TEXT NOT NULL DEFAULT 'default'
asset_id TEXT NOT NULL
client_request_id TEXT
filename TEXT NOT NULL
media_type TEXT NOT NULL
extension TEXT NOT NULL
size_bytes INTEGER NOT NULL
sha256 TEXT NOT NULL
usage_hint TEXT NOT NULL DEFAULT 'reference'
status TEXT NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
expires_at TEXT
deleted_at TEXT
```

推荐：

```text
usage_hint = reference | data
status = active | deleted | expired
```

不要把“正式模板状态”塞进 attachment `role=template`；模板是独立领域资产。

唯一约束：

```text
(session_id, client_request_id) WHERE client_request_id IS NOT NULL
```

### 4.2 `chat_attachment_assets`：物理文件与解析事实

```text
asset_id TEXT PRIMARY KEY
session_id INTEGER NOT NULL
user_id INTEGER NOT NULL
tenant_id TEXT NOT NULL DEFAULT 'default'
sha256 TEXT NOT NULL
media_type TEXT NOT NULL
extension TEXT NOT NULL
size_bytes INTEGER NOT NULL
storage_key TEXT NOT NULL
parse_status TEXT NOT NULL
parser_version TEXT NOT NULL DEFAULT ''
manifest_json TEXT NOT NULL DEFAULT '{}'
error_code TEXT NOT NULL DEFAULT ''
error_message TEXT NOT NULL DEFAULT ''
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

唯一约束：

```text
(tenant_id, user_id, session_id, sha256)
```

说明：

- 同一会话重复上传相同内容可以产生多个 `chat_attachments`；
- 多个附件记录指向同一个 `asset_id`；
- 解析状态属于 asset，而不是附件展示记录；
- parser 升级时对同一 asset 重新构建 parts，不需要重复保存原文件；
- 默认禁止跨用户/跨会话全局 dedup。

### 4.3 `chat_attachment_parts`

```text
part_id TEXT PRIMARY KEY
asset_id TEXT NOT NULL
ordinal INTEGER NOT NULL
part_type TEXT NOT NULL
text_content TEXT NOT NULL DEFAULT ''
locator_json TEXT NOT NULL DEFAULT '{}'
metadata_json TEXT NOT NULL DEFAULT '{}'
content_hash TEXT NOT NULL
parser_version TEXT NOT NULL
```

`part_type`：

```text
text
table
image
circuit
ocr_text
visual_evidence
```

### 4.4 `chat_attachment_jobs`

解析任务应绑定 `asset_id`：

```text
job_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL
user_id INTEGER NOT NULL
session_id INTEGER NOT NULL
asset_id TEXT NOT NULL
job_type TEXT NOT NULL
status TEXT NOT NULL
attempt INTEGER NOT NULL DEFAULT 0
max_attempts INTEGER NOT NULL DEFAULT 3
payload_json TEXT NOT NULL DEFAULT '{}'
result_json TEXT NOT NULL DEFAULT '{}'
lease_owner TEXT NOT NULL DEFAULT ''
lease_expires_at TEXT
parser_version TEXT NOT NULL
error_code TEXT NOT NULL DEFAULT ''
error_message TEXT NOT NULL DEFAULT ''
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
completed_at TEXT
```

幂等键：

```text
(asset_id, job_type, parser_version)
```

### 4.5 `turn_attachments`

历史 turn 不能依赖附件仍然存在，因此保存快照：

```text
turn_id TEXT NOT NULL
attachment_id TEXT NOT NULL
ordinal INTEGER NOT NULL
filename_snapshot TEXT NOT NULL
media_type_snapshot TEXT NOT NULL
usage_hint_snapshot TEXT NOT NULL
content_hash_snapshot TEXT NOT NULL
parser_version_snapshot TEXT NOT NULL
PRIMARY KEY(turn_id, attachment_id)
```

附件删除后历史消息仍可显示：

```text
design-review.pdf（已删除）
```

但不得继续读取原文件。

### 4.6 Cleanup Outbox

不建议再做一个字段过少的 `chat_attachment_deletion_outbox`。

推荐新建通用：

```text
session_cleanup_outbox
```

字段至少包含：

```text
outbox_id
session_id
user_id
tenant_id
status                 # pending/running/retrying/completed/dead_letter
targets_json           # attachments/exports/artifacts/checkpoints
idempotency_key UNIQUE
retry_count
max_retries
lease_owner
lease_expires_at
last_error
next_retry_at
created_at
updated_at
completed_at
```

所有 cleanup handler 必须幂等。

---

## 5. SourceScope 与权限模型

### 5.1 API Scope

新增：

```python
SourceScope = Literal[
    "auto",
    "attachment_only",
    "knowledge_base_only",
    "attachment_and_knowledge_base",
]
```

`CreateTurnRequest` 增加：

```python
attachment_ids: list[str] = []
source_scope: SourceScope = "auto"
```

### 5.2 `auto` 的确定性解析

```text
General Chat + no attachment
    -> original general chat

General Chat + attachment
    -> attachment_only

KB Chat + no attachment
    -> knowledge_base_only

KB Chat + attachment
    -> attachment_and_knowledge_base
```

用户明确指定“只看附件/只查知识库”时，`SourceScopePlanner` 可以进一步**收窄**作用域；任何模型规划都不能扩大后端已授权的 scope。

### 5.3 权限硬边界

附件工具每次调用都重新验证：

```text
user_id + session_id + attachment_id
```

知识库工具继续走现有 KB ACL。

禁止：

- 根据文件名猜 attachment；
- 根据 hash 越权引用其他用户资产；
- 让模型传任意本地路径；
- 让 SQL 或 Circuit filter 绕过 attachment scope；
- 让 Agent 修改 `source_type` 获取未挂载资源。

---

## 6. 新增 AttachmentProcessorRouter

新增：

```text
src/attachments/router.py
```

接口：

```python
class AttachmentProcessorRouter:
    def resolve(self, extension: str) -> AttachmentProcessor:
        ...
```

默认路由：

```text
.pdf/.docx/.txt/.md -> LocalDocumentAttachmentProcessor
.xlsx              -> SpreadsheetAttachmentProcessor
.edf/.edif         -> CircuitAttachmentProcessor
.xls               -> reject
.xlsm              -> template conversion or explicitly read-only policy
```

**不要修改 `src/pipelines/registry.py` 为二维 source registry。**

---

## 7. 本地 PDF / DOCX 解析

### 7.1 本地确定性处理流程

```text
Upload
 -> MIME / extension / signature validation
 -> stream to private storage
 -> SHA-256
 -> create/reuse AttachmentAsset
 -> enqueue parse job
 -> deterministic parser
 -> canonical parts
 -> lexical index
 -> manifest
 -> ready/degraded/failed
```

### 7.2 PDF

第一版：

```text
page -> text blocks -> parts
```

locator：

```json
{"page": 8, "block": 3}
```

如果页面文本密度过低：

```text
parse_status = degraded
ocr_candidate = true
```

不要伪装成完整解析成功。

### 7.3 DOCX

复用现有 `python-docx`/OOXML 依赖，提取：

```text
heading
paragraph
table
link
embedded image metadata
```

locator：

```json
{
  "heading_path": ["3 电源设计", "3.2 额定电压"],
  "paragraph": 12
}
```

第一版不承诺 DOCX 固定自然页码；页码只在渲染后产生。

---

## 8. Hardware 专项本地检索

### 8.1 第一阶段不是单纯“FTS5 默认 tokenizer”

Hardware 文档中大量存在：

```text
STM32H743ZI
TPS62130
VDD_3V3
ETH_TXP
+12V
R1
U2
CAN_H
```

SQLite FTS5 默认 `unicode61` 对 CJK 连续文本、标点硬件标识、substring 搜索存在明显局限；单纯改为 trigram 又会对小于 3 个字符的 `R1/U2/EN/FB` 产生新问题。

### 8.2 推荐组合策略

```text
AttachmentRetrievalService
        |
        +-- ExactIdentifierRetriever
        |      raw / normalized / prefix
        |
        +-- SparseTextRetriever
        |      unicode61 FTS5
        |
        +-- OptionalTrigramRetriever
               CJK / substring >= 3 chars
        |
        +-- OptionalDenseRetriever
               Phase 6
        |
        -> FusionRanker
        -> ContextPacker
        -> Evidence
```

### 8.3 Query Normalizer

新增：

```text
AttachmentQueryNormalizer
```

负责：

- FTS-safe quoting；
- 原始 query 与 normalized query 分离；
- 提取硬件 identifier；
- 不直接把用户输入拼进 `MATCH`；
- 特殊字符保持可查询。

重点测试：

```text
+12V
-5V
VDD_3V3
A/B
R1/R2
STM32H7*
USB_DP
CAN_H/CAN_L
连续中文
```

### 8.4 FTS5 不可用

服务启动探测 FTS5。

如果不可用：

```text
小附件 -> bounded lexical scan + degraded diagnostic
大附件 -> attachment_search unavailable/degraded
```

不得导致整个服务启动失败，也不得生成虚假 Evidence。

### 8.5 Context Budget

`CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS` 是 hard cap，不是固定注入长度。

```text
available =
model_context_window
- system_prompt
- history
- tool_results
- reserved_output
- safety_margin

attachment_budget = min(
    available * configured_ratio,
    CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS
)
```

---

## 9. Excel 作用域复用

### 9.1 不创建新的 Excel parser

继续复用现有：

```text
parse_xlsx
SpreadsheetIndexService
spreadsheet search
read-only SQL
```

### 9.2 抽象 ScopeResolver

新增建议：

```text
src/agents/scopes/base.py
src/agents/scopes/knowledge_base.py
src/agents/scopes/attachment.py
```

例如：

```python
@dataclass(frozen=True)
class SpreadsheetScope:
    source_type: str
    db_path: str
    allowed_record_ids: frozenset[str]
    department_id: str | None = None
    kb_name: str = ""
    attachment_ids: tuple[str, ...] = ()
```

现有工具从：

```python
kb_scope_from_context(...).require_department(...)
```

逐步重构为：

```python
scope = spreadsheet_scope_resolver.resolve(rt)
```

SQL 安全能力必须继续共用：

```text
sqlglot validation
read-only whitelist
LIMIT
allowed tables
attachment record filtering
```

### 9.3 XLSX 安全限制必须在 parser 内部生效

不仅限制 `MAX_ROWS`，还应在读取期间约束：

```text
max_rows
max_cells
max_sheets
max_shared_strings
max_uncompressed_bytes
max_compression_ratio
max_cell_text_length
```

解析后再检查已经太晚。

---

## 10. EDF / EDIF 作用域复用

继续复用：

```text
EdfParser
CircuitIndexService
CircuitQueryEngine
```

附件侧新增：

```text
attachment_id -> asset_id -> design_id
```

Circuit storage：

```text
KB:
STORAGE_DIR/circuits/<kb_name>/<design_id>

Attachment:
CHAT_ATTACHMENT_STORAGE_DIR/circuits/<session_id>/<safe_namespace>/<design_id>
```

实现方式使用：

```python
CircuitStore(root=attachment_circuit_root)
```

不要通过带 `/` 的虚拟 `kb_name` 注入目录层级。

Circuit query 的授权从“强制 department”抽成：

```text
KnowledgeBaseCircuitScopeResolver
AttachmentCircuitScopeResolver
```

query engine 核心保持一份。

---

## 11. Deep Agents 接入与 General Chat 修复

### 11.1 扩展 ToolRuntime

当前 ToolRuntime 已保存 KB、ctx、document context、session id、事件等请求级状态。

增加：

```text
attachment_refs
source_scope
attachment_service
source_scope_resolver
```

推荐结构：

```python
@dataclass(frozen=True)
class AttachmentRef:
    attachment_id: str
    asset_id: str
    session_id: int
    filename: str
    media_type: str
```

### 11.2 工具集合

新增薄工具：

```text
attachment_list
attachment_search
attachment_read
attachment_table_query
attachment_circuit_search
attachment_visual_analyze   # Phase 6
```

### 11.3 取消 `is_general` 对所有数据工具的一刀切

当前 runner 对 general chat 不挂 KB toolset。

改为按 scope 装配：

```python
tools = []

if effective_scope.allows_kb:
    tools += build_kb_tools(...)

if effective_scope.allows_attachments:
    tools += build_attachment_tools(...)

if memory_allowed:
    tools += [memory_tool]
```

这样得到：

```text
General + no attachment
    -> 原 general chat

General + attachment
    -> Deep Agent + attachment tools

KB + no attachment
    -> 当前 KB Deep Agent

KB + attachment
    -> KB tools + attachment tools
```

### 11.4 Evidence 统一

附件 Evidence 与 KB Evidence 使用同一个 `Evidence` 类型。

建议 metadata：

```json
{
  "source_type": "chat_attachment",
  "attachment_id": "att-...",
  "asset_id": "asset-...",
  "filename": "design.pdf",
  "backend": "local_attachment",
  "locator": {"page": 8, "block": 3}
}
```

不要给 `Evidence` 新增一套附件专用平行模型。

---

## 12. Turn 等待附件状态机

### 12.1 新状态

```text
waiting_for_attachments
```

推荐状态：

```text
waiting_for_attachments
    -> pending
    -> streaming
    -> completed

waiting_for_attachments
    -> failed

waiting_for_attachments
    -> cancelled
```

### 12.2 不让 Chat Worker 轮询附件

推荐由：

```text
Attachment Worker
    -> asset ready/failed
    -> AttachmentTurnCoordinator
    -> atomically resolve waiting turn
```

Chat Worker 继续只消费 `pending`，降低对现有队列模型的影响。

### 12.3 多附件规则

turn 创建时冻结：

```text
required_attachment_ids
```

默认：

```text
所有显式选择附件都是 required
```

如果任一 required attachment `failed`：

```text
turn -> failed
error_code = attachment_parse_failed
```

`degraded` 附件可以进入 pending，但必须把 degraded 原因带入 runtime/事件。

### 12.4 必改位置

至少同步修改：

```text
chat_turns status handling
list_active_turns
cancel turn
SSE/status response
frontend status rendering
attachment ready coordinator
session deletion
recovery tests
```

---

## 13. 文档模板与文档生成功能兼容方案

### 13.1 核心原则

```text
Chat Attachment = 模板来源
TemplateVersion = 正式、独立、不可变的模板资产
```

以下核心保持：

```text
sanitize_template
TemplateAnalysis
TemplateVersion
Template activation/review
DocumentContext
DocumentAuthoringToolset
renderer
DocumentGenerationWorker
```

### 13.2 新模板入口

当前前端“上传模板”最终要统一为“添加附件”，但必须分阶段迁移。

最终交互：

```text
添加附件
  -> template.docx
  -> [作为文档模板]
  -> 选择/确认目标 KB
  -> KB write permission
  -> analyze-as-template
  -> TemplateVersion / Analysis
  -> DocumentContext
```

### 13.3 新 API

保留现有：

```http
POST /document-generation/templates/analyze
```

新增：

```http
POST /api/v1/document-generation/templates/analyze-from-attachment
```

请求：

```json
{
  "attachment_id": "att-123",
  "kb_name": "project-a",
  "template_name": "硬件设计说明书"
}
```

后端：

```text
attachment ACL
 -> target KB write permission
 -> read server-side attachment bytes
 -> existing analyze_document_template(...)
 -> existing sanitize/analyze/version flow
```

浏览器不重复上传文件。

### 13.4 模板权限不能随附件放宽

```text
上传普通附件
    -> session access

转换为模板
    -> target KB write/admin permission
```

不得因为“能上传附件”就获得创建 KB 模板的权限。

### 13.5 Template provenance

给 `TemplateVersion` 增加可选 provenance：

```text
origin_source_type = chat_attachment
origin_attachment_id
origin_session_id
origin_content_hash
```

但正式 TemplateVersion 必须保存自己的 sanitized bytes。

删除原 attachment 后，已经创建的 TemplateVersion 继续有效。

### 13.6 DocumentContext 不合并进 AttachmentContext

继续保持两个正交字段：

```text
attachment_ids
    = 本轮数据来源

document_context
    = 本轮使用哪个正式模板
```

示例：

```json
{
  "query": "根据附件和知识库生成设计说明书",
  "attachment_ids": ["att-data-1", "att-data-2"],
  "source_scope": "attachment_and_knowledge_base",
  "document_context": {
    "analysis_id": "...",
    "template_version_id": "..."
  },
  "document_flow": true
}
```

### 13.7 必须增加 Document Flow 的 Multi-source Evidence Bridge

当前 `runner.py` 在 `document_flow_routed` 时将 tools 收敛为 `document_tools`。

因此仅给普通 Agent 增加附件工具，不足以实现：

```text
模板 + 附件 + KB -> 文档生成
```

推荐方案不是让 renderer 直接读附件，而是在文档生成域增加：

```text
DocumentEvidenceProvider
```

接口：

```python
class DocumentEvidenceProvider(Protocol):
    def retrieve(
        self,
        *,
        query: str,
        source_scope: SourceScope,
        attachment_ids: list[str],
        kb_name: str,
        ctx: RequestContext,
    ) -> list[Evidence]:
        ...
```

默认组合：

```text
KnowledgeBaseEvidenceProvider
+ AttachmentEvidenceProvider
-> CompositeDocumentEvidenceProvider
```

文档 harness / generation work order 保存：

```text
source_scope_snapshot
attachment_refs_snapshot
kb_scope_snapshot
```

这样 Document Flow 可以使用统一 Evidence，而不破坏 TemplateVersion/renderer 安全边界。

---

## 14. API 最终设计

### 14.1 Attachment API

```http
POST   /api/v1/conversations/{session_id}/attachments
GET    /api/v1/conversations/{session_id}/attachments
GET    /api/v1/conversations/{session_id}/attachments/{attachment_id}
DELETE /api/v1/conversations/{session_id}/attachments/{attachment_id}
POST   /api/v1/conversations/{session_id}/attachments/{attachment_id}/retry
```

上传支持：

```text
client_request_id
```

返回：

```text
attachment_id
asset_id
filename
media_type
size_bytes
status
parse_status
manifest
error_code
error_message
created_at
expires_at
```

### 14.2 Turn API

`CreateTurnRequest`：

```json
{
  "query": "...",
  "attachment_ids": ["att-1", "att-2"],
  "source_scope": "auto",
  "document_context": null,
  "document_flow": null
}
```

`TurnView` / `MessageView` 返回附件快照摘要和等待状态。

### 14.3 Template-from-Attachment API

```http
POST /api/v1/document-generation/templates/analyze-from-attachment
```

保留旧模板 API 直到新入口稳定，不做破坏性迁移。

---

## 15. 前端最终设计

### 15.1 Composer

最终：

```text
“上传模板” -> “添加附件”
```

支持：

```text
.pdf
.docx
.txt
.md
.xlsx
.xlsm（受限）
.edf
.edif
```

### 15.2 Attachment UI

新增：

```text
AttachmentChip.tsx
AttachmentList.tsx
```

展示：

```text
filename
parse status
selected/unselected
degraded reason
retry
delete
作为模板（仅支持 xlsx/xlsm/docx 且有 KB write 权限）
```

### 15.3 模板迁移顺序

在 `analyze-from-attachment` 和 DocumentContext bridge 未完成前：

```text
保留旧“上传模板”入口
```

完成并通过回归测试后，再在 feature flag 下切换为统一“添加附件”。

不能先删除旧模板入口再补转换链路。

### 15.4 Source Scope UI

第一版可提供简单选择：

```text
自动
仅附件
仅知识库
附件 + 知识库
```

自然语言范围识别作为 Agent planner 增强，但 UI/API 明确 scope 始终优先。

---

## 16. Phase 6：OCR / Visual / Hybrid Retrieval

该阶段是增强能力，不阻塞附件核心链路；**本地没有 GPU 也可以完整实施。**

### 16.1 OCR：本地 CPU

流程：

```text
PDF text density too low
 -> render selected page
 -> LocalOcrEngine
 -> OCR parts
 -> Evidence
```

抽象：

```python
class OcrEngine(Protocol):
    def recognize(self, image: bytes) -> OcrResult:
        ...
```

可实现：

```text
LocalCpuOcrEngine
RemoteOcrEngine
DisabledOcrEngine
```

OCR 不要求 GPU；Worker 异步执行即可。

### 16.2 Visual：默认远程按页调用

新增：

```text
AttachmentVisualAnalyzer
MultimodalModelGateway
```

默认职责：

```text
只处理被命中的页面/用户指定页面
不默认上传完整 PDF
不替代本地 parser
不生成 embedding
```

推荐火山方舟在线推理：

```text
Base URL: https://ark.cn-beijing.volces.com/api/v3
Responses: /api/v3/responses
Model: 使用控制台当前实际可用 Model/Endpoint ID
```

配置不要 fallback 到 `MEMORY_EMBEDDING_API_KEY`。

推荐：

```text
ARK_API_KEY / CredentialProvider
    -> Memory embedding capability
    -> Attachment visual capability
```

两者可以最终使用同一个 secret，但不要形成跨领域配置依赖。

### 16.3 Visual 权限

调用前重新验证：

```text
attachment ownership
page bounds
max pages
image size
remote inference policy
```

请求只包含：

```text
选中页图片
最小必要 text context
用户问题
```

### 16.4 Hybrid Retrieval

Dense Retrieval 可选：

```text
Sparse + Dense -> RRF -> optional rerank
```

Embedding 可以：

```text
远程 API
或
本地 CPU 小模型
```

不要求本地 GPU。

第一阶段不开 Dense 也完全可行。

### 16.5 CPU-only 推荐部署

```text
8~16 CPU cores
16~32 GB RAM
SSD
GPU: none
```

本地运行：

```text
FastAPI
Deep Agents
SQLite / FTS5
PDF/DOCX parser
CPU OCR
Spreadsheet/Circuit services
Attachment Worker
```

远程可选：

```text
Main LLM
Embedding API
Doubao visual API
```

只有未来要求“视觉模型也完全离线本地运行”时，才把 GPU 作为基础设施考虑。

---

## 17. 文件安全与资源限制

上传：

```text
stream write
MIME + extension + signature
size limits
session quota
tenant quota
SHA-256
```

OOXML / ZIP：

```text
zip bomb detection
max uncompressed bytes
compression ratio
recursive archive policy
embedded object policy
external link policy
macro never execute
```

Parser Worker：

```text
timeout
memory budget
page limit
row/cell limit
```

LLM / Agent：

```text
附件正文 = untrusted data
不可覆盖 system prompt
不可生成本地路径能力
不可执行附件脚本
```

日志：

```text
记录 IDs/hash/status/size bucket/latency
不记录完整正文
不记录页面图像
不记录 API key
```

---

## 18. 生命周期与删除

### 18.1 单附件删除

```text
soft-delete attachment record
 -> detach from future turns
 -> if no attachment references asset:
      schedule asset cleanup
```

历史 `turn_attachments` 快照继续存在。

### 18.2 Session 删除

`SessionCleanupCoordinator`：

```text
1. mark session resources unavailable
2. cancel queued attachment jobs
3. delete/reclaim attachment assets and parts
4. delete FTS/vector/OCR/visual cache
5. clean result exports/artifacts
6. delete agent checkpoint
7. cleanup failure -> session_cleanup_outbox
```

删除 API 不因下游磁盘清理失败而长期阻塞；清理动作由 outbox 重试。

---

## 19. 配置

核心：

```text
CHAT_ATTACHMENTS_ENABLED
CHAT_ATTACHMENT_STORAGE_DIR
CHAT_ATTACHMENT_INDEX_DB_PATH
CHAT_ATTACHMENT_MAX_BYTES
CHAT_ATTACHMENT_MAX_FILES_PER_SESSION
CHAT_ATTACHMENT_MAX_PAGES
CHAT_ATTACHMENT_MAX_ROWS
CHAT_ATTACHMENT_MAX_CELLS
CHAT_ATTACHMENT_MAX_UNCOMPRESSED_BYTES
CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS
CHAT_ATTACHMENT_RETENTION_SECONDS
CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS
CHAT_ATTACHMENT_CONTEXT_RATIO
```

检索：

```text
CHAT_ATTACHMENT_FTS_ENABLED
CHAT_ATTACHMENT_TRIGRAM_ENABLED
CHAT_ATTACHMENT_DENSE_ENABLED
CHAT_ATTACHMENT_RETRIEVAL_MODE=lexical|hybrid
CHAT_ATTACHMENT_EMBEDDING_PROVIDER
CHAT_ATTACHMENT_EMBEDDING_MODEL
```

OCR：

```text
CHAT_ATTACHMENT_OCR_ENABLED
CHAT_ATTACHMENT_OCR_PROVIDER=local_cpu|remote
CHAT_ATTACHMENT_OCR_MAX_PAGES
```

Visual：

```text
CHAT_ATTACHMENT_VISUAL_ENABLED
CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED
CHAT_ATTACHMENT_VISUAL_PROVIDER=volcengine_ark
CHAT_ATTACHMENT_VISUAL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
CHAT_ATTACHMENT_VISUAL_MODEL
CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN
CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES
CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS
ARK_API_KEY
```

---

## 20. Observability

集中到现有 observability 层，不在附件模块里散落直接 OTel 调用。

建议：

```text
hdb.attachment.upload
hdb.attachment.asset_reuse
hdb.attachment.parse
hdb.attachment.parse_duration
hdb.attachment.search
hdb.attachment.read
hdb.attachment.waiting_turn
hdb.attachment.ocr
hdb.attachment.visual
hdb.attachment.visual_failure
hdb.attachment.degraded
hdb.attachment.cleanup
hdb.attachment.cleanup_retry
```

增加诊断字段：

```text
source_scope
processor_kind
parser_version
retrieval_mode
matched_backend
```

不得记录正文。

---

## 21. 代码修改范围

### 21.1 新增后端

```text
src/attachments/__init__.py
src/attachments/models.py
src/attachments/store.py
src/attachments/service.py
src/attachments/router.py
src/attachments/jobs.py
src/attachments/worker.py
src/attachments/coordinator.py
src/attachments/local_document_parser.py
src/attachments/index.py
src/attachments/retrieval.py
src/attachments/query_normalizer.py
src/attachments/evidence.py
src/attachments/template_bridge.py

src/agents/scopes/base.py
src/agents/scopes/knowledge_base.py
src/agents/scopes/attachment.py
src/agents/tools/attachment_tools.py

src/document_authoring/evidence_provider.py
src/core/session_cleanup.py
```

Phase 6：

```text
src/attachments/ocr.py
src/attachments/visual.py
src/core/multimodal_gateway.py
src/core/embedding_gateway.py
```

### 21.2 修改后端

```text
src/api/schemas.py
src/api/routes/query.py
src/api/routes/conversations.py
src/api/routes/document_generation.py
src/core/conversation.py
src/agents/runner.py
src/agents/tools/runtime.py
src/agents/tools/spreadsheet_tools.py
src/agents/tools/circuit_tools.py
src/document_authoring/models.py
src/document_authoring/service.py
src/document_authoring/work_order_store.py
src/workers/*
src/settings.py
src/observability/metrics.py
```

### 21.3 保持行为不变

```text
src/pipelines/registry.py           # 不为附件重构二维 registry
src/pipelines/document_rag/         # KB RAGFlow 行为不改
src/api/routes/upload.py            # KB upload 行为不改
src/pipelines/spreadsheet/           # parser 复用，只做 scope 接入
src/circuit/                         # parser/query 核心复用，只做 scope/storage adapter
```

`src/ingestion/parser_registry.py` 不是附件实现的强制入口。

### 21.4 前端新增

```text
frontend/src/api/attachments.ts
frontend/src/pages/chat/components/AttachmentChip.tsx
frontend/src/pages/chat/components/AttachmentList.tsx
frontend/src/pages/chat/components/AttachmentSourceScope.tsx
```

### 21.5 前端修改

```text
frontend/src/api/types.ts
frontend/src/pages/chat/components/Composer.tsx
frontend/src/pages/chat/ChatPage.tsx
frontend/src/pages/chat/useKbChat.ts
```

---

## 22. 分阶段实施计划

## Phase 0：契约、回归锁与 Migration

目标：先固定不会反复变化的领域契约。

实施：

```text
AttachmentRef
AttachmentAsset
SourceScope
parse status
turn_attachments snapshot
DB migrations
feature flags
```

同时补当前行为 regression tests：

```text
PipelineRegistry extension conflict
General Chat current behavior
KB document search current behavior
Template upload/analyze/generate current behavior
Spreadsheet/Circuit KB permission behavior
```

Definition of Done：

- migration 可重复执行；
- 新表未启用时不改变现有用户行为；
- 旧测试全部通过。

---

## Phase 1：附件生命周期基础设施

实施：

```text
AttachmentService
AttachmentAsset storage
upload/list/detail/delete/retry
client_request_id idempotency
asset reuse
parse job queue
AttachmentTurnCoordinator skeleton
frontend attachment cards
```

此时可只支持上传和状态，不要求 Agent 能回答附件问题。

**保留旧“上传模板”入口。**

Definition of Done：

- 不需 KB write 权限即可上传当前会话普通附件；
- 无法读其他会话附件；
- 相同文件在同 session 复用 asset；
- 删除附件不会破坏历史 turn snapshot。

---

## Phase 2：本地 PDF / DOCX + Hardware Lexical Retrieval

实施：

```text
PDF parser
DOCX parser
canonical parts
FTS5 probe
ExactIdentifierRetriever
SparseTextRetriever
optional trigram
QueryNormalizer
attachment_search
attachment_read
Evidence
```

专项测试：

```text
中文
料号
型号
RefDes
Net Name
+12V
VDD_3V3
STM32H743
R1/U2/EN/FB
```

Definition of Done：

- 能回答附件内容问题并返回定位；
- 无文本 PDF 明确 degraded；
- FTS5 缺失不导致服务崩溃。

---

## Phase 3：Deep Agents + General Chat + Turn Waiting

实施：

```text
CreateTurnRequest.attachment_ids/source_scope
ToolRuntime attachment scope
attachment tools
scope-based tool assembly
General Chat + Attachment Deep Agent
waiting_for_attachments
AttachmentTurnCoordinator
SSE / frontend status
```

Definition of Done：

```text
General + Attachment works
KB only does not read attachments
Attachment only does not call RAGFlow
Combined can return both Evidence classes
```

---

## Phase 4：Excel / EDF Attachment Scope

实施：

```text
SpreadsheetScopeResolver
CircuitScopeResolver
attachment-scoped SpreadsheetIndexService storage
attachment CircuitStore root
SQL scope enforcement
attachment_id -> design_id mapping
```

Definition of Done：

- 同一 Excel 在 KB 和 Attachment 的领域解析结果一致；
- 同一 EDF 的器件、网络、模块结果一致；
- 差异只存在于 ACL/storage namespace；
- KB SQL/Circuit 安全策略没有被附件路径绕开。

---

## Phase 5：模板附件化与 Document Flow Multi-source

实施顺序：

```text
analyze-from-attachment API
Template provenance
Attachment card “作为模板”
DocumentEvidenceProvider
source_scope_snapshot in document generation
attachment Evidence bridge
regression tests
```

全部稳定后：

```text
Composer “上传模板” -> “添加附件”
```

旧模板 API 继续保留兼容。

Definition of Done：

```text
Attachment DOCX/XLSX/XLSM -> TemplateVersion
TemplateVersion 不依赖原 attachment 生命周期
Template + KB -> generation works
Template + Attachment -> generation works
Template + Attachment + KB -> generation works
```

---

## Phase 6：OCR / Visual / Hybrid Retrieval

实施状态：已完成可插拔 CPU OCR、按页视觉分析和可选 Dense/RRF 混合检索；运行时仍由独立开关与凭据控制，高级能力不可用时统一返回 degraded 诊断。

实施：

### 6A CPU OCR

```text
PDF renderer
LocalOcrEngine
ocr_text parts
```

### 6B Dense/Hybrid

```text
EmbeddingGateway
optional vector store
DenseRetriever
RRF
retrieval evaluation
```

### 6C Visual

```text
AttachmentVisualAnalyzer
MultimodalModelGateway
selected-page remote visual inference
visual Evidence
```

Definition of Done：

- CPU-only 环境可以运行；
- 远程视觉关闭时核心功能不受影响；
- Vision failure 只产生 degraded，不破坏本地附件检索。

---

## Phase 7：清理、观测、灰度与发布

实施状态：会话清理 outbox、附件/文档工单与产物回收、队列/阶段/附件观测已接入；灰度开关保留在部署配置层。

实施：

```text
SessionCleanupCoordinator
session_cleanup_outbox
export/artifact/checkpoint cleanup
metrics
quota
load tests
retention cleanup
feature flag rollout
```

发布顺序：

```text
internal dev
-> single tenant pilot
-> selected users
-> default on
```

---

## 23. 测试矩阵

### 23.1 权限

1. 普通附件不需要 KB write。
2. 其他用户/会话 attachment id 返回权限拒绝且不泄露文件存在性。
3. `attachment_only` 不调用 RAGFlow。
4. `knowledge_base_only` 不读取 attachment。
5. analyze-as-template 必须重新检查目标 KB write/admin。
6. attachment SQL / Circuit query 无法越权其他 asset。

### 23.2 数据模型与幂等

1. 同一 `client_request_id` 返回同一 attachment。
2. 同 session 同 hash 两个 attachment 共享一个 asset。
3. 删除一个 attachment 不误删仍被引用 asset。
4. parser version 更新可安全重建 parts。

### 23.3 状态机

1. parse 中创建 turn -> waiting。
2. 所有 required ready -> pending。
3. required failed -> failed。
4. degraded -> pending + diagnostic。
5. cancel waiting turn 生效。
6. 页面刷新/切换不丢状态。

### 23.4 本地文档

1. PDF page locator 稳定。
2. DOCX heading/paragraph/table locator 稳定。
3. 扫描件不产生虚假文本证据。
4. 小文件全文注入不突破动态 token budget。

### 23.5 Hardware Retrieval

专项：

```text
连续中文
STM32H743ZI
H743
TPS62130
VDD_3V3
+12V
R1
U2
EN
FB
USB_DP
CAN_H/CAN_L
```

同时验证 FTS 特殊字符不会产生查询语法错误。

### 23.6 Excel / Circuit

1. KB/Attachment parser 输出一致。
2. SQL 仍只读。
3. attachment scope 过滤不可被 SQL 绕过。
4. Circuit store 不允许路径逃逸。
5. attachment 和 KB 同名 design 不冲突。

### 23.7 模板

1. 原 `/document-generation/templates/analyze` 回归通过。
2. attachment -> template 使用相同 sanitizer。
3. 原 attachment 删除后 TemplateVersion 仍可生成文档。
4. 无 KB write 权限不能把 attachment 转为模板。
5. document_context 与 attachment_ids 可同时存在。
6. Template + Attachment + KB 能生成带正确 Evidence 的文档。

### 23.8 OCR / Visual

1. 视觉开关关闭不产生远程请求。
2. 只发送授权页面。
3. 供应商超时 -> degraded。
4. CPU-only 环境 OCR 可运行。
5. Visual 结果保留 attachment/page/provider/model/request id。

### 23.9 删除

1. Session 删除后附件不可访问。
2. parts/index/file 被清理。
3. export/artifact/checkpoint 清理失败进入 outbox。
4. outbox 重试幂等。

---

## 24. 验收标准（最终 Definition of Done）

只有以下全部成立，才认为最终目标完成：

### 用户体验

- 普通聊天和 KB 聊天均可添加附件；
- 用户可明确选择附件/KB/联合范围；
- 能查看解析状态、失败原因、重试和删除；
- 支持将合法 DOCX/XLSX/XLSM 附件显式转换为模板；
- 用户可根据模板 + KB + Attachment 生成文档。

### 架构

- KB PDF/DOCX 继续 RAGFlow；
- Chat PDF/DOCX 不自动进入 RAGFlow；
- `PipelineRegistry` 的 KB 行为无回归；
- Excel/EDF 只有一套领域 parser/query engine；
- Agent 不拥有任意文件系统能力；
- Attachment/KB 使用统一 Evidence。

### 安全

- session/user/KB ACL 全部后端强制；
- 模板转换重新检查 KB write；
- OOXML active content 不执行；
- SQL 只读且 scope locked；
- Remote Visual 明确受管理员配置控制。

### 生命周期

- 上传、解析、turn、删除、过期、cleanup 可恢复；
- 历史 turn 保留附件快照；
- session 删除可最终清除 attachment/export/checkpoint 资源。

### 部署

- 核心功能在无 GPU 环境可运行；
- OCR 可 CPU 执行；
- Dense 可关闭；
- Visual 可关闭或远程按需；
- 任一高级能力关闭不破坏核心附件链路。

---

## 25. P0 / P1 实施优先级

### P0：进入代码前必须解决

```text
1. 独立 AttachmentProcessorRouter，不破坏 PipelineRegistry 不变量
2. AttachmentRecord / AttachmentAsset 分层
3. General Chat + Attachment Agent 路由
4. waiting_for_attachments 完整状态机
5. CircuitStore attachment root，不伪造 kb_name 路径
6. Ark 在线推理 Base URL 使用 /api/v3
7. 旧模板入口在新模板 bridge 完成前不得删除
8. Document Flow 必须能获取 attachment Evidence
```

### P1：强烈推荐同时完成

```text
1. Spreadsheet/Circuit ScopeResolver
2. SessionCleanupCoordinator + 完整 outbox 状态机
3. Hardware-specific lexical + trigram/exact 组合策略
4. Turn attachment snapshots + soft delete
5. CredentialProvider 替代 Visual -> MEMORY_EMBEDDING_API_KEY fallback
6. parser 内部 XLSX/ZIP 资源限制
```

---

## 26. 回滚策略

1. `CHAT_ATTACHMENTS_ENABLED=false`：隐藏新入口，原 KB 和模板能力继续工作。
2. Attachment Worker 停止：原文件和任务保留，可恢复。
3. Local search 故障：附件仍可上传/删除；KB RAGFlow 不受影响。
4. Dense/Visual/OCR 分别有独立开关，可单独关闭。
5. 新 template-from-attachment 故障：保留旧 `/document-generation/templates/analyze`。
6. 数据库 migration 只 additive，不删除原 chat/KB/template 数据。
7. 新 frontend 入口必须 feature flag 灰度，不做一次性替换。

---

## 27. 实施纪律

开发过程中遵守以下约束：

```text
不要因为“代码复用”破坏来源权限边界。
不要因为“统一路由”扩大现有 KB ingestion 回归面。
不要因为“Agent 更聪明”把 ACL 决策交给模型。
不要因为“附件已经存在”绕过模板 sanitizer。
不要因为“Hybrid 更先进”阻塞第一版 lexical retrieval。
不要因为“视觉效果好”默认把完整文件发送到远程模型。
不要为了本功能引入本地 GPU 作为部署前提。
```

最终稳定抽象应是：

```text
Source
Scope
Asset
Evidence
Tool
Lifecycle
```

底层未来可以替换：

```text
FTS5 -> Elasticsearch/OpenSearch
Dense store -> sqlite-vec/Qdrant/pgvector
Doubao -> approved local/cloud vision provider
RAGFlow -> another KB retrieval backend
```

但这些替换不应改变上层 Agent、权限模型、文档模板链和用户交互契约。

---

## 28. 代码基线参考

本方案主要基于以下 `feature/fix-module-matching` 文件核查：

- `src/pipelines/registry.py`
- `src/api/schemas.py`
- `src/core/conversation.py`
- `src/agents/runner.py`
- `src/agents/tools/runtime.py`
- `src/agents/tools/spreadsheet_tools.py`
- `src/agents/tools/circuit_tools.py`
- `src/circuit/store.py`
- `src/ingestion/parser_registry.py`
- `src/api/routes/document_generation.py`
- `src/document_authoring/service.py`
- `src/agents/tools/document_authoring_tools.py`
- `frontend/src/pages/chat/components/Composer.tsx`
- `frontend/src/pages/chat/ChatPage.tsx`

外部技术参考：

- SQLite FTS5：https://www.sqlite.org/fts5.html
- 火山方舟 Doubao Seed 2.0：https://www.volcengine.com/docs/82379/1795150
- 火山方舟 Responses API 工具调用：https://www.volcengine.com/docs/82379/1958524?lang=zh

---

## 29. 最终结论

本方案完成后的系统边界应稳定为：

```text
Knowledge Base
    = 长期共享知识资产
    = RAGFlow + 现有结构化领域能力

Chat Attachment
    = 会话私有临时资产
    = 本地解析 + 本地作用域 + 可选 OCR/Visual/Hybrid

Document Template
    = 独立、sanitize、版本化、可审计的正式模板资产
    = Attachment 可以成为其来源，但不能替代 TemplateVersion

Deep Agents
    = 意图理解 + SourceScope 下的工具编排
    = 不拥有任意本地文件系统能力

Evidence
    = KB / Attachment / Structured data 的统一事实接口

Export / Document Generation
    = 在 Evidence 基础上的独立结果交付层
```

建议严格按 Phase 0 -> Phase 7 推进，不提前引入 GPU、本地大视觉模型或重型向量基础设施。先完成来源、权限、生命周期、General Chat 路由和模板兼容，再逐步增加 OCR、Visual 和 Hybrid Retrieval。
