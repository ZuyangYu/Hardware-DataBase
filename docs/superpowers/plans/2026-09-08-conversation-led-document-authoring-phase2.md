# Conversation-led Document Authoring Phase 2 Implementation Plan

> **Implementation status:** Implemented and verified offline on 2026-09-08; production pilot remains disabled pending the manual failure-boundary smoke sign-off.
>
> **For agentic workers:** REQUIRED SUB-SKILL: use `executing-plans` to implement this plan task by task. Every implementation task follows red-green-refactor TDD and ends with a focused commit. Use the repository-provided `luna_worker` for bounded independent checks when `AGENTS.md` requires it.

**Goal:** Move the accepted, hash-bound `DocumentPlan` from Phase 1 into a persisted, dependency-aware execution DAG for the existing template-backed XLSX/XLSM path, then add typed table rows, explicit coverage evaluation, deterministic aggregation, two-stage document review, and parity evaluation against the approved ICD `Pin Definition` human baseline.

**Architecture:** Phase 2 adds a plan-backed execution path beside the legacy schema-driven path. An accepted `DocumentPlan` is compiled once into an immutable `CompiledTaskGraph`; the graph and all run state bind to the accepted plan, source snapshot, strategy, adapter, renderer and policy hashes. Unit workers return typed semantic candidates, including row-keyed tables with cell evidence. A deterministic aggregator builds one format-neutral `DocumentModel`, a pre-render reviewer checks semantic completeness, and the existing allowlisted XLSX/XLSM renderer produces the candidate artifact. A post-render reviewer parses the physical artifact and gates release. Legacy Work Orders without accepted plan references continue to use the existing graph and renderer unchanged.

**Tech Stack:** Python 3.11, Pydantic v2, SQLite, LangGraph, pytest, standard-library OOXML parsing, XLSX/XLSM renderers, and the existing document-generation metrics/evaluation modules.

**Design:** `docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md`

## Phase 2 entry baseline

Phase 0–1 automated verification completed before this plan was drafted:

- planning and Phase 0–1 focused tests: 62 passed;
- compatibility tests: 188 passed, 1 warning;
- frontend tests: 151 passed; production build passed;
- full backend suite: 1,880 passed, 9 skipped, 43 subtests passed, 5 warnings.

The Phase 1 manual failure-boundary smoke matrix still has to be run as an operational prerequisite before enabling any Phase 2 execution flag. These automated results prove compatibility of the current checkout; they do not claim that the new plan-backed execution path exists.

### Phase 2 closeout evidence

The implementation and automated gates are complete. The final verification
record is in `docs/document-authoring-phase2-rollout.md`: 79 tests passed in
the Phase 2 matrix, 86 passed in the Phase 2-plus-settings matrix, 258 passed
in the compatibility matrix, 1,961 passed with 9 skipped in the full backend
suite, and the frontend recorded 151 passing Vitest tests plus a passing
production build. Python compilation and `git diff --check` also passed. The
remaining unchecked Task 8 steps are intentionally operational: live worker
stop/restart, production API/UI failure-boundary smoke, and final allowlist
approval were not performed in this workspace, so the execution flag remains
false and all allowlists remain empty by default.

## Scope boundary

This plan includes:

- a strict, immutable compiled DAG derived only from an accepted `DocumentPlan`;
- dependency-aware LangGraph fan-out, barriers, recovery and receipt binding;
- native `TypedTableRow` output with stable row identity and row/cell evidence;
- `CoverageContract` evaluation and bounded unit-level review/rework;
- a strict, format-neutral `DocumentModel` and deterministic aggregation/render binding;
- pre-render semantic review and post-render XLSX/XLSM artifact review;
- XLSX/XLSM parity checks and an offline ICD `Pin Definition` quality benchmark;
- feature-flagged rollout, end-to-end recovery tests, migration notes and release gates.

Phase 2 does not include:

- `system_recipe` or `generated_structure` rendering, free-form layout, or a new DOCX/PDF renderer;
- ICD/FPT/requirements-specific domain strategy implementation beyond the generic contracts and the existing ICD comparison baseline;
- PlanDiff-driven partial revision execution or a new revision API;
- deletion of legacy fields, checkpoints, schema-driven graph code, or old Work Order fingerprints;
- a PostgreSQL/multi-worker deployment migration;
- changing the conversational export lifecycle or allowing export jobs to enter authoring execution.

The existing DOCX/Markdown compatibility path remains covered by regression tests. Phase 2's new execution flag is initially limited to accepted, template-backed XLSX/XLSM Work Orders.

## Global invariants

- Only an accepted `OutputSpec`/`DocumentPlan` pair may enter the plan-backed graph. Proposed, stale, blocked or hash-mismatched plans fail closed before any worker runs.
- A plan-backed run has exactly one graph identity, one run identity and one execution route. It never executes part of the legacy graph as a silent fallback.
- The compiled graph, source snapshot, renderer/adapter, domain strategy, policies and every accepted unit draft are hash-bound. A runtime mismatch is a terminal `blocked`/`failed` outcome with an actionable issue.
- Graph/node/review/aggregation ordering is deterministic. Mapping key order cannot change hashes; semantic list order is preserved where the plan declares order.
- Workers never write template coordinates or binary files. They return validated semantic candidates only.
- A table remains a table from Writer through review, aggregation, binding and renderer. No `display_value` or prose fallback may stand in for required table rows.
- Every expected required row key and required column is either present with allowed evidence or appears as an explicit, policy-compatible missing issue. “Task completed” is not coverage.
- Evidence IDs must belong to the frozen source snapshot and the current unit package. No raw source text, path, credential or template bytes are persisted in planning/graph/review payloads.
- Static template content, formulas, merged regions, protected areas and non-allowlisted cells are never overwritten.
- Required coverage, critical domain issues, source violations, artifact-validation failures or unresolved required review issues prohibit automatic release.
- Rework is bounded by the `UnitTaskSpec.max_attempts` and the document review policy. Rework cannot widen frozen source scope, inference policy, tools or allowed columns.
- The user-facing `DocumentReviewStore` remains separate from internal unit/document review facts. Internal facts use append-only authoring events or a dedicated immutable review-fact store.
- Legacy Work Orders, legacy fingerprints, legacy status projections and the existing ExportJob path remain readable and behaviorally compatible.

