# 通用 Agent Artifact 导出与后台任务设计

- 日期：2026-09-02
- 状态：待用户评审
- 范围：知识库问答、结构化检索、普通对话和现有文档生成流程的统一结果导出

## 1. 背景与问题

当前系统的聊天回答主要以 Markdown 文本和证据列表返回，文档生成则有独立的模板、工单和产物流程。两条链路都能产生可交付内容，但没有统一的结果产物模型，因此普通知识库检索无法稳定地产出 Excel、Word、PPT 或 PDF，也无法在页面切换后统一恢复导出任务。

本设计借鉴 Codex/Claude Code 的产品级模式：Agent 运行、工具执行和文件 Artifact 分离；浏览器的流式连接只负责观察，不拥有任务本身。Codex 官方文档描述了隔离环境、后台运行、并行任务和结果审查；文件工作流将文档、表格、演示文稿和 PDF 作为独立文件产物。Claude Code 官方文档也将后台任务、输出文件、并行会话和 worktree 隔离分开管理。完整内部实现未公开，本设计只借鉴可验证的抽象行为，不复制其代码执行权限模型。

## 2. 目标

1. 用户可以在对话中直接要求输出 Markdown、Excel、Word、PPT 或 PDF。
2. 用户可以在已完成的回答上通过导出菜单生成文件，不需要重新检索。
3. 知识库检索、表格 SQL 查询、普通问答和文档生成都能使用统一的 Artifact 卡片和下载接口。
4. 导出任务与浏览器连接解耦。切换会话、知识库、页面或刷新浏览器不会取消后台任务。
5. 同一次结果可以生成多个格式，每个格式独立显示状态、失败和重试。
6. 导出内容保留知识库证据和引用关系，不能因格式转换产生虚构数据。
7. 任务和产物遵循当前用户、租户、部门和知识库权限；下载时重新授权。
8. 现有模板化文档生成继续工作，并可以逐步复用通用 Artifact 存储和下载能力。

## 3. 非目标

1. 本期不允许 Agent 通过任意 Shell、任意路径或任意脚本生成文件。
2. 本期不替换现有文档模板分析、审核、工单和人工确认流程。
3. 本期不把所有自然语言都强行转换为结构化表格；没有可靠列结构时必须保留为报告或明确标记缺失值。
4. 本期不保证多个 Agent 写入同一个项目工作区时自动合并；并行写操作仍需资源锁或独立工作区。
5. 本期不把 PDF 当作源数据格式。PDF 是最终排版产物。

## 4. 设计原则

### 4.1 AgentRun 与浏览器订阅分离

`chat_turns` 是一次 AgentRun 的持久化控制面。SSE、轮询或页面状态只是订阅者。订阅断开只停止前端接收，不调用取消接口；只有用户明确点击“停止”才请求取消。旧任务的事件和异步清理不能覆盖新会话状态。

### 4.2 结果快照优先于文本再解析

导出使用不可变的 `ResultSnapshot`，而不是再次读取页面 DOM 或重新运行 Agent。快照同时保存最终回答、结构化结果、证据和来源定位；不同格式渲染器从同一快照读取数据。

### 4.3 格式与内容形态分离

`xlsx`、`docx`、`pptx`、`pdf` 是文件格式；`narrative`、`table`、`report`、`slides`、`source_catalog` 是内容形态。用户要求 Excel 不代表回答一定能转换成高质量表格，Agent 必须明确选择或确认内容形态。

### 4.4 模型只决定语义，后端决定文件

模型通过受控 schema 或工具声明输出意图和内容结构，不生成二进制、不提供文件路径、不执行任意转换命令。文件写入、MIME、扩展名、大小限制和内容校验均由后端控制。

## 5. 领域对象

### 5.1 AgentRun

第一阶段继续使用现有聊天 `ChatTurn` 作为 AgentRun 的持久化实现，包含：

- `run_id/turn_id`、`session_id`、用户和权限范围；
- `pending/streaming/completed/cancelled/failed` 状态；
- 检索证据、工具诊断、最终回答和错误；
- worker lease、heartbeat、事件序号和可恢复信息。

### 5.2 ResultSnapshot

建议新增 `result_snapshots` 控制面表，并在结果较大时把内容放入受保护的存储文件：

```text
snapshot_id
tenant_id / user_id / department_id
knowledge_base_name / knowledge_base_id
session_id / turn_id / assistant_message_id
schema_version
answer_markdown
result_payload_json 或 payload_storage_ref
evidence_json
source_hash
created_at
```

