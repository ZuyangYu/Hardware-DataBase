# 本地会话附件与知识库 RAGFlow 双通道设计

- 日期：2026-09-03
- 状态：Proposed（已按代码核查意见修订，等待用户审阅）
- 本次修订：补充混合检索、动态上下文注入、火山方舟 `Doubao-Seed-2.0-lite` 按需视觉分析通道，并对齐当前 `.env` 的火山连接配置。
- 范围：将聊天中的“上传模板”升级为会话附件；会话附件中的 PDF/DOCX 在本地解析和检索；知识库文件继续使用现有 RAGFlow 链路；Excel/EDF 复用现有结构化解析与查询能力；Deep Agents 负责意图理解和工具编排。
- 关联设计：[2026-09-02-agent-artifact-export-design.md](./2026-09-02-agent-artifact-export-design.md)、[2026-09-03-conversational-export-design.md](./2026-09-03-conversational-export-design.md)

## 1. 决策摘要

本设计采用“共享领域能力、按来源分流”的方案：

```text
知识库文件
  -> 现有 Ingestion
  -> PipelineRegistry(source_type=knowledge_base)
  -> RAGFlow / 现有结构化管线

会话附件
  -> ChatAttachmentService
  -> PipelineRegistry(source_type=chat_attachment)
  -> 本地 PDF/DOCX 解析
  -> 现有 Excel/EDF 解析与查询服务

页面视觉分析（按需）
  -> 本地页面渲染 / 图片提取
  -> MultimodalModelGateway
  -> 火山方舟 Doubao-Seed-2.0-lite
```

两条链路共享 `Evidence`、内容定位、解析状态、观测和 Agent 工具协议，但不共享知识库权限、物理存储、生命周期或默认检索范围。

本设计中的“本地附件处理”指原文件存储、格式解析、分块、索引和权限控制在本系统内完成。若启用火山方舟视觉分析，只有经过来源和页码筛选的页面图像及必要的上下文会发送到火山方舟；这不等同于完全内网推理，是否允许外发由部署配置和组织隐私策略决定。

明确不采用以下做法：

- 不把会话附件自动上传到 RAGFlow；
- 不复制一套 Excel 或 EDF 解析器；
- 不让模型直接访问本地文件路径；
- 不让模型自由执行用户文件中的脚本；
- 不把供应商原生文件输入能力作为系统的唯一实现；
- 不让附件内容在未被用户选择时自动参与后续所有对话。

## 2. 当前基线与问题

当前聊天上传入口是文档模板专用能力：

- `frontend/src/pages/chat/components/Composer.tsx` 只在文档创作构建开关（`VITE_AGENT_DOCUMENT_TOOLS_ENABLED` / `VITE_DOCUMENT_AUTHORING_CHAT_ENABLED`，默认开启）开启时显示上传入口，并接受 `.xlsx/.xlsm/.docx`；UI 内的“文档生成模式”开关不控制上传入口；
- `frontend/src/pages/chat/ChatPage.tsx` 的 `handleTemplateUpload` 调用模板分析 API；
- `/document-generation/templates/analyze` 要求知识库写权限，并把文件交给文档创作服务；
- `CreateTurnRequest` 只有 `document_context`，没有通用附件引用；
- `ToolRuntime` 只有知识库、文档模板和会话 ID 上下文，没有通用附件引用；
- `document_search` 绑定 RAGFlow，表格和电路工具绑定当前知识库。

现有领域能力已经存在：

- `src/pipelines/spreadsheet/xlsx_parser.py`、`SpreadsheetIndexService` 和表格 SQL 工具负责 Excel 结构化处理；
- `src/circuit/parsers`、`CircuitIndexService` 和 `CircuitQueryEngine` 负责 EDF/EDIF；
- 知识库 PDF/DOCX 由 RAGFlow 管线处理；
- `MultiSourceAgentRunner` 已通过 `deepagents.create_deep_agent` 动态调度工具，并且当前主动排除了通用文件系统和 Shell 工具。

因此新增功能的核心不是再造一个“附件解析系统”，而是补充会话附件的存储、权限、解析作用域、局部检索工具和生命周期。

## 3. 目标与非目标

### 3.1 目标

1. 将聊天入口从“上传模板”改为“添加附件”。
2. 会话附件中的 PDF/DOCX 在应用本地完成安全检查、解析、分块和检索。
3. 知识库中的 PDF/DOCX 继续进入现有 RAGFlow，现有知识库上传、权限和检索行为不变。
4. Excel 和 EDF/EDIF 附件复用现有解析器、索引服务和查询引擎，只增加附件作用域。
5. 用户可以要求只使用附件、只使用知识库，或联合使用两者。
6. 用户上传附件后，下一条消息可以通过自然语言指定总结、抽取、比较、校验、计算或导出任务。
7. Agent 返回的附件证据包含文件名和页码、段落、工作表、单元格、网络等定位信息。
8. 解析任务和依赖附件的对话任务支持后台执行、页面切换和恢复。
9. 删除会话时清理附件原件、解析索引、未完成任务和相关导出文件。
10. 模板能力继续可用，但只作为附件的特殊角色，不影响普通附件解析。

### 3.2 非目标

1. 本期不替换 RAGFlow 的知识库文档解析和检索。
2. 本期不把本地 PDF/DOCX 索引升级为新的通用本地向量平台；第一阶段使用章节/页分块和 SQLite FTS5，向量检索另行评估。
3. 本期不提供在线 Word/PDF 编辑器。
4. 本期不允许 Agent 自由运行 Shell、访问任意路径或执行附件中携带的宏/脚本。
5. 本期不自动把附件归档到共享知识库；归档是单独的显式操作。
6. 本期不保证扫描件、复杂 PDF 表格和复杂版式 100% 还原；解析能力必须通过状态和降级信息对用户可见。
7. `Doubao-Seed-2.0-lite` 作为本期推荐的视觉理解适配器，不替代本地解析器、附件索引或知识库 RAGFlow；完全内网场景默认关闭远程视觉调用。

## 4. 方案比较

