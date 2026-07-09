# Circuit Query Incremental Develop Integration Design

Updated: 2026-07-09

## Goal

Integrate circuit design query capability from `composite-query-angent` into the current `develop` architecture as an incremental feature. The integration must keep the develop pipeline and LangGraph agent model as the source of truth.

## Non-Goals

- Do not restore `src/query_router/*` as a top-level query path.
- Do not restore `src/core/rag_pipeline.py` or the old `src/rag_backends/*` application shape.
- Do not make circuit query a separate top-level router beside `MultiSourceAgentRunner`.
- Do not reintroduce old Streamlit router controls such as `QUERY_ROUTER_USE_LLM`.
- Do not claim full composite circuit/manual QA until circuit indexing and `circuit_query` return real evidence.

## Current State

The current integration worktree already follows the new develop direction:

- `src/core/app_pipeline.py` calls `MultiSourceAgentRunner`.
- `src/agents/*` owns multi-source query orchestration.
- `src/pipelines/*` owns ingestion and structured pipeline routing.
- `src/pipelines/registry.py` registers `circuit_design` for `.edf` and `.edif`.
- `src/agents/runner.py` registers a `circuit_query` tool.

The remaining gap is functional: `src/agents/tools/circuit_tools.py` currently returns an empty evidence list. `CircuitPipelineHandler` archives circuit files and writes ledger rows, but does not parse, index, or expose structured circuit evidence.

## Design Decision

Use a phased integration:

1. Build the smallest working circuit upload -> parse/index -> query -> evidence loop.
2. Incrementally migrate reusable circuit-domain modules from the feature branch behind this loop.
3. Move composite multi-source behavior into the existing LangGraph planner instead of restoring the old query router.

This keeps circuit query as a sibling structured pipeline and agent tool, not a competing architecture.

## Architecture

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

The important boundary is `CircuitIndexService`. It adapts circuit-domain code to the develop pipeline and agent interfaces.

## Components

### CircuitIndexService

Create a service module, preferably `src/circuit/index_service.py`, with a small public API:

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

`CircuitIndexService` is responsible for:

- choosing the correct parser for `.edf` / `.edif`;
- writing parsed circuit state into the scoped circuit store;
- preserving `kb_name`, `department_id`, source file, record id, and provenance;
- mapping query engine results into `src.agents.state.Evidence`;
- hiding old feature-branch implementation details from pipelines and agents.

### CircuitPipelineHandler

Modify `src/pipelines/ingestion.py` so `CircuitPipelineHandler` receives either a `CircuitIndexService` or a callback from `runtime_factory`.

Phase 1 may parse synchronously after archive and ledger write, matching a minimal viable path. If parsing becomes slow, keep the same handler interface and move execution behind a worker later.

Expected record states:

- `archived`: source file is stored and ledgered.
- `indexed`: structured circuit state was parsed and queryable.
- `failed`: parser/indexing failed, with `error_message` populated.

The handler must not upload `.edf` / `.edif` to RAGFlow.

### CircuitQueryTool

Replace the current empty implementation in `src/agents/tools/circuit_tools.py`.

The tool should:

- accept the existing `run(query, kb_name, ctx, top_k, filters)` signature;
- call `CircuitIndexService.query`;
- return `list[Evidence]`;
- return an empty list only when no indexed circuit evidence exists;
- surface tool failures as exceptions so `retrieve_evidence` diagnostics can mark the tool call failed.

### LangGraph Multi-Source Planner

Keep `src/agents/graph.py` as the only top-level query workflow.

The planner should continue to generate `circuit_query` calls when a sub-question expects `circuit_design`. Multi-source questions should be represented as multiple tool calls:

- actual connection, net, refdes, module facts -> `circuit_query`;
- design manual and explanatory text -> `document_rag`;
- BOM, quantities, alternatives, test matrix -> `spreadsheet_semantic` or `spreadsheet_cell`.

Final synthesis stays in the develop agent answer path, using circuit evidence alongside document and spreadsheet evidence.

### Migrated Circuit Modules

Allow migrating reusable modules from `composite-query-angent` under `src/circuit/*` when they serve the new service boundary:

