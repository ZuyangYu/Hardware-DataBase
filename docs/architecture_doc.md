# Hardware RAG 当前架构文档

> 版本: v0.1.0
> 更新时间: 2026-07

## 1. 系统定位

Hardware RAG 当前是一个面向硬件设计资料、项目文档和结构化表格的多源问答系统。系统保留“知识库”作为业务隔离、权限治理和资产归属单元，但不再在项目内维护本地向量知识库。

文档检索统一由 RAGFlow 承担；查询编排由 LangGraph agent 承担；Excel 等结构化资产由独立 pipeline 建立结构化索引，并通过 agent tool 参与检索。

## 2. 核心原则

1. RAGFlow 是唯一文档检索后端。
2. LangGraph 只负责查询流程编排，不实现底层检索算法。
3. Agent 最终答案生成使用项目自有 `LLMClient`，不依赖检索框架的全局模型配置。
4. Word/PDF 与 Excel 等多源资产由 pipeline 分别处理，再在 agent 层统一调度。
5. 人工确认是主流程的一部分：先确认问题理解，再确认检索范围。

## 3. 分层架构

```text
Streamlit UI
  |
  v
AppPipeline
  - 认证上下文
  - 知识库治理
  - 文档资产入口
  - 查询入口
  |
  v
MultiSourceAgentRunner
  - LangGraph thread/session
  - 人工确认状态
  - Tool adapter 调度
  - 答案生成与 trace 输出
  |
  v
LangGraph Query Graph
  - analyze_question
  - confirm_question
  - scan_kb_catalog
  - plan_source_selection
  - confirm_sources
  - retrieve_evidence
  - score_and_compare_evidence
  - judge_sufficiency
  - compose_answer
  - verify_grounding
  |
  v
Tools / Services
  - RAGFlow document retrieval
  - Spreadsheet semantic / cell / profile search
  - Pipeline document catalog
  |
  v
Storage / External Systems
  - RAGFlow datasets
  - SQLite auth / logs / document ledger
  - Spreadsheet structured indexes
  - Pipeline archive files
```

## 4. 主要模块

### AppPipeline

位置: `src/core/app_pipeline.py`

职责:

- 初始化 RAGFlow backend、DocumentManager 和 MultiSourceAgentRunner。
- 提供 Streamlit 调用的统一入口。
- 执行知识库创建、删除、列表、文件上传、文件删除、查询等应用级操作。
- 将权限上下文传递给 backend 和 agent。

### RAGFlowBackend

位置: `src/pipelines/document_rag/ragflow_backend.py`

职责:

- 上传普通文档到 RAGFlow。
- 查询 RAGFlow retrieval API。
- 映射业务知识库、部门、source group 与 RAGFlow dataset/document。
- 管理解析任务、删除文档、删除知识库相关外部资产。

### MultiSourceAgentRunner

位置: `src/agents/runner.py`

职责:

- 驱动 LangGraph 查询流程。
- 保存和恢复人工确认状态。
- 扫描知识库文件目录。
- 统一调用文档检索 tool 和表格检索 tool。
- 使用 `LLMClient` 生成最终答案。

### LLMClient

位置: `src/core/llm_client.py`

职责:

- 封装 agent 最终答案生成模型。
- 支持 Ollama 和 OpenAI-compatible API。
- 与 RAGFlow、LangGraph、LlamaIndex 等检索/编排框架解耦。

### Spreadsheet Pipeline

位置: `src/pipelines/spreadsheet/`

职责:

- 解析 `.xlsx` 文件。
- 保存 sheet、row、cell、semantic row、profile 等结构化索引。
- 向 agent tool 提供语义行检索、单元格检索和表格 profile 查询。

## 5. 查询流程

```text
用户提问
  -> analyze_question
  -> 用户确认问题理解
  -> scan_kb_catalog
  -> plan_source_selection
  -> 用户确认检索范围
  -> retrieve_evidence
  -> merge_evidence
  -> score_and_compare_evidence
  -> judge_sufficiency
  -> 必要时补检索
  -> compose_answer
  -> verify_grounding
  -> 返回答案和 Agent Trace
```

## 6. 数据流

### 上传

```text
Streamlit upload
  -> AppPipeline.upload_files
  -> DocumentManager
  -> IngestionOrchestrator
  -> PipelineRegistry 路由
     - document_rag: RAGFlow
     - spreadsheet: Spreadsheet pipeline
  -> PipelineDocumentStore 记录台账
```

### 检索

```text
Agent source plan
  -> DocumentRAGTool
     -> RAGFlowBackend.retrieve
  -> Spreadsheet tools
     -> SpreadsheetIndexService
  -> evidence merge / coverage judge
```

## 7. 配置

核心配置集中在 `config/settings.py`:

```env
RAGFLOW_BASE_URL=http://localhost:9380
RAGFLOW_API_KEY=
RAGFLOW_GOVERNANCE_DATASET_NAME=department_governance
RAGFLOW_DESIGN_DATASET_NAME=project_design_assets
RAGFLOW_TIMEOUT_SECONDS=120
RAGFLOW_SIMILARITY_THRESHOLD=0.25
RAGFLOW_VECTOR_WEIGHT=0.4

AGENT_LLM_PROVIDER=ollama
AGENT_OLLAMA_BASE_URL=http://localhost:11434
AGENT_OLLAMA_MODEL=qwen2.5:32b

AGENT_CUSTOM_API_KEY=
AGENT_CUSTOM_BASE_URL=
AGENT_CUSTOM_MODEL=
AGENT_CUSTOM_MAX_TOKENS=4096
AGENT_TEMPERATURE=0.2
AGENT_TIMEOUT_SECONDS=120

FINAL_TOP_K=5
AGENT_MAX_RETRIEVAL_ROUNDS=3
```

## 8. 已移除内容

以下旧链路不再属于当前项目运行时:

- 本地向量知识库
- LlamaIndex 检索和生成封装
- 项目内 Chroma/DocStore 管理
- 项目内 BM25/RRF/hybrid retriever
- 旧 `src/rag_backends/local_backend.py`
- 旧 `src/core/rag_pipeline.py`
- 旧 `src/ingestion/index_builder.py`

## 9. 验收重点

- 查询前必须有问题理解确认。
- 检索前必须展示建议检索文件和原因。
- 文档证据来自 RAGFlow。
- Excel 证据来自 spreadsheet pipeline。
- 最终答案必须基于 evidence，并能说明缺失信息。
- 代码中不应重新引入本地向量检索或 LlamaIndex 依赖。
