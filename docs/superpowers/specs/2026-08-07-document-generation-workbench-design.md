# 文档生成工作台与聊天式需求澄清设计

## 1. 背景与目标

当前 5175 前端将文档生成拆成“上传模板 / 新建任务 / 任务与下载”三个简单页签，任务页主要显示原始状态字符串。后端 Harness 已具备模板 Schema、信息需求、受控检索、Managed Writer、证据校验和渲染安全检查，但缺少面向用户的需求澄清层；`waiting_human`、`needs_review` 和系统错误也容易被用户理解为同一种“卡住”。

本设计的目标是：

1. 上传模板后解析、拆解并持久化模板结构，后续复用同一模板版本的分析结果。
2. 采用聊天式逐问逐答确认用户真实需求；需求明确时自动跳过人工复核并开始生成。
3. 将聊天答案保存为可恢复、可审计的 `GenerationBrief`，而不是只存在浏览器状态中。
4. 复用现有 Document Harness 和多路检索能力，由确定性后端控制模板写入、权限、证据范围和发布。
5. 用用户可理解的状态、错误原因和下一步操作替代“retrieving + 节点 complete + 错误 -”这类无法行动的提示。
6. 对完全通过需求、证据、内容和渲染校验的结果支持自动发布；有风险的结果仍明确进入复核或阻塞。

## 2. 非目标

- 不构建能够任意访问文件、数据库或 Shell 的全能 Agent。
- 不绕过现有来源快照、Evidence Package、模板 hash 和 renderer 安全策略。
- 不在本阶段替换 React/Vite、RAGFlow 或现有 Harness。
- 不强制历史工单重新执行；历史工单只增加兼容的状态修复、重试或重新开始入口。

## 3. 用户流程

```text
上传模板
  → 模板结构分析与持久化
  → AI 生成需求摘要
  → 聊天式逐问逐答澄清
  → 用户确认需求并开始生成
  → 冻结模板、Schema、GenerationBrief 和来源快照
  → 字段级检索与证据校验
  → 受控内容生成
  → 一致性与渲染安全检查
  → 预览、自动发布或风险处理
```

澄清交互遵循以下规则：

- 一次只问一个主题，最多展示三个选项，同时允许自由输入。
- 先展示“我已理解”和“仍待确认”，避免用户重复描述。
- 优先询问会改变输出的决策：项目/版本、数据范围、缺失数据策略、推断策略、语言和详细程度。
- 需求没有歧义时自动进入 `ready_to_generate`，不显示人工复核阻塞。
- 需求确认之后，聊天区切换成生成进度和可解释的事件日志。

示例首条消息：

> 已读取《硬件设计评审表.xlsx》，识别到 6 个工作表、42 个可填字段和 3 个重复表格。我建议生成硬件设计评审文档。请先确认：使用哪个项目版本？

## 4. 逻辑架构

### 4.1 模块边界

- `TemplateAnalyzer`：解析工作表、章节、字段、占位符、示例值、合并区域、公式和布局约束，输出版本化 `TemplateSchema` 与分析报告。
- `RequirementClarifier`：读取模板分析和用户回答，生成澄清问题，维护 `GenerationBrief`。
- `RetrievalPlanner`：把已确认的 Brief 和模板语义单元转换为 `InformationRequirement`，选择文档、Excel、原理图等检索能力。
- `ManagedWriter`：只接收已验证 Evidence Package，生成结构化字段草稿和断言。
- `Validator`：执行证据支持、模板污染、跨字段一致性、需求契合和格式检查。
- `Renderer`：按 allowlist 和填充计划确定性写入模板，执行重复长文本 fan-out 等安全检查。
- `GenerationOrchestrator`：负责检查点、租约、有限重试、状态转换和结果收尾。

Agent 只能执行理解、提问、检索规划和受控写作；模板文件读写、权限、来源冻结、状态机、校验、渲染和 artifact 发布由后端确定性服务执行。

