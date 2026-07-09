# 电路查询增量接入 develop 架构设计

更新日期：2026-07-09

## 目标

将 `composite-query-angent` 中的电路设计查询能力，作为增量功能接入当前 `develop` 架构。接入过程必须以 develop 的 pipeline 和 LangGraph agent 模型作为架构事实来源。

## 非目标

- 不恢复 `src/query_router/*` 作为顶层查询路径。
- 不恢复 `src/core/rag_pipeline.py` 或旧的 `src/rag_backends/*` 应用结构。
- 不让电路查询成为独立于 `MultiSourceAgentRunner` 的另一个顶层 router。
- 不重新引入旧 Streamlit router 控制项，例如 `QUERY_ROUTER_USE_LLM`。
- 在电路索引和 `circuit_query` 尚未返回真实证据前，不宣称已经具备完整的电路/手册复合问答能力。

## 当前状态

当前集成工作区已经遵循新的 develop 方向：

- `src/core/app_pipeline.py` 调用 `MultiSourceAgentRunner`。
- `src/agents/*` 负责多源查询编排。
- `src/pipelines/*` 负责摄入和结构化 pipeline 路由。
- `src/pipelines/registry.py` 为 `.edf` 和 `.edif` 注册了 `circuit_design`。
- `src/agents/runner.py` 注册了 `circuit_query` 工具。

剩余缺口是功能性的：`src/agents/tools/circuit_tools.py` 当前返回空证据列表。`CircuitPipelineHandler` 会归档电路文件并写入 ledger 记录，但不会解析、索引或暴露结构化电路证据。

## 设计决策

采用分阶段集成：

1. 先打通最小可用的“电路上传 -> 解析/索引 -> 查询 -> 证据”闭环。
2. 在这个闭环背后，逐步迁移 feature 分支中可复用的电路领域模块。
3. 将复合多源行为迁入现有 LangGraph planner，而不是恢复旧 query router。

这样可以让电路查询成为同级的结构化 pipeline 和 agent tool，而不是一套竞争性架构。

## 架构

```text
streamlit_app.py
  -> AppPipeline
  -> IngestionOrchestrator
  -> CircuitPipelineHandler
  -> CircuitIndexService
       -> circuit parsers / store / query engine

streamlit_app.py
  -> AppPipeline.query()
  -> MultiSourceAgentRunner
  -> LangGraph workflow
  -> CircuitQueryTool
  -> CircuitIndexService.query()
  -> list[Evidence]
```

关键边界是 `CircuitIndexService`。它负责把电路领域代码适配到 develop 的 pipeline 和 agent 接口。

## 组件

### CircuitIndexService

创建一个 service 模块，优先放在 `src/circuit/index_service.py`，并提供小而稳定的公开 API：

```python
class CircuitIndexService:
    def index_file(
        self,
        *,
        kb_name: str,
        record_id: int | None,
        file_path: str,
        original_name: str,
        department_id: str | None = None,
        uploaded_by: str = "",
    ) -> CircuitIndexResult:
        ...

    def query(
        self,
        *,
        kb_name: str,
        query: str,
        ctx: RequestContext | None,
        top_k: int = 5,
        filters: dict | None = None,
    ) -> list[Evidence]:
        ...

    def delete_record(self, record: PipelineDocumentRecord) -> None:
        ...
```

`CircuitIndexService` 负责：

- 为 `.edf` / `.edif` 选择正确 parser；
- 将解析后的电路状态写入带作用域的 circuit store；
- 保留 `kb_name`、`department_id`、源文件、record id 和 provenance；
- 将 query engine 结果映射为 `src.agents.state.Evidence`；
- 对 pipelines 和 agents 隐藏旧 feature 分支的实现细节。

### CircuitPipelineHandler

修改 `src/pipelines/ingestion.py`，让 `CircuitPipelineHandler` 接收 `CircuitIndexService`，或接收来自 `runtime_factory` 的回调。

Phase 1 可以在归档和 ledger 写入后同步解析，以满足最小可用路径。如果后续解析耗时变长，保留相同 handler 接口，再把执行迁移到 worker 背后。

预期记录状态：

- `archived`：源文件已存储并写入 ledger。
- `indexed`：结构化电路状态已解析且可查询。
- `failed`：parser 或索引失败，并填充 `error_message`。

该 handler 不能把 `.edf` / `.edif` 上传到 RAGFlow。

### CircuitQueryTool

替换 `src/agents/tools/circuit_tools.py` 中当前的空实现。

该工具应当：

- 接受现有 `run(query, kb_name, ctx, top_k, filters)` 签名；
- 调用 `CircuitIndexService.query`；
- 返回 `list[Evidence]`；
- 只有在不存在已索引电路证据时才返回空列表；
- 将工具失败作为异常抛出，使 `retrieve_evidence` diagnostics 能把该 tool call 标记为 failed。

### LangGraph 多源 Planner

继续让 `src/agents/graph.py` 作为唯一顶层查询 workflow。

当子问题期望 `circuit_design` 证据时，planner 应继续生成 `circuit_query` 调用。多源问题应表示为多个 tool calls：

- 实际连接、网络、位号、模块事实 -> `circuit_query`；
- 设计手册和说明文本 -> `document_rag`；
- BOM、数量、替代料、测试矩阵 -> `spreadsheet_semantic` 或 `spreadsheet_cell`。