`result_payload` 使用版本化的 Result Envelope：

```text
ResultEnvelope
├── blocks: heading / paragraph / list / code / table / image
├── tables: columns / rows / value_types
├── assets: image/chart 引用
├── citations: evidence_id / locator / citation_number
└── metadata: query / language / source snapshot
```

表格单元格需要保留 `text/number/date/boolean/url` 等类型；行或 block 可以绑定一个或多个 citation。快照创建后不可修改，修订需要创建新的快照或新的导出任务。

### 5.3 ExportJob

建议新增 `export_jobs` 表：

```text
export_job_id
snapshot_id
tenant_id / user_id / department_id / knowledge_base_name
session_id / turn_id / assistant_message_id
format
content_shape
render_options_json
status: queued/running/completed/failed/cancelled/dead_letter
attempt / max_attempts
lease_owner / lease_token / lease_expires_at
idempotency_key
error_code / error_message / retryable
created_at / updated_at / completed_at
```

任务状态和结果必须在数据库中可恢复。多个格式的任务相互独立；一个 PDF 失败不能使已经完成的 Markdown 或 Excel 失效。

### 5.4 Artifact

建议新增 `artifacts` 表，二进制存储在 `ARTIFACT_STORAGE_ROOT` 或后续对象存储中：

```text
artifact_id
export_job_id / snapshot_id
tenant_id / user_id / knowledge_base_name
format / filename / media_type
storage_ref
size_bytes / sha256
preview_ref
created_at / expires_at
```

客户端永远不能直接获得本地路径。下载必须经过鉴权 API，并使用安全的 `Content-Disposition` 文件名。

## 6. 导出意图协议

### 6.1 ExportPlan

Agent 或前端提交统一的计划对象：

```json
{
  "formats": ["xlsx", "pdf"],
  "source_ref": {"type": "turn", "id": "turn-123"},
  "content_shape": "table",
  "title": "接口查询结果",
  "language": "zh-CN",
  "include_citations": true,
  "options": {}
}
```

约束：

- 格式只允许 `md/markdown/xlsx/docx/pptx/pdf`，服务端统一别名；
- 一次最多 5 种格式；
- `source_ref` 只能引用当前用户可见的 turn 或 snapshot；
- 不接受路径、脚本、模板 URL 或任意 MIME；
- `options` 按格式分别校验，未知字段拒绝；
- `idempotency_key` 由客户端提供或由 `turn_id + format + options_hash` 派生。

### 6.2 Agent 侧工具

新增的 Agent 工具建议命名为 `declare_export_request`，它只登记用户明确要求的导出计划，不立即写文件：

```text
declare_export_request(
  formats,
  content_shape,
  title,
  include_citations,
  options
) -> export_request_id
```

Agent 完成检索和回答后，服务端在同一事务中生成 ResultSnapshot，并将声明的计划转换为 ExportJob。这样即使模型提前声明导出，也不会把不完整的中间回答提交给渲染器。

自然语言格式识别优先使用结构化输出或带 schema 的 tool calling；服务端仍需做别名、数量、权限和范围校验。模型不支持结构化输出时，直接工具调用是兼容路径；无法可靠识别时不自动创建导出任务。

### 6.3 已完成回答上的导出

前端导出菜单直接调用 `POST /exports`，引用已完成的 `turn_id` 或 `snapshot_id`，不再次调用 Agent。它与 Agent 声明路径使用相同的 ExportPlan、幂等键、任务状态和 Artifact 卡片。

## 7. 数据流与状态

```text
用户消息
  ↓
持久化 ChatTurn / AgentRun
  ↓
Agent 检索与工具调用
  ↓
最终回答 + 结构化结果 + 证据
  ↓ 同一事务
ResultSnapshot + ExportJob + outbox event
  ↓
ExportWorker claim/lease
  ↓
RendererRegistry 生成临时文件
  ↓ 校验通过后原子发布
Artifact completed
  ↓
artifact_ready 事件 / 任务中心轮询
  ↓
预览或下载
```

AgentRun、ExportJob 和 Artifact 的生命周期独立：

- AgentRun 完成后可以没有导出任务；
- 一个 AgentRun 可以有多个 ExportJob；
- ExportJob 完成后可以有一个主要 Artifact 和一个可选预览；
- 页面断开只删除订阅，不改变上述状态；
- Worker 崩溃时，lease 过期的 queued/running 任务可以被重新领取。

## 8. API

新增通用接口：

