# Hardware DataBase

Hardware DataBase 是一个面向硬件设计资料、项目文档和结构化表格的智能数据基座。

## 核心特性

- **Agentic 查询流程**：问题拆解确认、文件扫描、检索范围确认、多源检索、证据覆盖度判断、自动补检索、最终 grounded answer。
- **多源 pipeline**：普通文档进入 RAGFlow 检索链路；Excel 进入结构化表格索引，并由 agent 统一调度。
- **RAGFlow 后端固定化**：文档上传、解析任务、检索、删除和知识库治理都通过 RAGFlow 后端适配层完成。
- **权限与治理**：支持部门、用户、知识库权限、审计日志、查询 trace 和文件处理状态。
- **独立模型配置**：Agent 最终答案生成使用项目自有 `LLMClient`，支持 Ollama 或 OpenAI-compatible API，不再复用旧 RAG 框架模型封装。

## 项目结构

```text
Hardware-RAG/
├── assets/                     # 应用示例图片
├── config/
│   └── settings.py             # 全局配置与 .env 加载
├── data/                       # (自动生成) 原始文档存储
├── docs/                       # 架构与设计文档
│   ├── architecture_doc.md
│   ├── langgraph_agentic_query_design.md
│   └── pipeline_contract.md
├── src/
│   ├── agents/                 # LangGraph Agent 编排
│   │   ├── graph.py
│   │   ├── runner.py
│   │   ├── prompts.py
│   │   ├── state.py
│   │   ├── query_tokens.py
│   │   └── tools/              # 检索工具适配
│   ├── core/                   # 应用管线、鉴权、LLM 客户端
│   │   ├── app_pipeline.py
│   │   ├── auth.py
│   │   ├── llm_client.py
│   │   ├── conversation.py
│   │   ├── source_group_router.py
│   │   ├── app_logs.py
│   │   └── logger.py
│   ├── ingestion/              # 容器检查、路径与解析任务
│   ├── pipelines/              # 多源 pipeline
│   │   ├── document_rag/       # RAGFlow 文档检索
│   │   └── spreadsheet/        # Excel 结构化表格索引
│   └── services/               # 文档治理、路由与资产清理
├── storage/                    # (自动生成) 归档、索引、日志与鉴权库
├── tests/                      # 单元与集成测试
├── streamlit_app.py            # 前端启动入口
├── pyproject.toml
├── requirements.txt
└── README.md
```

## 环境要求