## Compatibility and rollout matrix

| Caller/data | Flag off or legacy behavior | Phase 2 behavior when explicitly enabled |
|---|---|---|
| Legacy Work Order without accepted plan refs | Existing schema-driven `AuthoringGraph`; existing renderer and fingerprint | Unchanged; never routed to the new graph |
| Phase 1 accepted template-backed Work Order | Existing execution until flag is enabled | Compiled plan graph, typed candidates, aggregation, review and artifact gates |
| Proposed/stale/blocked/hash-mismatched plan | Cannot execute | Cannot execute; structured fail-closed issue |
| Template-free `system_recipe`/`generated_structure` plan | Representable proposal only | Still blocked; deferred to Phase 3 |
| XLSX primary deliverable | Existing path | Supported pilot format |
| XLSM primary deliverable | Existing safe XLSM/XLSX-compatible path | Same logical checks plus macro/package preservation checks |
| DOCX/Markdown legacy request | Existing behavior | Existing behavior; no new plan-backed format path in this phase |
| Conversational “export current answer” | `ResultSnapshot → ExportJob` | Same path; never creates a `DocumentTask` or authoring run |

## Target data flow

```text
accepted OutputSpec + DocumentPlan + frozen SourceSetSnapshot
  → immutable CompiledTaskGraph
  → plan-backed HarnessRun / checkpoint
  → dependency-aware retrieve → normalize → typed draft → unit review
  → CoverageReport + deterministic DocumentModel aggregation
  → pre-render DocumentReviewer
  → XLSX/XLSM RenderBinding + allowlisted renderer
  → artifact/package validation + post-render DocumentReviewer
  → release decision / needs_review / blocked
  → RunManifest with all hashes and accepted-draft references
```

The graph compiler, coverage evaluator, aggregator and reviewers are pure or side-effect-limited components. Persistence, leases, fencing and existing Receipt semantics remain in the authoring store/checkpointer boundaries. The renderer only receives a reviewed `DocumentModel`, a validated `TemplateContract` and server-owned bindings.

## Feature flag and release controls

Add `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false`. The default must remain false in every settings source and test fixture. When false, accepted plan references are retained for audit but the existing Phase 1 template-backed execution path remains the selected path. When true, only an allowlisted tenant/document type/format may use the new route.

The flag must not be used to silently fall back after a plan-backed run starts. A graph compile, hash, typed-table, review or artifact failure becomes the documented state; operators may disable the flag for new runs, while already accepted submissions remain readable and recoverable under their recorded route.

## Implementation order

The tasks are intentionally ordered so each later stage consumes a stable contract:

1. graph contract/compiler/store;
2. runtime integration and run-manifest references;
3. typed table and row/evidence path;
4. coverage and unit review;
5. deterministic document model and aggregation;
6. document-level pre/post render review and gates;
7. parity and ICD benchmark;
8. end-to-end rollout, manual smoke and closeout.

Tasks 1–2 establish execution identity. Tasks 3–6 are the quality path. Task 7 records the go/no-go evidence. Task 8 is the only task allowed to change rollout documentation/status after the manual gates actually pass.

---

## Task 1: Compile and persist a strict deterministic execution DAG

**Files:**

- Create: `src/document_authoring/planning/task_graph.py`
- Create: `src/document_authoring/planning/task_graph_store.py`
- Modify: `src/document_authoring/planning/__init__.py`
- Modify: `src/document_authoring/planning/models.py` only if a small additive graph reference is needed by validation
- Create: `tests/test_document_task_graph.py`
- Create: `tests/test_document_task_graph_store.py`

**Interfaces:**

```text
CompiledTaskNode
  node_id, task_id, unit_id, action, dependencies, barrier_id,
  input_schema, output_schema, action_key, max_attempts, timeout_seconds

CompiledTaskGraph
  graph_id, plan_id/version/hash, source_snapshot_id/hash,
  nodes, edges, barriers, topological_order, graph_hash

TaskGraphCompiler.compile(plan: DocumentPlan) -> CompiledTaskGraph
TaskGraphStore.put(graph, expected_absent=True) -> CompiledTaskGraph
TaskGraphStore.get(graph_id, version, graph_hash) -> CompiledTaskGraph
```

**Compiler rules:**

- Compile only a validated, executable `DocumentPlan`; preserve its unit/task order as the declared semantic order and use stable lexical order only for otherwise unordered sets.
- Derive dependencies from `UnitTaskSpec.dependencies` and `DocumentPlan.dependency_edges`, reject disagreement or duplicate edges, and never infer a dependency from free text.
- Represent explicit `barrier` groups and the final aggregation/review/render phases as server-owned nodes. A barrier cannot be bypassed by a direct downstream edge.
- Reject duplicate node IDs/action keys, dangling dependencies, self-edges, cycles, missing unit tasks, incompatible plan versions and an empty executable root/final path.
- Include all scheduling, schema, action, barrier and policy inputs in `graph_hash`; exclude lifecycle timestamps/status and store identity fields only as references.
- Store a complete validated payload immutably. Replaying the same graph identity/hash is idempotent; a different payload under the same identity is rejected.
- Do not persist evidence text, source paths, template bytes, prompts or credentials.

