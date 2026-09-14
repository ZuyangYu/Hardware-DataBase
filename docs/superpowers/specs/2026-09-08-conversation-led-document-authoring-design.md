# 对话驱动的通用文档创作架构设计

- 日期：2026-09-08
- 状态：Proposed，等待用户审阅
- 设计级别：目标架构与分阶段迁移基线
- 关联设计：
  - `2026-08-06-document-generation-optimization-design.md`
  - `2026-08-07-document-generation-workbench-design.md`
  - `2026-08-31-agentic-document-authoring-design.md`
  - `2026-09-01-document-authoring-entry-convergence-design.md`
  - `2026-09-03-conversational-export-design.md`

## 1. 决策摘要

采用“对话驱动、文档平台执行”的混合架构：

```text
用户自然语言需求
  → Conversation Orchestrator
  → OutputSpec 草案与建议
  → Document Planning Service 生成计划提案
  → HITL Gate 1：确认将生成什么
  → 冻结 OutputSpec、DocumentPlan、来源快照和策略
  → 持久化 Task DAG
  → 并行 Workers
  → 单元级 Reviewer
  → 确定性 Aggregator / Renderer
  → 文档级 Reviewer 与产物校验
  → HITL Gate 2：异常处理或发布批准
  → Artifact、Run Manifest 与审计记录
```

对话是主要用户入口，负责理解、追问、推荐和解释；独立的文档规划与执行服务负责完整性、并行执行、证据治理、确定性渲染、恢复和审批。聊天模型不得直接生成或发布 Office/PDF 二进制文件。

有模板和无模板流程最终必须编译成同一种 `DocumentPlan` 和 `TaskGraph`。两者只在布局契约的产生方式上不同：有模板使用 `TemplateContract`，无模板使用 `StructureContract`。后续检索、起草、审核、聚合、渲染和发布复用同一执行内核。

## 2. 背景与问题证据

现有实现已经具备 `ConversationOrchestrator`、`DocumentTask`、持久化澄清会话、Work Order、来源快照、LangGraph 单元并发、字段级证据、确定性模板渲染、审核和版本对象。这些能力可以作为演进基础，不需要推倒重建。

但当前链路仍以“模板字段填充”为中心，尚不能稳定达到人工编写文档的完整性和结构质量：

1. `GenerationSession.template_version_id` 是必填项，需求模型不能自然表达无模板文档。
2. `GenerationBrief` 记录部分生成政策，但没有统一表达受众、内容大纲、表格、目标身份、输出格式、审批等级和版式来源。
3. Planner 主要把已存在的 Schema 单元送进执行图，没有先建立“目标内容全集、行列覆盖和依赖”的版本化计划。
4. 表格数据模型和渲染通道已存在，但常规 Writer 仍不能可靠地产生 `TypedTableRow`；复杂表格容易被标量化或留空。
5. 当前图有单元级校验和最终必填/OOXML 门禁，但缺少一个显式、独立的渲染后文档级 Reviewer，无法统一发现跨字段矛盾、表格错位、重复内容和整体可读性问题。
6. 推荐默认值可能在直接入口中被服务端自动确认。低风险文档可以一键采用推荐方案，但正式工程文档不能把隐式默认值当作用户确认。
7. 会话/任务卡片可以生成 `session` 或 `task` 深链，但工作台主要消费 `kb` 和 `workOrder`，未形成所有阶段都可恢复的导航闭环。
8. 修订目前可以创建新 Work Order，但部分章节/字段修订未编译为受影响子图，且产物 lineage/hash 仍需由运行清单严格证明。
9. 对话式导出和受治理的文档创作存在意图重叠风险。“导出当前回答为 PDF”不应进入知识库文档规划，“基于多个来源创建 ICD”也不应退化为普通回答导出。

本设计将这些缺口收敛到一个统一的需求、计划、执行和审核模型中。

## 3. 目标

1. 用户可以仅通过对话表达并逐步完善不同类型的文档需求。
2. 同时支持用户模板、系统模板和无模板结构生成。
3. 文档类型不写死在核心流程中；ICD、FPT、需求规格书、测试报告和通用报告通过插件化领域策略扩展。
4. Planner 在执行前枚举完整的章节、字段、表格、行范围、依赖、来源要求和验收规则。
5. 独立任务可并发执行，依赖和共享表格通过 DAG 显式串联或设置 barrier。
6. 单元审核、文档审核和产物校验形成两层 Reviewer，而不是只验证局部字段。
7. HITL 只出现在真正改变结果或承担发布责任的位置，不要求人工逐字段审批。
8. 所有运行都可恢复、可审计、可重放，并能从 Artifact 追溯到确认需求、计划版本、来源快照、证据和执行器版本。
9. 在保持当前 API 和历史 Work Order 可读的前提下分阶段迁移。

