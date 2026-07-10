# Hardware-DataBase develop Integration Plan

Updated: 2026-07-09
Baseline: `origin/develop` at `58ee20d` (`Merge pull request #19 from ZuyangYu/fix/ragflow-retrieve-local-filter`)
Integration branch/worktree: `integrate-develop` at `D:\workspace\DataBase\Hardware-DataBase-integrate-develop`

## Current Conflict Finding

The `develop` branch has moved the application from the old `src/core/rag_pipeline.py`
and `src/rag_backends/*` shape into the newer pipeline and LangGraph agent shape:

- ingestion is owned by `src/pipelines/*`
- routing/catalog behavior is owned by `src/core/source_group_router.py` and `src/pipelines/registry.py`
- multi-source query behavior is owned by `src/agents/*`
- document RAG is represented as a pipeline-backed tool, not as the old central `RAGPipeline`

The Hardware-DataBase feature work still carries a full `src/circuit/*` subsystem
plus old integration assumptions around query routing and upload parsing. The
correct integration direction is therefore not to restore the deleted RAG stack.
It is to adapt circuit upload and circuit query into the new sibling-pipeline model.

## Resolved Integration Shape

The local conflict-resolution branch uses the current `develop` architecture as
the source of truth and adds the circuit feature through narrow extension points:

- `src/pipelines/registry.py`
  - adds `circuit_design` as a structured pipeline
  - stores circuit records with `dataset_kind="circuit"`, `content_kind="circuit_design"`, and `processor_kind="circuit_design"`
  - routes `.edf` and `.edif` to the circuit processor

- `src/pipelines/ingestion.py`
  - adds `CircuitPipelineHandler`
  - archives circuit files and writes scoped ledger records
  - deliberately does not upload circuit files to RAGFlow
  - marks records as archived and waiting for circuit parsing

- `src/pipelines/runtime_factory.py`
  - registers `CircuitPipelineHandler` beside the RAGFlow and spreadsheet handlers

- `src/core/source_group_router.py`
  - keeps umbrella design routing while classifying netlist and schematic subgroups

- `src/agents/runner.py` and `src/agents/tools/circuit_tools.py`
  - exposes a `circuit_query` tool to the multi-source agent
  - returns no evidence until the structured circuit backend is connected

- `src/agents/prompts.py`
  - allows planner and sufficiency checks to select `circuit_query` when `circuit_design` evidence is missing

## Migration Phases

1. Keep the new pipeline shell
   - Preserve the current `develop` ingestion/agent architecture.
   - Do not bring back `src/core/rag_pipeline.py`, `src/rag_backends/*`, or the old `query_router` path.
   - Keep the first circuit handler limited to archive + ledger + audit.

2. Wire circuit structured indexing
   - Move `src/circuit/upload_service.py` behind the `CircuitPipelineHandler`.
   - Replace old parser registry/model factory assumptions with explicit circuit parser services.
   - Store derived graph/entity evidence under department and KB scope.
   - Add cleanup hooks equivalent to spreadsheet index deletion.

3. Replace the placeholder query tool
   - Connect `CircuitQueryTool` to the scoped circuit store/query engine.
   - Return agent evidence in the same shape used by document and spreadsheet tools.
   - Add support for source-scoped queries so the planner can target a specific circuit archive.

4. Update UI entry points
   - Keep `streamlit_app.py` on the latest `develop` layout.
   - Add circuit upload/status affordances through existing pipeline record views.
   - Add circuit browsing panels only after the backend store has stable scoped reads.

5. Dependency cleanup
   - Keep `spydrnet` or other circuit-specific dependencies only if they are exercised by the parser path.
   - Avoid reintroducing the old LlamaIndex/Chroma stack from the feature branch unless a concrete circuit component still needs it.

## Verification Gates

The local integration branch currently passes the focused circuit integration tests:

```text
python -m unittest discover -s tests -p "test_circuit_*.py" -v
```

Expected result on 2026-07-09: 6 tests pass.

Before merging into `develop`, also run the existing develop test suite in an
environment with the full project dependencies installed, especially `langgraph`.
The current machine only exposed a bare Miniconda Python, so the focused tests
avoid importing the full LangGraph runner graph.

## Open Risks

- The branch has Windows line-ending noise in the working tree. Review with
  `git diff --ignore-space-at-eol` or normalize line endings before creating the PR.
- `src/circuit/*` is still largely untracked in the local integration worktree.
  It needs a second pass to remove old architectural assumptions before staging.
- `CLAUDE.md`, `_verify.txt`, `src/test_data/*`, and `src/ui/*` are feature-branch
  artifacts. Decide explicitly whether they belong in this integration PR.
- Circuit query currently has a registered tool name but no connected evidence backend.
  This is intentional for conflict resolution, but not sufficient for end-user circuit QA.
