# Hardware DataBase

Hardware DataBase 是一个面向硬件设计资料、项目文档和结构化表格的智能数据基座。当前版本采用 **RAGFlow 作为唯一文档检索后端**，使用 **LangGraph Agent** 编排查询流程，并通过独立 pipeline 处理 Word/PDF/Excel 等多源资产。

本项目已移除旧的本地向量知识库、LlamaIndex、本地 BM25/Chroma 检索链路。业务上的“知识库”仍保留，用于权限、文件台账、RAGFlow dataset 映射和多源资产治理。

## 核心特性

- **Agentic 查询流程**：问题拆解确认、文件扫描、检索范围确认、多源检索、证据覆盖度判断、自动补检索、最终 grounded answer。
- **多源 pipeline**：普通文档进入 RAGFlow 检索链路；Excel 进入结构化表格索引，并由 agent 统一调度。
- **RAGFlow 后端固定化**：文档上传、解析任务、检索、删除和知识库治理都通过 RAGFlow 后端适配层完成。
- **权限与治理**：支持部门、用户、知识库权限、审计日志、查询 trace 和文件处理状态。
- **独立模型配置**：Agent 最终答案生成使用项目自有 `LLMClient`，支持 Ollama 或 OpenAI-compatible API，不再复用旧 RAG 框架模型封装。

## 项目结构

```text
Hardware-RAG/
├── config/
│   └── settings.py
├── docs/
│   ├── langgraph_agentic_query_design.md
│   └── pipeline_contract.md
├── src/
│   ├── agents/
│   │   ├── graph.py
│   │   ├── runner.py
│   │   └── tools/
│   ├── core/
│   │   ├── app_pipeline.py
│   │   ├── auth.py
│   │   ├── llm_client.py
│   │   └── app_logs.py
│   ├── pipelines/
│   │   ├── document_rag/
│   │   └── spreadsheet/
│   └── services/
├── streamlit_app.py
├── pyproject.toml
└── requirements.txt
```

## 安装

```bash
uv sync
uv run streamlit run streamlit_app.py
```

也可以使用 pip：

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 关键配置

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

# 或使用 OpenAI-compatible API
AGENT_LLM_PROVIDER=custom
AGENT_CUSTOM_API_KEY=
AGENT_CUSTOM_BASE_URL=https://api.openai.com/v1
AGENT_CUSTOM_MODEL=
AGENT_CUSTOM_MAX_TOKENS=4096

FINAL_TOP_K=5
AGENT_MAX_RETRIEVAL_ROUNDS=3
```

## 查询流程

```text
Streamlit
  -> AppPipeline
  -> MultiSourceAgentRunner
  -> LangGraph
  -> Tool adapters
     - RAGFlow document retrieval
     - Spreadsheet semantic/cell/profile search
  -> LLMClient 生成最终答案
```

## 说明

- “知识库”现在是业务隔离与权限治理单元，不代表本地向量库。
- 检索后端固定为 RAGFlow，项目内不再维护本地向量索引。
- Excel 等结构化数据不走本地 RAG，而是由 spreadsheet pipeline 建立结构化索引后交给 agent 调度。