```http
POST /api/v1/exports
GET  /api/v1/exports?session_id=&status=
GET  /api/v1/exports/{export_job_id}
POST /api/v1/exports/{export_job_id}/retry
POST /api/v1/exports/{export_job_id}/cancel
GET  /api/v1/artifacts/{artifact_id}/preview
GET  /api/v1/artifacts/{artifact_id}/download
```

`POST /exports` 返回 `202 Accepted` 和任务状态；如果幂等键已经存在，返回现有任务而不是重复生成。列表接口必须按用户、租户、会话和知识库范围过滤。Artifact 下载在读取文件前再次检查当前权限；权限失效时返回 403/404，不泄露 Artifact 是否存在。

现有 `/document-generation/artifacts/*` 接口保持兼容。模板文档产物可以在后续迁移到通用 Artifact 表，但迁移前不得改变旧客户端的字段和状态语义。

## 9. RendererRegistry

渲染器以格式注册，接口类似：

```text
render(snapshot, plan) -> RenderedArtifact(bytes, filename, media_type, preview)
```

初始实现：

- Markdown：保存规范化 Markdown，可附带标题和引用来源；
- XLSX：使用真实结构化 rows，增加“结果”“来源引用”“元数据”工作表，保留数值和日期类型；
- DOCX：标题、章节、列表、表格和引用来源附录；
- PPTX：标题页、结论页、表格/图表页和来源页，限制每页内容量；
- PDF：优先由受控 HTML/CSS 生成；需要保持 Office 模板版式时，使用隔离的 LibreOffice headless 转换。

所有渲染器必须：

1. 写入隔离临时目录；
2. 限制行数、页数、章节数、图片大小和总输出大小；
3. 生成后校验扩展名、MIME、文件签名和可解析性；
4. 校验通过后使用原子 rename 或对象存储提交；
5. 不允许访问用户提供的任意本地路径或外网资源。

## 10. 检索结果的正确性规则

1. 结构化表格/SQL 查询优先导出真实查询行，不从最终回答反向猜测数据。
2. 文档检索导出必须包含文件名、页码/段落/表格定位等来源信息。
3. LLM 派生字段必须标识为派生字段，并保留其来源 citation。
4. 证据不足的字段写为“未找到”“待确认”或空值，禁止补造。
5. 用户要求“来源清单”时，`source_catalog` 只导出可见证据，不扩大检索范围。
6. 用户要求报告时，回答正文和证据附录都进入快照，避免 PDF/Word 只保留摘要而丢失审计线索。

## 11. Worker 与并行

新增独立的 `hardware-database-export-worker`，不要让文件渲染阻塞聊天和解析队列。它使用数据库 claim、lease、heartbeat 和重试机制，支持多个进程部署。

后台继续和真正并行需要分别验收：

- 后台继续：页面离开后，原 AgentRun/ExportJob 仍完成；
- 并行执行：多个 worker 可以同时领取不同任务；
- 同一资源写入：必须按 project/knowledge_base/template 维度加锁；
- 只读知识库检索：允许并行，但仍受用户、模型和后端限流约束。

当前已有的聊天持久化 worker 继续负责 ChatTurn。第一阶段只要求导出 worker 独立运行；真正增加 Agent worker 数量作为后续部署能力，不在导出功能中隐式改变现有模型并发配置。

## 12. 前端体验

1. 完成的助手消息显示“导出”菜单：Markdown、Excel、Word、PPT、PDF。
2. 用户在对话中明确要求格式时，回答下方自动出现 Artifact 卡片。
3. 卡片展示排队中、生成中、已完成、失败、已取消和权限失效状态。
4. 已完成产物展示“预览”和“下载”；下载沿用认证 blob 流程，不依赖未授权的裸 URL。
5. 应用 Shell 增加全局任务中心，跨聊天页、知识库页和设置页显示后台任务。
6. 进入聊天页面时按 `session_id` 恢复该会话的 ExportJob 和 Artifact；SSE 只作为低延迟更新，API 列表是恢复来源。
7. 导出失败只影响对应格式，并提供安全重试；重试复用快照，不重新执行检索。

## 13. 权限、安全和生命周期

- 创建、领取、查看状态、预览和下载都必须检查所有者/租户/部门/知识库范围；
- 下载时重新检查当前知识库读取权限；
- 数据库只保存引用和必要的快照，二进制文件存储目录默认 0600；
- 文件名使用服务端生成的安全标题和短 Artifact ID；
- 仅允许白名单格式、MIME 和扩展名；
- Markdown/HTML 预览必须防止脚本注入；PDF HTML/CSS 禁止脚本、文件读取和外部网络请求；
- 定期清理超过保留期的快照和 Artifact，同时保留审计记录的 hash 和元数据；
- 记录创建、重试、取消、下载、权限拒绝和清理事件；
- 发现非法 Office 包、宏、外部链接或嵌入对象时拒绝发布，而不是把不安全文件提供下载。

