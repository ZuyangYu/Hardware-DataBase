# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Hardware DataBase (package name `hardware-database`) is a Streamlit-based multi-source Q&A
system for hardware design assets. It answers questions over **documents**
(Word/PDF via RAGFlow), **spreadsheets** (Excel structured index), and **circuit
designs** (EDIF/EDF netlists + schematic PDFs), orchestrated by a bounded
LangGraph agent that does question analysis, source planning, multi-round
retrieval, evidence-coverage judging, and grounded answer synthesis. It also
ships a RAGAS evaluation subsystem. Python >= 3.12.

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
uv run streamlit run streamlit_app.py      # launch the app

# Tests (pytest; tests are unittest.TestCase classes; vendor/spydrnet excluded via norecursedirs)
uv run python -m pytest                     # full suite
uv run python -m pytest tests/test_circuit_query_engine.py     # one file
uv run python -m pytest tests/test_kb_scope.py::KbScopeTests::test_scope_reads_department_and_kb_id_from_context  # single test

uv run ruff check .                         # lint

# RAGAS evaluation (requires the eval group)
uv sync --group eval
uv run hardware-database-eval validate --dataset evaluation/datasets/hardware_qa_v1.jsonl
uv run hardware-database-eval run --dataset evaluation/datasets/hardware_qa_v1.jsonl --output storage/evaluations
```

Tests import via the `src.` package path and run from the repo root.

## Configuration

`config/settings.py` is the single source of truth for all config constants and
path roots. It loads `.env` with `utf-8-sig` (BOM-tolerant). Most settings are
also editable live in the Streamlit sidebar ("⚙️ 系统配置"), which persists back
to `.env` via `AppPipeline.apply_settings` -> `save_settings_to_env` ->
`reload_settings`.

Two axes matter most:

- `AGENT_LLM_PROVIDER` (`ollama` | `custom`) - the model provider for the agent's
  `LLMClient` (`src/core/llm_client.py`). `custom` covers any OpenAI-compatible
  API (OpenRouter, DeepSeek, SiliconFlow, …). On HTTP 429 it retries with backoff
  then falls back to `AGENT_FALLBACK_MODEL`. This is the **live** provider axis;
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

Request flow: **Streamlit UI -> `AppPipeline.query` -> `MultiSourceAgentRunner`
-> LangGraph graph -> tool adapters -> `LLMClient` -> streamed answer.**

`AppPipeline` (`src/core/app_pipeline.py`) is the central orchestrator the UI
talks to. It owns a `RAGFlowBackend` (the retrieval/ingest backend), a
`DocumentManager` (governance facade), and a `MultiSourceAgentRunner` (the
agent). `query` streams answer deltas; `upload_files` routes by `source_group`
(see Ingestion) into the circuit pipeline or the RAGFlow backend.

### LangGraph agent (`src/agents/`)
`MultiSourceAgentRunner` (`runner.py`) builds and runs the graph
(`graph.py::build_multi_source_graph`) over `AgentState` (`state.py`). It is a
**bounded Plan-and-Execute** agent (not an open ReAct loop), with deterministic
fail-open fallbacks at every LLM node:

`route_query` -> (needs retrieval?) -> `analyze_question` -> `scan_kb_catalog`
-> `plan_source_selection` -> `retrieve_evidence` -> `merge_evidence` ->
`score_and_compare_evidence` -> `draft_intermediate_answer` -> `judge_sufficiency`
-> (insufficient & round < `AGENT_MAX_RETRIEVAL_ROUNDS`) -> `plan_next_retrieval`
-> back to `retrieve_evidence`; otherwise -> `compose_answer` -> `verify_grounding`
-> END. Small-talk / general-knowledge routes short-circuit to
`compose_direct_answer`.

`retrieve_evidence` runs the planner's finite `ToolCallPlan`s in parallel
(`ThreadPoolExecutor`); after round 1 it may append bounded datasheet lookups for
part numbers discovered in circuit evidence. `score_and_compare_evidence` builds a
per-sub-question coverage matrix, detects conflicts, and scores evidence quality;
`judge_sufficiency` (LLM) decides `sufficient | partial_but_answerable |
insufficient_need_more`; `plan_next_retrieval` (LLM) proposes follow-up tool calls
validated against an allow-list and the KB catalog. `verify_grounding` is
rule-based (grounded iff evidence exists). Prompts in `prompts.py`; hardware
tokenizer for scoring in `query_tokens.py`.

### Tools (`src/agents/tools/`)
`AgentTool` Protocol (`base.py`): `run(query, kb_name, ctx, top_k, filters) ->
list[Evidence]`. Registered in `runner.py`:
- `DocumentRAGTool` ("document_rag") - `RAGBackend.retrieve` (RAGFlow).
- `CircuitQueryTool` ("circuit_query") - `CircuitIndexService.query` -> structured circuit `Evidence`.
- `SpreadsheetSemanticTool` / `SpreadsheetCellTool` - SQL over the per-KB spreadsheet index.
- `SpreadsheetProfileTool` - registered but never emitted by the planner (dormant).
- `PipelineCatalogTool` - `scan(kb)` lists available doc/spreadsheet/circuit sources for planning; not a retrieval tool.

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
  doc index), used as a semantic supplement by `CircuitQueryEngine`. No-op when no
  embedding model is bound.
- `parsers/`: `edf_parser.py` wraps the vendored SpyDrNet checkout for EDIF
  (monkey-patches SpyDrNet's parser, falls back to `edif_lite_parser.py`);
  `pdf_schematic_parser.py` (pypdf) for schematic PDFs.
- `relations/` (connectivity + derived power relations) and `analyzers/`
  (`module_analyzer`, `image_cropper`) support the full-fusion path.
- **Dormant full path** (present but not wired into the app): `CircuitOrchestrator`
  (`orchestrator.py`) + `ingest_workers.py` / `upload_service.py` / `manifest.py`
  do EDF+PDF fusion, connectivity graph (`graph_store.py` -> `.gpickle`), module
  screenshots, and cross-reference; the bounded `CircuitQueryAgent`
  (`query_agent.py` + `query_planner.py` / `llm_controlled_planner.py` /
  `recovery_manager.py` / `answer_synthesizer.py` / `response_policy.py` /
  `circuit_scope_resolver.py` / `entity_resolver.py` / `query_context.py` /
  `session_context_store.py` / `intent_parser.py`) is a richer Plan-and-Execute
  agent kept for future migration onto the shared `LLMClient`. **Do not assume
  these are called by the main flow.**

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
25-sample dataset at `evaluation/datasets/hardware_qa_v1.jsonl`; the Streamlit
"🧪 RAGAS 评估" tab is system-admin-only and wraps `EvaluationService`.

### Other subsystems
- `src/test_data/` - structured test-data domain (CSV/JSON ->
  `storage/test_data/`). **Ingest-only** today (registered via `parser_registry`);
  browsable via `src/ui/test_data_browser.py` but **not** queryable through the agent.
- `src/ui/` - Streamlit page components. Only `evaluation_page.py` is wired into
  `streamlit_app.py`; `circuit_browser` / `module_tree` / `schematic_viewer` /
  `test_data_browser` / `mermaid_topology` are prepared but not yet integrated.
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
  (`src/pipelines/document_rag/schemas.py`, `src/agents/state.py`,
  `src/circuit/models.py`, `src/evaluation/schemas.py`) - extend those rather than
  passing ad-hoc dicts across boundaries.
- The agent is **bounded and fail-open**: every LLM node has a deterministic
  fallback; tool errors are swallowed rather than propagated; multi-hop is
  hard-capped by `AGENT_MAX_RETRIEVAL_ROUNDS`. Preserve this when editing nodes.
- User-facing strings and answers are Chinese; keep that for prompts and UI text.

## Vendored dependency: `src/circuit/vendor/spydrnet/`

The circuit EDF parser loads its SpyDrNet checkout from
`src/circuit/vendor/spydrnet`, so the project does not require a sibling
repository or the PyPI `spydrnet` package at runtime. `edf_parser._load_spydrnet`
prepends that path on `sys.path` and monkey-patches SpyDrNet's EDIF parser.
`sch_parse/` and `spydrnet_extension/` contain project-specific additions on top
of upstream SpyDrNet. The vendor checkout is excluded from pytest
(`norecursedirs`) and from the built package.
