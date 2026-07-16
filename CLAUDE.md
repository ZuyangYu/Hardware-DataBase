# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Hardware RAG (package name `hardware-rag`) is a Streamlit-based knowledge-base Q&A
system for hardware technical documents. Beyond ordinary document RAG it also parses
**circuit designs** (EDIF/EDF netlists + schematic PDFs) into a structured store and
answers circuit-specific questions through a dedicated query router. Python >= 3.12.

The README gives a feature-level overview of all subsystems (`src/core/`, `src/circuit/`,
`src/query_router/`, `src/rag_backends/`). `docs/architecture_doc.md` is scoped to the
**core document-RAG** modules only — it carries an explicit scope note and defers the
circuit/router/backend subsystems. The detailed design intent for those newer subsystems
lives in the planning docs in the parent workspace directory (`../`):
- `../schematic_parsing_plan.md` — EDF/PDF parsing & circuit store layout (currently v1.6).
- `../circuit_query_agent_context_modification_plan.md` — circuit query agent & context isolation.

## Commands

This project uses **uv**. Always run Python through `uv run` (or activate `.venv` first),
otherwise imports fail.

```bash
uv sync                                    # create venv + install locked deps
uv run streamlit run streamlit_app.py      # launch the app

# Tests (pytest; tests are unittest.TestCase classes, no conftest/pytest config)
uv run python -m pytest                     # full suite
uv run python -m pytest tests/circuit/      # circuit subsystem only
uv run python -m pytest tests/circuit/test_circuit_core.py::CircuitCoreTests::test_classify_net_name  # single test

uv run ruff check .                         # lint (dev dependency group)
```

Tests import via the `src.` package path and run from the repo root.

## Configuration

Settings load from `.env` (UTF-8-BOM tolerant) via `config/settings.py`, which is the
single source of truth for all config constants and path roots. Most settings are also
editable live in the Streamlit sidebar ("⚙️ 系统配置"). Two axes matter most:

- `PROVIDER` (`ollama` | `custom`) — model provider. `custom` covers any OpenAI-compatible
  API (OpenRouter, DeepSeek, etc.). Embedding can use a different provider than the LLM.
- `RAG_BACKEND` (`local` | `ragflow`) — selects the retrieval backend (see below). Note the
  committed `.env` defaults to `ragflow` while `.env.example` shows `local`; check which
  backend is active before debugging retrieval.

`.env` is committed and contains real-looking keys — do not assume it is a safe template;
use `.env.example` for documentation of available variables.

## Architecture

Request flow: **Streamlit UI → `RAGPipeline.query` → 4 fail-open branches tried in
order** — return the first non-empty answer, else fall through:
1. `_try_composite_query` → `CompositeQueryAgent` (`src/query_router/composite_agent.py`):
   for questions spanning circuit facts + manual/spec evidence, runs the circuit Agent and
   document retrieval in parallel then fuses evidence. Gated by `should_use_composite_agent`
   + `COMPOSITE_QUERY_ENABLED` (default on); any failure → None (fail-open).
2. `_try_multihop_query` → bounded Stage-4 multi-hop agent (`src/query_router/multihop_agent.py`).
3. `_try_structured_query` → `UnifiedQueryRouter` for structured domains (circuit / test_data).
4. `RAGBackend.stream_answer` → ordinary document RAG.

`RAGPipeline` (`src/core/rag_pipeline.py`) is the central orchestrator the UI talks to.
Uploads are split: circuit files (netlist/schematic source groups) go to the circuit
ingestion path, everything else to the backend.

### Backend layer (`src/rag_backends/`)
`RAGBackend` (ABC in `base.py`) abstracts ingest / retrieve / stream_answer / delete / list.
`factory.create_rag_backend()` returns `LocalRAGBackend` (the in-repo hybrid retriever) or
`RAGFlowBackend` (delegates to an external RAGFlow service). All cross-layer data crosses as
the dataclasses in `schemas.py` (`Evidence`, `IngestResult`, `RequestContext`, etc.).

### Core document RAG (`src/core/`) — used by `LocalRAGBackend`
Hybrid retrieval: vector (ChromaDB embeddings) + BM25 (rank-bm25, Jieba tokenization for
Chinese) merged via **RRF**, then optional cross-encoder **Reranker**, then top-K → LLM.
Key modules: `hybrid_retriever.py`, `custom_rag_chat.py` (streaming + context cache + history),
`resource_manager.py` (thread-safe singleton owning ChromaDB client + models, with retry/
fallback), `model_factory.py` (builds LLM/Embedding/Reranker per provider), `bm25_cache.py`
(per-KB `.pkl` indexes with change-detection invalidation), `source_group_router.py` (maps a
query to source-group weights — keyword rules incl. HSI/接口文档 → 设计/文档 groups — so
retrieval can filter by group; consumed by `routed_retriever.py` and the RAGFlow backend).
`auth.py` provides role-based access (system admin / dept admin / user) backed by
`storage/auth.db`.

