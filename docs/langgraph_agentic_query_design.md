# LangGraph 多源 Agentic 查询编排设计

> 目标：在现有 Hardware RAG 的多 pipeline 基础上，引入 LangGraph 作为查询流程编排层，实现“问题拆解确认 → 知识库文件扫描 → 检索范围确认 → 多源检索 → 证据覆盖度评估 → 自动补检索 → grounded answer”的可控问答流程。

## 1. 背景与核心问题

当前项目已经具备多源数据处理基础：

- `document_rag` pipeline 处理 `.doc/.docx/.pdf` 文档。
- `spreadsheet` pipeline 处理 `.xlsx`，并落入结构化表格索引。
- `PipelineRegistry` 负责文件类型到 pipeline 的声明。
- `IngestionOrchestrator` 负责上传、归档、去重、handler 分发。
- `PipelineDocumentStore` 保存文档台账、处理状态、processor kind、dataset kind。
- 文档检索后端固定为 RAGFlow；旧的本地向量知识库和 LlamaIndex 检索链路已移除。

但查询侧目前仍偏向“一次性 RAG”：

```text
用户问题
  -> 文档检索
  -> 拼上下文
  -> LLM 生成
```

这个链路的问题是：

1. 没有先确认用户问题理解是否正确。
2. 没有先扫描当前知识库中实际挂载了哪些文件。
3. 没有让用户确认“应该查哪些文件以及为什么”。
4. 文档 RAG 和 Excel 结构化索引没有被统一调度。
5. 检索结果没有按子问题做覆盖度分析。
6. 缺失证据时不能自动、有目标地补检索。

因此需要新增一个查询编排层。

## 2. 设计结论

LangGraph 只负责查询流程编排，不负责底层检索。

```text
LangGraph 负责：
  - 问题分析
  - 人工确认
  - 知识库目录扫描
  - 多源检索计划
  - 多轮检索状态
  - 证据覆盖度判断
  - 是否补检索
  - 最终答案生成与校验流程

LangGraph 不负责：
  - RAGFlow 内部向量检索实现
  - Excel SQL 查询细节
  - 文件解析
  - 文档上传/归档/去重
  - 权限判断本身
```

底层能力继续由现有 pipeline 和 service 提供。LangGraph 只调用 tool adapter。

## 3. 分层架构

```text
streamlit_app.py
  |
  v
AppPipeline
  - 认证上下文
  - KB/session
  - 应用入口
  |
  v
MultiSourceAgentRunner
  - 创建/恢复 LangGraph thread
  - 处理 interrupt/resume
  - 把 graph 事件转成 UI 可展示内容
  |
  v
LangGraph Query Workflow
  - analyze_questiono
  - human_confirm_questin
  - scan_kb_catalog
  - plan_source_selection
  - human_confirm_sources
  - retrieve_evidence
  - score_and_compare_evidence
  - judge_sufficiency
  - compose_answer
  - verify_grounding
  |
  v
Tool Adapters
  - DocumentRAGTool
  - SpreadsheetSemanticTool
  - SpreadsheetCellTool
  - SpreadsheetProfileTool
  - PipelineCatalogTool
  |
  v
Existing Backends / Stores
  - RAGBackend.retrieve()
  - TableIndexStore
  - SpreadsheetIndexService
  - PipelineDocumentStore
  - DocumentArchiveManager
```

## 4. 推荐目录结构

```text
src/agents/
  __init__.py
  state.py
  graph.py
  runner.py
  prompts.py
  nodes/
    __init__.py
    analyze_question.py
    scan_catalog.py
    plan_sources.py
    retrieve.py
    evidence_scoring.py
    sufficiency.py
    answer.py
    verify.py
  tools/
    __init__.py
    base.py
    document_rag_tool.py
    spreadsheet_semantic_tool.py
    spreadsheet_cell_tool.py
    spreadsheet_profile_tool.py
    pipeline_catalog_tool.py
```

## 5. 主流程

```text
START
  |
  v
analyze_question
  |
  v
human_confirm_question
  |
  v
scan_kb_catalog
  |
  v
plan_source_selection
  |
  v
human_confirm_sources
  |
  v
retrieve_evidence
  |
  v
score_and_compare_evidence
  |
  v
judge_sufficiency
  |\
  | \-- insufficient --> replan_followup_queries
  |                         |
  |                         v
  |                    retrieve_evidence
  |
  \-- sufficient ----> compose_answer
                            |
                            v
                       verify_grounding
                            |
                            v
                           END
```