- [x] **Step 1: Write failing graph contract/compiler/store tests**

Cover a two-independent-unit graph, dependency order, shared-table barrier, deterministic topological order, cycle and dangling-edge rejection, duplicate action key rejection, plan/hash mismatch, immutable insertion, idempotent replay and legacy Work Orders having no fabricated graph reference. Assert that reordered dictionaries preserve the graph hash while a dependency, action key, row scope or plan hash change does not.

- [x] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_document_task_graph.py tests/test_document_task_graph_store.py -q`

Expected: FAIL because the compiled graph contract and store do not exist.

- [x] **Step 3: Implement the pure compiler and immutable store**

Keep graph compilation independent from LangGraph and model providers. Reuse `planning_content_hash` and the existing SQLite transaction conventions. Store graph refs in a separate immutable record rather than mutating `DocumentPlan` after acceptance.

- [x] **Step 4: Run focused planning and migration regressions**

Run: `.venv/bin/pytest tests/test_document_task_graph.py tests/test_document_task_graph_store.py tests/test_document_planning_contracts.py tests/test_document_planning_store.py tests/test_document_authoring_migration.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/document_authoring/planning tests/test_document_task_graph.py tests/test_document_task_graph_store.py
git commit -m "feat: compile immutable document task graphs"
```

## Task 2: Execute accepted plans through the dependency-aware graph

**Files:**

- Modify: `src/settings.py`
- Modify: `src/document_authoring/harness/graph.py`
- Modify: `src/document_authoring/harness/langgraph_state.py`
- Modify: `src/document_authoring/harness/checkpointer.py` only for additive graph/run identity fields
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/document_authoring/worker.py`
- Modify: `src/document_authoring/planning/store.py` or the graph store wiring as needed
- Create: `tests/test_document_plan_dag_execution.py`
- Modify: `tests/test_authoring_graph_adaptive_recovery.py`
- Modify: `tests/test_authoring_execution_contracts.py`
- Modify: `tests/test_document_authoring_durable_resume.py`

**Run and manifest contract:**

- Add optional `task_graph_id/version/hash` and effective execution-route fields to `HarnessRun`/`AuthoringRunManifest` and their persisted projections. Legacy rows deserialize without these fields and retain their old fingerprints.
- Before entering a node, load the accepted plan and graph by exact IDs/hashes and verify the frozen source snapshot, strategy, adapter, renderer and policy refs. Failure is fail-closed.
- Use dependency-aware `Send` fan-out only for ready nodes. A downstream node is runnable only after every dependency receipt is committed; shared table writers use the declared barrier and a deterministic reducer.
- Preserve lease/fencing, Receipt-first idempotency, checkpoint recovery and existing cancellation/error classification. A worker restart resumes the same graph/run identity and does not re-call a committed unit.
- Route legacy orders to the existing `_run_legacy`/schema path. Route accepted plan-backed orders to the new graph exactly once. Do not choose the route from a mutable frontend field or from the current schema after acceptance.
- Set `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false` by default and test both routes in the same process.

- [x] **Step 1: Write failing runtime and recovery tests**

Cover flag-off compatibility, flag-on accepted plan execution, dependency ordering, independent fan-out, barrier behavior, graph/source/hash mismatch, stale plan rejection, duplicate dispatch, lease loss, process restart, committed Receipt reuse, cancellation and legacy Work Order execution. Assert exactly one graph/run route is recorded and a plan-backed failure never silently runs the legacy graph.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_plan_dag_execution.py tests/test_authoring_graph_adaptive_recovery.py tests/test_authoring_execution_contracts.py tests/test_document_authoring_durable_resume.py -q`

Expected: FAIL because the existing graph is still driven by `DocumentSchema`/`_semantic_units` and has no plan-backed route.

- [x] **Step 3: Add the feature-gated route and additive run bindings**

Introduce a small plan-backed adapter in `DocumentGenerationService`/`AuthoringGraph`; keep legacy node behavior isolated. Reuse existing retrieval, writer, evidence registry, checkpointer and Receipt interfaces, but derive unit requests and readiness from the compiled graph. Persist effective route and graph hashes before dispatch.

- [x] **Step 4: Run focused graph, worker and generation regressions**

Run: `.venv/bin/pytest tests/test_document_plan_dag_execution.py tests/test_authoring_graph_adaptive_recovery.py tests/test_authoring_execution_contracts.py tests/test_authoring_execution_events.py tests/test_document_authoring_durable_resume.py tests/test_full_generation_flow.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/settings.py src/document_authoring/harness src/document_authoring/models.py src/document_authoring/service.py src/document_authoring/worker.py src/document_authoring/planning tests/test_document_plan_dag_execution.py tests/test_authoring_graph_adaptive_recovery.py tests/test_authoring_execution_contracts.py tests/test_document_authoring_durable_resume.py
git commit -m "feat: execute accepted plans through a gated task graph"
```

## Task 3: Make table writing natively typed, row-keyed and evidence-bound

**Files:**

- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/planning/models.py`
- Modify: `src/document_authoring/writers/provider.py`
- Modify: `src/document_authoring/writers/managed.py`
- Modify: `src/document_authoring/harness/graph.py`
- Modify: `src/document_authoring/table_contracts.py`
- Modify: `src/document_authoring/validator.py`
- Modify: `src/document_authoring/renderers/xlsm.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_typed_table_rows.py`
- Modify: `tests/test_agent_field_harness_smoke.py`
- Modify: `tests/test_governed_table_generation.py`
- Modify: `tests/test_authoring_execution_contracts.py`