## 4. 非目标

- 不构建可访问任意文件、Shell、数据库或外部网络的通用 Agent。
- 不允许 LLM 直接写入模板坐标、生成宏、修改公式或发布文件。
- 不把普通问答的 `ResultSnapshot → ExportJob` 流程合并进受治理的文档创作状态机。
- 不在第一阶段同时实现所有文档类型和所有格式；格式能力按 registry 分期开放。
- 不提供在线协同 Office 编辑器；复杂人工编辑仍在工作台或下载后的办公软件中完成。
- 不承诺所有低质量或冲突来源都能自动修复；安全失败必须转为可操作的人工异常。

## 5. 与既有设计的关系

### 5.1 保留的契约

- 来源必须冻结为 `SourceSetSnapshot`，所有证据继续绑定 tenant、run、snapshot 和 content hash。
- Agent 只能检索和提出结构化候选；FillPlan、模板保护、渲染、权限和发布由确定性服务控制。
- 固定标签、表头、公式、受保护/隐藏区域和未授权区域不得自动覆盖。
- `needs_clarification`、`needs_review`、`blocked` 和 `failed` 保持互斥语义，并提供结构化 `next_actions`。
- durable job/outbox、lease/fencing、Receipt、幂等键和追加式业务事件仍是生产执行前置条件。
- Chat 显示摘要与动作，Workbench 承担证据、diff、复杂异常和正式审批。

### 5.2 明确变更的契约

`2026-08-31-agentic-document-authoring-design.md` 将“自由排版生成全新文档”列为非目标。本设计根据新的产品要求，增加受控的无模板路径，但不开放自由二进制写入：

- 无模板文档必须先形成受 schema 校验的 `StructureContract`；
- 只能使用服务端注册的版式 recipe、字体、组件和 renderer；
- 模型只生成逻辑内容和有限的 layout hints；
- 宏、脚本、外部链接、嵌入对象和任意 OOXML 片段默认禁止；
- 最终文件仍由确定性 renderer 生成并通过格式校验。

因此这里的“无模板”指用户不必提供模板，不代表没有结构契约或渲染规则。

### 5.3 与对话式导出的边界

路由遵循以下语义：

| 用户目的 | 执行路径 | 权威输入 |
|---|---|---|
| 将当前回答/检索结果另存为 PDF、Word、Excel、PPT | Conversational Export | 不可变 `ResultSnapshot + ExportPlan` |
| 从知识库、附件或项目资料创建一份新文档 | Document Authoring | 已确认 `OutputSpec + DocumentPlan + SourceSetSnapshot` |
| 提供模板并要求填充/生成 | Document Authoring | 上述对象加 `TemplateContract` |
| 仅询问源 PDF/Excel 中的内容 | Retrieval/QA | 查询与检索范围 |

当“整理成文档”无法判断是导出当前回答还是重新创作时，只询问一个分流问题。两类 Artifact 可以共享下载和保留期基础设施，但不得共享业务状态、审批结论或来源清单语义。

## 6. 核心领域模型

### 6.1 `OutputSpec`：用户确认的输出契约

`OutputSpec` 是对话和文档平台之间唯一的需求交接契约。对话期间创建版本化草案；用户确认后冻结该版本和 hash，后续变更创建新版本，不能原地修改。

下列 JSON 是字段形态示例，ID 和 hash 均为固定示例值：

```json
{
  "output_spec_id": "output-spec-example-001",
  "version": 3,
  "status": "proposed",
  "purpose": "用于软硬件接口评审",
  "audience": ["硬件", "软件", "系统"],
  "document_type": "icd",
  "target_identity": {
    "project": "ADAS",
    "product": "ADAS-ECU",
    "release": "current_published"
  },
  "artifact": {
    "deliverables": [
      {"format": "xlsx", "role": "primary", "required": true, "requested_by": "user"},
      {"format": "pdf", "role": "derivative", "required": false, "requested_by": "system"}
    ]
  },
  "layout_source": {
    "mode": "provided_template",
    "template_version_id": "template-version-example-003"
  },
  "outline": [
    {"unit_id": "cover", "kind": "section", "required": true},
    {"unit_id": "pin_definition", "kind": "table", "required": true}
  ],
  "table_requirements": [
    {
      "unit_id": "pin_definition",
      "row_scope": "all_selected_connectors_and_pins",
      "required_columns": ["connector", "pin", "signal", "direction", "description"]
    }
  ],
  "source_scope": {
    "knowledge_bases": ["ADAS"],
    "attachments": [],
    "version_policy": "current_published"
  },
  "language": "zh-CN",
  "style": {"tone": "engineering", "detail": "review_ready"},
  "missing_data_policy": "mark_tbd",
  "inference_policy": "forbid",
  "approval_policy_id": "formal-engineering-v1",
  "accepted_recommendations": [],
  "confirmed_by": null,
  "confirmed_at": null,
  "content_hash": "sha256:example-output-spec-hash"
}
```