可选增强：

```text
verify_grounding
  -> human_confirm_answer
  -> END
```

## 6. 关键节点设计

### 6.1 analyze_question

职责：理解用户真实意图，并拆解成可验证的子问题。

输入：

- 原始问题
- 最近对话历史
- 当前 KB 名称

输出：

- 问题理解摘要
- 子问题列表
- 关键实体
- 需要的证据类型
- 初步风险提示

示例输出：

```json
{
  "intent": "cross_source_component_analysis",
  "summary": "用户想了解 TPS5430 在项目中的用量、替代料以及设计注意事项。",
  "entities": ["TPS5430"],
  "sub_questions": [
    {
      "id": "sq_1",
      "question": "TPS5430 在 BOM 中的用量是多少？",
      "expected_evidence": ["spreadsheet_table"]
    },
    {
      "id": "sq_2",
      "question": "TPS5430 是否有替代料？",
      "expected_evidence": ["spreadsheet_table", "document_text"]
    },
    {
      "id": "sq_3",
      "question": "TPS5430 的设计注意事项是什么？",
      "expected_evidence": ["document_text"]
    }
  ],
  "needs_user_confirmation": true
}
```

### 6.2 human_confirm_question

职责：暂停 graph，等待用户确认问题理解。

用户可选操作：

- `approve`：确认问题拆解。
- `edit`：修改问题理解或子问题。
- `cancel`：取消本轮查询。

推荐 UI 文案：

```text
我理解你要查：
1. TPS5430 在 BOM 中的用量
2. TPS5430 是否有替代料
3. TPS5430 的设计注意事项

是否按这个方向扫描知识库？
```

### 6.3 scan_kb_catalog

职责：扫描当前用户可访问 KB 中已经挂载的文件和结构化产物。

数据来源：

- `PipelineDocumentStore`
- `RAGBackend.list_documents()`
- `DocumentArchiveManager`
- `SpreadsheetIndexService.get_document_profile()`

需要收集：

- 文件名
- 原始文件名
- processor kind
- content kind
- dataset kind
- source group
- 解析状态
- 本地归档路径
- 文档大小
- 上传时间/更新时间
- Excel sheet/profile 摘要

示例 catalog item：

```json
{
  "record_id": 12,
  "document_name": "Project_BOM.xlsx",
  "processor_kind": "spreadsheet_table",
  "content_kind": "spreadsheet_table",
  "source_group": "material",
  "status": "indexed",
  "local_path": "material/Project_BOM.xlsx",
  "profile": {
    "sheet_count": 3,
    "sheets": [
      {
        "sheet_name": "BOM",
        "row_count": 238,
        "semantic_row_count": 220,
        "headers": ["MPN", "Manufacturer", "Quantity", "Alternative"]
      }
    ]
  }
}
```

### 6.4 plan_source_selection

职责：根据问题拆解和 catalog，生成“查哪些文件、为什么查、怎么查”的计划。

输出必须面向用户可读，因为下一步要人工确认。

示例：

```json
{
  "source_plan": [
    {
      "source_name": "Project_BOM.xlsx",
      "processor_kind": "spreadsheet_table",
      "reason": "该文件是 BOM 表，包含 MPN、用量、替代料字段，适合回答 TPS5430 的用量和替代料。",
      "tool_calls": [
        {
          "tool_name": "spreadsheet_semantic",
          "query": "TPS5430 MPN quantity alternative 替代料 用量",
          "top_k": 10
        },
        {
          "tool_name": "spreadsheet_cell",
          "query": "TPS5430",
          "top_k": 20
        }
      ]
    },
    {
      "source_name": "Power_Design_Guide.docx",
      "processor_kind": "ragflow",
      "reason": "该文档属于设计说明，适合查 TPS5430 的 layout、输入输出电容、热设计等注意事项。",
      "tool_calls": [
        {
          "tool_name": "document_rag",
          "query": "TPS5430 design guideline layout thermal capacitor 注意事项",
          "top_k": 8
        }
      ]
    }
  ],
  "skipped_sources": [
    {
      "source_name": "Meeting_Minutes.docx",
      "reason": "文件更像项目会议纪要，和料号用量/设计注意事项关联较弱。"
    }
  ]
}
```

### 6.5 human_confirm_sources

