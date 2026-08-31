# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Hardware DataBase (package name `hardware-database`) is a multi-source Q&A
system for hardware design assets. It answers questions over **documents**
(Word/PDF via RAGFlow), **spreadsheets** (Excel structured index), and **circuit
designs** (EDIF/EDF netlists + schematic PDFs), orchestrated by a bounded
`deepagents` (`create_deep_agent`) agent loop that dynamically plans tool calls
for multi-source retrieval and grounded answer synthesis. It also ships a RAGAS
evaluation subsystem. Python >= 3.12.

The UI is a React + TypeScript SPA in `frontend/` talking to the FastAPI
backend in `src/api/`. The former Streamlit app (`streamlit_app.py`, `src/ui/`)
has been removed; `frontend/` + `src/api/` are the only UI path now.

`docs/architecture_doc.md` is the live whole-system architecture doc (v0.1.0,
2026-07) - read it first for the big picture. Other design notes in `docs/`:
`pipeline_contract.md` (pipeline isolation contract), `langgraph_agentic_query_design.md`
(query graph design), `joint_retrieval_test_cases.md` (source of the eval
dataset), `hardware_database_develop_integration_plan.md`.

> Note: an earlier version of this file described a removed local hybrid-retrieval
> architecture (`src/query_router/`, `src/rag_backends/`, `src/core/rag_pipeline.py`,
> `hybrid_retriever.py`, `bm25_cache.py`, ChromaDB+BM25+RRF+reranker). **None of that
> exists anymore** - document retrieval is RAGFlow-only and query orchestration is
> the LangGraph agent in `src/agents/`. If a referenced path is missing, assume it
> was removed rather than not yet written.

## Commands

This project uses **uv**. Always run Python through `uv run` (or activate `.venv`
first), otherwise imports fail. `uv sync` installs the `dev` group (pytest, ruff)
by default; the `eval` group (ragas/openai) is opt-in.

```bash
uv sync                                    # create venv + install locked deps (incl. dev)
uv run hardware-database-server            # start the FastAPI backend (default 127.0.0.1:8000)
# frontend dev server: cd frontend && npm install && npm run dev

# Tests (pytest; tests are unittest.TestCase classes; vendor/spydrnet excluded via norecursedirs)
uv run python -m pytest                     # full suite
uv run python -m pytest tests/test_circuit_query_engine.py     # one file
uv run python -m pytest tests/test_kb_scope.py::KbScopeTests::test_scope_reads_department_and_kb_id_from_context  # single test

uv run ruff check .                         # lint

# RAGAS evaluation (requires the eval group)
uv sync --group eval
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
uv run hardware-database-eval run --dataset evaluation/datasets/hardware_qa_v1.jsonl --output storage/evaluations

# API 服务 + CLI + MCP（为前后端分离铺路；RAGFlow key 只在服务侧）
uv run hardware-database-server             # 启动 API（默认 127.0.0.1:8000；HDB_API_HOST/PORT）
uv run hardware-database login --user <u>   # 登录，令牌存 ~/.config/hardware-database/
uv run hardware-database list-kb            # 列出可访问知识库
uv run hardware-database query --kb <name> "问题"                  # 检索（流式）；--json 输出结构化结果
uv run hardware-database upload --kb <name> --group <g> FILE...    # 上传（部门管理员；--group 缺省自动分类）
uv run hardware-database list-files --kb <name>
uv run hardware-database delete --kb <name> --file <name>          # 需 admin

# MCP server（把 API 暴露成 Claude Code 等本地 agent 的工具；stdio 传输）
uv run hardware-database-mcp               # stdio MCP server；需 API 服务在跑 + 已 login
```

Tests import via the `src.` package path and run from the repo root.

## Configuration

`src/settings.py` is the single source of truth for all config constants and
path roots. It loads `.env` with `utf-8-sig` (BOM-tolerant). Most settings are
also editable live in the frontend admin "系统配置" page (`frontend/src/pages/admin/ConfigPage.tsx`
via `PUT /api/v1/config`), which persists back to `.env` via
`AppPipeline.apply_settings` -> `save_settings_to_env` -> `reload_settings`.