约束：

- `outline` 和 `table_requirements` 表达用户要得到的内容，而不是 Excel 坐标。
- `target_identity` 对正式文档必填；项目、产品或版本不明确时不得创建 Work Order。
- `layout_source.mode` 为 `provided_template | system_recipe | generated_structure`。
- `artifact.deliverables` 至少有一个 `primary`，且每项格式必须被选定 adapter/renderer 支持。用户明确要求的交付物标记 `required=true`；可选衍生格式失败不撤销已验证的主 Artifact，但必须显示可重试的交付告警。任一必需交付物未完成时，任务不能显示为全部交付完成。
- 对话生成的建议与用户接受的建议分开保存，不能把模型建议伪装为用户答案。
- `source_scope` 是允许范围，可以包含知识库、附件、结构化输入和经用户确认的事实陈述。Planner 提案时创建不可变但尚不可执行的 `SourceSetSnapshot`，确认时重新授权并将该 snapshot 原样绑定到 Work Order。
- 用户提供的事实若参与正文，必须作为带 actor、时间和 content hash 的 `user_assertion` 来源进入 snapshot；生成指令和事实来源不能混为一类。
- `approval_policy_id` 必须引用服务端已注册且带版本的策略，不能接收任意自由文本。
- `layout_source` 是判别联合类型：`provided_template` 必须带 template version；`system_recipe` 必须带 recipe ID/version；`generated_structure` 必须带 structure constraints profile。

现有 `GenerationBrief` 在迁移期作为 `OutputSpecDraft` 的兼容投影。新逻辑只允许单向投影到 `OutputSpec`，不得同时把两个对象都当作权威来源。

### 6.2 `DocumentPlan`：可执行的文档蓝图

`DocumentPlanningService` 接收 `OutputSpec`、可用来源清单、模板分析结果和能力 registry，输出版本化 `DocumentPlan`。Planner 可以使用模型辅助理解，但计划必须通过服务端 schema、覆盖率和能力校验。

```text
DocumentPlan
├── status: proposed | accepted | stale
├── output_spec_ref + hash
├── source_snapshot_ref + hash
├── domain_strategy_ref + version
├── layout_contract
│   ├── TemplateContract，或
│   └── StructureContract
├── semantic_units[]
├── dependency_edges[]
├── coverage_contract
├── retrieval_specs[]
├── unit_review_policy
├── document_review_policy
├── render_spec
├── approval_policy
└── plan_hash
```

`DocumentPlan` 只保存版本化 ID、hash、有限摘要和规则，不保存原始附件、整段证据、模型 prompt、路径或凭据。

Planner 必须完成以下工作：

1. 枚举全部目标章节、字段、表格、预期行集合和必填列；
2. 为每个语义单元分配稳定 ID、输出 schema、来源能力、依赖、预算和 reviewer；
3. 建立 `CoverageContract`，明确什么条件才算“完整”；
4. 选择领域策略和布局 adapter；找不到受支持组合时在入队前失败；
5. 生成用户可读的计划摘要和异常，而不是直接开始生成；
6. 对正式工程文档，无法确定目标身份、范围、模板映射或行集合时返回澄清问题；
7. 提案时冻结候选来源为不可变 snapshot 并纳入 plan hash；用户确认后把 `OutputSpec` 和 `DocumentPlan` 标为 accepted，将同一个 snapshot 与全部 policy hash 绑定到 Work Order。

### 6.3 `CoverageContract`：完整性不是“字段跑完”

Coverage 必须按文档结构定义，而不是按已生成任务数量反推：