职责：暂停 graph，等待用户确认检索范围。

用户可选操作：

- `approve`：按计划检索。
- `edit`：修改 query、top_k、文件范围。
- `add_source`：指定额外文件。
- `remove_source`：排除某些文件。
- `cancel`：取消本轮查询。

这是系统最重要的人工确认点。

### 6.6 retrieve_evidence

职责：执行已批准的工具调用。

规则：

- 只调用 tool adapter。
- 不在 graph node 中写底层检索细节。
- 每个 tool 返回统一 `Evidence`。
- tool 内部必须遵守 `RequestContext` 权限边界。

多源工具：

```text
DocumentRAGTool
  -> RAGBackend.retrieve()

SpreadsheetSemanticTool
  -> table_semantic_rows.semantic_text

SpreadsheetCellTool
  -> table_cells.value/header/raw_value

SpreadsheetProfileTool
  -> table_sheets/profile

PipelineCatalogTool
  -> PipelineDocumentStore
```

### 6.7 score_and_compare_evidence

职责：根据子问题对证据进行覆盖度分析。

输出 Coverage Matrix：

```json
{
  "coverage": [
    {
      "sub_question_id": "sq_1",
      "sub_question": "TPS5430 在 BOM 中的用量是多少？",
      "coverage_score": 0.92,
      "status": "covered",
      "supporting_evidence_ids": ["xlsx_12_BOM_31"],
      "missing": []
    },
    {
      "sub_question_id": "sq_2",
      "sub_question": "TPS5430 是否有替代料？",
      "coverage_score": 0.45,
      "status": "partial",
      "supporting_evidence_ids": ["xlsx_12_BOM_31"],
      "missing": ["替代料字段为空或未检出"]
    },
    {
      "sub_question_id": "sq_3",
      "sub_question": "TPS5430 的设计注意事项是什么？",
      "coverage_score": 0.35,
      "status": "weak",
      "supporting_evidence_ids": ["doc_7_chunk_18"],
      "missing": ["缺少 layout/thermal 明确描述"]
    }
  ],
  "conflicts": [],
  "recommended_followups": [
    {
      "reason": "替代料信息不足",
      "tool_name": "spreadsheet_semantic",
      "query": "TPS5430 alternative substitute compatible 替代 兼容",
      "top_k": 10
    },
    {
      "reason": "设计注意事项证据较弱",
      "tool_name": "document_rag",
      "query": "TPS5430 layout thermal input capacitor output capacitor design note",
      "top_k": 8
    }
  ]
}
```

### 6.8 judge_sufficiency

职责：判断是否可以回答。

判断维度：

- 每个子问题是否覆盖。
- 关键事实是否有来源。
- 多源证据是否冲突。
- 是否缺少用户明确要求的信息。
- 是否达到最大补检索轮数。

状态：

```text
sufficient
partial_but_answerable
insufficient_need_more
need_user_clarification
```

### 6.9 replan_followup_queries

职责：针对缺口生成下一轮检索计划。

约束：

- 只针对缺口补查。
- 不重复上一轮完全相同 query。
- 默认最多补检索 2 轮。
- 如果补查仍无结果，要在最终答案里说明缺失项。

### 6.10 compose_answer

职责：基于证据生成最终答案。

要求：

- 只基于已采纳 evidence。
- 区分“证据明确说明”和“基于证据推断”。
- 明确标注来源。
- 对缺失信息直接说明。
- 对冲突信息并列说明，不强行合并。

### 6.11 verify_grounding

职责：检查答案中的关键结论是否有 evidence 支撑。

输出：

```json
{
  "grounded": true,
  "unsupported_claims": [],
  "weak_claims": [
    {
      "claim": "TPS5430 需要重点关注散热",
      "reason": "文档只提到布局注意事项，未明确给出热设计限制。"
    }
  ],
  "citation_coverage": 0.87
}
```

如果发现 unsupported claims，应返回 `compose_answer` 重写。

## 7. State Schema

建议使用 `TypedDict` 或 Pydantic model。LangGraph state 内部可以保持 dict，但边界输入输出建议用 Pydantic 校验。

```python
class AgentState(TypedDict):
    thread_id: str
    kb_name: str
    user_query: str
    history: list[tuple[str, str]]
    ctx: dict

    question_analysis: dict
    question_approval: dict | None

    catalog: dict
    source_plan: dict
    source_approval: dict | None

    retrieval_round: int
    evidence: list[dict]
    merged_evidence: list[dict]
    coverage_matrix: dict
    sufficiency: dict

    answer: str
    verification: dict

    pending_human_action: dict | None
    trace: list[dict]
```

