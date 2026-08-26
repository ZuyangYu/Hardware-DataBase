# Hardware DataBase 当前架构文档

> 版本: v0.2.0
> 更新时间: 2026-07

## 1. 系统定位

Hardware DataBase 当前是一个面向硬件设计资料、项目文档、结构化表格和电路设计（EDF 网表/原理图）的硬件数据平台。系统保留“知识库”作为业务隔离、权限治理和资产归属单元；对话只是外接检索入口之一，平台核心是硬件资产管理、解析、召回与治理。

文档检索统一由 RAGFlow 承担；查询编排由 LangGraph agent 承担；Excel 与 EDF 网表等结构化资产由独立 pipeline 建立结构化索引，并通过 agent tool 参与检索。

## 2. 核心原则

1. RAGFlow 是唯一文档检索后端。
2. LangGraph 只负责查询流程编排，不实现底层检索算法。
3. Agent 最终答案生成使用项目自有 `LLMClient`，不依赖检索框架的全局模型配置。
4. Word/PDF、Excel、EDF 网表等多源资产由 pipeline 分别处理，再在 agent 层统一调度。
5. Agent 是**有界且 fail-open** 的：每个 LLM 节点都有确定性回退，工具错误被吞掉而非上抛，多跳检索受 `AGENT_MAX_RETRIEVAL_ROUNDS` 硬上限约束。

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
  - Tool adapter 调度
  - 答案生成与 trace 输出
  |
  v
LangGraph Query Graph
  - route_query                 # 路由：需要检索 / 闲聊直答
  - analyze_question            # 问题拆解
  - scan_kb_catalog             # 扫描知识库可用来源
  - plan_source_selection       # 检索范围规划
  - retrieve_evidence           # 并行执行 tool 调用
  - merge_evidence              # 去重 / 截断
  - score_and_compare_evidence  # 覆盖度矩阵 / 冲突 / 质量评分
  - draft_intermediate_answer   # 草稿
  - judge_sufficiency           # 证据是否充分（多跳判断）
  - plan_next_retrieval         # 必要时规划补检索
  - compose_answer              # grounded answer
  - verify_grounding            # 规则校验是否有证据支撑
  - compose_direct_answer       # 闲聊 / 通用知识直答（旁路）
  |
  v
Tools / Services
  - DocumentRAGTool        -> RAGFlow document retrieval
  - CircuitQueryTool       -> Circuit 结构化检索
  - SpreadsheetSemanticTool / SpreadsheetCellTool -> Excel 结构化索引
  - PipelineCatalogTool    -> 知识库来源目录（供规划，非检索）
  |
  v
Storage / External Systems
  - RAGFlow datasets（governance / design）
  - SQLite：auth / 会话 / 文档台账 / 解析任务（本地开发单机）
  - Spreadsheet 结构化索引（per-KB SQLite）
  - Circuit 结构化存储 + per-KB 向量索引（ChromaDB）
  - Pipeline 归档文件
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

- 上传普通文档到 RAGFlow，并维护 governance / design 两个物理 dataset。
- 查询 RAGFlow retrieval API，按 `source_group` 做元数据硬过滤（含 source-name 回退）。
- 映射业务知识库、部门、source group 与 RAGFlow dataset/document。
- 管理解析任务、删除文档、删除知识库相关外部资产。
- 持有 `PipelineRuntimeBundle`（store / archive / ingestion / runtime + spreadsheet & circuit 索引服务）。

### MultiSourceAgentRunner

位置: `src/agents/runner.py`

职责:

- 驱动 LangGraph 查询流程（`src/agents/graph.py::build_multi_source_graph`）。
- 注册并调度文档检索、电路检索、表格检索等 tool adapter。
- 使用 `LLMClient` 流式生成最终答案，并输出 Agent trace / token 用量。
- 每个 LLM 节点失败时走确定性回退（fail-open）。

### LLMClient

位置: `src/core/llm_client.py`

职责:

- 封装 agent 最终答案生成模型。
- 支持 Ollama 和 OpenAI-compatible API（`AGENT_LLM_PROVIDER`）。
- HTTP 429 指数退避重试（重试耗尽后抛出原始错误，无跨模型回退）。
- 与 RAGFlow、LangGraph 等检索/编排框架解耦。

### Spreadsheet Pipeline

位置: `src/pipelines/spreadsheet/`

职责:

- 解析 `.xlsx` 文件（纯标准库 OOXML 解析）。
- 保存 sheet、row、cell、semantic row、profile 等结构化索引到 per-KB SQLite。
- 向 agent tool 提供语义行检索与单元格检索。

### Circuit 子系统

位置: `src/circuit/`

职责:

- 解析 EDF/EDIF 网表（`parsers/edf_parser.py`，基于 vendored SpyDrNet；`edif_lite_parser.py` 兜底）。
- `CircuitIndexService`（`index_service.py`）将解析结果存入 `CircuitStore`（`storage/circuits/{kb}/{design_id}/circuit_state.json`），并在保存时触发 `CircuitVectorIndex` 的 fail-soft 重建索引。
- `CircuitQueryEngine`（`query_engine.py`）提供网络、实例、模块、模块连接、电源/偏置/保护拓扑等结构化检索。
- `CircuitVectorIndex`（`vector_index.py`）是 per-KB 的 ChromaDB 语义索引（`circuit_kb_{kb}`），作为关键词匹配的语义补充；未绑定 embedding 模型时为 no-op。
- 查询入口为 `CircuitQueryTool`（`src/agents/tools/circuit_tools.py`）-> `CircuitIndexService.query`。
- `orchestrator.py` / `ingest_workers.py` / `query_agent.py` 等是 EDF+PDF 融合与有界 Plan-and-Execute agent 的完整路径，目前**未接入主流程**，保留待迁移到共享 `LLMClient`。

