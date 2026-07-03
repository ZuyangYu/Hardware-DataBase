# Pipeline Contract

This project treats every content processor as a sibling pipeline under one
application orchestration layer. Document RAG, spreadsheets, and future content
types must not be hidden inside each other.

## Layers

1. App/API layer
   - Authenticates the user.
   - Resolves department and KB permissions.
   - Calls document lifecycle and future agent services.

2. Pipeline registry
   - Declares which pipeline owns a file type.
   - Stores stable identifiers such as `processor_kind`, `content_kind`, and
     optional `dataset_kind`.
   - Rejects known unsupported variants explicitly.

3. Ingestion orchestrator
   - Performs common upload flow: route, reject unsupported variants, hash,
     duplicate detection, archive, handler dispatch, audit, and rollback.
   - Lives in `src/pipelines/ingestion.py`.
   - Must not contain pipeline-specific parsing logic.

4. Pipeline handler
   - Implements `PipelineHandler` for one content family.
   - Owns submit/reuse/rollback behavior for its pipeline.
   - The handler may call pipeline services, external APIs, or background
     workers, but must keep persistent evidence department-scoped.

5. Pipeline implementation
   - Owns parsing, indexing, cleanup, and profile generation for one content
     family.
   - Must not bypass department and KB scope.

6. Retrieval/agent layer
   - Scans department-scoped catalogs.
   - Routes a question to RAG, spreadsheet tools, or future pipeline tools.
   - Produces answers from evidence returned by those tools.

## Adding A Pipeline

Add a `PipelineSpec` in `src/pipelines/registry.py`:

- `key`: stable internal pipeline key.
- `label`: human-readable name.
- `processor_kind`: stored on document records.
- `content_kind`: high-level content family.
- `supported_extensions`: extensions the pipeline accepts.
- `rejected_extensions`: extensions that should be explicitly skipped with a
  clear user message.
- `stage`: `retrieval` for RAG-like document retrieval, `structured` for
  database/tool-backed evidence.
- `dataset_kind`: optional storage partition used by the current backend.

Then add a service or backend adapter that owns:

- parse/index
- profile/catalog read
- document cleanup
- KB cleanup
- department-scoped physical storage

Finally add a `PipelineHandler` and register it with the active ingestion
orchestrator. The handler should own only pipeline-specific behavior. Shared
upload concerns belong in `IngestionOrchestrator`.

## Isolation Rule

Every pipeline must scope persistent data by department and KB. The standard
layout is:

```text
storage/<pipeline_store>/departments/<department_id>/kbs/<kb_name>/
```

Global databases may only store routing metadata if every read/write includes
department constraints. Pipeline-owned evidence stores should prefer physical
department separation.