### 方案 A：所有附件复用 RAGFlow

上传附件后创建临时 RAGFlow 文档或临时数据集。

优点：实现较快，检索能力成熟。

缺点：会话级删除和权限隔离复杂；临时数据集生命周期难治理；普通附件会污染 RAGFlow 资源；无法自然复用本地 Excel/EDF 结构化能力；敏感附件必须离开本地部署。

结论：不采用。

### 方案 B：本地附件服务 + 现有领域解析器复用

PDF/DOCX 由本地解析器处理，Excel/EDF 复用现有管线，知识库仍由 RAGFlow 处理。Deep Agents 只通过来源受限的工具访问数据。

优点：权限和生命周期清晰；不污染知识库；适合临时文件和内网部署；可复用当前结构化能力；供应商无关。

缺点：需要维护本地 PDF/DOCX 解析器；扫描件 OCR 和复杂版式需要后续能力；会增加本地存储和索引任务。

结论：推荐。

### 方案 C：本地保存文件，直接交给模型供应商原生文件输入

本地只保存附件，模型调用时将文件 ID 或原文件交给供应商处理。

优点：应用侧解析代码少；小文件和视觉分析体验较好。

缺点：依赖模型供应商能力；不能保证 Ollama 和 OpenAI-compatible provider 行为一致；权限、引用、数据留存和成本边界不易统一；不能替代 Excel/EDF 的确定性计算。

结论：只作为图片、复杂页面或局部视觉分析的可选补充，不作为核心附件链路。

## 5. 总体架构

```text
React Chat
  |
  | POST attachment / POST turn(attachment_ids)
  v
FastAPI
  |
  +-- ChatAttachmentService
  |     +-- 安全检查
  |     +-- 私有存储
  |     +-- 解析任务
  |     +-- 生命周期
  |
  +-- SourceRouter
  |     +-- knowledge_base -> existing RAGFlow path
  |     +-- chat_attachment -> local attachment path
  |
  v
Attachment Processing Kernel
  +-- local document parser: PDF/DOCX
  +-- existing spreadsheet parser/index: XLSX/CSV extension path
  +-- existing circuit parser/index: EDF/EDIF
  +-- canonical manifest / parts / evidence
  |
  v
Deep Agents
  +-- attachment_list
  +-- attachment_search
  +-- attachment_read
  +-- attachment_table_query -> existing spreadsheet service
  +-- attachment_circuit_search -> existing circuit service
  +-- document_search -> RAGFlow only
  |
  v
ResultSnapshot / ResultEnvelope / ExportJob
  +-- chat answer
  +-- Markdown / XLSX / DOCX / PPTX / PDF Artifact
```

### 5.1 来源引用

所有 Agent 工具使用明确的 `SourceRef`：

```json
{
  "source_type": "chat_attachment",
  "source_id": "att-01H...",
  "session_id": 42,
  "filename": "design-review.pdf"
}
```

知识库引用则使用：

```json
{
  "source_type": "knowledge_base",
  "knowledge_base_name": "project-design",
  "document_id": "ragflow-document-id"
}
```

来源类型是权限和路由的硬边界。模型不能自行修改 `source_type`，后端根据用户、会话和已持久化的附件关系重新解析并校验。

### 5.2 单一解析事实

同一个文件在同一个作用域内只解析一次，使用以下键进行幂等控制：

```text
tenant_id + scope_type + scope_id + content_hash + parser_version
```

对会话附件，`scope_type` 固定为 `chat_session`，`scope_id` 即 `session_id`。幂等作用于解析产物（canonical parts、索引、manifest），不限制同一会话内上传多份相同内容的附件记录：后一份相同 `(session_id, sha256, parser_version)` 的附件直接复用已就绪的解析产物，不再重复入队解析任务。

解析器输出统一的领域对象，索引只是它的投影：

```text
CanonicalAsset
  +-- manifest
  +-- parts: text/table/image/circuit
  +-- locators
  +-- diagnostics
  +-- parser_version
```

允许知识库和附件各自有索引，但不能复制 Excel 或 EDF 的底层解析代码。跨作用域共享解析缓存时必须经过租户和权限边界，默认不做跨用户全局去重。

### 5.3 多模态模型通道

视觉模型通过后端 `MultimodalModelGateway` 接入，Agent 和解析器不直接依赖火山方舟 SDK：

```text
AttachmentVisualAnalyzer
  -> MultimodalModelGateway
  -> provider=volcengine_ark
  -> model=Doubao-Seed-2.0-lite
```

本期推荐使用火山方舟的 `Doubao-Seed-2.0-lite` 作为视觉理解模型。它的逻辑名称与火山方舟实际 Endpoint ID 分离保存。方舟官方示例使用过形如 `doubao-seed-2-0-lite-260215` 的模型 ID，后续模型版本可能变化；生产配置必须使用控制台实际开通的 Endpoint ID，不把日期版本硬编码到业务代码。方舟 Responses API 支持通过函数调用接入外部工具，本方案只将它用于受限的视觉分析请求，不让模型获得本地文件系统权限。

连接配置与当前 `.env` 保持一致：视觉网关的默认 Base URL 使用 `https://ark.cn-beijing.volces.com/api/plan/v3`，API Key 复用当前环境中已有的火山凭据来源，前提是该凭据已开通视觉 Endpoint 权限。实现时建议让 `CHAT_ATTACHMENT_VISUAL_BASE_URL` 回退到 `MEMORY_EMBEDDING_BASE_URL`，让 `CHAT_ATTACHMENT_VISUAL_API_KEY` 回退到 `MEMORY_EMBEDDING_API_KEY`，从而避免在 `.env` 中复制密钥；如果权限不同，才通过显式视觉配置覆盖，不能静默使用无权限凭据。`EVAL_EMBEDDING_*` 只用于评估，不作为线上附件视觉调用配置。视觉模型使用独立的 `CHAT_ATTACHMENT_VISUAL_MODEL`，不能误用 `MEMORY_EMBEDDING_MODEL`。