- section：要求的章节是否存在，是否包含必需子单元；
- scalar/paragraph：必填值和关键断言是否有支持证据；
- table：预期 row keys、必填列、唯一性、排序和每行/单元格证据；
- cross-unit：共享身份、版本、术语、单位和引用是否一致；
- artifact：目标 sheet/section、公式、样式、合并区、页数或格式约束是否满足。

以 ICD `Pin Definition` 为例，完成条件至少是“选定 connector 的预期 pin key 全部出现，必填列逐行满足，重复/缺失 pin 为零或进入异常”，不能用“表格字段已执行一次”代表完成。

## 7. 可插拔边界

### 7.1 `DomainStrategy`

领域策略只处理文档语义，不处理二进制格式。建议最小接口：

```text
identify_requirements(OutputSpec, SourceCatalog) -> DomainRequirements
compile_semantic_units(OutputSpec, LayoutContract) -> SemanticUnit[]
build_dependency_edges(SemanticUnit[]) -> Edge[]
normalize_candidate(UnitTask, Evidence[]) -> TypedCandidate
review_unit(TypedCandidate, Evidence[]) -> ReviewIssue[]
review_document(DocumentModel, CoverageReport) -> ReviewIssue[]
```

Registry 使用 `strategy_id + version + supported_document_types` 显式注册。未命中特定领域时使用 `generic_report`，它只支持章节、段落、列表和显式列定义的表格；不能猜测未知行业结构。

ICD 策略负责 connector/pin 范围、row identity、方向/电气属性规范化、重复与缺失 pin、版本一致性等领域规则。FPT、需求规格书和测试报告以独立策略扩展，不在核心 orchestrator 中增加文档类型 if/else。

### 7.2 `LayoutAdapter` 与 `Renderer`

布局层按格式注册：

```text
LayoutAdapter.inspect(input) -> TemplateContract | RecipeCapabilities
LayoutAdapter.bind(semantic_units) -> RenderBinding[]
LayoutAdapter.validate_contract(contract) -> ContractIssue[]
Renderer.render(DocumentModel, contract, bindings) -> CandidateArtifact
Renderer.validate_artifact(candidate) -> ArtifactValidationReport
```

三种布局来源：

1. `provided_template`：解析用户模板，固定模板 hash、可写区域、公式、静态内容和绑定。
2. `system_recipe`：使用经审核的标准封面、章节、表格和样式 recipe。
3. `generated_structure`：Planner 生成有限的逻辑结构，服务端把它映射到白名单组件；适合无模板的通用报告。

模板模式必须维持 region allowlist 和 FillPlan。无模板模式没有坐标白名单，但必须有结构组件 allowlist、资源上限和格式 schema。两者都禁止模型输出原始 OOXML、宏或任意样式代码。

## 8. 对话与计划确认

### 8.1 对话职责

Conversation Orchestrator 负责：

- 区分问答、附件分析、对话式导出和文档创作；
- 从自然语言和上下文持续更新 `OutputSpecDraft`；
- 只追问会改变结果的缺失决策；
- 根据来源、文档类型和组织策略提出可解释建议；
- 展示计划提案、状态、异常和发布摘要；
- 把复杂查看和正式审批深链到 Workbench。

它不负责：Excel 坐标、OOXML、行号分配、证据真实性裁决、任务租约或发布。

### 8.2 计划提案与 Gate 1

用户不应确认一组内部参数，而应确认一份易理解的“将生成什么”摘要。流程为：

1. 对话形成 `OutputSpecDraft`；
2. Planning Service 以 `proposed` 状态编译 `DocumentPlan`，并返回风险、缺口和能力降级；
3. `OutputSpecConfirmationCard` 展示文档类型、格式、模板/recipe、章节/表格、目标身份、来源版本、缺失/推断政策和审批方式；
4. 用户选择确认、修改或“采用推荐方案”；
5. 服务端重新校验权限、模板和候选 snapshot；在同一确认事务中接受 `OutputSpec` 和 `DocumentPlan`、绑定该 `SourceSetSnapshot` 与 policy hash，然后幂等创建 Work Order/job。

若确认前来源被撤销、用户失去访问权、模板发生变化，或用户要求改用新来源版本，原计划标记 `stale` 并重新提案，不能把旧确认应用到新输入。知识库后来发布新版本不会暗中替换已经提案并展示给用户的 snapshot。

### 8.3 四类对话卡片

1. `RequirementClarificationCard`：一次只问一个主题，最多三个选项，并允许自由输入。
2. `OutputSpecConfirmationCard`：汇总最终输出和系统建议，支持确认或修改。
3. `GenerationStatusCard`：展示排队、阶段、覆盖率、阻塞和可执行的下一步。
4. `ReviewSummaryCard`：展示异常数量、严重度、发布条件和 Artifact 入口；发布完成后在同一卡片提供预览和下载，不要求用户另行进入任务中心。