## 8. Evidence Schema

统一所有来源的证据格式。

```python
class Evidence(BaseModel):
    id: str
    content: str
    source_name: str
    content_kind: str
    processor_kind: str
    score: float = 0.0
    locator: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
```

示例：文档证据

```json
{
  "id": "doc_7_chunk_18",
  "content": "TPS5430 layout should keep the input capacitor close to VIN and GND...",
  "source_name": "Power_Design_Guide.docx",
  "content_kind": "document_text",
  "processor_kind": "ragflow",
  "score": 0.81,
  "locator": {
    "chunk_id": "chunk_18",
    "page": 12
  },
  "metadata": {
    "source_group": "design"
  }
}
```

示例：Excel 证据

```json
{
  "id": "xlsx_12_BOM_31",
  "content": "MPN: TPS5430; Quantity: 2; Manufacturer: TI; Alternative: TPS54331",
  "source_name": "Project_BOM.xlsx",
  "content_kind": "spreadsheet_table",
  "processor_kind": "spreadsheet_table",
  "score": 0.94,
  "locator": {
    "record_id": 12,
    "sheet_name": "BOM",
    "row_index": 31
  },
  "metadata": {
    "headers": ["MPN", "Quantity", "Manufacturer", "Alternative"]
  }
}
```

## 9. Tool Adapter 设计

### 9.1 Base Interface

```python
class AgentTool(Protocol):
    name: str
    description: str

    def run(
        self,
        query: str,
        kb_name: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        ...
```

### 9.2 DocumentRAGTool

实现：

```text
DocumentRAGTool.run()
  -> rag_backend.retrieve(kb_name, query, top_k, ctx)
  -> map schemas.Evidence to agents.Evidence
```

### 9.3 SpreadsheetSemanticTool

查询：

- `table_semantic_rows.semantic_text`
- `table_semantic_rows.raw_text`
- `table_semantic_rows.values_json`

适合：

- BOM 行级事实
- 参数表
- 供应商/替代料
- 测试矩阵

### 9.4 SpreadsheetCellTool

查询：

- `table_cells.value`
- `table_cells.raw_value`
- `table_cells.header`

适合：

- 精确料号
- 单元格级 lookup
- 某列值过滤

### 9.5 SpreadsheetProfileTool

查询：

- workbook/sheet profile
- header row
- sheet names
- row/column count
- semantic row count

适合：

- 计划阶段判断哪个 sheet 值得查
- 用户确认时展示表格概况

### 9.6 PipelineCatalogTool

查询：

- `PipelineDocumentStore`
- `RAGBackend.list_documents()`
- archive metadata

适合：

- scan_kb_catalog
- 展示当前 KB 文件清单
- 过滤未解析/失败/过期文件

## 10. AppPipeline 集成

当前：

```text
AppPipeline.query()
  -> backend.stream_answer()
```

目标：

```text
AppPipeline.query()
  -> MultiSourceAgentRunner.stream()
```

示例：

```python
class AppPipeline:
    def __init__(self):
        self.backend = create_rag_backend()
        self.documents = DocumentManager(self.backend)
        self.agent = MultiSourceAgentRunner(
            rag_backend=self.backend,
            document_store=PipelineDocumentStore(),
            spreadsheet_service=SpreadsheetIndexService(),
        )

    def query(self, msg, kb_name, history, ctx=None):
        yield from self.agent.stream(
            query=msg,
            kb_name=kb_name,
            history=history,
            ctx=ctx,
        )
```

后续 `RAGBackend.stream_answer()` 可以保留为兼容路径或降级路径，但不再作为主查询编排入口。

## 11. Streamlit 交互设计

需要在 `st.session_state` 中保存：

```text
agent_thread_id
agent_pending_action
agent_pending_payload
agent_last_trace
```

当 graph interrupt 时，UI 不应把它当错误，而应展示确认面板。

### 11.1 问题确认面板

展示：

- 问题理解摘要
- 子问题
- 关键实体
- 需要的证据类型

操作：

- 确认
- 修改
- 取消

### 11.2 文件检索范围确认面板

展示：