### Circuit subsystem (`src/circuit/`)
Parses EDF netlists and schematic PDFs into a structured, cross-referenced circuit model.
- `CircuitOrchestrator` incrementally merges EDF (`apply_edf_parse`) and PDF
  (`apply_pdf_parse`) results per KB, fusing them when both sources are present.
- Parsers in `parsers/`: `edf_parser.py` wraps the vendored `src/circuit/vendor/spydrnet`
  checkout for EDIF (it prioritizes that path on `sys.path` and monkey-patches SpyDrNet's
  EDIF parser), with `edif_lite_parser.py` as a fallback; `pdf_schematic_parser.py` uses
  pdfplumber/PyMuPDF.
- `CircuitStore` persists to `storage/circuits/{kb}/{design_id}/` (circuit_state.json,
  connectivity_graph.gpickle, module_screenshots/, pdf_cache/) plus a global
  `storage/circuits/index.json`. Old flat layouts auto-migrate on next save.
- `CircuitQueryEngine` answers structured queries (refdes, nets, pins, power, cross-reference)
  over the store.
- `relations/` (extractor / derivers / models / views) extracts and derives cross-reference
  relations (refdes↔net↔module↔power) consumed by the query engine and synthesizer.
- Circuit questions enter via `query_tool.query_circuit_data` → `CircuitQueryAgent`
  (`query_agent.py`), a **bounded Plan-and-Execute agent** (not an open ReAct loop): intent
  → scope/entity resolution → finite tool plan (`query_planner.py`; an LLM-controlled variant
  in `llm_controlled_planner.py`) → bounded execution against `CircuitQueryEngine`
  (Map-Reduce across circuits) → recovery (`recovery_manager.py`) → evidence-backed
  synthesis (`answer_synthesizer.py`, shaped by `response_policy.py`). The older
  `query_router/` agents remain as a fallback path in `rag_pipeline.py` (see below).

### Query router (`src/query_router/`)
`UnifiedQueryRouter` decides which tool handles a query, combining keyword rules with an
optional LLM function-call router (`llm_function_router.py`). Tools are registered in
`registry.py` / `tool_registry.py`. `composite_agent.py` (`CompositeQueryAgent`) is the
top-level parallel circuit+doc orchestrator (see Request flow). `multihop_agent.py` and
`circuit_answer_agent.py` are the legacy multi-step circuit reasoners, now a fallback path in
`rag_pipeline.py` — the primary circuit agent lives in `src/circuit/` (see above).
`relevance_reranker.py` / `result_trimmer.py` cap context size for the router's own answer
synthesis.

### Ingestion (`src/ingestion/`)
`source_groups.py` defines the document taxonomy (文档资料/物料数据/设计数据/网表数据/
原理图数据/测试数据/…). The **source group decides the ingestion path** — netlist/schematic
groups go to the circuit pipeline, the rest to the RAG backend. `docling_parser.py` /
`parse_strategies.py` / `parser_registry.py` handle document parsing; `kb_paths.py` validates
KB names and resolves per-KB storage paths.

### Persisted state (`storage/`, gitignored, auto-generated)
ChromaDB (`chroma_db/`), circuit store (`circuits/`), BM25/reranker caches, `auth.db`,
query sessions, logs, and RAGFlow file mappings. Directories prefixed `_test_*` / `_*_smoke`
are test scratch space.

## Conventions

- Modules and tests are organized by subsystem; mirror that when adding code
  (`src/<subsystem>/`, `tests/<subsystem>/`).
- Cross-layer data passes as the dataclasses/schemas defined per layer — extend those rather
  than passing ad-hoc dicts across boundaries.
- User-facing strings and answers are Chinese; keep that for prompts and UI text.

## Vendored dependency: `src/circuit/vendor/spydrnet/`

The circuit EDF parser loads its SpyDrNet checkout from `src/circuit/vendor/spydrnet`, so the
project does not require a sibling repository or the PyPI `spydrnet` package at runtime.
`sch_parse/` and `spydrnet_extension/` contain project-specific additions on top of upstream
SpyDrNet. A sibling `../spydrnet` checkout may be retained as a human-maintained backup, but it
is not part of the application import path or dependency resolution.