所有卡片由服务端事实投影，刷新后从持久化 session/task/event 恢复。工作台必须同时接受 `task`、`session` 和 `workOrder` 深链，解析后归一到同一个 `DocumentTask`。

## 9. Task DAG 与执行模型

### 9.1 编译结果

Task Graph 由确定性 compiler 从已冻结 `DocumentPlan` 生成并持久化。LLM 可以建议依赖，但不能在运行中任意增加工具或绕过节点。

每个 `UnitTaskSpec` 至少包含：

- 稳定 `task_id`、`unit_id` 和 plan version；
- 前置依赖和 barrier；
- 输入 schema、输出 schema 和 row scope；
- 允许的来源、检索器、工具和预算；
- unit reviewer 和 pass policy；
- 最大尝试次数、超时和幂等 action key。

### 9.2 推荐执行顺序

```text
preflight
  → plan/load frozen context
  → fan-out independent units
      → retrieve
      → normalize
      → draft typed candidate
      → unit review
      → persist accepted unit draft
  → barrier / deterministic aggregation
  → document semantic review
  → deterministic render
  → artifact validation + post-render document review
  → release decision
```

并行只用于独立单元。以下情况必须建立依赖或 barrier：

- 后续字段依赖已解析的产品/版本/主体身份；
- 表格行集合依赖 connector、模块或章节范围；
- 多个单元写入同一表格或共享顺序；
- 汇总、交叉引用、目录和图表依赖上游内容；
- 修订只重跑受影响单元，但下游聚合和文档审核必须重跑。

### 9.3 Worker 约束

Worker 只做单一职责：检索、规范化、起草或确定性计算。所有输出均为强类型候选，不直接写文件。表格 Worker 必须输出 `TypedTableRow[]`，每行包含稳定 row key、列值和单元格/行级证据；不得把完整表格压成 `display_value` 字符串。

Worker 调用继续使用 frozen scope、Evidence Registry、预算、lease/fencing 和 Receipt-first 幂等协议。运行时发现 plan、snapshot、strategy 或 adapter hash 不一致时 fail closed。

## 10. 两层 Reviewer 与受限返工

### 10.1 单元级 Reviewer

每个字段、段落或表格分区提交前检查：

- 输出是否符合 value/row schema；
- evidence ID 是否属于当前冻结快照；
- 每个事实或表格行是否获得足够证据支持；
- 是否存在冲突、越界推断、单位/枚举错误；
- 是否满足字段或领域策略的 pass policy。

Reviewer 返回结构化结果：

```text
pass | rework | needs_human | blocked
  + issue_code
  + severity
  + affected_unit/row/cell
  + evidence_ids
  + suggested_action
```

`rework` 只允许在原策略和预算内补检或重写，不能自动放宽来源、缺失或推断政策。达到尝试上限后转 `needs_human` 或 `blocked`。

### 10.2 文档级 Reviewer

单元全部通过仍不等于文档通过。文档级 Reviewer 在聚合后和渲染后分别执行：

- `pre_render`：CoverageContract、跨字段/跨章节一致性、术语、目标身份、引用完整性、重复内容和领域规则；
- `post_render`：目标 sheet/section、行列错位、公式和固定内容保护、OOXML/PDF 可解析性、截断、溢出、分页和必要的视觉结构检查。

语义 Reviewer 可以使用独立模型给出 issue proposal，但确定性规则决定是否满足必填覆盖、格式安全和发布阈值。生成模型不得自行批准自己的高风险输出。

文档级返工同样有界：`pre_render` 语义问题回到明确受影响的 unit 子图后重新聚合；纯 `post_render` 布局问题只重跑 adapter/renderer。无法安全定位、超过预算或涉及用户决策时进入 HITL，不得无限自循环。

## 11. HITL 策略

HITL 采用风险分级，而不是逐字段确认。

现有“模板映射确认”和“ICD 范围确认”不会被删除，而是作为 Gate 1 中的独立必答决策聚合展示；它们仍分别产生审核事实。两道 Gate 指两个生命周期边界，不代表每个边界只能有一个问题或一个审核记录。

### 11.1 Gate 1：计划确认

以下情况必须显式确认后才能入队：