虽然方舟也提供文档理解入口，第一版仍不把完整 PDF/DOCX 直接交给供应商处理，而是由本地服务完成解析和页面筛选后，再将必要的页面图像发送给视觉模型。这样可以保持附件权限、删除、引用和降级状态由本系统控制；供应商原生整文件输入只作为经过隐私审批的后续实验通道。

默认职责边界：

- 现有文本 Agent 模型继续负责意图理解、检索工具编排和结果组织，不因上传图片自动切换整个 Agent 模型；
- `Doubao-Seed-2.0-lite` 只处理被选中的 PDF 页面图像、DOCX 提取图片或 OCR/文本辅助信息；
- 本地 PDF/DOCX 解析和 FTS/向量索引仍是事实来源，视觉模型输出作为带页码的 `visual_evidence`，不能覆盖原始解析事实；
- `Doubao-Seed-2.0-lite` 是视觉推理模型，不用它生成向量。未来需要图像/文本联合向量检索时，另行选择视觉 embedding 模型并记录其版本，不能复用视觉推理模型的输出作为 embedding；
- 远程模型调用失败、超时或被策略禁止时，系统回退到文本/OCR 路径并将结果标记为 `degraded`，不能让整个附件查询无理由失败。

接口约定：

```text
analyze_pages(
  attachment_id,
  page_refs,
  user_question,
  text_context,
  max_pages,
) -> VisualAnalysisResult
```

网关负责请求格式转换、超时、重试、脱敏、模型 ID、供应商请求 ID 和用量记录；上层只接收结构化结论、页码/图像定位、置信信息和诊断信息。

视觉结果如需缓存，缓存键至少包含 `attachment_content_hash + render_version + page_refs + question_hash + provider + endpoint_id + prompt_version`，并遵守会话权限，不能因为文件内容相同就跨用户共享视觉结果。

## 6. 数据模型与存储

### 6.1 `chat_attachments`

会话附件元数据放在当前会话数据库中，以便和会话权限、删除关系保持一致：

```text
attachment_id TEXT PRIMARY KEY
session_id INTEGER NOT NULL
user_id INTEGER NOT NULL
tenant_id TEXT NOT NULL DEFAULT 'default'
filename TEXT NOT NULL
media_type TEXT NOT NULL
extension TEXT NOT NULL
size_bytes INTEGER NOT NULL
sha256 TEXT NOT NULL
role TEXT NOT NULL DEFAULT 'reference'
status TEXT NOT NULL
storage_key TEXT NOT NULL
manifest_json TEXT NOT NULL DEFAULT '{}'
parser_version TEXT NOT NULL DEFAULT ''
error_code TEXT NOT NULL DEFAULT ''
error_message TEXT NOT NULL DEFAULT ''
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
expires_at TEXT
deleted_at TEXT
```

`role` 取值：

```text
reference  普通参考资料
data       数据分析输入
template   文档模板
```

`tenant_id` 取自 `RequestContext.tenant_id`。当前 chat 域为单租户（`chat_sessions` 只有 `user_id`/`department_id`，conversation 域固定写 `default`），该列用于与 `result_exports`、`document_authoring` 等域的表结构对齐并预留多租户，参与唯一约束但不承担实际隔离；隔离主体是 `user_id + session_id`。

### 6.2 `chat_attachment_parts`

解析正文和定位信息放入独立的附件索引数据库，避免大量文本放大会话数据库：

```text
part_id TEXT PRIMARY KEY
attachment_id TEXT NOT NULL
ordinal INTEGER NOT NULL
part_type TEXT NOT NULL
text_content TEXT NOT NULL DEFAULT ''
locator_json TEXT NOT NULL DEFAULT '{}'
metadata_json TEXT NOT NULL DEFAULT '{}'
content_hash TEXT NOT NULL
```

`part_type` 初始支持 `text`、`table`、`image`，结构化附件可增加 `circuit`。

附件索引数据库增加 FTS5 投影，用于 PDF/DOCX 的本地全文检索。FTS 结果必须回查 `chat_attachment_parts`，不能把 FTS 行直接作为最终证据。

FTS5 是本项目首次引入，需要补充以下落地约定：

- 目标运行环境的 SQLite 必须编译包含 FTS5 模块；服务启动时显式探测可用性，探测失败时附件检索功能降级关闭，不允许启动失败或生成虚假证据；
- 附件索引库是独立于 `AUTH_DB_PATH` 的 SQLite 文件，连接与生命周期管理参考 `memory.db` 独立库先例；写路径收敛到解析 Worker 单写者，读路径使用 WAL；
- `chat_attachment_parts.attachment_id` 跨库引用主库记录，不建外键，一致性由应用层和 §11.3 的删除顺序保证。

### 6.3 `chat_attachment_jobs`

```text
job_id TEXT PRIMARY KEY
tenant_id TEXT NOT NULL
user_id TEXT NOT NULL
session_id INTEGER NOT NULL
attachment_id TEXT NOT NULL
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

任务使用现有 Worker 的 claim/lease/heartbeat 模式，页面断开不改变任务状态。字段和幂等索引对齐现有 `document_authoring_jobs`：建立 `(tenant_id, user_id, session_id, attachment_id, job_type)` 幂等唯一索引，配合上传幂等键避免同一附件重复入队。

### 6.4 `turn_attachments`

不要只把附件 ID 放进不可查询的 JSON；使用关联表保存每个 turn 的附件快照：

```text
turn_id TEXT NOT NULL
attachment_id TEXT NOT NULL
role_snapshot TEXT NOT NULL
ordinal INTEGER NOT NULL
PRIMARY KEY(turn_id, attachment_id)
```

`CreateTurnRequest` 增加 `attachment_ids`，后端在创建 turn 时校验附件属于当前用户和当前会话，并将关联关系一次性持久化。后续附件被删除或过期时，历史 turn 仍保留引用状态，但不能继续读取原文件。

### 6.5 `chat_attachment_deletion_outbox`

删除清理失败的可重试队列，模式对齐现有 `memory_deletion_outbox`：

```text
outbox_id TEXT PRIMARY KEY
session_id INTEGER NOT NULL
attachment_id TEXT NOT NULL DEFAULT ''
payload_json TEXT NOT NULL DEFAULT '{}'
reason TEXT NOT NULL DEFAULT ''
attempt INTEGER NOT NULL DEFAULT 0
next_retry_at TEXT
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