- 建议检索文件
- 每个文件的 processor kind
- 每个文件的原因
- 将执行的 tool query
- 被跳过文件及原因

操作：

- 确认
- 删除某文件
- 添加某文件
- 修改 query
- 取消

### 11.3 检索过程展示

展示：

- 当前轮次
- 正在查哪个 tool
- 命中证据数量
- 覆盖度矩阵

### 11.4 最终答案展示

展示：

- 答案正文
- 来源引用
- 缺失项
- grounding verification 结果

## 12. Checkpoint 与 Resume

每个查询 thread 使用稳定 ID：

```text
thread_id = chat_session_id + ":" + kb_name
```

checkpoint 存储建议：

```text
storage/agent_checkpoints/
```

后续更推荐 SQLite：

```text
storage/agent_state.db
```

resume 输入示例：

```json
{
  "action": "approve",
  "edited_payload": null
}
```

或：

```json
{
  "action": "edit",
  "edited_payload": {
    "source_plan": [...]
  }
}
```

## 13. 最大轮次与安全阈值

默认建议：

```text
MAX_RETRIEVAL_ROUNDS = 3
MAX_TOOL_CALLS_PER_ROUND = 8
MAX_EVIDENCE_PER_TOOL = 10
MAX_FINAL_EVIDENCE = 30
MIN_COVERAGE_TO_ANSWER = 0.70
MIN_GROUNDING_COVERAGE = 0.80
```

当达到最大补检索轮次仍不足时：

- 不继续查。
- 输出已覆盖信息。
- 明确列出缺失信息。
- 建议用户补充文件或扩大检索范围。

## 14. 与 Google Agentic RAG 思路的对应关系

本设计吸收的核心点：

```text
1. 不把检索当一次性动作，而是计划驱动的循环。
2. 先理解问题，再决定查什么。
3. 检索后判断上下文是否足够。
4. 不足时针对缺口补检索。
5. 最终答案必须被 evidence 支撑。
```

本项目额外增加：

```text
1. 问题理解人工确认。
2. 知识库文件扫描后再决定查哪些文件。
3. 检索范围人工确认。
4. Word/PDF 与 Excel 结构化索引同级调度。
5. 用 coverage matrix 显式判断证据覆盖。
```

## 15. 迁移计划

### Phase 1: 骨架与 Catalog

- 新增 `src/agents/state.py`
- 新增 `src/agents/tools/base.py`
- 新增 `PipelineCatalogTool`
- 新增 LangGraph skeleton
- AppPipeline 保持原查询路径不变，通过 feature flag 开启 agent query

### Phase 2: 问题确认与文件范围确认

- 实现 `analyze_question`
- 实现 `human_confirm_question`
- 实现 `scan_kb_catalog`
- 实现 `plan_source_selection`
- 实现 `human_confirm_sources`
- Streamlit 增加 pending approval UI

### Phase 3: 多源检索工具

- 实现 `DocumentRAGTool`
- 实现 `SpreadsheetSemanticTool`
- 实现 `SpreadsheetCellTool`
- 实现 `SpreadsheetProfileTool`
- 统一 Evidence schema

### Phase 4: 证据覆盖度与补检索

- 实现 `score_and_compare_evidence`
- 实现 `judge_sufficiency`
- 实现 `replan_followup_queries`
- 增加最大轮次和去重策略

### Phase 5: 答案生成与校验

- 实现 `compose_answer`
- 实现 `verify_grounding`
- UI 展示 coverage matrix 和引用来源

### Phase 6: 已完成的本地检索链路收敛

Agent 层不依赖 LlamaIndex，项目内也不再保留本地向量知识库检索后端。文档检索统一走 RAGFlow；模型生成统一走项目自有 `LLMClient`；Excel 等结构化数据由 pipeline service 暴露为 agent tool。

## 16. 验收标准

1. 用户提问后，系统先展示问题理解，并等待确认。
2. 用户确认后，系统扫描当前 KB 文件清单。
3. 系统展示建议检索的文件和原因，并等待确认。
4. 用户确认后，系统才开始检索。
5. 检索覆盖 Word/PDF 和 Excel 两类 evidence source。
6. 系统能展示每个子问题的覆盖度。
7. 证据不足时，系统能自动补检索。
8. 最终答案包含来源，并明确说明缺失项。
9. LangGraph 不直接写底层检索逻辑，只调用 tool adapter。
10. 旧的本地 backend 查询路径不再保留。