最终 synthesis 仍保留在 develop 的 agent answer 路径中，使用电路证据以及文档、表格证据共同生成回答。

### 迁移的电路模块

当旧模块服务于新的 service 边界时，允许从 `composite-query-angent` 迁移 `src/circuit/*` 下的可复用模块：

- parsers 和 models；
- circuit store；
- query engine；
- entity resolver；
- recovery helpers；
- response policy helpers；
- 电路领域 query agent 内部逻辑，但只能通过 `CircuitIndexService` / `CircuitQueryTool` 调用。

不要迁移 `src/query_router/*`。`CompositeQueryAgent` 中值得保留的逻辑，应转译为 LangGraph planner 行为或 prompt 规则。

## 数据流

### 摄入

1. 用户上传 `.edf` 或 `.edif`。
2. `PipelineRegistry.route_file()` 选择 `processor_kind="circuit_design"`。
3. `IngestionOrchestrator` 归档文件。
4. `CircuitPipelineHandler.submit()` 写入电路 ledger 记录。
5. `CircuitIndexService.index_file()` 解析并存储结构化电路数据。
6. Handler 将进度和状态更新为 `indexed` 或 `failed`。

### 查询

1. 用户提出电路问题或复合问题。
2. `MultiSourceAgentRunner` 运行 LangGraph workflow。
3. Planner 针对电路证据需求包含 `circuit_query`。
4. `retrieve_evidence` 调用 `CircuitQueryTool`。
5. `CircuitQueryTool` 返回标准化的 `Evidence`。
6. 现有评分、充分性判断、补充检索规划和最终回答 synthesis 使用这些 evidence。

## Evidence 映射

电路证据必须使用和其他工具相同的模型：

```python
Evidence(
    id="circuit:<record_id>:<stable_locator>",
    content="<human-readable grounded circuit fact>",
    source_name="<original EDF/EDIF filename>",
    content_kind="circuit_design",
    processor_kind="circuit_design",
    score=<float>,
    locator={
        "record_id": record_id,
        "circuit_id": circuit_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
    },
    metadata={
        "kb_name": kb_name,
        "department_id": department_id,
        "source_group": source_group,
    },
)
```

Evidence 内容必须直接基于已解析的电路数据。LLM synthesis 可以重写最终回答，但不应编造电路事实。

## 错误处理

- Parser 失败时，将 pipeline record 更新为 `failed`，并存储异常摘要。
- 查询没有已索引电路的 KB 时返回无证据，而不是崩溃。
- 使用无效 source filter 查询时返回无证据。
- 未预期的 service 错误向上传递给 `retrieve_evidence`，由现有逻辑记录 failed tool diagnostics。
- 删除操作应尽力执行，并按现有 handler result 风格报告清理失败。

## 测试

Phase 1 的最小测试：

- routing：`.edf` 和 `.edif` 映射到 `circuit_design`；
- ingestion：`CircuitPipelineHandler.submit()` 调用 index service 并更新状态；
- ingestion failure：parser/index error 将记录标记为 failed 并写入错误消息；
- query tool：已索引 fixture 数据返回非空 `Evidence`；
- query empty：没有已索引电路数据时返回 `[]`；
- cleanup：删除电路记录会调用 circuit index cleanup；
- agent integration：`circuit_design` source plan 会触发 `circuit_query`。

现有 `.gitignore` 中忽略 `tests/` 的规则必须修正，确保新增电路测试会被提交并在 CI 中运行。

## 验证

在已安装项目依赖的环境中，Phase 1 满足要求的前提是以下命令通过：

```text
python -m unittest discover -s tests -p "test_circuit_*.py" -v
python -m unittest tests.test_agentic_runner -v
python -m py_compile src\core\app_pipeline.py src\agents\tools\circuit_tools.py src\agents\runner.py src\agents\graph.py
```

如果缺少 `langgraph`，必须先修复依赖安装，再声明完成完整 agent 验证。

## 迁移计划

### Phase 1：最小工作闭环

- 添加 `CircuitIndexService`。
- 将 `CircuitPipelineHandler` 接入索引。
- 替换空的 `CircuitQueryTool` 实现。
- 添加可提交的测试和 `.gitignore` 测试例外。
- UI 变更限制在现有 pipeline 状态展示范围内。

### Phase 2：电路查询质量增强

- 将 feature 分支中选定的 `src/circuit/*` 模块迁移到 `CircuitIndexService` 背后。
- 添加实体解析、作用域搜索和 recovery 行为。
- 只有当电路 session context 能以现有 chat/session context 为 key，且不会新增顶层 router 时，才保留该能力。

### Phase 3：复合多源行为

- 将 `CompositeQueryAgent` 中有价值的行为转译为 LangGraph planning prompts 和 source selection。
- 让电路/手册/BOM 复合问题在同一检索轮次中产生多个 tool calls。
- 改进 sufficiency checks，使缺失电路证据和缺失文档或表格证据能够分别判断。

## 验收标准

- `develop` 架构仍以 `src/pipelines/*` 和 `src/agents/*` 为中心。
- `.edf/.edif` 文件成为可查询的结构化电路记录。
- `CircuitQueryTool.run()` 能为已索引电路数据返回真实 `Evidence`。
- 复合问题使用现有 LangGraph runner 和多个 tools。
- 生产路径不导入 `src/query_router/*`。
- 新增电路测试被 git 跟踪，并可在 CI 中运行。
- 当使用电路证据时，最终回答包含电路来源。