Agent checkpoint 删除（`forget_agent_thread` -> `checkpointer.delete_thread`）当前失败会被静默吞掉；本设计将 checkpoint、`result_exports` 和本地文件的清理失败统一写入该 outbox，由 Worker 重试，不阻塞会话删除响应。

## 7. 来源路由和解析器复用

现有路由入口是 `src/pipelines/registry.py` 的 `route_file(file_path, source_group=None)`（模块级封装同签名），`source_group` 决定文本类扩展走外部对话还是 RAGFlow；`src/ingestion/parser_registry.py` 是 `source_group -> ParserFactory` 注册表，没有独立路由函数。本设计将该路由扩展为“来源类型 + 扩展名”二维路由，最小改动是给 `route_file` 增加可选的 `source_type` 参数并保持默认行为不变：

```python
route_file(file_path, source_group=None, source_type="knowledge_base")
route_file(file_path, source_group=None, source_type="chat_attachment")
```

### 7.1 知识库路径

`source_type=knowledge_base` 保持现有行为：

```text
PDF/DOCX -> RAGFlow
XLSX     -> existing spreadsheet index
EDF/EDIF -> existing circuit index
```

现有 `/kbs/{kb_name}/files` API、`PipelineDocumentStore`、RAGFlow 数据集和知识库权限不做行为改变。

### 7.2 会话附件路径

`source_type=chat_attachment` 初始路由：

```text
PDF/DOCX/TXT/MD -> local document parser + local FTS index
XLSX            -> existing xlsx parser + attachment-scoped index
EDF/EDIF        -> existing circuit parser + attachment-scoped design store
```

`.xls` 继续明确拒绝；`.xlsm` 默认只作为受控模板或只读附件处理，必须先进行宏/外链隔离，不能直接执行宏内容。

### 7.3 Excel 复用规则

会话附件不新增 `attachment_xlsx_parser`。现有 `parse_xlsx` 产出的 workbook 模型作为唯一解析结果，再通过作用域适配写入附件索引。

现状澄清：`spreadsheet_row_search`、`spreadsheet_cell_lookup` 和只读 SQL 是闭包绑定 `SpreadsheetIndexService` 实例的 Agent 工具（`src/agents/tools/spreadsheet_tools.py`），不是 service 方法；现有实现通过 `kb_scope_from_context(...).require_department(...)` 强制 department 作用域。附件作用域没有 department 语义：附件版工具工厂在闭包中直接绑定本轮 `attachment_id` 集合，跳过 department 校验，改用 `user_id + session_id + attachment_id` 归属校验。只读 SQL 维持现有 sqlglot 校验、表白名单和 LIMIT 收敛，并在 SQL 执行前注入附件记录范围，禁止模型通过表名或 SQL 自行越权。

### 7.4 EDF/EDIF 复用规则

会话附件不新增 EDF 解析器。现有 `EdfParser`、`CircuitIndexService` 和 `CircuitQueryEngine` 继续负责解析和查询；新增一个附件作用域的设计引用适配器：

```text
attachment_id -> design_id -> CircuitIndexService.query
```

查询证据增加 `attachment_id`、文件名、实例、网络或模块定位。知识库和附件使用不同的存储命名空间，避免同名 `design_id` 冲突：现有电路存储按 `kb_name` 目录划分（`STORAGE_DIR/circuits/{kb_name}/{design_id}/`），附件统一落入保留虚拟命名空间 `__chat_attachments__/{session_id}/`；现有电路工具强制 `department_id` 过滤，附件版工具跳过 department 元数据过滤，改按附件归属校验。`design_id` 继续沿用内容寻址生成规则。

### 7.5 模板规则

普通附件不会触发文档模板分析。用户明确说“把这个附件作为模板生成文档”后才执行：

```text
attachment.role=template
  -> 现有模板清洗与安全检查
  -> 现有 template analyzer
  -> DocumentContext
  -> 文档生成流程
```

现有模板分析接口和 `DocumentContext` 保持兼容；模板附件的来源可以记录 `origin_attachment_id`，但模板版本仍由现有文档创作存储管理。

## 8. 本地 PDF/DOCX 处理

### 8.1 解析阶段

上传完成后先执行确定性处理：

1. 检查真实 MIME、扩展名和文件签名；
2. 计算 SHA-256 并写入私有存储；
3. 解析 PDF 页或 DOCX 文档结构；
4. 生成带顺序和定位信息的 canonical parts，并记录 `page_count`、`has_embedded_image`、`renderable`、`text_density`、`ocr_status` 等视觉候选元数据；
5. 写入 FTS5 索引，并按配置为后续 DenseRetriever 保留 embedding 任务入口；
6. 生成 manifest 和可用性摘要；视觉页面只登记候选，不在上传解析阶段默认发送给远程模型；
7. 更新 `ready`、`degraded` 或 `failed` 状态。

第一版 PDF 适配器使用可部署的本地文本解析库，按页生成内容；DOCX 复用项目已有 `python-docx`/OOXML 依赖（`python-docx>=1.1` 已在 pyproject.toml 中），提取标题、段落、表格和链接。PDF 扫描件和复杂版式在 OCR/渲染能力启用前标记为 `degraded`，不能假装已完整读取。

### 8.2 定位信息

PDF：

```json
{"page": 8, "block": 3}
```

DOCX：

```json
{"heading_path": ["3 电源设计", "3.2 额定电压"], "paragraph": 12}
```

DOCX 的自然页码依赖排版渲染，第一版不承诺固定页码；需要视觉还原时另行生成渲染页图。

### 8.3 检索与上下文策略

附件检索统一经过 `AttachmentRetrievalService`，Agent 不直接感知底层索引实现：

```text
ACL / source scope / attachment_ids 过滤
  -> SparseRetriever（第一版 FTS5）
  -> DenseRetriever（可选，本地 embedding + sqlite-vec 等）
  -> RRF 融合
  -> 可选 rerank
  -> ContextPacker
  -> Evidence / Citation
```