### 4.2 数据流与持久化对象

新增或扩展以下对象：

#### `GenerationBrief`

- `purpose`：文档用途
- `scope`：项目、产品、版本、时间范围
- `source_policy`：允许使用的来源和版本
- `output_policy`：语言、详细程度和格式
- `missing_data_policy`：留空、标记“未提供”或继续生成
- `inference_policy`：禁止推断、允许有限推断或必须显式标注推断
- `confirmed`、`confidence`、`updated_at`

#### `ClarificationMessage`

- `question_id`
- `question`
- `options`
- `answer`
- `reason`
- `status`

#### `GenerationStatus`

- `phase`
- `display_label`
- `progress`
- `current_unit`
- `error_code`
- `error_message`
- `retryable`
- `next_actions`

Brief、澄清消息、模板分析报告和每阶段检查点均需持久化，并绑定租户、用户、模板版本和工单，保证重试、恢复和审计可用。

## 5. 状态机与自动化策略

主流程状态：

```text
draft
→ analyzing_template
→ needs_clarification
→ ready_to_generate
→ retrieving
→ generating
→ validating
→ rendering
→ completed
```

风险/终态：

```text
needs_review
blocked
failed
cancelled
```

状态语义必须互斥：

- `needs_clarification`：仅表示用户需求缺少会影响生成的决策。
- `needs_review`：证据不足、内容冲突、低置信度或语义校验未通过。
- `blocked`：渲染安全检查、模板 hash、权限、来源越界或基础设施错误阻止继续。
- `failed`：有限重试后仍无法执行，且不是内容风险。
- `retrieving/generating/validating/rendering`：只能表示实际运行中的阶段；超时、租约丢失或收尾异常必须转换为明确的失败/阻塞状态并保存错误。

自动发布条件：

1. GenerationBrief 已确认。
2. 所有必填语义单元均有冻结来源内的支持证据。
3. Draft、断言、需求契合和跨单元一致性校验通过。
4. 模板 hash 与 Schema 版本匹配。
5. Renderer 安全检查通过，未发现重复长文本异常覆盖或越界写入。

任何条件不满足都不得伪装成完成；根据原因进入 `needs_review`、`blocked` 或 `failed`。

## 6. 前端工作台设计

### 6.1 页面骨架

采用单页工作台，保留顶部知识库选择和新建任务入口：

```text
┌──────────────┬──────────────────────────────┬──────────────────┐
│ 流程步骤      │ 聊天式需求澄清 / 生成日志       │ 模板与证据摘要     │
│ 模板解析      │ AI：我识别到 42 个字段……       │ 模板版本           │
│ 需求确认      │ 用户：按当前发布版本生成       │ 字段映射           │
│ 检索生成      │ AI：缺失数据采用“未提供”       │ 来源文件           │
│ 校验发布      │ [输入框] [发送]                │ 置信度/告警         │
└──────────────┴──────────────────────────────┴──────────────────┘
```

左侧流程步骤显示状态、耗时和当前节点；中间显示消息、选项按钮、确认摘要和生成事件；右侧使用可折叠卡片展示模板、映射、来源和风险。窄屏将右栏折叠为底部抽屉，聊天输入区保持固定。

### 6.2 状态文案

前端将后端状态映射为中文可行动文案，同时保留原始状态和错误码供详情展开：

| 原始状态 | 展示文案 | 说明 |
|---|---|---|
| `analyzing_template` | 正在分析模板 | 解析结构和字段 |
| `needs_clarification` | 需要补充需求 | 在聊天区回答问题 |
| `ready_to_generate` | 等待开始生成 | 需求已明确 |
| `retrieving` | 正在检索资料 | 显示当前字段和检索轮次 |
| `generating` | 正在生成内容 | 显示已完成字段数 |
| `validating` | 正在校验内容 | 显示校验项 |
| `rendering` | 正在写入模板 | 显示渲染进度 |
| `completed` | 已完成 | 提供预览和下载 |
| `needs_review` | 需要检查内容 | 展示证据、冲突和处理建议 |
| `blocked` | 生成被阻止 | 展示原因、重试和修复入口 |
| `failed` | 任务失败 | 展示可重试性和技术详情 |