- ICD、需求规格书、测试结论等正式工程文档；
- 新模板或存在不确定模板映射；
- 项目/产品/版本、connector/模块/章节范围存在选择；
- 允许推断、派生计算或覆盖样例值；
- 来源包含临时附件、多个冲突版本或未发布数据；
- 用户要求自动发布。

普通低风险文档可以显示“按推荐方案生成”，但点击本身必须形成带 actor、spec hash 和时间的确认事实。新任务不得仅因为服务端存在推荐默认值而静默确认。

### 11.2 Gate 2：异常与发布

以下情况进入人工处理：

- 缺失、冲突、低置信度或无法满足的必填内容；
- 安全关键字段、领域规则失败或模板/来源 hash 变化；
- renderer/artifact 校验异常；
- 高风险文档的最终发布；
- tenant policy 明确要求审批。

只有低风险文档、无异常、全部确定性门禁通过且 tenant policy 明确允许时，才可自动发布。人工批准、拒绝和修订必须绑定当前 Artifact/plan/run hash；过期决定不能作用于新产物。

## 12. 聚合、渲染与 Artifact

Aggregator 按 `DocumentPlan` 的稳定顺序构建逻辑 `DocumentModel`：章节、段落、列表、类型化表格、引用、缺失项和 layout hints。它负责确定性合并、排序、去重、引用索引和跨单元标识，不调用模型补写事实。

Renderer 只消费已审核的 `DocumentModel + LayoutContract + RenderBindings`：

- 模板文档写入 allowlist 区域并保护所有静态结构；
- 无模板文档使用已注册 recipe/组件；
- 主格式与衍生格式分别校验和发布；
- 候选文件在审批后复用同一字节，不在批准后重新检索或渲染。

每个 Artifact 附带不可变 `RunManifest`：

```text
artifact hash
output_spec id/version/hash
document_plan id/version/hash
task_graph version/hash
source_snapshot id/hash
domain strategy id/version
layout contract + renderer id/version
accepted unit draft ids/hashes
coverage and review report hashes
approval decision id/hash
parent artifact/revision refs
```

任何 lineage 字段都必须由本次运行事实计算或验证，不能用修订请求中的可选值回填缺失证明。

## 13. 生命周期与持久化

`DocumentTask` 继续作为 Chat、Workbench 和 API 共用的用户级身份。建议关系：

```text
DocumentTask
  ├── GenerationSession / conversation refs
  ├── OutputSpec versions
  ├── DocumentPlan versions
  ├── WorkOrder(s)
  │     └── HarnessRun / TaskGraphRun
  ├── ReviewDecision(s)
  └── Artifact lineage
```

用户可见状态统一为：

```text
draft
→ needs_clarification
→ awaiting_plan_confirmation
→ planned
→ queued
→ running
→ needs_review | awaiting_release
→ completed
```

旁路终态为 `blocked | failed | cancelled`。执行阶段可作为 `running.phase` 展示，不再把细粒度阶段混成互相冲突的任务终态。历史 `ready_to_generate/retrieving/generating/validating/rendering` 通过兼容 projection 映射，不要求立即迁移历史记录。

Work Order 必须引用已确认的 `output_spec_version/hash`、`document_plan_version/hash`、`source_snapshot_id/hash`、strategy/adapter/policy 版本和 requested executor。运行时记录 effective executor 和所有降级原因。

## 14. 修订模型

修订不是“带同一 revision_id 再做一次完整生成”。流程为：

1. 用户在对话或 Workbench 提出变更；
2. 系统创建新的 `OutputSpec` revision，并展示语义 diff；
3. Planner 生成新的 `DocumentPlan` 和 `PlanDiff`；
4. compiler 计算直接受影响单元和传递依赖，生成最小可执行子图；
5. 未受影响的已接受 draft 可按 hash 引用复用；
6. aggregation、document review 和 artifact render 必须重新执行；
7. 新 RunManifest 绑定 parent artifact 和实际复用/重跑清单。

全量、章节和字段修订共用该机制。范围无法安全确定时明确升级为全量重跑，并在确认卡片中告知用户。

## 15. 错误处理与恢复

- 所有确认、提交、worker action、review decision、发布和修订均有稳定幂等键。
- 每个 DAG 节点在外部调用、事实提交和 checkpoint 前后执行 fencing 校验。
- 已提交 unit draft 通过 Receipt 重建，不重复调用模型；未提交的过期 attempt 才可重试。
- 节点错误不能让 task 停留在看似运行中的状态；必须归类为可重试、`needs_review`、`blocked` 或 `failed`。
- 基础设施失败不自动转成内容缺失，内容冲突也不伪装成系统错误。
- 恢复始终使用同一个 task/work order/run identity，除非用户启动修订或明确重启。
- 多 worker 生产部署仍要求共享事务型业务数据库和兼容的 LangGraph checkpointer；本地 SQLite 仅支持规定的单进程模式。