落地规则：

- 第一阶段实现 FTS5 和接口契约；混合检索作为可开关能力，不把 `sqlite-vec` 或某一个 embedding 模型写死为唯一实现；
- 向量检索启用后，稀疏和稠密召回分别取候选集，再使用 RRF 融合，不直接比较不可校准的原始分数；
- 索引记录 `embedding_model`、`embedding_version`、`embedding_dimension`、归一化方式和 `parser_version`，任一关键版本变化都能触发重建；
- 文件较小且用户要求整体摘要、翻译或改写时，使用受模型上下文预算保护的全文读取；
- 全文注入预算动态计算：模型上下文上限减去系统提示、历史消息、工具结果、预留输出和安全余量，不使用固定的 8K/16K 阈值；
- 针对参数、器件、条款等定位问题，即使文件较小也优先通过检索获得可回溯 Evidence；
- 普通问题由 `attachment_search` 统一完成关键词/语义召回和有限次补检索，读取必须携带 `attachment_id` 和当前会话权限；
- 视觉问题只对命中的页面或用户明确指定的页面调用 `AttachmentVisualAnalyzer`；无文本扫描件可根据 OCR 状态和页图候选进行有限页面采样；
- 工具结果包装为现有 `Evidence`；`Evidence` 没有顶层 `backend` 字段，`backend=local_attachment` 或 `backend=volcengine_ark` 写入 `Evidence.metadata`，并保留文件、页码、图像和模型信息；
- 解析出的正文和视觉结果只作为数据，不作为系统指令或工具指令。

## 9. Deep Agents 接入

### 9.1 Runtime

扩展现有 `ToolRuntime`：

```text
attachment_refs
source_scope
attachment_service
```

`ToolRuntime` 仍是每次 Agent 执行的请求级对象，用于保存附件范围、取消信号、工具诊断、证据和事件回调。附件服务不能从全局变量读取当前会话。

### 9.2 工具集合

新增薄适配器，不在 Agent 工具中重复底层解析：

```text
attachment_list
  -> 返回当前 turn 可用的文件清单和 manifest

attachment_search(query, attachment_ids?)
  -> AttachmentRetrievalService（第一版 FTS5，后续可选混合检索）并返回 Evidence

attachment_read(attachment_id, locator)
  -> 读取页、章节、段落或表格

attachment_visual_analyze(attachment_id, page_refs, question)
  -> 按页权限和数量限制调用 MultimodalModelGateway

attachment_table_query(operation, attachment_id, sheet?, filters?)
  -> 调用现有 spreadsheet 只读工具链（闭包绑定 SpreadsheetIndexService，附件作用域）

attachment_circuit_search(query, attachment_id)
  -> 调用现有 CircuitIndexService/CircuitQueryEngine
```

现有 `document_search` 继续只访问 RAGFlow 知识库；如果用户未授权联合检索，不能把附件内容混入 `document_search`。

### 9.3 意图和来源范围

Agent 的任务计划至少包含：

```json
{
  "action": "answer | summarize | extract | compare | calculate | validate | export",
  "source_scope": "attachment_only | knowledge_base_only | attachment_and_knowledge_base",
  "attachment_ids": ["att-01H..."],
  "operations": [],
  "output": {
    "mode": "chat | artifact",
    "format": "markdown | xlsx | docx | pptx | pdf"
  },
  "clarification_needed": []
}
```

优先级：

1. 系统安全和权限；
2. 用户明确指定的文件和数据范围；
3. 用户明确指定的操作和输出结构；
4. 默认检索和默认输出规则。

用户说“只分析这个 PDF”时只挂载附件工具；用户说“结合附件和知识库”时同时挂载两类工具；只上传但没有在本轮选择的附件不能自动参与回答。

### 9.4 证据和导出

本地附件证据与 RAGFlow 证据进入同一个 `ToolRuntime.evidence` 和 `ResultEnvelope`。导出规划器只读取结构化结果、答案和证据，不直接读取原始文件路径。

用户明确要求 PDF、Word、Excel 或 PPT 时，沿用现有对话式导出规则直接创建 Artifact 任务；用户没有要求文件时只返回聊天答案。附件分析和文件导出是两个阶段，不能因为文件已经上传就默认生成导出文件。

视觉分析调用不改变来源范围：`attachment_only` 只能分析已授权附件页面，`attachment_and_knowledge_base` 仍需分别调用附件工具和知识库工具。视觉模型返回的内容必须附带 `attachment_id`、页码、供应商和模型版本，才能进入导出结果。

### 9.5 多模态工具调用约束

`attachment_visual_analyze` 是后端受限工具，不允许模型传入任意 URL、文件路径或未挂载的附件。后端在调用前重新校验：

1. 页面属于当前会话且附件状态可读；
2. 页码不超过单轮上限，图像大小经过压缩和格式校验；
3. 用户问题和文本辅助上下文已与文件数据分隔；
4. 当前部署允许向火山方舟发送数据；
5. 超时、限流、供应商错误和视觉低置信结果均写入诊断，不伪造确定性证据。

## 10. API 与前端

### 10.1 附件 API

```http
POST   /api/v1/conversations/{session_id}/attachments
GET    /api/v1/conversations/{session_id}/attachments
GET    /api/v1/attachments/{attachment_id}
DELETE /api/v1/conversations/{session_id}/attachments/{attachment_id}
```

上传接口返回 `AttachmentView`：

```text
attachment_id
filename
media_type
size_bytes
role
status
manifest
error_message
created_at
expires_at
```

上传接口支持可选的 `client_request_id` 幂等：同一 `(session_id, client_request_id)` 重复上传返回同一 `attachment_id`。`sha256` 相同的重复文件允许存在多条附件记录，解析产物按 §5.2 幂等复用。

### 10.2 Turn API

`CreateTurnRequest` 增加：

```json
{
  "attachment_ids": ["att-01H..."],
  "source_scope": "attachment_only"
}
```