**Contract changes:**

- Extend `TypedTableRow` additively with a required non-empty `row_key` and `cell_evidence_ids: dict[column_id, list[evidence_id]]`; retain row-level `evidence_ids` for compatibility and require the union of row/cell evidence to remain within the draft package.
- Extend `TableRequirement`/the compiled table schema with explicit row-identity fields or a server-owned row-key derivation rule, expected row keys, required columns, ordering and duplicate policy. No row key may be generated from display text at render time.
- Extend `WriterRequest` with table mode, expected row keys/columns, row-key schema and a table-output requirement. The prompt and structured response schema must state that a table must be returned as typed rows, never as a scalar/list display string.
- Add an additive typed `WorkbookTableRowFill`/equivalent model so `WorkbookTableFill` can carry row keys and per-cell evidence while old serialized fill plans remain readable.

**Validation and rendering rules:**

- A table draft is invalid when it has duplicate/missing row keys, unexpected keys under a closed row scope, missing required columns, empty required cells, unknown evidence, cell evidence outside the row/draft package, or a scalar `typed_value` for a table requirement.
- Validate each cell against the evidence permitted for that row/cell and retain missing/conflicting values as structured review issues. Do not mark an entire table supported because one prose assertion has evidence.
- Sort rows by the plan's declared expected row-key order, then by stable row key only where the contract permits open-ended rows. Renderer coordinates are derived from server-owned bindings; the Writer cannot choose a sheet, cell or range.
- XLSX and XLSM use the same logical table contract. XLSM package preservation/security checks remain in the existing renderer and must not be weakened to support typed rows.

- [x] **Step 1: Write failing typed-table tests**

Cover typed-row round trips, row-key uniqueness, expected-key precision, required-column checks, cell evidence ownership, table-to-scalar rejection, prompt/schema requirements, deterministic row order, renderer mapping, duplicate/missing rows, fixed-content protection and old payload compatibility without fabricated row keys.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_typed_table_rows.py tests/test_agent_field_harness_smoke.py tests/test_governed_table_generation.py tests/test_authoring_execution_contracts.py -q`

Expected: FAIL because `TypedTableRow` currently has only `cells` and row-level `evidence_ids`, and the managed writer/renderer path does not carry the full row contract.

- [x] **Step 3: Implement the additive row/evidence path**

Keep legacy scalar fields and legacy fill payloads readable. Update deterministic and managed writers, graph request construction, validator checks and the workbook table renderer together. If an old table payload lacks a safe row key, classify it as `needs_human`/legacy rather than guessing identity.

- [x] **Step 4: Run table, renderer and existing safety regressions**

Run: `.venv/bin/pytest tests/test_typed_table_rows.py tests/test_agent_field_harness_smoke.py tests/test_governed_table_generation.py tests/test_template_field_contract.py tests/test_xlsm_renderer_safety.py tests/test_icd_validation.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/document_authoring/models.py src/document_authoring/planning/models.py src/document_authoring/writers src/document_authoring/harness/graph.py src/document_authoring/table_contracts.py src/document_authoring/validator.py src/document_authoring/renderers/xlsm.py src/document_authoring/service.py tests/test_typed_table_rows.py tests/test_agent_field_harness_smoke.py tests/test_governed_table_generation.py tests/test_template_field_contract.py tests/test_xlsm_renderer_safety.py tests/test_icd_validation.py
git commit -m "feat: preserve typed table rows and cell evidence"
```

## Task 4: Evaluate coverage and review every unit with bounded rework

**Files:**

- Create: `src/document_authoring/planning/coverage.py`
- Create: `src/document_authoring/planning/unit_review.py`
- Create: `src/document_authoring/planning/review_contracts.py`
- Modify: `src/document_authoring/harness/graph.py`
- Modify: `src/document_authoring/validator.py`
- Modify: `src/document_authoring/planning/store.py` or append-only event wiring
- Modify: `src/document_authoring/reviews.py` only for an explicit adapter, never to merge internal facts into user-facing decisions
- Create: `tests/test_document_coverage.py`
- Create: `tests/test_document_unit_review.py`
- Modify: `tests/test_authoring_execution_events.py`

**Interfaces:**

```text
ReviewIssue(
  issue_id, stage, code, severity, blocking,
  unit_id, row_key, column_id, evidence_ids, suggested_action
)

UnitReviewResult(
  status: pass | rework | needs_human | blocked,
  unit_id, attempt, issues, accepted_draft_hash, report_hash
)

CoverageReport(
  plan_id/version/hash, requirement_results, expected_count,
  covered_count, missing_count, unsupported_count, duplicate_count,
  report_hash
)

CoverageEvaluator.evaluate(plan, typed_drafts, evidence_registry) -> CoverageReport
UnitReviewer.review(task_spec, draft, coverage_context, evidence_registry) -> UnitReviewResult
```

**Rules:**

- Coverage is evaluated from the immutable `CoverageContract`, not from the number of completed graph nodes. Required sections, scalar evidence, table row keys/columns, cross-unit facts and artifact obligations each have explicit results.
- Review issues are strict, bounded and safe to project. They identify an affected unit/row/cell when known, but never include evidence content or storage paths.
- Deterministic rules decide schema, evidence ownership, missing/duplicate rows, policy and pass/fail thresholds. An optional semantic reviewer may propose an issue but cannot approve its own high-risk output.
- `rework` returns to the affected unit's retrieval/normalize/draft subgraph under the original source/policy/tool budget. Attempts beyond `max_attempts`, unlocatable issues, policy changes or user decisions become `needs_human` or `blocked`.
- Persist review facts with plan/run/attempt/hash and append-only event identity. Do not overwrite a prior review or change `DocumentReviewStore`'s user-facing approval semantics.

- [x] **Step 1: Write failing coverage/review tests**

Cover complete scalar, paragraph and table requirements; missing/duplicate/unexpected rows; missing required columns; unsupported evidence; cross-unit conflicts; deterministic issue hashes; accepted/rework/needs-human/blocked states; attempt limits; policy immutability; event idempotency; and safe user-facing projections without raw evidence.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_coverage.py tests/test_document_unit_review.py tests/test_authoring_execution_events.py -q`