## 16. 可观测性与评测

业务事件至少记录：

- OutputSpec 提议、修改、确认和 stale；
- Planner/strategy/adapter 选择、计划校验和 plan hash；
- DAG 节点派发、attempt、耗时、降级和 Receipt；
- evidence 查询、过滤、接受和覆盖；
- unit/document review issue、返工和人工决定；
- render、artifact validation、发布和 revision lineage。

事件 payload 继续脱敏，不写原始文件、证据正文、路径或凭据。OTel 用于实时链路和性能，追加式业务事件与 RunManifest 是审计权威。

核心质量指标：

- 需求确认后计划变更率和澄清轮数；
- 目标单元、表格行和必填列 coverage；
- evidence-supported claim/row 比例；
- typed draft 成功率和 scalar/table fallback 率；
- unit rework、document review、HITL 和退回率；
- template static overwrite、source scope violation 和 artifact validation failure；
- 与人工基准的字段/row-key precision、recall、顺序和跨字段一致性；
- 成功任务时延、模型/工具调用次数和 Token。

## 17. 迁移方案

这是目标架构规格，不应作为一次大提交实施。拆成六个可独立验收、可回滚的实施计划。

### Phase 0：契约和 shadow planner

- 定义 `OutputSpec`、`DocumentPlan`、`CoverageContract`、`LayoutContract` 和 `UnitTaskSpec`；
- 建立 DomainStrategy/LayoutAdapter/Renderer capability registry；
- 用现有 GenerationBrief/Schema/Work Order 生成 shadow plan，不改变当前执行结果；
- 建立计划 diff、hash、事件和回归 fixture。

Gate：现有模板任务均可产生稳定、完整、可重复的 shadow plan；旧执行路径不受影响。

### Phase 1：对话需求与确认闭环

- Conversation Orchestrator 写入 `OutputSpecDraft`；
- 增加 plan proposal 和 `OutputSpecConfirmationCard`；
- 禁止新文档任务的服务端静默确认；
- 工作台支持 `task/session/workOrder` 深链和同一任务恢复；
- 保留旧 API 可选字段和状态 projection。

Gate：刷新、重连、重复确认和跨入口操作均落到同一 DocumentTask，且只创建一个 Work Order。

### Phase 2：模板路径迁入统一计划与 DAG

- compiler 用 DocumentPlan 生成持久化 DAG；
- 修复 Writer 的原生表格输出和逐行证据；
- 实现 CoverageContract、unit review、确定性 aggregation 和 post-render document review；
- 以当前 XLSX/XLSM 模板路径做 parity 和质量对比。

Gate：模板固定内容误覆盖和来源越界为零；必填表格不再标量化；不完整文档不能自动发布。

### Phase 3：受控无模板生成

- 首先开放 `generic_report + DOCX/PDF` 和 `structured_table + XLSX` 系统 recipe；
- 再开放经过评测的 generated structure；
- 复用同一 Task DAG、Reviewer、RunManifest 和审批策略；
- 格式能力不足在计划阶段明确反馈，不进入队列。

Gate：无模板 Artifact 通过结构 schema、内容 coverage、格式解析和视觉基线测试，且无任意 OOXML/宏注入路径。

### Phase 4：领域策略、修订和发布策略

- ICD/FPT/需求规格书等策略接入 registry；
- 支持 PlanDiff 和最小受影响子图；
- 完成高风险 Gate 1/Gate 2、签名审批和 lineage 验证；
- tenant 级低风险自动发布策略在评测达标后按文档类型开放。

Gate：正式工程文档的范围、必填覆盖、领域一致性和审批均可从 RunManifest 重建。

### Phase 5：兼容路径收口

- 观察旧 GenerationBrief/Schema 直接执行比例；
- 对历史数据完成只读迁移或明确标记 legacy；
- 删除服务端自动确认和不经过 DocumentPlan 的新任务入口；
- 在备份恢复和两个发布周期无新写入后退役旧字段/表。

## 18. 测试与验收

### 18.1 合同测试