不再展示“节点 complete；错误 -”这类状态矛盾；节点完成但收尾失败时，以最终错误状态和下一步操作为准。

### 6.3 异常操作

每个异常必须提供结构化的 `next_actions`，前端按能力渲染按钮：

- `retry_retrieval`：重新检索
- `retry_generation`：重新生成失败字段
- `edit_brief`：修改需求
- `view_evidence`：查看证据
- `replace_template`：使用新模板版本
- `restart_work_order`：从检查点重新运行
- `contact_admin`：联系管理员

## 7. API 兼容策略

优先复用现有模板、工单、状态、artifact API；新增会话接口承载聊天状态：

```text
POST /api/v1/document-generation/sessions
GET  /api/v1/document-generation/sessions/{session_id}
POST /api/v1/document-generation/sessions/{session_id}/messages
POST /api/v1/document-generation/sessions/{session_id}/confirm
GET  /api/v1/document-generation/work-orders/{id}/events
POST /api/v1/document-generation/work-orders/{id}/retry
```

`events` 初期可以由前端轮询现有 status 接口实现；后续增加 SSE 时保持事件 DTO 不变。旧工单响应增加可选字段，不删除已有字段，确保旧前端和历史数据兼容。

## 8. 错误处理与恢复

每个阶段保存检查点和幂等键。重试只从最近一个有效检查点继续：

- 模板分析失败：重试分析，不重复创建模板版本。
- 需求澄清中断：恢复聊天会话，不丢失已确认回答。
- 检索失败：按 Harness 预算有限重试；用尽预算后进入 `needs_review` 或 `failed`。
- 渲染失败：保存 `blocked`、错误码和模板/单元定位，不保留旧的 `retrieving` 状态。
- Worker 租约丢失：由调度器回收并安全重试，避免旧 Worker 提交结果。

不得通过关闭 duplicate long value fan-out 或证据校验来“强行生成”。安全失败必须对用户透明。

## 9. 测试与验收

### 前端

- 工作台流程栏、聊天消息、选项回复、确认按钮和状态映射组件测试。
- `needs_clarification`、`needs_review`、`blocked`、`completed` 的渲染和操作测试。
- 刷新页面后能够恢复会话、工单和当前步骤。
- 窄屏布局中右栏抽屉和聊天输入区可用。

### 后端/API

- Brief 和澄清消息保存、恢复、权限隔离。
- 需求确认后只创建一个幂等工单。
- 状态转换不允许跳过校验从风险态进入完成态。
- 收尾渲染异常必然写入 `blocked` 或 `failed`，不能停留 `retrieving`。
- 完全通过校验时生成并发布；证据不足或冲突时不会自动发布。

### 端到端

覆盖“上传 xlsx → 模板分析 → 聊天确认 → 检索 → 生成 → 预览/下载”和“渲染安全失败 → 明确阻塞 → 重试/修复”两条主路径，并保留既有模板分析、RAGFlow 检索和 artifact 权限回归测试。

## 10. 分阶段交付

1. **工作台 UI**：重构 `DocumentGenerationPage`、状态文案、步骤栏、异常卡片；兼容现有轮询 API。
2. **需求澄清**：增加 `GenerationBrief`、会话 API 和聊天式逐问逐答；确认后再启动现有 Harness。
3. **自动生成与恢复**：接入自动发布门控、阶段检查点、失败恢复和 SSE 事件；修复历史 `retrieving` 工单的可操作入口。

每阶段可独立回滚，且不改变现有来源冻结、证据落域和模板渲染安全边界。