`TurnView`、`MessageView` 和会话附件查询返回本轮使用的附件摘要，避免前端只能依赖临时状态。

如果附件仍在解析，创建 turn 可以进入 `waiting_for_attachments`。这是 `chat_turns.status` 的新增枚举值，需同步 turn 状态机（`waiting_for_attachments -> pending -> ...`）、SSE 事件和 `TurnView`/`MessageView` 的状态透出，前端据此渲染等待提示。解析成功后由 Worker 自动转入 `pending`；如果附件解析失败，turn 进入可解释的失败或降级状态，不把原始文件路径暴露给前端。

### 10.3 前端入口

修改：

- `Composer.tsx`：按钮和隐藏 input 改为“添加附件”；
- `ChatPage.tsx`：从模板上传状态改为附件列表和附件状态；
- `useKbChat.ts`：维护选中附件并发送 `attachment_ids`；
- `frontend/src/api/types.ts`：增加附件和来源类型；
- 新增附件 API 和附件卡片组件。

普通聊天和知识库聊天都可以显示“添加附件”；文档生成权限只在用户选择“作为模板”时校验。附件卡片支持选择/取消选择、删除、重试解析和查看降级原因。

## 11. 权限、生命周期与安全

### 11.1 权限边界

- 上传附件只要求当前用户能访问会话；不因普通附件要求知识库写权限；
- 如果本轮同时查询知识库，仍按现有知识库读权限校验；
- 所有附件工具按 `user_id + session_id + attachment_id` 二次校验；
- 不能通过文件名、哈希、SQL 表名或路径推导其他附件；
- 附件默认只在所属会话可用；需要共享必须创建明确的共享授权。

### 11.2 文件安全

- 流式写盘并限制单文件、单会话和租户总容量；
- MIME、扩展名和文件签名同时校验；
- 检查 ZIP 炸弹、递归压缩和异常解压比例；
- DOCX/XLSX 的宏、外链和嵌入对象不执行；
- 解析器在 Worker 中运行，并受超时、内存和页数限制；现有 `parse_xlsx` 一次性载入整个 workbook 模型，需增加行数上限（`CHAT_ATTACHMENT_MAX_ROWS`），超限时返回可解释错误而不是静默截断；
- 不提供任意 Shell、任意 Python 或任意本地路径工具；
- 文件正文按不可信数据处理，不能覆盖系统提示或工具策略；
- 火山方舟调用只在显式配置和本轮视觉路由触发时发生；默认不上传完整原始附件，只发送选定页面图像和最小必要文本；
- `CHAT_ATTACHMENT_VISUAL_API_KEY`（默认复用 `MEMORY_EMBEDDING_API_KEY`）只保存在服务端，前端不接触供应商密钥；记录供应商请求 ID、模型 ID 和用量，不记录完整图像或正文；
- 使用火山方舟意味着选定页面内容离开本地网络边界，管理员必须在部署文档中明确数据驻留、供应商保留策略和合规要求；完全内网部署关闭 `CHAT_ATTACHMENT_VISUAL_ENABLED`，或改用已批准的本地视觉模型；
- 日志记录文件 ID、哈希、状态和统计信息，不记录完整正文。

### 11.3 删除与保留

删除会话时：

1. 标记附件和任务不可用；
2. 停止或取消仍未开始的解析任务；
3. 删除附件关联、parts、FTS 索引和本地原文件；
4. 删除或标记由该会话产生的导出任务和 Artifact。现状 `delete_session` 不清理 `result_snapshots`/`result_export_jobs`/`result_artifacts` 及磁盘文件（conversational-export 设计的同项承诺也未实现），本设计阶段 1 提供统一的会话删除清理钩子补齐该项，conversational-export 复用同一钩子，不各自实现；
5. 删除 Agent checkpoint 中的会话引用；
6. 失败清理写入 §6.5 的 `chat_attachment_deletion_outbox`，由 Worker 重试，不阻塞会话删除响应。

附件过期只影响附件内容访问；历史消息可以保留文件名、状态和“文件已过期”提示，不保留可下载的原文件引用。

## 12. 错误与降级

| 场景 | 状态/行为 |
|---|---|
| 文件类型不支持 | 上传拒绝，返回允许类型 |
| 文件超过大小/页数限制 | 上传拒绝 |
| PDF 无可提取文本 | `degraded`，提示需要 OCR 或图片分析 |
| DOCX 结构异常 | `failed`，保留错误码和重试入口 |
| FTS 无命中 | Agent 可以补检索；仍无证据则明确未找到 |
| 附件权限失效 | 工具返回拒绝，不泄露文件存在性 |
| 解析任务 Worker 崩溃 | lease 过期后重试 |
| LLM 不可用 | 不影响原文件和解析状态；turn 按现有失败策略处理 |
| 本地索引损坏 | 可根据原文件和 parser version 重建 |

Agent 继续遵守当前 fail-open 规则：工具异常返回空结果并发出 `degraded` 事件；但上传安全校验失败、越权和文件损坏必须在 API 层明确拒绝。

## 13. 兼容与迁移

1. 保留 `/document-generation/templates/analyze` 及现有 `DocumentContext`，旧模板工作台不迁移。
2. 保留 `/kbs/{kb_name}/files` 和知识库 RAGFlow 上传逻辑，不把历史 `pipeline_documents` 迁移为附件。
3. 新增数据库表采用 additive migration，不删除现有列。
4. `document_context` 和 `attachment_ids` 可以同时存在；模板流程优先使用服务端重授权后的 `DocumentContext`。
5. 前端文案和组件从模板改名为附件，但模板功能仍可以通过附件角色触发。
6. 增加 `CHAT_ATTACHMENTS_ENABLED` 开关，默认关闭新入口，完成单租户验证后再逐步开启。
7. 用户显式选择“归档到知识库”时，重新调用现有 KB 上传/解析流程，生成新的知识库记录；不把会话附件记录直接伪装成知识库文件。

## 14. 代码修改范围

### 14.1 新增