- OutputSpec 草案、确认、版本和 hash 不可变性；
- Planner 对相同输入产生稳定 plan，缺失能力在入队前失败；
- TemplateContract/StructureContract 联合类型只允许一种布局来源；
- Work Order 拒绝未确认、stale 或 hash 不匹配的计划；
- DomainStrategy、Adapter、Renderer 的 registry 和版本兼容。

### 18.2 执行与恢复测试

- DAG 仅并发无依赖单元，barrier 后按 schema 顺序聚合；
- worker 失败、lease loss、进程重启和重复消息不造成重复事实或重复发布；
- reviewer 返工次数有界，不能放宽冻结政策；
- table task 产生稳定 row key、完整列和逐行证据；
- 部分修订只重跑受影响子图，聚合/审核/渲染始终重跑。

### 18.3 用户体验测试

- 有模板和无模板请求均可从对话完成澄清和确认；
- 只询问真正影响输出的内容，建议与用户决定可区分；
- “导出当前回答”和“创建新文档”正确分流；
- 刷新、切换会话或从卡片进入 Workbench 后保持同一 task/session/work order；
- 失败卡片给出可执行 next action，不显示矛盾状态。

### 18.4 安全与发布门禁

- 跨 tenant/KB/source snapshot 的证据和审批全部拒绝；
- 模板静态内容、公式、保护区和宏策略无回归；
- generated structure 拒绝脚本、宏、外部关系和未注册组件；
- required coverage、domain critical issue 或 artifact validation 未通过时无法发布；
- 高风险文档无法通过推荐默认值静默跨过 HITL。

### 18.5 ICD 人工标准专项验收

以人工编写的 `600608964_ADAS_ICD_0506_1957_shoulin.wang.xlsx` 为质量基准，对 `Pin Definition` 做规范化对比，不以二进制完全相同为目标：

- connector + pin 的 row-key 覆盖率；
- signal/direction/description 等必填列完整率；
- 重复、漏行、错序和跨 connector 串行；
- 每行证据支持和来源版本一致性；
- 表头、合并区、公式、样式和固定内容保护；
- 未找到数据是否按确认政策标记，而非猜测；
- Reviewer 是否在发布前发现上述差异。

发布门禁至少要求：来源越界为零、固定内容误覆盖为零、必填无证据自动填充为零、预期 row key 未解释缺失为零。Phase 0 必须为每个准备开放的文档类型记录版本化的 precision/recall、人工退回率和时延阈值；没有阈值或未达到阈值时，该类型只能运行 shadow 或强制人工发布。

## 19. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 通用模型演变成不可控万能 Agent | 核心 contracts、registry、工具白名单和确定性 compiler；模型只提案 |
| 无模板版式质量不稳定 | 先 system recipe，后 generated structure；schema、资源上限、视觉回归和格式校验 |
| Planner 漏掉表格行或章节 | CoverageContract 从目标范围生成，Reviewer 对目标全集验收，不从任务结果反推 |
| 并发导致顺序、重复或共享表格冲突 | 稳定 row key、依赖边、barrier、schema-order reducer 和 Receipt |
| Reviewer 与 Writer 共享偏差 | 确定性门禁优先；高风险使用独立 reviewer 配置并保留人工发布 |
| HITL 过多造成体验变差 | 只确认改变结果的决策；高置信度单元自动通过，异常聚合展示 |
| 新旧状态和 API 长期双写漂移 | 单向 projection、版本/hash、shadow 对账和明确退役窗口 |
| 与 conversational export 误路由 | 后端权威路由、单一分流问题、两套不可混用的业务对象和状态机 |
| 迁移范围过大 | 六个阶段独立计划、feature flag、shadow、parity gate 和逐文档类型开放 |

## 20. 实施准备判定

本规格经用户确认后，首先只为 Phase 0–1 编写实施计划；Phase 2–5 在前一阶段 Gate 通过后分别编写计划。这样可以先固化通用需求/计划契约和对话闭环，再迁移执行器，避免同时改写聊天、模板、DAG、renderer 和审批造成不可验证的大爆炸发布。

Phase 0–1 可以开始的条件：

- `OutputSpec` 与现有 GenerationBrief/DocumentTask/Work Order 的兼容关系已确认；
- 对话式导出与 document authoring 路由样例通过产品评审；
- 高风险文档类型和 tenant 默认审批政策有明确配置；
- shadow plan 不改变当前生产 Artifact；
- 所有新增对象具备 tenant 权限、幂等、hash 和保留期规则。

在上述条件未满足前，只允许契约、fixture 和 shadow 观测，不切换新任务执行路径。
