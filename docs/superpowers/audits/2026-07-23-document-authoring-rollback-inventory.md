# Document Authoring Rollback Inventory — 2026-07-23

## Scope and method

This is a read-only classification of the governed document-authoring foundation
introduced by `18f2d68` and its live wiring at `HEAD` (`630d11e`).  It used the
specified changed-file inventory and reference search, then checked the approved
template-upload design.  No application source, `docs/ADAS/` material, or CAM
XLSM/XLSX asset was changed, staged, deleted, or reverted.

The approved design explicitly retains Project, ProjectBaseline,
SourceSetSnapshot, Evidence, TemplateVersion, WorkOrder, HarnessRun, renderer,
approval, and audit controls.  Therefore a removal connected to any of those
controls is not an autonomous rollback action.  No isolated, unreferenced dead
code was identified in the reviewed implementation.

## File inventory

| Path | Classification | Direct callers/tests | User-visible impact | Action before approval |
| --- | --- | --- | --- | --- |
| `src/agents/claim_evidence.py` | retain | `src/projects/evidence.py`, authoring models/service; P2a tests | Bounded requirements, evidence status, and validated draft assertions | none |
| `src/pipelines/document_rag/schemas.py` | retain | request context and project evidence boundary | Carries project/baseline scope and evidence lineage | none |
| `src/pipelines/document_store_sqlite.py` | retain | project source catalog and ingestion metadata | Preserves source-version/project metadata used to form frozen source sets | none |
| `src/projects/__init__.py` | retain | package consumers | Stable Project domain exports | none |
| `src/projects/access_service.py` | retain | `ProjectService` | Enforces persisted project-principal authorization | none |
| `src/projects/evidence.py` | retain | retrieval service, `DocumentGenerationService`, P2a tests | Validates evidence packages before semantic drafting | none |
| `src/projects/models.py` | retain | all Project and authoring services/tests | Defines Project, Baseline, SourceSetSnapshot, and evidence contracts | none |
| `src/projects/retrieval.py` | retain | harness runtime, P2a tests | Fail-closed retrieval limited to the frozen project source set | none |
| `src/projects/service.py` | retain | `AppPipeline`, authoring service/UI/tests | Creates and authorizes project/baseline-bound work | none |
| `src/projects/store.py` | retain | project/access/retrieval services and P2a tests | Durable Project, baseline, source, and snapshot records | none |
| `src/document_authoring/__init__.py` | retain | package consumers | Stable public authoring contracts | none |
| `src/document_authoring/models.py` | retain | all authoring modules and P2a tests | Defines template, work-order, approval, artifact, and Harness state | none |
| `src/document_authoring/deterministic_rules.py` | retain | `DocumentGenerationService`, P2a tests | Runs deterministic document units without a writer | none |
| `src/document_authoring/validator.py` | retain | service, Harness graph/runtime, renderer tests | Rejects unsupported/unverified output before rendering or release | none |
| `src/document_authoring/work_order_store.py` | retain | service, Harness runtime, UI status/downloads, P2a tests | Durable WorkOrder, artifacts, checkpoints, outbox, audit, and approval state | none |
| `src/document_authoring/worker.py` | retain | `DocumentGenerationService` | Executes queued deterministic generation outside the UI request | none |
| `src/document_authoring/service.py` | retain | `AppPipeline`, P2a tests | Transaction boundary for templates, baseline-bound work orders, generation, and release | none |
| `src/document_authoring/renderers/__init__.py` | retain | renderer package consumers | Stable renderer export boundary | none |
| `src/document_authoring/renderers/xlsm.py` | retain | service and P2a tests | Applies only approved XLSX/XLSM fills and protects formulas/active content | none |
| `src/document_authoring/writers/__init__.py` | retain | writer package consumers | Stable managed-writer exports | none |
| `src/document_authoring/writers/provider.py` | retain | managed writer and Harness graph | Constrains provider request/response contract | none |
| `src/document_authoring/writers/managed.py` | retain | service, Harness graph/runtime, P2a tests | Limits semantic drafting to managed, evidence-bound writers | none |
| `src/document_authoring/harness/__init__.py` | retain | harness package consumers | Stable Harness exports | none |
| `src/document_authoring/harness/policy.py` | retain | store, graph/runtime, P2a tests | Defines budget, lease, and permitted-tool policy | none |
| `src/document_authoring/harness/graph.py` | retain | Harness runtime | Bounded authoring execution graph with evidence validation | none |
| `src/document_authoring/harness/runtime.py` | retain | service, P2a tests | Durable authoring status, fencing, checkpoints, pause/cancel, and human routing | none |
| `src/core/app_pipeline.py` | retain | `streamlit_app.py`, document page | Wires ProjectService and DocumentGenerationService into the application | none |
| `src/ui/document_generation_page.py` | retain | `streamlit_app.py` | Project/baseline/template selection, run status, controls, and artifact download | none |
| `streamlit_app.py` | retain | Streamlit entry point | Routes authenticated users to Document Generation | none |
| `tests/test_document_authoring_p2a.py` | retain | pytest | Regression coverage for snapshots, evidence-bound Harness operation, renderer policy, durable state, and idempotency | none |
| `docs/document_authoring_p2a.md` | retain | implementation/design reference | Documents P2a governed authoring behavior | none |
| `docs/document_authoring_p2b.md` | retain | implementation/design reference | Documents managed-writer/Harness behavior | none |
| `docs/document_authoring_p2c.md` | retain | implementation/design reference | Documents later controlled authoring scope | none |
| `docs/poc_xlsm_template_inspection.md` | retain | implementation/design reference | Records XLSM safety investigation supporting renderer policy | none |

## Capabilities intentionally absent

| Capability/path family | Classification | Direct callers/tests | User-visible impact | Action before approval |
| --- | --- | --- | --- | --- |
| P3 DOCX/Markdown analyzers and renderers | not_present | No `DocxRenderer` import or implementation | Current foundation cannot automatically analyze/render DOCX or Markdown | Implement separately; do not treat absence as rollback |
| P3 batch/scheduled generation | not_present | No scheduler/batch module | No recurring or bulk document jobs | Implement separately |
| P3 authenticated REST API, business CLI, external-agent/MCP adapter | not_present | No API/CLI/MCP module | Streamlit/internal Python service remains the supported surface | Implement separately |
| P4 ProjectFact store and ProjectSnapshot cache | not_present | No facts/snapshot implementation | Retrieval remains source-snapshot/evidence based | Implement separately |
| P5 cross-source knowledge graph and advanced-document features | not_present | No graph implementation | No graph-assisted generation features | Implement separately |

## Rollback decision

- `deletion_candidate`: none found.  All reviewed implementation files have a
  live dependency, a test dependency, or retain an explicit security/audit
  boundary.
- `requires_user_approval`: none proposed.  Any future removal that touches
  Streamlit routing, source snapshots, immutable templates, Harness execution,
  renderers, approval, audit, or a user-owned document/CAM asset must first be
  listed explicitly and approved by the user.
- `docs/ADAS/` and CAM XLSM/XLSX assets: outside this audit's modification and
  staging scope; no action taken.

## Verification record

The specified reference search found `DocumentGenerationService` in the
application pipeline and P2a tests; the Streamlit entry point imports and calls
the document-generation page.  The destructive-command scan for this report is
expected to return no matches.