- parsers and models;
- circuit store;
- query engine;
- entity resolver;
- recovery helpers;
- response policy helpers;
- domain query agent internals, if invoked only through `CircuitIndexService` / `CircuitQueryTool`.

Do not migrate `src/query_router/*`. Any logic worth preserving from `CompositeQueryAgent` should be translated into LangGraph planner behavior or prompt rules.

## Data Flow

### Ingestion

1. User uploads `.edf` or `.edif`.
2. `PipelineRegistry.route_file()` selects `processor_kind="circuit_design"`.
3. `IngestionOrchestrator` archives the file.
4. `CircuitPipelineHandler.submit()` writes a circuit ledger record.
5. `CircuitIndexService.index_file()` parses and stores structured circuit data.
6. Handler updates progress and status to `indexed` or `failed`.

### Query

1. User asks a circuit or composite question.
2. `MultiSourceAgentRunner` runs the LangGraph workflow.
3. Planner includes `circuit_query` for circuit evidence needs.
4. `retrieve_evidence` invokes `CircuitQueryTool`.
5. `CircuitQueryTool` returns normalized `Evidence`.
6. Existing scoring, sufficiency, follow-up planning, and final answer synthesis use that evidence.

## Evidence Mapping

Circuit evidence must use the same model as other tools:

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

Evidence content must be directly grounded in parsed circuit data. LLM synthesis may rewrite final answers, but should not invent circuit facts.

## Error Handling

- Parser failure updates the pipeline record to `failed` and stores the exception summary.
- Querying a KB with no indexed circuits returns no evidence, not a crash.
- Querying with an invalid source filter returns no evidence.
- Unexpected service errors propagate to `retrieve_evidence`, which already records failed tool diagnostics.
- Delete operations should be best-effort but report cleanup failures in the existing handler result style.

## Testing

Minimum tests for Phase 1:

- routing: `.edf` and `.edif` map to `circuit_design`;
- ingestion: `CircuitPipelineHandler.submit()` calls the index service and updates status;
- ingestion failure: parser/index error marks the record failed with a message;
- query tool: indexed fixture data returns non-empty `Evidence`;
- query empty: no indexed circuit data returns `[]`;
- cleanup: deleting a circuit record calls circuit index cleanup;
- agent integration: a circuit-design source plan triggers `circuit_query`.

The existing `.gitignore` rule that ignores `tests/` must be corrected so new circuit tests are committed and run in CI.

## Verification

Phase 1 is acceptable when these commands pass in an environment with project dependencies installed:

```text
python -m unittest discover -s tests -p "test_circuit_*.py" -v
python -m unittest tests.test_agentic_runner -v
python -m py_compile src\core\app_pipeline.py src\agents\tools\circuit_tools.py src\agents\runner.py src\agents\graph.py
```

If `langgraph` is missing, dependency installation must be fixed before claiming full agent verification.

## Migration Plan

### Phase 1: Minimal Working Loop

- Add `CircuitIndexService`.
- Connect `CircuitPipelineHandler` to indexing.
- Replace the empty `CircuitQueryTool` implementation.
- Add committed tests and `.gitignore` test exceptions.
- Keep UI changes limited to existing pipeline status display.

### Phase 2: Circuit Query Quality

- Migrate selected `src/circuit/*` modules from the feature branch behind `CircuitIndexService`.
- Add entity resolution, scoped search, and recovery behavior.
- Preserve circuit session context only if it can be keyed by existing chat/session context and does not add a new top-level router.

### Phase 3: Composite Multi-Source Behavior

- Translate useful `CompositeQueryAgent` behavior into LangGraph planning prompts and source selection.
- Make circuit/manual/BOM questions produce multiple tool calls in one retrieval round.
- Improve sufficiency checks for missing circuit evidence separately from missing document or spreadsheet evidence.

## Acceptance Criteria

- `develop` architecture remains centered on `src/pipelines/*` and `src/agents/*`.
- `.edf/.edif` files become queryable structured circuit records.
- `CircuitQueryTool.run()` returns real `Evidence` for indexed circuit data.
- Composite questions use the existing LangGraph runner and multiple tools.
- No production path imports `src/query_router/*`.
- New circuit tests are tracked by git and can run in CI.
- The final answer includes circuit sources when circuit evidence is used.