- **Python**：>= 3.12
- **RAGFlow**：需准备一个可访问的 RAGFlow 实例作为文档检索后端，并获取其 API Key。
- **Ollama**：当 `AGENT_LLM_PROVIDER=ollama`（默认）时需要本地 [Ollama](https://ollama.com/) 服务并拉取对应模型；若改用 OpenAI-compatible API 则无需 Ollama。

## 安装

### 方式一：使用 uv（推荐）

[uv](https://github.com/astral-sh/uv) 是一个极速的 Python 包管理器。

安装 uv（如尚未安装）：

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

同步环境并启动（`uv run` 会自动加载虚拟环境，无需手动 activate）：

```bash
uv sync
uv run streamlit run streamlit_app.py
```

### 方式二：使用 pip

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
streamlit run streamlit_app.py
```

## 配置说明

支持两种配置方式：**页面配置**（推荐）和 **`.env` 文件配置**。

> 注意：仓库根目录的 `.env.example` 为旧架构模板，其中的 `RAG_BACKEND`、`PROVIDER`、`CUSTOM_LLM_MODEL`、`BM25_TOP_K`、`RERANKER_TYPE`、`CHUNK_SIZE` 等变量在当前版本已失效，请以下方变量为准。

### 方式一：页面配置（推荐）

启动应用后，进入侧边栏的 **「⚙️ 系统配置」**，可在页面上直接修改模型、RAGFlow、检索等配置，点击「🔄 应用配置」立即生效，配置会持久化保存到 `.env` 文件。

### 方式二：`.env` 文件配置

在项目根目录创建 `.env`，按需填写（以下为 `config/settings.py` 实际读取的变量及默认值）：

```env
# ==================== RAGFlow 后端 ====================
RAGFLOW_BASE_URL=http://localhost:9380
RAGFLOW_API_KEY=
RAGFLOW_GOVERNANCE_DATASET_NAME=department_governance
RAGFLOW_DESIGN_DATASET_NAME=project_design_assets
RAGFLOW_TIMEOUT_SECONDS=120
RAGFLOW_SIMILARITY_THRESHOLD=0.25
RAGFLOW_VECTOR_WEIGHT=0.4

# ==================== Agent LLM ====================
# provider: ollama | custom
AGENT_LLM_PROVIDER=ollama
AGENT_OLLAMA_BASE_URL=http://localhost:11434
AGENT_OLLAMA_MODEL=qwen2.5:32b
AGENT_TEMPERATURE=0.2
AGENT_TIMEOUT_SECONDS=120

# 或使用 OpenAI-compatible API（provider=custom 时生效）
AGENT_CUSTOM_API_KEY=
AGENT_CUSTOM_BASE_URL=https://api.openai.com/v1
AGENT_CUSTOM_MODEL=
AGENT_CUSTOM_MAX_TOKENS=4096

# ==================== 检索与 Agent 行为 ====================
FINAL_TOP_K=5
AGENT_MAX_RETRIEVAL_ROUNDS=3

# ==================== 鉴权（通常无需修改）====================
AUTH_DB_PATH=storage/auth.db
AUTH_DEFAULT_ADMIN_USERNAME=admin
AUTH_DEFAULT_ADMIN_PASSWORD=
AUTH_SESSION_TTL_HOURS=24

# ==================== 存储路径（通常无需修改）====================
PIPELINE_ARCHIVE_ROOT=storage/pipeline_archives
RAGFLOW_FILE_ROOT=storage/pipeline_archives

# ==================== 提示词（可选）====================
SYSTEM_PROMPT=
NO_CONTEXT_PROMPT=
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

## 常见问题

**Q: 运行 `uv sync` 时提示 lock file is not up to date？**
A: 通常发生在手动修改了 `pyproject.toml` 之后。运行 `uv lock` 更新锁定文件，再执行 `uv sync`。

**Q: 启动时提示 `ModuleNotFoundError`？**
A: 使用 uv 时必须用 `uv run streamlit run streamlit_app.py` 启动，或先 `source .venv/bin/activate` 再运行；使用 pip 时请确认已激活虚拟环境并安装依赖。

**Q: 上传文件后在问答中搜不到内容？**
A: 当前检索后端为 RAGFlow，请按顺序排查：1) RAGFlow 服务是否可达、`RAGFLOW_API_KEY` 是否正确；2) `RAGFLOW_GOVERNANCE_DATASET_NAME` / `RAGFLOW_DESIGN_DATASET_NAME` 对应的 dataset 是否存在；3) 在「📊 日志中心」或文件处理状态中确认 RAGFlow 解析任务是否完成。

**Q: Agent 响应很慢或超时？**
A: 可适当调大 `AGENT_TIMEOUT_SECONDS`；检查 `AGENT_MAX_RETRIEVAL_ROUNDS` 是否过大导致多轮补检索；确认 LLM 模型（Ollama 或 custom API）的响应速度。

**Q: 首次启动后无法使用问答？**
A: 请先进入「⚙️ 系统配置」检查并补全 RAGFlow 与 Agent 模型配置，点击「应用配置」生效。

## 说明

- “知识库”现在是业务隔离与权限治理单元，不代表本地向量库。
- 检索后端固定为 RAGFlow，项目内不再维护本地向量索引。
- Excel 等结构化数据不走本地 RAG，而是由 spreadsheet pipeline 建立结构化索引后交给 agent 调度。

## License

MIT License