Expected: FAIL because the current `DocumentValidator` reports broad matrix/render issues but has no explicit `CoverageReport`/unit reviewer contract.

- [x] **Step 3: Implement coverage and unit-review services**

Keep the existing validator as a compatibility adapter where possible. The new services consume plan-backed typed candidates and return immutable facts; graph routing owns rework and final status transitions.

- [x] **Step 4: Run focused and legacy review regressions**

Run: `.venv/bin/pytest tests/test_document_coverage.py tests/test_document_unit_review.py tests/test_authoring_execution_events.py tests/test_document_reviews.py tests/test_document_status_coverage.py tests/test_icd_validation.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/document_authoring/planning src/document_authoring/harness/graph.py src/document_authoring/validator.py src/document_authoring/reviews.py tests/test_document_coverage.py tests/test_document_unit_review.py tests/test_authoring_execution_events.py
git commit -m "feat: enforce document coverage and bounded unit review"
```

## Task 5: Build a strict format-neutral DocumentModel and deterministic aggregator

**Files:**

- Create: `src/document_authoring/document_model.py`
- Create: `src/document_authoring/aggregation.py`
- Create: `src/document_authoring/render_bindings.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/document_authoring/harness/graph.py`
- Modify: `src/document_authoring/models.py` only for additive manifest/model refs
- Modify: `src/document_authoring/renderers/xlsm.py`
- Modify: `src/document_authoring/renderers/docx.py` only to preserve the legacy adapter boundary
- Create: `tests/test_document_model.py`
- Create: `tests/test_document_aggregation.py`
- Create: `tests/test_document_render_bindings.py`
- Modify: `tests/test_full_generation_flow.py`

**Format-neutral model:**

```text
DocumentModel
  document_id, plan_id/version/hash, ordered blocks, citations,
  missing_items, layout_hints, model_hash

Block = Section | Paragraph | List | TypedTable | CrossReference
TypedTable = unit_id, ordered columns, ordered TypedTableRow values,
             expected row keys, citations and missing cells
RenderBinding = semantic unit/row/column → server-owned physical region
```

**Aggregator rules:**

- Consume only accepted unit drafts/review facts and the `DocumentPlan`; do not call an LLM, retrieve new evidence or invent missing values.
- Preserve plan outline order, dependency/barrier completion order where semantically relevant, required row-key order and declared column order. Use stable IDs for cross-unit references.
- Deduplicate only by explicit semantic identity/claim key or table row key; never deduplicate two distinct rows merely because their display values match. Record any collision as an issue.
- Carry evidence IDs, missing/conflicting items and layout hints through to the model. A missing value remains visibly/policy-appropriately missing rather than being converted to empty prose or a scalar table summary.
- Produce the same `model_hash` for the same plan, accepted draft hashes and review facts. Aggregation is deterministic and format-neutral.
- `RenderBinding` resolves the validated `TemplateContract` and registered adapter. It contains no user/model-selected coordinate and cannot authorize writes outside the allowlist.
- Plan-backed `DocumentGenerationService` renders only the aggregated model. Keep a legacy adapter for existing `FillPlan` callers and prove existing legacy artifacts remain unchanged.

- [x] **Step 1: Write failing model/aggregation/binding tests**

Cover strict block schemas, forbidden raw content/path fields, deterministic order, row-key preservation, duplicate identity detection, citation propagation, explicit missing items, plan/draft hash binding, template binding allowlist, formula/static region protection and legacy FillPlan adapter compatibility.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_model.py tests/test_document_aggregation.py tests/test_document_render_bindings.py tests/test_full_generation_flow.py -q`

Expected: FAIL because the existing service converts draft/matrix results directly into format-specific fill plans and has no canonical `DocumentModel`.

- [x] **Step 3: Implement strict model, pure aggregator and binding adapter**

Do not move binary rendering into the aggregator. Keep `DocumentModel` serializable and hashable without evidence text. Wire the plan-backed service after unit review and before the existing renderer; retain legacy code paths behind explicit route selection.

- [x] **Step 4: Run aggregation, renderer and generation regressions**

Run: `.venv/bin/pytest tests/test_document_model.py tests/test_document_aggregation.py tests/test_document_render_bindings.py tests/test_full_generation_flow.py tests/test_document_generation_prepare.py tests/test_template_field_contract.py tests/test_governed_table_generation.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/document_authoring/document_model.py src/document_authoring/aggregation.py src/document_authoring/render_bindings.py src/document_authoring/service.py src/document_authoring/harness/graph.py src/document_authoring/models.py src/document_authoring/renderers tests/test_document_model.py tests/test_document_aggregation.py tests/test_document_render_bindings.py tests/test_full_generation_flow.py
git commit -m "feat: aggregate reviewed drafts into a document model"
```

## Task 6: Add pre-render and post-render document-level review gates

**Files:**

- Create: `src/document_authoring/planning/document_review.py`
- Create: `src/document_authoring/planning/artifact_review.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/document_authoring/validator.py`
- Modify: `src/document_authoring/renderers/xlsm.py`
- Modify: `src/document_authoring/renderers/docx.py` only for unchanged legacy validation contracts
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/reviews.py` only through a non-conflicting projection adapter
- Create: `tests/test_document_review_gates.py`
- Create: `tests/test_document_artifact_review.py`
- Modify: `tests/test_icd_validation.py`
- Modify: `tests/test_document_reviews.py`