```text
src/api/routes/attachments.py
src/attachments/models.py
src/attachments/store.py
src/attachments/service.py
src/attachments/jobs.py
src/attachments/local_document_parser.py
src/attachments/local_attachment_index.py
src/agents/tools/attachment_tools.py
frontend/src/api/attachments.ts
frontend/src/pages/chat/components/AttachmentChip.tsx
frontend/src/pages/chat/components/AttachmentList.tsx
tests/test_chat_attachments.py
tests/test_local_attachment_parser.py
tests/test_attachment_agent_tools.py
```

实际实现应优先复用现有 `src/ingestion/parser_registry.py`、`src/pipelines/registry.py`、`src/pipelines/spreadsheet/` 和 `src/circuit/`，不创建平行的 Excel/EDF 解析目录。

### 14.2 修改

```text
src/api/schemas.py
src/api/routes/query.py
src/api/routes/conversations.py
src/core/conversation.py
src/agents/runner.py
src/agents/tools/runtime.py
src/workers/*
src/settings.py
frontend/src/api/types.ts
frontend/src/pages/chat/components/Composer.tsx
frontend/src/pages/chat/ChatPage.tsx
frontend/src/pages/chat/useKbChat.ts
docs/architecture_doc.md
```

### 14.3 明确不修改行为

```text
src/api/routes/upload.py       # 知识库上传入口
src/pipelines/document_rag/    # 知识库 RAGFlow 路径
src/pipelines/spreadsheet/     # 只扩展作用域适配
src/circuit/                   # 只扩展附件引用适配
```

## 15. 分阶段实施计划

### 阶段 0：契约和开关

- 定义 `SourceRef`、`AttachmentView`、附件状态和 `attachment_ids` API 字段；
- 增加数据库迁移和 feature flag；
- 不改变知识库上传和 RAGFlow 行为；
- 添加跨作用域访问拒绝测试。

### 阶段 1：附件基础设施

- 上传、列表、删除和状态 API（含 `client_request_id` 幂等）；
- 会话私有存储；
- `chat_attachments`、`turn_attachments`、任务表和删除 outbox；
- Worker claim/lease/重试；
- 前端附件卡片；
- 会话删除清理，含统一的 result_exports 清理钩子和 `chat_attachment_deletion_outbox` 消费。

### 阶段 2：本地 PDF/DOCX

- PDF 按页解析；
- DOCX 按标题、段落和表格解析；
- canonical parts 和 FTS5（含运行环境 FTS5 可用性探测，探测失败时检索降级关闭）；
- 建立 `AttachmentRetrievalService`、`SparseRetriever`、`DenseRetriever`、`FusionRanker` 和 `ContextPacker` 接口；第一版启用 FTS5，全文注入使用动态上下文预算；
- `attachment_search`、`attachment_read`；
- 证据定位、降级状态和 Agent 事件。

### 阶段 3：Deep Agents 和意图路由

- `ToolRuntime` 注入附件作用域；
- Agent 识别 attachment-only、KB-only 和联合查询；
- 附件内容不作为系统指令；
- 附件证据进入统一 ResultEnvelope；
- 支持附件分析后的结构化导出（依赖对话式导出 Phase 1 的 Snapshot/ExportJob 通路；`ExportDocumentPlanner` 属对话式导出 Phase 2，未落地前本阶段不承诺结构化文档规划）。

### 阶段 4：Excel/EDF 作用域复用

- 将现有表格服务绑定到 `SourceRef`；
- 将现有电路服务绑定到 `attachment_id -> design_id`；
- 校验同一文件在 KB 和附件作用域的解析结果一致；
- 禁止附件索引进入知识库检索。

### 阶段 5：高级能力

- OCR 和 PDF 页面渲染；
- 图片/复杂图表分析：通过 `MultimodalModelGateway` 接入火山方舟 `Doubao-Seed-2.0-lite`，只发送筛选后的页面图像；
- 本地向量检索的独立评估，评估通过后再开启 DenseRetriever 和 RRF；
- 多附件比较；
- 显式归档到知识库。

## 16. 测试和验收

### 16.1 API 和权限

1. 普通用户可以上传当前会话附件，不需要 KB 写权限。
2. 用户不能读取其他用户或其他会话的附件。
3. `attachment_ids` 不属于当前会话时 turn 创建失败。
4. 知识库 `document_search` 不会返回会话附件。
5. 附件工具不会读取知识库中未授权的文件。
6. 会话删除后，附件、解析结果和下载引用均不可访问，关联导出任务和 Artifact 一并清理。
7. 同一 `client_request_id` 重复上传返回同一附件记录，不产生重复解析任务。

### 16.2 解析和索引

1. PDF 的页码和文本块顺序稳定。
2. DOCX 的标题、段落、表格和链接可回溯。
3. 无文本 PDF 正确进入 `degraded`，不生成虚假证据。
4. 相同内容、相同作用域和相同 parser version 不重复解析。
5. 解析失败后可以根据原文件重建索引。
6. 运行环境缺少 FTS5 时，上传和解析仍可用，`attachment_search` 返回明确的降级说明。
7. 小文件全文注入不会超过动态上下文预算，且仍保留 Evidence 定位。
8. 启用 DenseRetriever 时，embedding 模型或 parser 版本变化会触发可观测的重建任务。

### 16.3 复用和一致性

1. 同一 Excel 在知识库和附件作用域的解析字段、行数、类型和值一致。
2. 同一 EDF/EDIF 的器件、网络、模块和连接关系一致。
3. 作用域变化只影响权限、存储和索引，不改变领域解析结果。
4. 普通 `.docx` 不自动触发模板分析。
5. 只有明确选择模板角色后才生成 `DocumentContext`。

### 16.4 Agent 和后台

1. “只分析附件”不调用 RAGFlow。
2. “只查知识库”不读取附件。
3. “结合附件和知识库”可以同时获得两类 Evidence，并保留 backend/source locator。
4. 切换页面不会中断解析或 turn。
5. Worker 崩溃后任务可由 lease 过期机制恢复。
6. 用户要求 PDF/Word/Excel 等格式时，结果进入现有 Artifact/ExportJob，而不是导出原始对话 transcript。

