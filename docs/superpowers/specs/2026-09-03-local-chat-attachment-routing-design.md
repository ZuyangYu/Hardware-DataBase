# 本地会话附件与知识库 RAGFlow 双通道设计

- 日期：2026-09-03
- 状态：Proposed，等待用户审阅
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
```

两条链路共享 `Evidence`、内容定位、解析状态、观测和 Agent 工具协议，但不共享知识库权限、物理存储、生命周期或默认检索范围。

明确不采用以下做法：

- 不把会话附件自动上传到 RAGFlow；
- 不复制一套 Excel 或 EDF 解析器；
- 不让模型直接访问本地文件路径；
- 不让模型自由执行用户文件中的脚本；
- 不把供应商原生文件输入能力作为系统的唯一实现；
- 不让附件内容在未被用户选择时自动参与后续所有对话。

## 2. 当前基线与问题

当前聊天上传入口是文档模板专用能力：

- `frontend/src/pages/chat/components/Composer.tsx` 只在文档创作开关开启时显示上传入口，并接受 `.xlsx/.xlsm/.docx`；
- `frontend/src/pages/chat/ChatPage.tsx` 的 `handleTemplateUpload` 调用模板分析 API；
- `/document-generation/templates/analyze` 要求知识库写权限，并把文件交给文档创作服务；
- `CreateTurnRequest` 只有 `document_context`，没有通用附件引用；
- `ToolRuntime` 只有知识库和文档模板上下文；
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

## 6. 数据模型与存储

### 6.1 `chat_attachments`

会话附件元数据放在当前会话数据库中，以便和会话权限、删除关系保持一致：

```text
attachment_id TEXT PRIMARY KEY
session_id INTEGER NOT NULL
user_id INTEGER NOT NULL
tenant_id TEXT NOT NULL
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

### 6.3 `chat_attachment_jobs`

```text
job_id TEXT PRIMARY KEY
attachment_id TEXT NOT NULL
job_type TEXT NOT NULL
status TEXT NOT NULL
attempt INTEGER NOT NULL DEFAULT 0
lease_owner TEXT NOT NULL DEFAULT ''
lease_expires_at TEXT
parser_version TEXT NOT NULL
error_code TEXT NOT NULL DEFAULT ''
error_message TEXT NOT NULL DEFAULT ''
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
completed_at TEXT
```

任务使用现有 Worker 的 claim/lease/heartbeat 模式，页面断开不改变任务状态。

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

## 7. 来源路由和解析器复用

将文件路由从单纯的扩展名路由扩展为“来源类型 + 扩展名”路由：