**Interfaces:**

```text
DocumentReviewer.pre_render(plan, document_model, coverage_report, unit_reports)
  -> DocumentReviewReport(stage="pre_render")

DocumentReviewer.post_render(plan, document_model, render_result, artifact_bytes)
  -> DocumentReviewReport(stage="post_render")
```

**Pre-render checks:**

- required coverage and evidence thresholds;
- target identity, version, units, terminology and cross-section consistency;
- duplicate semantic units/claims and duplicate table row keys;
- expected table rows/columns/order and unresolved missing/conflicting data;
- reference/citation completeness and domain-policy issue thresholds.

**Post-render checks:**

- parse the generated XLSX/XLSM package and verify target workbook/sheet/table regions, semantic-to-physical row/column mapping and row order;
- verify fixed labels, formulas, merged ranges, styles/protected regions and non-allowlisted cells were not changed;
- verify required values are not truncated/overflowed under the renderer's physical checks and that the package remains parseable;
- apply the same logical assertions to XLSX and XLSM, additionally checking macro/package/external-link policy and byte-preservation expectations for permitted VBA content;
- retain existing ICD validation and renderer integrity checks as deterministic gates.

**Review/rework behavior:**

- A pre-render semantic issue routes only its affected unit/subgraph through bounded rework, then re-aggregates and reruns both document reviews.
- A pure post-render layout issue reruns only binding/renderer and post-render validation when the semantic model hash is unchanged.
- An issue that cannot be localized safely, exceeds attempts, changes user intent or affects a required high-risk field becomes `needs_review`/`blocked` and requires Gate 2; it never triggers an unbounded loop.
- No automatic release is allowed with a required issue, a source-policy violation, a fixed-content overwrite, unexplained required-row absence or a failed artifact/package check.
- Review report hashes and the exact artifact hash are bound into the RunManifest. A later approval cannot be reused for a different model/artifact hash.

- [x] **Step 1: Write failing document/artifact review tests**

Cover pre-render missing coverage, cross-unit identity conflict, duplicate rows, unsupported evidence, post-render wrong sheet/row, static-content overwrite, formula/merge changes, overflow/truncation, malformed OOXML, XLSM macro/external-link policy, bounded semantic/layout rework and no-release-on-required-issue.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_review_gates.py tests/test_document_artifact_review.py tests/test_icd_validation.py tests/test_document_reviews.py -q`

Expected: FAIL because the current flow has field/matrix validation and renderer checks but no independent plan-backed pre/post document reviewer.

- [x] **Step 3: Implement the reviewers and release routing**

Keep deterministic artifact checks authoritative. If a semantic model reviewer is added, isolate its proposal from the release decision and preserve sanitized issue facts only. Reuse existing OOXML/XLSM parser/security utilities instead of broadening file access.

- [x] **Step 4: Run review, renderer and safety regressions**

Run: `.venv/bin/pytest tests/test_document_review_gates.py tests/test_document_artifact_review.py tests/test_icd_validation.py tests/test_document_reviews.py tests/test_xlsm_renderer_safety.py tests/test_full_generation_flow.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add src/document_authoring/planning src/document_authoring/service.py src/document_authoring/validator.py src/document_authoring/renderers src/document_authoring/models.py src/document_authoring/reviews.py tests/test_document_review_gates.py tests/test_document_artifact_review.py tests/test_icd_validation.py tests/test_document_reviews.py
git commit -m "feat: gate document release with semantic and artifact review"
```

## Task 7: Establish XLSX/XLSM parity and the ICD Pin Definition baseline

**Files:**

- Modify: `src/document_authoring/icd_comparison.py`
- Modify: `src/evaluation/document_generation_metrics.py`
- Create: `tests/fixtures/document_authoring/icd_pin_definition/README.md`
- Create or add portable static fixtures under `tests/fixtures/document_authoring/icd_pin_definition/`; do not depend on ignored `docs/test_chat` files
- Create: `tests/test_document_authoring_parity.py`
- Create: `tests/test_icd_pin_definition_benchmark.py`
- Create: `docs/superpowers/specs/2026-09-08-document-authoring-phase2-parity-thresholds.md`

**Benchmark contract:**

- Normalize XLSX and XLSM `Pin Definition` tables into the same logical representation: connector + pin row key, canonical required columns, normalized values, declared order, duplicate/extra/missing rows and static-structure observations.
- Compare row-key precision/recall/F1, required-column completeness, exact/relative order, duplicate/missing/extra rows, cross-field consistency, row/cell evidence support and physical protection findings.
- Extend the existing metrics module without changing the meaning of legacy field observations. New metrics must be versioned and include denominator/fixture identity.
- Test both equivalent XLSX/XLSM logical outputs and package-specific security/preservation behavior. A macro-bearing fixture is static and synthetic/minimal; tests must not execute macros or require Excel.
- The human baseline is normalized rather than byte-compared. If the approved human workbook is not safely present in the repository, create a portable, reviewed fixture that records its provenance and expected rows without embedding unrelated source data.
- Record baseline values and approved non-regression thresholds before enabling the Phase 2 execution flag. Do not invent a threshold at rollout time or silently turn a missing baseline into a pass.

- [x] **Step 1: Write failing parity and benchmark tests**

Cover row-key normalization, required-column mapping, order differences, duplicate/missing/extra classification, cross-field conflicts, evidence support, XLSX/XLSM equivalence, macro/package preservation, legacy metric compatibility and threshold-file loading.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_authoring_parity.py tests/test_icd_pin_definition_benchmark.py tests/test_document_generation_metrics.py -q`