Two axes matter most:

- `AGENT_LLM_PROVIDER` (`ollama` | `custom`) - the model provider for the agent's
  `LLMClient` (`src/core/llm_client.py`). `custom` covers any OpenAI-compatible
  API (OpenRouter, DeepSeek, SiliconFlow, …). On HTTP 429 it retries with backoff
  retries with backoff (no cross-model fallback exists). This is the **live** provider axis;
  the old `PROVIDER` variable is dead.
- Retrieval backend is **fixed to RAGFlow** - there is no local vector backend and
  no `RAG_BACKEND` switch. Embedding/parse happen RAGFlow-side; the project no
  longer configures local embedding / reranker / BM25.

`.env` is committed and contains **real API keys** (RAGFlow, OpenRouter,
SiliconFlow) plus a stale block of dead old-architecture variables
(`RAG_BACKEND`, `PROVIDER`, `CUSTOM_LLM_MODEL`, `BM25_TOP_K`, `RERANKER_TYPE`,
`CHUNK_SIZE`, `RRF_K`, `VECTOR_TOP_K`, `USE_OLLAMA_EMBEDDING`, …) that
`settings.py` simply ignores. **Do not treat `.env` as a safe template** - use
`.env.example`, which is the accurate current template (including the optional
`EVAL_*` eval-embedding block). Eval config (`EVAL_LLM_*` / `EVAL_EMBEDDING_*`)
is read by `src/evaluation/config.py`, not `settings.py`.

## Architecture

Request flow: **React frontend -> FastAPI (`src/api/`) -> `AppPipeline.query` ->
`MultiSourceAgentRunner` -> deepagents agent loop (`create_deep_agent`) -> tool
adapters -> streamed answer (model via `src.core.model_factory`).**

`AppPipeline` (`src/core/app_pipeline.py`) is the central orchestrator the UI
talks to. It owns a `RAGFlowBackend` (the retrieval/ingest backend), a
`DocumentManager` (governance facade), and a `MultiSourceAgentRunner` (the
agent). `query` streams answer deltas; `upload_files` routes by `source_group`
(see Ingestion) into the circuit pipeline or the RAGFlow backend.

### Agent loop (`src/agents/`)
`MultiSourceAgentRunner` (`runner.py`) wraps `deepagents.create_deep_agent`
(a compiled LangGraph graph under the hood, but with a fixed general agent loop —
no hand-written nodes). It is **OpenAI-agents-style ReAct**, not a bounded
Plan-and-Execute graph: the model decides which retrieval tools to call, in what
order, and how many hops, inside the tool-calling loop. Every tool call emits
`tool_started` / `tool_result` events that drive the frontend's live trace.

Multi-hop retrieval is **agentic by design** (the 2026-08 bounded-graph with the
deterministic gap-ledger forced re-retrieval was removed in `9db28ca`). Guardrails
are observational only, never planner-style: (1) a pre-answer self-check rule in
`SYSTEM_PROMPT` ("证据是否覆盖全部要点，不足先补证"); (2) multi-hop depth stats in the
footer (`rt.diagnostics` / `rt.queries`); (3) a fail-open `balanced_route` retry in
`document_search` when full-KB retrieval yields zero hits (drops the naive
source_group hard filter, keeps frozen `source_names` scope — see M2 in the
2026-08-27 audit).

`AGENT_MAX_RETRIEVAL_ROUNDS` (settings) maps to the loop's `recursion_limit`
budget (`deep` → `rounds*12`, `fast` → `rounds*6`); it is NOT a hard cap on a
planner. `SYSTEM_PROMPT` / `NO_CONTEXT_PROMPT` (settings, editable in system
config) override the default agent prompt and the no-evidence fallback answer.
Fail-open: tool errors are swallowed (return empty, emit a `degraded` event),
and a trace/footer is always produced.