```python
route_file(filename, source_type="knowledge_base")
route_file(filename, source_type="chat_attachment")
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

现有 `spreadsheet_row_search`、`spreadsheet_cell_lookup` 和只读 SQL 能力应增加可选的 `source_refs`，或由工具工厂闭包绑定本轮附件来源。工具必须在 SQL 前注入附件记录范围，禁止模型通过表名或 SQL 自行越权。

### 7.4 EDF/EDIF 复用规则

会话附件不新增 EDF 解析器。现有 `EdfParser`、`CircuitIndexService` 和 `CircuitQueryEngine` 继续负责解析和查询；新增一个附件作用域的设计引用适配器：

```text
attachment_id -> design_id -> CircuitIndexService.query
```

查询证据增加 `attachment_id`、文件名、实例、网络或模块定位。知识库和附件使用不同的存储命名空间，避免同名 `design_id` 冲突。

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
4. 生成带顺序和定位信息的 canonical parts；
5. 写入 FTS5 索引；
6. 生成 manifest 和可用性摘要；
7. 更新 `ready`、`degraded` 或 `failed` 状态。

第一版 PDF 适配器使用可部署的本地文本解析库，按页生成内容；DOCX 复用项目已有 `python-docx`/OOXML 依赖，提取标题、段落、表格和链接。PDF 扫描件和复杂版式在 OCR/渲染能力启用前标记为 `degraded`，不能假装已完整读取。

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

### 8.3 检索策略

- 文件较小且用户要求整体摘要时，使用受上限保护的章节读取；
- 普通问题使用 FTS5 检索相关 parts；
- Agent 可以改写查询并进行有限次补检索；
- 每次读取必须携带 `attachment_id` 和当前会话权限；
- 工具结果包装为现有 `Evidence`，并写入 `backend=local_attachment`；
- 解析出的正文只作为数据，不作为系统指令或工具指令。

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
  -> 本地 PDF/DOCX FTS 检索并返回 Evidence

attachment_read(attachment_id, locator)
  -> 读取页、章节、段落或表格

attachment_table_query(operation, attachment_id, sheet?, filters?)
  -> 调用现有 SpreadsheetIndexService 的只读查询

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

### 10.2 Turn API

`CreateTurnRequest` 增加：

```json
{
  "attachment_ids": ["att-01H..."],
  "source_scope": "attachment_only"
}
```

`TurnView`、`MessageView` 和会话附件查询返回本轮使用的附件摘要，避免前端只能依赖临时状态。

如果附件仍在解析，创建 turn 可以进入 `waiting_for_attachments`；解析成功后由 Worker 自动转入 `pending`。如果附件解析失败，turn 进入可解释的失败或降级状态，不把原始文件路径暴露给前端。

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
- 解析器在 Worker 中运行，并受超时、内存和页数限制；
- 不提供任意 Shell、任意 Python 或任意本地路径工具；
- 文件正文按不可信数据处理，不能覆盖系统提示或工具策略；
- 日志记录文件 ID、哈希、状态和统计信息，不记录完整正文。

### 11.3 删除与保留

删除会话时：

1. 标记附件和任务不可用；
2. 停止或取消仍未开始的解析任务；
3. 删除附件关联、parts、FTS 索引和本地原文件；
4. 删除或标记由该会话产生的导出任务和 Artifact；
5. 删除 Agent checkpoint 中的会话引用；
6. 失败清理写入可重试的删除 outbox，不阻塞会话删除响应。

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
frontend/src/pages/chat/Composer.tsx
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

- 上传、列表、删除和状态 API；
- 会话私有存储；
- `chat_attachments`、`turn_attachments` 和任务表；
- Worker claim/lease/重试；
- 前端附件卡片；
- 会话删除清理。

### 阶段 2：本地 PDF/DOCX

- PDF 按页解析；
- DOCX 按标题、段落和表格解析；
- canonical parts 和 FTS5；
- `attachment_search`、`attachment_read`；
- 证据定位、降级状态和 Agent 事件。

### 阶段 3：Deep Agents 和意图路由

- `ToolRuntime` 注入附件作用域；
- Agent 识别 attachment-only、KB-only 和联合查询；
- 附件内容不作为系统指令；
- 附件证据进入统一 ResultEnvelope；
- 支持附件分析后的结构化导出。

### 阶段 4：Excel/EDF 作用域复用

- 将现有表格服务绑定到 `SourceRef`；
- 将现有电路服务绑定到 `attachment_id -> design_id`；
- 校验同一文件在 KB 和附件作用域的解析结果一致；
- 禁止附件索引进入知识库检索。

### 阶段 5：高级能力

- OCR 和 PDF 页面渲染；
- 图片/复杂图表分析；
- 本地向量检索的独立评估；
- 多附件比较；
- 显式归档到知识库。

## 16. 测试和验收

### 16.1 API 和权限

1. 普通用户可以上传当前会话附件，不需要 KB 写权限。
2. 用户不能读取其他用户或其他会话的附件。
3. `attachment_ids` 不属于当前会话时 turn 创建失败。
4. 知识库 `document_search` 不会返回会话附件。
5. 附件工具不会读取知识库中未授权的文件。
6. 会话删除后，附件、解析结果和下载引用均不可访问。

### 16.2 解析和索引

1. PDF 的页码和文本块顺序稳定。
2. DOCX 的标题、段落、表格和链接可回溯。
3. 无文本 PDF 正确进入 `degraded`，不生成虚假证据。
4. 相同内容、相同作用域和相同 parser version 不重复解析。
5. 解析失败后可以根据原文件重建索引。

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

## 17. 观测与配置

新增指标：

```text
hdb.attachment.upload
hdb.attachment.parse
hdb.attachment.parse_duration
hdb.attachment.parse_bytes
hdb.attachment.search
hdb.attachment.read
hdb.attachment.degraded
hdb.attachment.cleanup
```

所有指标只记录 attachment ID 的哈希或不可逆短标识、类型、大小区间、状态和耗时，不记录正文。

新增配置：

```text
CHAT_ATTACHMENTS_ENABLED
CHAT_ATTACHMENT_STORAGE_DIR
CHAT_ATTACHMENT_INDEX_DB_PATH
CHAT_ATTACHMENT_MAX_BYTES
CHAT_ATTACHMENT_MAX_FILES_PER_SESSION
CHAT_ATTACHMENT_MAX_PAGES
CHAT_ATTACHMENT_PARSE_TIMEOUT_SECONDS
CHAT_ATTACHMENT_RETENTION_SECONDS
CHAT_ATTACHMENT_OCR_ENABLED
```

本地模型和远程模型的隐私边界需要在配置说明中明确：文件不进入 RAGFlow 不代表解析片段不会发送给远程 LLM；完全内网处理应使用本地模型或脱敏策略。

## 18. 回滚策略

1. 关闭 `CHAT_ATTACHMENTS_ENABLED` 即隐藏新入口，已有知识库功能不受影响。
2. 附件 Worker 停止后，未完成任务保持可恢复状态，不删除原文件。
3. 本地附件 API 出现问题时，现有 KB API 和文档模板工作台继续运行。
4. 数据库迁移只新增表和索引，不回写或删除历史会话数据。
5. 若本地解析质量不满足要求，可以保留上传和下载，关闭 `attachment_search`，不影响 RAGFlow 知识库检索。

## 19. 最终验收结论

本设计完成后，系统应满足以下边界：

```text
KB 文档 = RAGFlow 的知识库资产
会话附件 = 本地、私有、可过期的临时资产
Excel/EDF = 复用现有领域解析器和查询引擎
Deep Agents = 意图理解和工具编排层
导出 = 结构化结果到 Artifact 的独立交付层
```

实现顺序应先完成附件契约、权限和生命周期，再接入本地 PDF/DOCX，最后扩展 Excel/EDF 的作用域适配。这样不会在当前知识库和导出功能尚未稳定时引入不可回滚的 RAGFlow 或解析行为变化。