### 16.5 多模态和供应商边界

1. 未启用视觉开关或完全内网配置时，不产生火山方舟请求。
2. 视觉请求只包含当前会话已授权且被选中的页面，不包含原始本地路径。
3. `Doubao-Seed-2.0-lite` 能够返回页面级视觉结论，并保留 `attachment_id`、页码、模型 ID 和供应商请求 ID。
4. 火山方舟超时、限流或不可用时，系统返回文本/OCR 结果或可解释的 `degraded` 状态。
5. 视觉结果不能绕过知识库和附件的来源权限，也不能注入系统提示或工具策略。

## 17. 观测与配置

新增指标：

```text
hdb.attachment.upload
hdb.attachment.parse
hdb.attachment.parse_duration
hdb.attachment.parse_bytes
hdb.attachment.search
hdb.attachment.read
hdb.attachment.visual
hdb.attachment.visual_duration
hdb.attachment.visual_failure
hdb.attachment.degraded
hdb.attachment.cleanup
```

所有指标只记录 attachment ID 的哈希或不可逆短标识、类型、大小区间、状态和耗时，不记录正文。指标实现遵循现有约定：在 `src/observability/metrics.py` 集中封装 `hdb.attachment.*`，不在附件模块内直接调用 OTel。

新增配置：

```text
CHAT_ATTACHMENTS_ENABLED
CHAT_ATTACHMENT_STORAGE_DIR
CHAT_ATTACHMENT_INDEX_DB_PATH
CHAT_ATTACHMENT_MAX_BYTES
CHAT_ATTACHMENT_MAX_FILES_PER_SESSION
CHAT_ATTACHMENT_MAX_PAGES
CHAT_ATTACHMENT_MAX_ROWS
CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS
CHAT_ATTACHMENT_RETENTION_SECONDS
CHAT_ATTACHMENT_OCR_ENABLED
CHAT_ATTACHMENT_RETRIEVAL_MODE=fts5|hybrid
CHAT_ATTACHMENT_DENSE_ENABLED
CHAT_ATTACHMENT_EMBEDDING_PROVIDER=ollama
CHAT_ATTACHMENT_EMBEDDING_MODEL
CHAT_ATTACHMENT_EMBEDDING_VERSION
CHAT_ATTACHMENT_CONTEXT_MAX_TOKENS
CHAT_ATTACHMENT_VISUAL_ENABLED
CHAT_ATTACHMENT_VISUAL_PROVIDER=volcengine_ark
CHAT_ATTACHMENT_VISUAL_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3
CHAT_ATTACHMENT_VISUAL_MODEL
CHAT_ATTACHMENT_VISUAL_API_KEY
CHAT_ATTACHMENT_VISUAL_MAX_PAGES_PER_TURN
CHAT_ATTACHMENT_VISUAL_MAX_IMAGE_BYTES
CHAT_ATTACHMENT_VISUAL_TIMEOUT_SECONDS
CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED
```

模型配置说明：`CHAT_ATTACHMENT_VISUAL_BASE_URL` 默认取当前 `.env` 中的 `MEMORY_EMBEDDING_BASE_URL`，其值为 `https://ark.cn-beijing.volces.com/api/plan/v3`；`CHAT_ATTACHMENT_VISUAL_API_KEY` 默认取 `MEMORY_EMBEDDING_API_KEY`，不在代码、文档、前端或日志中写入实际值。`CHAT_ATTACHMENT_VISUAL_MODEL` 保存火山方舟实际 Endpoint ID；当前官方示例使用过 `doubao-seed-2-0-lite-260215`，也可能存在更新的日期版本，不能把示例 ID 当作永久别名。推荐在隐私审批前将 `CHAT_ATTACHMENT_VISUAL_ENABLED=false`、`CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED=false`；审批通过并完成 Endpoint 配置后再开启。文件不进入 RAGFlow 不代表解析片段不会发送给远程 LLM；完全内网处理应使用本地视觉模型或脱敏策略。

## 18. 回滚策略

1. 关闭 `CHAT_ATTACHMENTS_ENABLED` 即隐藏新入口，已有知识库功能不受影响。
2. 附件 Worker 停止后，未完成任务保持可恢复状态，不删除原文件。
3. 本地附件 API 出现问题时，现有 KB API 和文档模板工作台继续运行。
4. 数据库迁移只新增表和索引，不回写或删除历史会话数据。
5. 若本地解析质量不满足要求，可以保留上传和下载，关闭 `attachment_search`，不影响 RAGFlow 知识库检索。
6. 若火山方舟视觉通道不可用，关闭 `CHAT_ATTACHMENT_VISUAL_ENABLED` 或 `CHAT_ATTACHMENT_REMOTE_INFERENCE_ALLOWED`，回退到文本/OCR/`degraded`，不影响本地附件和知识库功能。

## 19. 最终验收结论

本设计完成后，系统应满足以下边界：

```text
KB 文档 = RAGFlow 的知识库资产
会话附件 = 本地、私有、可过期的临时资产
Excel/EDF = 复用现有领域解析器和查询引擎
Deep Agents = 意图理解和工具编排层
Doubao-Seed-2.0-lite = 按需页面视觉分析适配器，不是本地解析器或向量模型
导出 = 结构化结果到 Artifact 的独立交付层
```

实现顺序应先完成附件契约、权限和生命周期，再接入本地 PDF/DOCX，最后扩展 Excel/EDF 的作用域适配。这样不会在当前知识库和导出功能尚未稳定时引入不可回滚的 RAGFlow 或解析行为变化。

## 20. 供应商参考

- [火山方舟 Doubao-Seed-2.0-lite 基础使用](https://www.volcengine.com/docs/82379/1795150)：模型调用、图片理解、视频理解和文档理解入口。
- [火山方舟 Responses API 工具调用](https://www.volcengine.com/docs/82379/1958524?lang=zh)：函数调用和服务端工具编排方式。
- [火山引擎模型产品页](https://www.volcengine.com/product/doubao/)：推理模型与独立 embedding 模型的能力边界。