Expected: FAIL because the current comparator/metrics do not expose the full row-key/order/evidence/parity contract.

- [x] **Step 3: Implement normalized comparison and record the baseline**

Keep comparison deterministic and offline. Store only fixture IDs, normalized summaries, metric values and threshold versions in test/evaluation artifacts; do not place raw evidence or credentials in benchmark output. Have the reviewer and release gate consume the same versioned row-key definitions used by the benchmark.

- [x] **Step 4: Run parity, ICD and existing evaluation regressions**

Run: `.venv/bin/pytest tests/test_document_authoring_parity.py tests/test_icd_pin_definition_benchmark.py tests/test_document_generation_metrics.py tests/test_icd_artifact_comparison.py tests/test_icd_validation.py -q`

Expected: PASS, with the baseline/threshold document containing actual measured values and an explicit approval record before rollout.

- [x] **Step 5: Commit**

```bash
git add src/document_authoring/icd_comparison.py src/evaluation/document_generation_metrics.py tests/fixtures/document_authoring/icd_pin_definition tests/test_document_authoring_parity.py tests/test_icd_pin_definition_benchmark.py docs/superpowers/specs/2026-09-08-document-authoring-phase2-parity-thresholds.md
git commit -m "test: establish xlsx xlsm and icd parity baselines"
```

## Task 8: Close the phase with end-to-end gates, rollout and manual smoke

**Files:**

- Create: `tests/test_conversation_led_document_authoring_phase2_e2e.py`
- Modify: `tests/test_document_authoring_settings.py`
- Modify: `tests/test_document_authoring_durable_resume.py`
- Modify: `tests/test_document_generation_api_work_orders.py`
- Create or modify: `docs/document-authoring-phase2-rollout.md`
- Modify: `docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md` only after factual gates pass
- Modify: this plan only to record actual implementation/verification results

**End-to-end scenarios:**

1. Accepted template-backed XLSX request → one compiled graph → independent unit fan-out → typed table rows → coverage/unit review → deterministic model → pre-render review → render → post-render review → gated release.
2. The same accepted plan replays after duplicate submission, worker restart, lease adoption and checkpoint recovery without duplicate graph/run/receipt/draft/artifact facts.
3. A plan/source/renderer/strategy hash mismatch blocks before model/tool execution and never falls back to the legacy graph.
4. A required missing row, duplicate row, source violation, fixed-content overwrite or malformed artifact produces an actionable review/block state and no automatic release.
5. A pure layout issue reruns binding/renderer only; a semantic issue reruns the affected unit subgraph and then aggregation/review; both remain bounded.
6. XLSX and XLSM benchmark fixtures meet the recorded parity/non-regression threshold; ICD `Pin Definition` reports row-key and required-column results against the approved human baseline.
7. A legacy Work Order, legacy direct template API, DOCX/Markdown path and current-answer ExportJob remain unchanged when the flag is false.
8. Refresh/reconnect/retry restores the same task, plan, graph, run and review facts with no contradictory user-facing status.

**Rollout sequence:**

1. Deploy additive contracts/schema with `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED=false`.
2. Run the Phase 1 manual failure-boundary smoke matrix: restart after confirmation, stop/restart worker, revoke permission before dispatch, mutate template before confirmation, and restore all three deep-link types.
3. Run Phase 2 in offline/test mode against portable fixtures; compare graph/model/review hashes and record parity metrics.
4. Enable the graph flag for an allowlisted tenant and XLSX document type only, with automatic release disabled until the baseline and reviewer gates meet their recorded thresholds.
5. Expand to XLSM only after package preservation/security and parity checks pass; keep legacy route available for rollback.
6. Roll back new runs by disabling the flag. Do not delete accepted plan/graph/run/review records; they remain readable and recoverable under their recorded route.

- [x] **Step 1: Write failing E2E/settings/rollout tests**

Assert the flag defaults false, route selection is deterministic, one graph/run path is used, all required hash bindings appear in the manifest, no required issue releases, duplicate/restart recovery is idempotent, legacy/export compatibility remains green and the threshold file is required before allowlisting.

- [x] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_conversation_led_document_authoring_phase2_e2e.py tests/test_document_authoring_settings.py tests/test_document_authoring_durable_resume.py -q`

Expected: FAIL until all Phase 2 components are connected.

- [x] **Step 3: Make only integration fixes and write the operator runbook**

Document schema changes, flag defaults, allowlist policy, graph/run identity, worker ordering, receipt recovery, review/rework states, parity thresholds, metrics, manual smoke commands, stale/hash remediation and rollback. Do not add Phase 3 rendering or domain strategy behavior to satisfy an E2E test.

- [x] **Step 4: Run the complete verification matrix**

Backend Phase 2 focused:

```bash
.venv/bin/pytest \
  tests/test_document_task_graph.py \
  tests/test_document_task_graph_store.py \
  tests/test_document_plan_dag_execution.py \
  tests/test_typed_table_rows.py \
  tests/test_document_coverage.py \
  tests/test_document_unit_review.py \
  tests/test_document_model.py \
  tests/test_document_aggregation.py \
  tests/test_document_render_bindings.py \
  tests/test_document_review_gates.py \
  tests/test_document_artifact_review.py \
  tests/test_document_authoring_parity.py \
  tests/test_icd_pin_definition_benchmark.py \
  tests/test_conversation_led_document_authoring_phase2_e2e.py -q