### Tools (`src/agents/tools/`)
Tools are plain factory closures `make_*_tool(rt, ...)` returning a LangChain
tool; they share one per-request `ToolRuntime` (`runtime.py`) that scopes
`kb_name` / `ctx` / cancellation / event callback so concurrent sessions never
leak state. Registered in `runner.py`:
- `document_search` (`make_document_search`) - `RAGBackend.retrieve` (RAGFlow).
- `circuit_search` (`make_circuit_search`) - `CircuitIndexService.query` -> structured circuit `Evidence`.
- `spreadsheet_row_search` / `spreadsheet_cell_lookup` (`make_spreadsheet_tools`) - SQL over the per-KB spreadsheet index.
- `conversation_search` (`make_conversation_search`) - external conversation records.
- `list_kb_sources` (`make_catalog_tool`) - lists available doc/spreadsheet/circuit sources; not a retrieval tool.

### Backend layer (`src/pipelines/document_rag/`)
`RAGBackend` ABC (`base.py`); `factory.create_rag_backend()` returns the sole
impl `RAGFlowBackend` (`ragflow_backend.py`). It owns a `PipelineRuntimeBundle`
(store / archive / ingestion / runtime + spreadsheet & circuit index services)
and delegates HTTP to a `RAGFlowClient` over `/api/v1/datasets`, `/retrieval`,
etc. Two physical datasets: **governance** (`department_governance`) and **design**
(`project_design_assets`); the upload's `source_group` picks the dataset kind.
`retrieve` applies `route_source_groups` (keyword -> source-group weights) as a
hard metadata filter, with a source-name fallback retry. Cross-layer data crosses
as the dataclasses in `schemas.py` (`Evidence`, `IngestResult`, `RequestContext`,
`DocumentInfo`, …).

### Pipelines & services (`src/pipelines/`, `src/services/`)
- `registry.py` (`PipelineRegistry` / `PipelineSpec`) routes a file by extension to a processor kind: `document_rag` (`.doc/.docx/.pdf` -> RAGFlow), `spreadsheet` (`.xlsx` -> structured index), `circuit_design` (`.edf/.edif` -> circuit). `.xls` is rejected.
- `ingestion.py` (`IngestionOrchestrator` + `RAGFlowDocumentHandler` / `SpreadsheetPipelineHandler` / `CircuitPipelineHandler`) does per-content-hash dedup, archiving, handler dispatch, and parse-task lifecycle. RAGFlow parsing is remote-async; spreadsheet parsing runs on a daemon worker (`runtime.py`); circuit indexing runs synchronously inside `submit`.
- `document_store_sqlite.py` (`PipelineDocumentStore` at `storage/pipeline_documents.db`) is the document / parse-task ledger (department+KB scoped).
- `spreadsheet/` (`xlsx_parser.py` pure-stdlib OOXML -> `table_store.py` per-KB SQLite at `storage/table_indexes/.../table_indexes.db`).
- `services/`: `document_manager.py` (governance facade), `document_routing.py` (status constants / extension sets), `document_archive.py` (`storage/pipeline_archives/departments/...`), `kb_scope.py` (dept+KB scoping), `pipeline_asset_cleanup.py`, `spreadsheet_index_service.py`.

### Circuit subsystem (`src/circuit/`)
Parses EDIF netlists (+ schematic PDFs in the full path) into a structured,
cross-referenced circuit model. **Two paths exist on disk; only the simpler is
live.**

- **Live ingestion**: `CircuitPipelineHandler` -> `CircuitIndexService.index_file`
  (`index_service.py`) parses via `EdfParser` and saves a `CircuitDesign` to
  `CircuitStore` (`storage/circuits/{kb}/{design_id}/circuit_state.json` + global
  `index.json`), writing a `pipeline_metadata.json` sidecar. Saving triggers a
  fail-soft reindex of `CircuitVectorIndex`.
- **Live query**: `CircuitQueryTool` -> `CircuitIndexService.query` ->
  `CircuitQueryEngine` (`query_engine.py`) structured searches (nets, instances,
  modules, module connections, power/bias/protection topologies) + deterministic
  `question_analysis.analyze_question`; results mapped to `Evidence` by
  `CircuitEvidenceMapper`.