### Pipelines 与 Services

位置: `src/pipelines/`、`src/services/`

职责:

- `registry.py` 按扩展名路由到 processor kind：`document_rag`（`.doc/.docx/.pdf`）、`spreadsheet`（`.xlsx`）、`circuit_design`（`.edf/.edif`）；`.xls` 被拒绝。
- `ingestion.py`（`IngestionOrchestrator` + 三个 handler）负责按内容哈希去重、归档、handler 分发与解析任务生命周期。
- `document_store_sqlite.py` 是文档/解析任务台账（部门 + KB 隔离）。
- `services/` 提供文档治理、路由、归档、KB scope 与资产清理。

## 5. 查询流程

```text
用户提问
  -> route_query（判断是否需要检索）
  -> analyze_question（问题拆解）
  -> scan_kb_catalog（扫描知识库可用来源）
  -> plan_source_selection（检索范围规划）
  -> retrieve_evidence（并行调用 document_rag / circuit_query / spreadsheet 工具）
  -> merge_evidence（去重、截断）
  -> score_and_compare_evidence（覆盖度矩阵、冲突检测、质量评分）
  -> draft_intermediate_answer
  -> judge_sufficiency（充分 / 可答但不足 / 需补检索）
  -> 若需补检索且未达上限：plan_next_retrieval -> 回到 retrieve_evidence
  -> compose_answer（基于证据的 grounded answer）
  -> verify_grounding（规则校验证据支撑）
  -> 返回答案和 Agent Trace
```

闲聊 / 通用知识类问题经 `route_query` 直接走 `compose_direct_answer` 旁路，不进入检索链路。

## 6. 数据流

### 上传

```text
Streamlit upload
  -> AppPipeline.upload_files
  -> DocumentManager
  -> IngestionOrchestrator
  -> PipelineRegistry 路由（按扩展名）
     - document_rag (.doc/.docx/.pdf): RAGFlow
     - spreadsheet (.xlsx): Spreadsheet pipeline
     - circuit_design (.edf/.edif): Circuit 子系统
  -> PipelineDocumentStore 记录台账
```

`source_group` 决定 RAGFlow dataset（governance / design），扩展名决定 pipeline。

### 检索

```text
Agent source plan
  -> DocumentRAGTool   -> RAGFlowBackend.retrieve
  -> CircuitQueryTool  -> CircuitIndexService.query -> CircuitQueryEngine
  -> Spreadsheet tools -> SpreadsheetIndexService -> per-KB SQLite
  -> evidence merge / coverage judge / 必要时补检索
```

## 7. 配置

### 独立 Worker

HTTP API 只负责鉴权、创建任务和 SSE 订阅；对话 turn 与表格解析由独立进程领取数据库中的持久化任务：

```bash
hardware-database-server
.venv/bin/python -m src.workers.main
```

安装包重新安装后也可使用 `hardware-database-worker`。Worker 通过 `WORKER_POLL_INTERVAL_SECONDS` 控制空队列轮询间隔，`WORKER_PARSE_BATCH_SIZE` 控制每轮最多处理的表格任务数。当前 SQLite worker 用于本地单机和开发验证；生产多实例部署应将同一领取接口迁移至 PostgreSQL，并以 Redis 队列承担唤醒和调度。

核心配置集中在 `src/settings.py`（单一事实来源，`.env` 以 `utf-8-sig` 加载，UI「⚙️ 系统配置」可在线修改并回写 `.env`）：

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
AGENT_RATE_LIMIT_MAX_RETRIES=4

FINAL_TOP_K=5
AGENT_MAX_RETRIEVAL_ROUNDS=3
```

RAGAS 评估相关变量（`EVAL_LLM_*` / `EVAL_EMBEDDING_*`）由 `src/evaluation/config.py` 读取，裁判 LLM 默认复用 `AGENT_*`，embedding 必须显式配置。

## 8. 已移除内容

以下旧链路不再属于当前项目运行时:

- 本地向量知识库
- LlamaIndex 检索和生成封装
- 项目内 Chroma/DocStore 管理（文档侧；电路侧保留 per-KB 语义索引）
- 项目内 BM25/RRF/hybrid retriever
- 旧 `src/rag_backends/local_backend.py`
- 旧 `src/core/rag_pipeline.py`
- 旧 `src/ingestion/index_builder.py`
- 旧 `src/query_router/`（`UnifiedQueryRouter` / `CompositeQueryAgent` / `multihop_agent` 等）

## 9. 验收重点

- 查询前必须有问题拆解与检索范围规划。
- 文档证据来自 RAGFlow；Excel 证据来自 spreadsheet pipeline；电路证据来自 circuit 结构化检索。
- 多跳检索受 `AGENT_MAX_RETRIEVAL_ROUNDS` 约束，证据不足时走 fail-open 回退而非无限循环。
- 最终答案必须基于 evidence，并能说明缺失信息；`verify_grounding` 以规则校验证据支撑。
- 代码中不应重新引入本地文档向量检索或 LlamaIndex 依赖。
