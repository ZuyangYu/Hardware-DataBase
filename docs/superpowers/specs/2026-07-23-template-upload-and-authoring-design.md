# Template Upload and Governed Document Authoring Design

## Goal

Let a project member upload an XLSX, XLSM, or DOCX template in Streamlit, receive a rule-backed and LLM-assisted template analysis, confirm it with one normal-path action, and create a project-baseline-bound document-generation run whose progress and candidate artifact are visible in the same page.

## Scope

The first delivery supports only XLSX, XLSM, and DOCX templates. Markdown, batch/scheduled generation, external agents, MCP, REST/CLI delivery, ProjectFact, ProjectSnapshot, and graph expansion remain out of scope. Existing Project, ProjectBaseline, SourceSetSnapshot, Evidence, TemplateVersion, WorkOrder, HarnessRun, approval, and audit controls remain in scope and are not rollback candidates.

## User Flow

1. An authorized project member opens Document Generation and selects **Upload template**.
2. The member uploads one XLSX, XLSM, or DOCX file and gives it a display name. The server stores the exact bytes, calculates the content hash, and scans active content before any LLM call.
3. A format-specific rule analyzer extracts a format-neutral structural inventory. It includes worksheet names, cells, formulas, merged ranges, tables, headings, paragraphs, DOCX tables, content controls/bookmarks when present, and protected or active-content locations.
4. A managed LLM receives only the structural inventory. It returns a candidate schema containing semantic sections, fields, retrieval terms, candidate writable locations, writing hints, and confidence. It never receives the binary template, file paths, arbitrary project data, or unrestricted tool access.
5. The page presents a compact summary and preview: count of proposed sections, automatically writable units, human-only units, and blocked/risky units. The normal action is **Confirm and enable template**. Low-confidence, no-writable-location, or active-content findings expose an expandable correction form; users are not required to configure individual fields on the normal path.
6. Confirmation freezes the exact template hash, analysis result, approved writable bindings, renderer policy, and document schema. XLSM macro/formula/external-link/embedded-object regions and DOCX protected, signature, embedded, or external-link regions are never automatically writable.
7. The member selects a Project and an already approved ProjectBaseline. WorkOrder creation produces a SourceSetSnapshot, so generation can only retrieve that project and frozen baseline's permitted source versions.
8. The Document Harness plans field/section work, runs deterministic items directly, retrieves evidence for semantic items, calls the managed Writer with the validated Evidence Package, validates DraftAssertions, and submits an approved FillPlan to the format renderer.
9. Streamlit displays live task progress and errors, makes a review candidate downloadable, and presents the existing approval/release actions. An approved release remains hash-bound to the reviewed candidate, validation report, and source snapshot.

## Architecture

### Template Analysis

Introduce a format-neutral `TemplateAnalysis` contract and two deterministic analyzers:

- `WorkbookTemplateAnalyzer` for XLSX/XLSM. It produces sheet/cell/range structure while marking formulas, protected cells, macro relationships, external links, controls, embedded objects, and legacy/example regions as non-writable by default.
- `DocxTemplateAnalyzer` for DOCX. It produces headings, paragraphs, tables, content controls/bookmarks, and relationship/embedded-object inventories while marking unsafe or protected regions as non-writable by default.

`TemplateSchemaSuggester` consumes `TemplateAnalysis` and returns a structured candidate. It may propose semantics and writable locations, but every suggested writable location must already be present in the deterministic inventory and must not be protected by the analyzer. A failed or unavailable model leaves the template in `analysis_requires_human`; it cannot silently approve a schema.

### Registration and Approval

`DocumentGenerationService` owns one upload-to-enable transaction boundary. It registers the immutable template bytes, security report, candidate schema, regions, bindings, and renderer policy. `confirm_template_analysis` validates that candidate bindings still match the input hash and deterministic inventory, rejects protected locations, then records an approved TemplateVersion and DocumentSchema. Confirmation and approval are a single normal-path UI action but retain actor, timestamp, content hash, policy, and audit records.

### Writing and Rendering

The existing bounded Document Harness remains the only semantic orchestrator. It may retrieve only through the project-scoped Evidence Retrieval Service and writes only validated FillPlan entries. Deterministic fields do not invoke a writer. Add a `DocxRenderer` that copies the original DOCX package and edits only approved paragraph/table/content-control targets; it verifies all non-approved OOXML parts and relationships remain unchanged. The existing XLSM/XLSX renderer remains responsible for workbook output.

### Streamlit Experience

The document page has three tabs:

- **Upload template**: file upload, analysis start/status, compact result summary, expandable corrections for exceptions, and Confirm and enable template.
- **New generation**: existing Project/Baseline selection plus the enabled template; creation queues the work order.
- **Runs and downloads**: live stage timeline, current unit, retry/paused/waiting-human state, evidence/validation errors, candidate download, and approval controls.

The page refreshes job status using Streamlit's supported periodic rerun mechanism while a run is active. Progress data comes from durable WorkOrder/HarnessRun/checkpoint/outbox state, not page session state. Refreshing, navigating away, or signing in again never creates a duplicate run.

## Security and Failure Handling

- Template bytes are immutable and hash-bound throughout analysis, confirmation, rendering, and approval.
- XLSM active content remains quarantined unless an approved renderer policy allowlists the exact hash. DOCX active/embedded/external content is likewise blocked from automatic modification.
- A candidate analysis cannot approve an arbitrary locator. The service validates every binding against the rule-derived inventory.
- Missing evidence, retrieval failure, source access revocation, writer failure, low analysis confidence, and renderer integrity differences produce explicit blocked or waiting-human states. They never become fabricated values or a formal release.
- The Harness cannot access the template binary, arbitrary files, shell, SQL, or unscoped project data.

## Testing

- Unit tests cover XLSX/XLSM and DOCX structural extraction, protected-location rejection, LLM-candidate validation, confirmation hash binding, and active-content policy enforcement.
- Service tests cover upload, analysis, one-click confirmation, baseline-bound WorkOrder creation, deterministic fake Writer generation, and candidate/release hashes.
- Renderer tests use representative XLSX/XLSM and DOCX fixtures to assert only approved content changes and package relationships/non-approved parts are unchanged.
- UI tests cover upload state, analysis status, exception correction affordances, active-run polling display, error display, and candidate download.
- Integration tests use a deterministic fake LLM and project-scoped retrieval fixture to cover template upload through candidate output without a real external model.

## Separate Rollback Audit

Rollback is a separate, read-only audit. It will classify existing implementation into **retain**, **deletion candidate**, and **requires explicit approval**, identifying files, tests, dependencies, and user-visible impact. No deletion, history rewrite, or behavior rollback occurs until the user approves the exact deletion list. Code needed by this feature—project/baseline/source authorization, source snapshots, evidence validation, template contracts, renderers, work-order persistence, Harness state, and approval/audit controls—is retained.