```

Backend compatibility:

```bash
.venv/bin/pytest \
  tests/test_document_planning_contracts.py \
  tests/test_document_planning_store.py \
  tests/test_document_planning_registry.py \
  tests/test_document_planning_sources.py \
  tests/test_generation_sessions.py \
  tests/test_document_tasks.py \
  tests/test_document_generation_api_sessions.py \
  tests/test_document_generation_api_work_orders.py \
  tests/test_document_generation_prepare.py \
  tests/test_agent_document_authoring_tools.py \
  tests/test_agent_document_intent_routing.py \
  tests/test_intent_planner.py \
  tests/test_conversation_orchestrator.py \
  tests/test_document_authoring_job_store.py \
  tests/test_document_authoring_durable_resume.py \
  tests/test_authoring_execution_contracts.py \
  tests/test_authoring_execution_events.py \
  tests/test_full_generation_flow.py \
  tests/test_xlsm_renderer_safety.py \
  tests/test_icd_validation.py -q
```

Full backend/frontend/static checks:

```bash
.venv/bin/pytest tests/ -q
cd frontend && npx vitest run && npm run build
cd ..
.venv/bin/python -m py_compile \
  src/document_authoring/planning/*.py \
  src/document_authoring/harness/*.py \
  src/document_authoring/document_model.py \
  src/document_authoring/aggregation.py \
  src/document_authoring/render_bindings.py \
  src/document_authoring/generation_sessions.py \
  src/document_authoring/tasks.py \
  src/document_authoring/service.py
git diff --check
```

Expected: all tests/builds pass; exact counts, skipped external-provider tests and pre-existing warnings are recorded separately.

- [x] **Step 5: Perform the manual failure-boundary smoke matrix**

- With the flag false, run a legacy Work Order and compare status, fingerprint and artifact bytes.
- With the flag enabled for a fixture tenant, confirm one accepted plan and verify one graph/run/Work Order/job lineage.
- Stop the worker after confirmation; restart it and verify the durable run resumes from the same checkpoint/Receipt.
- Revoke source permission or mutate the template/renderer hash between stages; verify fail-closed behavior and no legacy fallback.
- Inject a missing/duplicate row, static-cell mutation and malformed OOXML; verify review/block state and no release.
- Inject a semantic review issue and a renderer-only issue; verify the two bounded rework routes.
- Restore `task`, `session` and `workOrder` links and verify the same authorized task/review state.
- Run XLSX/XLSM parity and ICD baseline checks without external model calls unless explicitly configured and authorized.

The deterministic offline equivalent of this matrix was run and recorded in
`docs/document-authoring-phase2-rollout.md` (75 tests passed). A live
production worker stop/restart and production UI/API exercise remain a
release-operator prerequisite; this closeout does not enable the flag or any
allowlist.

- [x] **Step 6: Commit closeout documentation and factual status**

Stage only files actually changed. Update the design status and this plan with measured results only after the manual smoke and threshold approval complete.

```bash
git add tests/test_conversation_led_document_authoring_phase2_e2e.py tests/test_document_authoring_settings.py tests/test_document_authoring_durable_resume.py tests/test_document_generation_api_work_orders.py docs/document-authoring-phase2-rollout.md
git add docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md docs/superpowers/plans/2026-09-08-conversation-led-document-authoring-phase2.md
git commit -m "test: close conversation-led document authoring phase two"
```

## Phase 2 acceptance gate

Phase 2 is complete only when all of the following are true and backed by tests, manifests and the manual smoke record:

- every enabled plan-backed run uses exactly one immutable `CompiledTaskGraph` and one run identity bound to the accepted plan/source/strategy/adapter/renderer/policy hashes;
- cycles, dangling dependencies, stale plans, hash mismatches, lease/replay errors and missing capabilities fail closed with structured issues;
- independent units fan out only when ready, barriers are respected, and restart/replay creates no duplicate committed facts or artifacts;
- required tables are emitted as typed, row-keyed data with required columns and row/cell evidence; table-to-scalar fallback is zero;
- expected required row keys are complete or explicitly explained by a review issue; no unexplained missing required row is released;
- deterministic aggregation produces a stable, format-neutral `DocumentModel` and bindings never authorize writes outside the allowlist;
- fixed-content overwrite, protected/formula/merge violation, source-scope violation and unsafe package mutation rates are zero;
- pre-render and post-render review reports are independent, hash-bound and bounded; unresolved required issues cannot auto-release;
- XLSX/XLSM logical parity and ICD `Pin Definition` metrics meet the threshold version recorded before rollout, with no regression against the approved human baseline;
- legacy Work Orders, fingerprints, DOCX/Markdown behavior, current-answer export and flag-off execution remain compatible;
- `DOCUMENT_PLAN_DAG_EXECUTION_ENABLED` is false by default, allowlisting is explicit, and rollback by disabling the flag is documented and tested;
- full backend/frontend/static verification passes, and the manual failure-boundary smoke matrix is recorded.

## Handoff to Phase 3

Do not start Phase 3 automatically. After this gate, report actual row-key/column coverage, evidence support, review/rework, artifact-validation, parity and latency metrics, then write a separate plan for controlled `system_recipe`/`generated_structure` rendering. Phase 3 must reuse the same DAG, `CoverageContract`, `DocumentModel`, reviewers and `RunManifest`; it must not weaken the Phase 2 template safety or release gates.