## 14. 可观测性

增加低基数指标和 trace 字段：

- `export_queue_depth`、`export_queue_oldest_age`；
- `export_jobs_total{format,status}`；
- `export_render_duration_ms{format}`；
- `export_bytes_total{format}`；
- `artifact_downloads_total{format}`；
- snapshot 大小、行数、页数、渲染器版本；
- `run_id`、`turn_id`、`snapshot_id`、`export_job_id`、`artifact_id` 之间的关联。

内容正文、证据原文和文件字节默认不写入普通日志；如需调试，遵循现有 `OBS_CAPTURE_*` 采集开关和长度限制。

## 15. 测试与验收

### 单元测试

- ExportPlan 格式别名、数量、选项和 schema 校验；
- ResultEnvelope 版本化、hash、citation 绑定和不可变性；
- 各 Renderer 的表格类型、标题、引用和边界限制；
- 文件签名、MIME、扩展名和安全文件名校验；
- idempotency、lease、重试、死信和过期恢复。

### API/Worker 集成测试

- 普通回答生成 Markdown 下载；
- 知识库文档证据生成带来源的 DOCX/PDF；
- 表格查询生成真实 XLSX 行和来源 Sheet；
- 多格式任务部分成功；
- 页面断开后任务继续，重新进入可恢复；
- 多用户不能读取、预览或下载彼此 Artifact；
- 权限撤销后不能下载旧 Artifact；
- worker 重启后 lease 过期任务可继续；
- 重试不重复检索、不产生重复 Artifact。

### 前端测试

- 导出菜单创建任务；
- Artifact 卡片各状态展示；
- 页面切换后恢复任务；
- 全局任务中心与会话卡片状态一致；
- 下载动作使用认证客户端并处理失败。

### 验收标准

用户可以在一次知识库对话中要求“请把结果导出成 Excel 和 PDF”，页面立即显示两个后台任务；离开当前页面后任务仍执行；回来后可以看到两个完成状态并下载有效文件；Excel 使用真实查询行或明确的检索结果结构，PDF/Excel/Markdown 保留来源引用；任何权限变化都不会绕过下载鉴权。

## 16. 分阶段交付

### Phase 1：基础 Artifact 和常用导出

- ResultSnapshot、ExportJob、Artifact 表和存储；
- Markdown、XLSX Renderer；
- `POST/GET /exports` 和 Artifact 下载；
- 当前回答导出菜单；
- 独立导出 worker；
- 任务恢复、幂等和权限测试。

### Phase 2：报告文件

- DOCX、PDF Renderer；
- 引用来源附录和预览；
- 任务中心；
- 重试、保留期和下载审计。

### Phase 3：演示和高并发

- PPTX Renderer、图表和主题；
- 多导出 worker 和并发配额；
- 资源锁、Artifact 历史版本和后续修订；
- 与现有模板化文档产物逐步统一存储接口。

## 17. 兼容与迁移

1. 不修改现有 ChatTurn 的完成、取消和 SSE 语义；只在完成阶段增加快照和导出事件。
2. 不删除或重命名现有文档生成接口；新 Artifact API 作为通用入口。
3. 现有文档工单的 `artifact_id` 可以通过适配器映射到通用 Artifact，但模板安全策略仍由原文档模块负责。
4. 所有新表采用启动时幂等 migration；旧数据库没有快照的历史回答不自动补生成快照，用户从旧消息点击导出时按权限创建新的快照。
5. 新能力由 `RESULT_EXPORT_ENABLED` 和每格式开关控制，未安装对应渲染依赖时只隐藏该格式，不影响聊天和其他格式。

## 18. 参考行为

- [Codex Cloud：隔离环境、后台任务、并行任务和结果审查](https://learn.chatgpt.com/docs/cloud)
- [Codex Long-running Work：Goal、恢复和并行会话](https://learn.chatgpt.com/docs/long-running-work)
- [Codex Work with files：文档、表格、演示文稿和 PDF 的预览/下载](https://learn.chatgpt.com/docs/artifacts-viewer)
- [Claude Code Background Tasks](https://code.claude.com/docs/en/interactive-mode)
- [Claude Code Parallel Agents and Worktrees](https://code.claude.com/docs/en/agents)