- `vector_index.py` (`CircuitVectorIndex`) - per-KB ChromaDB collection
  `circuit_kb_{kb}` over module/instance/net docs (deliberately separate from any
  doc index), used as a semantic supplement by `CircuitQueryEngine`. Explicitly
  disabled (clean no-op) when no embedding model or no chroma client is
  available; collections use cosine space.
- `parsers/`: `edf_parser.py` wraps the vendored SpyDrNet checkout for EDIF
  (monkey-patches SpyDrNet's parser, falls back to `edif_lite_parser.py`);
  `edf_partition.py` groups instances into page-based modules.
- `relations/` (connectivity + derived power relations) feed the live
  `CircuitQueryEngine`.
- **Note**: an earlier dormant "full-fusion" path (EDF+PDF orchestrator, graph
  store, module screenshots) and the richer bounded `CircuitQueryAgent` family
  (~5.7k lines, zero live references / zero tests) were removed in favor of the
  live `CircuitIndexService` path. Recover from git history if PDF fusion is
  ever revisited.

### Ingestion (`src/ingestion/`)
`source_groups.py` defines the 10-group taxonomy (文档资料 / 物料数据 / 设计数据 /
网表数据 / 原理图数据 / 测试数据 / 项目管理数据 / 外部数据 / 人员与组织数据 /
未分类). The **source group picks the RAGFlow dataset and the file extension
picks the pipeline**; `设计数据` (DESIGN) is the user-facing umbrella for
netlist/schematic. `kb_paths.py` validates KB names and enforces path containment.
`parser_registry.py` is a generic domain-manifest registry (the `test_data`
domain plugs in here). `parse_tasks.py` is a generic in-process parse-task manager
backing the UI panel. `container_inspector.py` inspects OOXML (`.docx`/`.xlsx`)
archives for embedded/media objects.

### Evaluation (`src/evaluation/`)
RAGAS subsystem exposed via the `hardware-database-eval` CLI (`cli.py`):
`validate` / `collect` / `score` / `run`. Judge LLM reuses `AGENT_*` by default
(or `EVAL_LLM_*`); embedding **requires** `EVAL_EMBEDDING_*`. Runs hardware domain
rules (`hardware_metrics.py`) + 5 RAGAS metrics, gates via `gates.py`
(`--fail-on-threshold`), and writes `summary.json` / `results.jsonl` /
`summary.csv` / `report.html` to `storage/evaluations/<run_id>/`. Built-in
25-sample dataset at `evaluation/datasets/hardware_qa_v1.jsonl`; the frontend
"RAGAS 评估" admin page is system-admin-only and wraps `EvaluationService` via
`src/api/routes/evaluation.py`.

### API 层 (`src/api/`)
前后端分离的后端。`src/api/` 是 FastAPI 服务(**就是后端**),只在 `AppPipeline` 外包一层 HTTP,不重写业务。所有业务路由挂在 `/api/v1` 前缀下(`/health` 保留根路径探针)。CORS 由 `HDB_API_CORS_ORIGINS`(逗号分隔)配置,默认放行本地开发 origin(5173/5174/3000),**生产部署必须显式设为前端实际域名**。RAGFlow key / `.env` / `auth.db` 只在服务侧。权限复用 `RAGFlowBackend._check_kb_access` 与 `RequestContext.has_kb_permission`:普通用户只能检索,部门管理员才能上传/建库/删除。`src/api/context.py::build_context_for_user` 把已认证 `AuthUser` 转成 `RequestContext`(复用 `build_request_context`,不重复权限逻辑)。查询走 **turns 执行模型**:`POST /conversations/{id}/turns` 创建轮次(服务端自动落库 user 消息)-> `POST /turns/{turn_id}/start` 由 worker 轮询执行 -> `GET /turns/{turn_id}/events` SSE 流式返回;旧的直连 `POST /query`(SSE delta/done/error)仍保留。上传 `POST /kbs/{kb}/files`(multipart,分片写盘 + 大小上限 `HDB_API_MAX_UPLOAD_BYTES`)。

**审计下沉**:管理写操作的 `record_audit` 住在 `AuthService._audit` / `AppPipeline._audit` 内部(fail-soft),所有入口自动覆盖、无双写。唯一例外是 `change_settings`(`apply_settings` 是 staticmethod 无 actor),由 `PUT /config` 路由层记一次。

**角色权力分离**(三个角色的可达范围与前端 admin 页面严格对齐):
- `system_admin` = **治理角色**,只碰元数据。可用:部门/用户/KB 挂载(`assign_kb`)/权限清单查看/系统配置/日志中心/RAGAS 评估/治理面板。**不能**访问任何 KB 内容(检索、上传、看文件、删文件、看解析任务)。`RequestContext.has_kb_permission` 对 sysadmin 恒返回 False;API 路由用 `deps.reject_system_admin_kb_access(ctx)` 提前拒并给明确错误信息("system_admin 是治理角色,不能访问知识库内容")。这条铁律的目的是防"平台管理员静默窥视各部门私有数据"。
- `dept_admin` = **部门治理 + 部门内容**。可用:本部门用户管理、本部门 KB 建/删/权限授予撤销、本部门文件上传/删除/查看、检索。
- `user` = **纯消费**。可用:对已被授权的 KB 检索、查看文件、看自己的会话。

**会话持久化**:查询走 turns 执行模型时,服务端自动把 user/assistant 消息写入 `conversations`(见 `src/api/routes/query.py`);旧的直连 `POST /query` 只流式返回答案并写 query trace + 证据到日志中心,**不**落库消息。

**注**:`src/cli/` 与 `src/mcp/` 已随「移除主项目内置 CLI/MCP」提交删除,本段历史描述的 CLI/MCP 客户端不再存在;API 是当前唯一的后端入口。


### Other subsystems
- `src/test_data/` - structured test-data domain (CSV/JSON ->
  `storage/test_data/`). **Ingest-only** today (registered via `parser_registry`);
  not yet queryable through the agent.
- `src/core/` - `app_pipeline.py` (orchestrator), `llm_client.py` (provider-neutral
  chat client), `auth.py` (role-based access: system_admin / dept_admin / user,
  backed by `storage/auth.db`; `build_request_context`), `conversation.py` (chat
  sessions in `auth.db`), `source_group_router.py` (query -> source-group weights,
  consumed by `RAGFlowBackend.retrieve`), `logger.py` / `app_logs.py`.

### Persisted state (`storage/`, gitignored, auto-generated)
`pipeline_documents.db`, `table_indexes/`, `circuits/`, `pipeline_archives/`,
`parse_tasks/`, `auth.db`, `evaluations/`, `logs/`, `query_sessions/`,
`circuit_uploads/`. Directories prefixed `_test_*` / `_*_smoke` are test scratch
space.

## Conventions

- Source modules are organized by subsystem (`src/<subsystem>/`). **Tests are
  flat** - one `tests/test_<topic>.py` per module, `unittest.TestCase`-based, with
  `tests/evaluation/` as the only subsystem subdir. Mirror that when adding tests.
- Cross-layer data passes as the dataclasses/schemas defined per layer
  (`src/pipelines/document_rag/schemas.py`, `src/agents/schemas.py`,
  `src/circuit/models.py`, `src/evaluation/schemas.py`) - extend those rather than
  passing ad-hoc dicts across boundaries.
- The agent is **fail-open**: tool errors are swallowed (empty result + a
  `degraded` event) rather than propagated, and a trace/footer is always produced.
  Loop budget is set by `AGENT_MAX_RETRIEVAL_ROUNDS` (→ `recursion_limit`); there
  is no hand-written graph to edit.
- User-facing strings and answers are Chinese; keep that for prompts and UI text.

## Vendored dependency: `src/circuit/vendor/spydrnet/`

The circuit EDF parser loads its SpyDrNet checkout from
`src/circuit/vendor/spydrnet`, so the project does not require a sibling
repository or the PyPI `spydrnet` package at runtime. `edf_parser._load_spydrnet`
prepends that path on `sys.path` and monkey-patches SpyDrNet's EDIF parser.
`sch_parse/` and `spydrnet_extension/` contain project-specific additions on top
of upstream SpyDrNet. The vendor checkout is excluded from pytest
(`norecursedirs`) and from the built package.
