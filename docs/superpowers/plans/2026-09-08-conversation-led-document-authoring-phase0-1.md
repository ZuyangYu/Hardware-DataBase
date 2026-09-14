# Conversation-led Document Authoring Phase 0–1 Implementation Plan

> **Implementation status:** Not started.
>
> **For agentic workers:** REQUIRED SUB-SKILL: use `executing-plans` to implement this plan task by task. Every implementation task follows red-green-refactor TDD and ends with a focused commit. For bounded, independent checks, prefer the repository-provided `luna_worker` as required by `AGENTS.md`.

**Goal:** Introduce the versioned planning contracts and shadow planner from Phase 0, then deliver the Phase 1 conversation-led requirement, plan-proposal, explicit-confirmation, durable-submission, status-card, and workbench-recovery loop without changing the current document execution graph or adding template-free rendering.

**Architecture:** `GenerationBrief` remains a legacy input only. New conversations build versioned `OutputSpec` drafts, compile a persisted `DocumentPlan` proposal against a frozen source snapshot and registered capabilities, and require an explicit hash-bound confirmation. Confirmation commits the accepted plan and a durable submission outbox in `document_authoring.db`; the standalone worker idempotently materializes the existing template-backed Work Order and its job in `auth.db`. Existing Work Orders remain readable and retain their original fingerprints. No Phase 0–1 code executes `StructureContract` plans; unsupported template-free plans remain reviewable proposals until Phase 3.

**Tech Stack:** Python 3.11, Pydantic v2, SQLite, FastAPI, LangChain tool wrappers, pytest; TypeScript, React, React Router, Vitest, Vite.

**Design:** `docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md`

## Scope boundary

This plan implements only the design's Phase 0 and Phase 1:

- planning contracts, capability registries, persistence, deterministic hashing and plan diff;
- legacy template-backed shadow planning with no execution effect;
- versioned conversational `OutputSpec` intake and minimum-question clarification;
- plan proposal, stale detection and explicit Gate 1 confirmation;
- transactional submission outbox and template-backed Work Order/job materialization;
- generic document-authoring routing with strict separation from conversational export;
- safe plan/status cards and `task/session/workOrder` workbench recovery;
- rollout flags, observability and end-to-end compatibility tests.

Explicitly deferred:

- template-free DOCX/PDF/XLSX rendering;
- replacing the current LangGraph execution graph with the new compiled `TaskGraph`;
- native table Writer repair, `TypedTableRow` execution and document-level post-render Reviewer;
- ICD/FPT/requirements-specific `DomainStrategy` implementations;
- PlanDiff-driven partial revision execution and final RunManifest expansion;
- removal of legacy GenerationBrief/session/Work Order endpoints.

Those belong to Phase 2–5 and must not be pulled into this implementation.

## Global constraints

- `OutputSpec`, `DocumentPlan`, source snapshot and policy hashes are server-owned. The browser or model may only submit user choices and expected hashes.
- `GenerationBrief → OutputSpecDraft → OutputSpec` is one-way. No new code writes an OutputSpec decision back into `GenerationBrief` and no resolver merges the two as peer authorities.
- An accepted OutputSpec or DocumentPlan is immutable. Changes create a new version and mark the old proposal stale when appropriate.
- `document_authoring.db` and `auth.db` are separate atomicity domains. Never claim a cross-database transaction; use the plan-submission outbox defined in Task 10.
- Phase 0 shadow planning is fail-soft and cannot change Work Order status, task status, job creation, renderer input or Artifact bytes.
- Phase 1 v2 execution is template-backed only. `system_recipe` and `generated_structure` proposals must report `layout_capability_unavailable` and cannot be submitted.
- New v2 tasks cannot be silently confirmed from recommended defaults. “采用推荐方案” is an explicit user action bound to the current spec and plan hashes.
- Keep existing `SourceSetSnapshot`/`KnowledgeBaseSourceSnapshot`, attachment snapshot, Evidence Registry, template allowlist, FillPlan, lease/fencing and approval rules unchanged.
- Conversational export keeps its own `ResultSnapshot → ExportJob` lifecycle. Authoring routes must never create an ExportJob as a fallback.
- New wire fields are optional; legacy callers continue to receive their existing behavior while `DOCUMENT_PLANNING_V2_ENABLED=false`.
- Every database migration is idempotent, preserves legacy payload JSON, and is covered by an old-schema upgrade test.
- Every task starts with a failing test and uses only focused changes. Do not refactor unrelated authoring code.

## Compatibility matrix

| Caller/data | Phase 0 behavior | Phase 1 v2 behavior |
|---|---|---|
| Existing template session API | Unchanged | Legacy contract remains available |
| Existing Work Order without plan refs | Unchanged | Readable as `legacy`; fingerprint unchanged |
| Existing direct template tool, v2 flag off | Unchanged | Unchanged |
| New document request, v2 flag on | Shadow/proposal only | OutputSpec → plan proposal → explicit confirmation |
| Template-free proposal | Persisted with capability issue | Cannot create Work Order until Phase 3 |
| Conversational export request | Existing ExportPlan path | Existing ExportPlan path |
| `?workOrder=` workbench link | Existing behavior | Existing behavior |
| `?task=` / `?session=` link | Link can be emitted but not consumed | Resolves to one DocumentTask and correct workbench phase |

---

## Phase 0 — contracts, persistence and shadow planning

### Task 1: Add strict planning contracts and canonical hashes

**Files:**

- Create: `src/document_authoring/planning/__init__.py`
- Create: `src/document_authoring/planning/models.py`
- Create: `tests/test_document_planning_contracts.py`
- Reuse: `src/document_authoring/harness/idempotency.py::canonical_json`

**Interfaces:**

- `DeliverableSpec(format, role, required, requested_by)`
- Discriminated `LayoutSource`: `ProvidedTemplateSource | SystemRecipeSource | GeneratedStructureSource`
- `OutlineUnitSpec`, `TableRequirement`, `SourceScopeSpec`, `OutputSpec`
- Discriminated `LayoutContract`: `TemplateContract | StructureContract`
- `CoverageRequirement`, `CoverageContract`, `SemanticUnitPlan`, `UnitTaskSpec`
- `PlanIssue`, `DocumentPlan`, `PlanDiff`
- `planning_content_hash(model, exclude=...) -> str`

**Required validation:**

- Pydantic models use `extra="forbid"` and bounded string/list sizes.
- Deliverables contain exactly one primary item and at least one required item.
- `provided_template` requires template and schema versions; the other layout modes reject template coordinates.
- Unit IDs and row keys are unique and dependency edges reference existing units without self-edges.
- A plan is executable only when it has no blocking issue and every required capability resolves to a registered version.
- Hash preimages exclude lifecycle timestamps/status but include every user decision, layout/strategy/capability version and frozen source hash.
- Hashing rejects NaN/Infinity and non-JSON objects through the existing `canonical_json` helper.

- [ ] **Step 1: Write failing contract tests**

Cover valid round trips plus rejection of: two primary deliverables, ambiguous layout source, duplicate units, dangling/self dependencies, unsupported free-form fields and a caller-supplied mismatched content hash. Assert that key order does not change the hash while a source, policy, required column or strategy version change does.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_document_planning_contracts.py -q`

Expected: FAIL because the planning package and contracts do not exist.

- [ ] **Step 3: Implement the minimal models**

Keep the new contracts isolated from `src/document_authoring/models.py`. Re-export only the public planning types from `planning/__init__.py`. Do not add persistence or planner behavior in this task.

- [ ] **Step 4: Run focused and canonical-hash regressions**

Run: `.venv/bin/pytest tests/test_document_planning_contracts.py tests/test_authoring_execution_contracts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning tests/test_document_planning_contracts.py
git commit -m "feat: define document planning contracts"
```

### Task 2: Persist immutable OutputSpec and DocumentPlan versions

**Files:**

- Create: `src/document_authoring/planning/store.py`
- Modify: `src/document_authoring/work_order_store.py`
- Create: `tests/test_document_planning_store.py`
- Modify: `tests/test_document_authoring_migration.py`

**Tables:**

```text
document_output_specs
  PRIMARY KEY(output_spec_id, version)
  tenant_id, user_id, task_id, status, content_hash, created_at, payload_json

document_plans
  PRIMARY KEY(document_plan_id, version)
  tenant_id, user_id, task_id, output_spec_id/version,
  status, plan_hash, source_set_snapshot_id/hash, created_at, payload_json

document_planning_events
  event_id PRIMARY KEY, task_id, event_type, idempotency_key,
  created_at, payload_json, UNIQUE(task_id, idempotency_key)
```

**Interfaces:**

- `DocumentPlanningStore.create_output_spec(...)`
- `get_output_spec(...)`, `get_latest_output_spec(task_id)`
- `create_plan(...)`, `get_plan(...)`, `get_latest_plan(task_id)`
- `mark_plan_stale(..., expected_plan_hash, reason_code)` using compare-and-swap
- `append_event(...)`, `list_events(task_id)`
- `DocumentAuthoringStore.planning` composed with the same `db_path`

- [ ] **Step 1: Write failing persistence and upgrade tests**

Assert immutable version insertion, owner/tenant filtering, idempotent event insertion, CAS stale transition, rejection of hash/row disagreement, and initialization from a pre-planning SQLite schema. Assert accepted rows cannot be overwritten through `create_*` or status updates.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_planning_store.py tests/test_document_authoring_migration.py -q`

Expected: FAIL because the tables and repository do not exist.

- [ ] **Step 3: Add idempotent schema initialization and repository methods**

Store complete validated payload JSON and indexed identity/hash columns. On read, compare indexed hashes and identities with the payload before returning a model. Do not use `INSERT OR REPLACE` for immutable versions.

- [ ] **Step 4: Run persistence regressions**

Run: `.venv/bin/pytest tests/test_document_planning_store.py tests/test_document_authoring_migration.py tests/test_document_authoring_p2a.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning/store.py src/document_authoring/work_order_store.py tests/test_document_planning_store.py tests/test_document_authoring_migration.py
git commit -m "feat: persist versioned document plans"
```

### Task 3: Add capability registries and frozen-source adapters

**Files:**

- Create: `src/document_authoring/planning/registry.py`
- Create: `src/document_authoring/planning/sources.py`
- Modify: `src/document_authoring/planning/__init__.py`
- Create: `tests/test_document_planning_registry.py`
- Create: `tests/test_document_planning_sources.py`

**Interfaces:**

- `DomainStrategy` protocol and `DomainStrategyDescriptor`
- `LayoutAdapter` protocol and `LayoutAdapterDescriptor`
- `RendererCapabilityDescriptor`
- `DomainStrategyRegistry`, `LayoutAdapterRegistry`, `RendererCapabilityRegistry`
- `FrozenSourceScope` with adapters from project `SourceSetSnapshot`, `KnowledgeBaseSourceSnapshot`, frozen attachment refs and hashed `user_assertion` refs
- Built-in compatibility entries: `legacy_document_schema@1`, `provided_template@1`, current XLSX/XLSM/DOCX/Markdown renderer capabilities

**Rules:**

- Registry keys are `(id, version)`; duplicate registration is an error.
- Lookup never falls back to a different version.
- A disabled or unsupported capability returns a structured `PlanIssue`; it does not disappear from the plan.
- Source adapters expose IDs, type, scope and content hash only. Raw source text, filesystem paths and credentials are forbidden.
- Register `system_recipe` and `generated_structure` as unavailable capability descriptors for Phase 0–1, so template-free proposals are representable but not executable.

- [ ] **Step 1: Write failing registry/source tests**

Cover exact-version lookup, duplicate rejection, unavailable capability diagnostics, stable adaptation of KB/project snapshots, attachment ordering, user-assertion hashing and rejection of raw content/path keys.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_planning_registry.py tests/test_document_planning_sources.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement protocols, registries and adapters**

The built-ins describe existing behavior only; they must not instantiate new renderers or call model providers. Keep source adaptation pure.

- [ ] **Step 4: Run focused and source-snapshot regressions**

Run: `.venv/bin/pytest tests/test_document_planning_registry.py tests/test_document_planning_sources.py tests/test_document_work_order_source_snapshots.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning tests/test_document_planning_registry.py tests/test_document_planning_sources.py
git commit -m "feat: register planning capabilities and source views"
```

### Task 4: Compile deterministic legacy template shadow plans and plan diffs

**Files:**

- Create: `src/document_authoring/planning/legacy.py`
- Create: `src/document_authoring/planning/service.py`
- Create: `src/document_authoring/planning/diff.py`
- Create: `tests/test_document_shadow_planner.py`
- Create: `tests/test_document_plan_diff.py`
- Read-only inputs: `src/document_authoring/template_analysis.py`, `src/document_authoring/models.py`

**Interfaces:**

- `legacy_brief_to_output_spec(...) -> OutputSpec`
- `LegacyTemplatePlanningAdapter.compile(...) -> DocumentPlan`
- `DocumentPlanningService.propose_shadow(...)`
- `diff_document_plans(parent, child) -> PlanDiff`

**Compiler behavior:**

- Preserve `DocumentSchema.fields` and `review_items` in schema order with stable unit IDs.
- Use `TemplateUnitBinding` and table schemas to produce physical binding references without embedding cell content.
- Convert scalar fields into scalar coverage requirements.
- Convert table-shaped fields into required-column coverage. If the row set cannot be derived, emit `row_scope_unresolved`; do not invent row keys.
- Freeze strategy, adapter, renderer and policy versions in the plan.
- Produce dependency edges only from explicit schema/domain facts; do not infer arbitrary dependencies with an LLM.
- The same fixed inputs produce the same semantic plan and plan hash.
- `PlanDiff` reports changed spec fields, units, coverage, layout, source and policy versions in stable order.

- [ ] **Step 1: Write failing compiler and diff tests**

Use in-memory model fixtures for: scalar template, repeating table, missing binding, unsupported capability, reordered input dictionaries and one-field plan revisions. Assert no source text or template bytes occur in serialized plans.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_shadow_planner.py tests/test_document_plan_diff.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement the pure compiler and diff**

Do not call Store, AppPipeline, RAGFlow or an LLM from the compiler. `DocumentPlanningService` coordinates registries and persistence around that pure core.

- [ ] **Step 4: Run compiler plus existing template contract tests**

Run: `.venv/bin/pytest tests/test_document_shadow_planner.py tests/test_document_plan_diff.py tests/test_template_field_contract.py tests/test_governed_table_generation.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning tests/test_document_shadow_planner.py tests/test_document_plan_diff.py
git commit -m "feat: compile legacy document shadow plans"
```

### Task 5: Run shadow planning without changing execution

**Files:**

- Modify: `src/settings.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/observability/metrics.py`
- Create: `tests/test_document_planning_shadow_integration.py`
- Modify: `tests/test_document_authoring_settings.py`

**Configuration:**

- `DOCUMENT_PLANNING_SHADOW_ENABLED=false`
- `DOCUMENT_PLANNING_V2_ENABLED=false`

**Behavior:**

- When shadow mode is enabled, create a proposed shadow OutputSpec/DocumentPlan after the current Work Order and frozen source snapshot exist.
- Associate the shadow records with the existing `DocumentTask`, but do not attach their IDs to the Work Order fingerprint or execution request.
- Emit sanitized `planning_shadow_succeeded` or `planning_shadow_failed` events and low-cardinality metrics for plan validity, blocking issue codes, units, tables and elapsed time.
- Shadow failure is fail-soft: return the exact legacy result, with identical Work Order status, job count and Artifact bytes.
- Repeated calls with the same Work Order are idempotent and reuse the stored shadow version.

- [ ] **Step 1: Write failing shadow integration tests**

Compare flag-off and flag-on runs. Assert equivalent Work Order fingerprints/status and zero extra authoring jobs; flag-on adds only planning records/events. Inject compiler failure and assert the legacy call still succeeds. Assert metrics/events do not contain source names, evidence text or file paths.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_planning_shadow_integration.py tests/test_document_authoring_settings.py -q`

Expected: FAIL.

- [ ] **Step 3: Wire fail-soft shadow planning behind the flag**

Use a small private adapter in `DocumentGenerationService` or `AppPipeline`; do not insert planning logic into the LangGraph nodes. Keep all existing feature defaults false.

- [ ] **Step 4: Run focused and generation-flow regressions**

Run: `.venv/bin/pytest tests/test_document_planning_shadow_integration.py tests/test_document_authoring_settings.py tests/test_document_generation_prepare.py tests/test_full_generation_flow.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/settings.py src/document_authoring/service.py src/core/app_pipeline.py src/observability/metrics.py tests/test_document_planning_shadow_integration.py tests/test_document_authoring_settings.py
git commit -m "feat: observe document planning in shadow mode"
```

---

## Phase 1 — conversational intake, plan confirmation and recovery

### Task 6: Extend GenerationSession and DocumentTask for planning lifecycle

**Files:**

- Modify: `src/document_authoring/generation_sessions.py`
- Modify: `src/document_authoring/tasks.py`
- Modify: `src/api/schemas.py`
- Modify: `tests/test_generation_sessions.py`
- Modify: `tests/test_document_tasks.py`
- Modify: `tests/test_document_authoring_migration.py`

**Model changes:**

- `GenerationSession.template_version_id: str | None`
- Add `contract_version: Literal["legacy_brief_v1", "output_spec_v1"]`
- Add `output_spec_id/version`, `document_plan_id/version`
- Add internal v2 session states `awaiting_plan`, `awaiting_plan_confirmation`, `planned`, `blocked` while retaining legacy states. `awaiting_plan` is a pre-proposal session detail, not a new user-visible `DocumentTask` status.
- Add the same current OutputSpec/DocumentPlan pointers to `DocumentTask`
- Add task states `awaiting_plan_confirmation` and `awaiting_release`; retain all existing states and projections

**Migration:**

- Rebuild `document_generation_sessions` only when its `template_version_id` column is still `NOT NULL`.
- Copy every legacy row and index, preserve payload JSON byte-for-byte, then use model defaults on read.
- Add nullable, queryable plan/spec pointer columns to sessions/tasks with idempotent `ALTER TABLE` helpers.
- Never replace an absent legacy plan with a fabricated confirmed plan.

- [ ] **Step 1: Write failing model, store and old-schema upgrade tests**

Cover a legacy template session, a v2 no-template draft session, invalid provided-template layout without a template, task status transitions, pointer ownership conflicts and repeated initialization of both old and new schemas.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_generation_sessions.py tests/test_document_tasks.py tests/test_document_authoring_migration.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement additive models and idempotent migration**

Keep legacy constructor behavior: an omitted `contract_version` on a persisted row means `legacy_brief_v1`. Only `output_spec_v1` sessions may omit a template.

- [ ] **Step 4: Run session/task/API serialization regressions**

Run: `.venv/bin/pytest tests/test_generation_sessions.py tests/test_document_tasks.py tests/test_document_generation_api_sessions.py tests/test_document_task_projection.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/generation_sessions.py src/document_authoring/tasks.py src/api/schemas.py tests/test_generation_sessions.py tests/test_document_tasks.py tests/test_document_authoring_migration.py
git commit -m "feat: track document planning lifecycle"
```

### Task 7: Build OutputSpec drafts through minimum-question clarification

**Files:**

- Create: `src/document_authoring/planning/intake.py`
- Modify: `src/document_authoring/requirement_clarifier.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/api/schemas.py`
- Modify: `src/api/routes/document_generation.py`
- Create: `tests/test_document_output_spec_intake.py`
- Modify: `tests/test_document_generation_api_sessions.py`

**API compatibility:**

- Extend `CreateGenerationSessionRequest` with optional `output_spec` input and make `template_version_id` optional only for v2 requests.
- Keep legacy `purpose`, `output_policy`, schema and template fields.
- Reuse `POST /sessions/{id}/messages` for question answers; dispatch by `session.contract_version`.
- A legacy session continues through `RequirementClarifier`. A v2 session uses `OutputSpecIntakeService` and never treats `GenerationBrief` as a second authority.

**Intake behavior:**

- Merge user input into a new OutputSpec version with compare-and-swap on expected current version.
- Ask only the next material question: purpose/audience, document type, required deliverables, layout source, outline/table scope, target identity, source scope, missing-data policy, inference policy or approval policy.
- Return one question at a time, at most three options, plus free-text support.
- Store system recommendations separately from `accepted_recommendations`.
- “采用推荐方案” copies named recommendation IDs into a new draft version but does not confirm a plan or create a Work Order.
- Once required intake fields are complete, move the session to `awaiting_plan`; do not reuse `ready_to_generate` for v2.

- [ ] **Step 1: Write failing intake and API tests**

Cover template-backed and template-free drafts, minimal question ordering, free text, recommendation acceptance, optimistic version conflict, refresh recovery and proof that `auto_confirm_recommended=True` cannot confirm a v2 session.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_output_spec_intake.py tests/test_document_generation_api_sessions.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement the v2 intake adapter and route dispatch**

Keep `RequirementClarifier`'s legacy paths intact. Put new field-merging, question selection and recommendation logic in `planning/intake.py`, not `AppPipeline`.

- [ ] **Step 4: Run intake, session and clarification regressions**

Run: `.venv/bin/pytest tests/test_document_output_spec_intake.py tests/test_document_generation_api_sessions.py tests/test_document_clarification_projection.py tests/test_generation_sessions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning/intake.py src/document_authoring/requirement_clarifier.py src/core/app_pipeline.py src/api/schemas.py src/api/routes/document_generation.py tests/test_document_output_spec_intake.py tests/test_document_generation_api_sessions.py
git commit -m "feat: clarify versioned output specifications"
```

### Task 8: Generalize document context and preserve export/authoring routing boundaries

**Files:**

- Modify: `src/core/intent.py`
- Modify: `src/core/conversation_orchestrator.py`
- Modify: `src/document_authoring/chat_context.py`
- Modify: `src/api/schemas.py`
- Modify: `src/api/routes/query.py`
- Modify: `src/agents/runner.py`
- Modify: `tests/test_intent_planner.py`
- Modify: `tests/test_conversation_orchestrator.py`
- Modify: `tests/test_agent_document_intent_routing.py`

**Contract:**

- Add v2 authoring context whose template reference is optional and whose server-owned identity may contain `task_id`, `generation_session_id` and OutputSpec reference.
- Continue accepting v1 template context unchanged.
- `document_flow=true` means “enter governed authoring,” not “a template must already be attached.” It is valid with an authorized KB/attachment scope; with neither source nor task context it fails 422.
- Add `document_authoring` intent while retaining `template_generation` for explicit template/fill commands.
- Explicit template/fill requests route authoring.
- “当前/刚才/上述结果另存为 PDF” and equivalent explicit result-delivery language route conversational export.
- “基于知识库/附件创建 ICD/报告” routes authoring.
- A genuinely ambiguous “整理成文档” produces one disambiguation reason/action; it must not create either job automatically.
- Comparison/retrieval continues to outrank either generation path where current rules require it.
- Server route remains authoritative; frontend hints cannot widen tool access.

- [ ] **Step 1: Write the routing table as failing parameterized tests**

Include Chinese and English cases for QA, comparison, current-result export, new document authoring, template filling, explicit v2 authoring, expired context and missing permitted scope. Assert allowed tool groups as well as route names.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_intent_planner.py tests/test_conversation_orchestrator.py tests/test_agent_document_intent_routing.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement v2 context and deterministic precedence**

Build a server-owned v2 context before persisting the turn whenever the authoritative route is authoring. The runner may mount document intake tools without a template, but template inspection/fill tools remain unavailable unless template refs exist.

- [ ] **Step 4: Run routing and turn persistence regressions**

Run: `.venv/bin/pytest tests/test_intent_planner.py tests/test_conversation_orchestrator.py tests/test_agent_document_intent_routing.py tests/test_conversation_wiring.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/intent.py src/core/conversation_orchestrator.py src/document_authoring/chat_context.py src/api/schemas.py src/api/routes/query.py src/agents/runner.py tests/test_intent_planner.py tests/test_conversation_orchestrator.py tests/test_agent_document_intent_routing.py
git commit -m "feat: route generic document authoring conversations"
```

### Task 9: Create and expose plan proposals with stale detection

**Files:**

- Modify: `src/document_authoring/planning/service.py`
- Modify: `src/document_authoring/planning/store.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/api/schemas.py`
- Modify: `src/api/routes/document_generation.py`
- Create: `tests/test_document_plan_proposals.py`
- Modify: `tests/test_document_work_order_source_snapshots.py`
- Modify: `tests/test_document_generation_api_sessions.py`

**Endpoints:**

```text
POST /api/v1/document-generation/sessions/{session_id}/plan-proposals?kb=...
GET  /api/v1/document-generation/plans/{plan_id}/versions/{version}?kb=...
```

The POST body contains only `client_request_id` and `expected_output_spec_version`. The server derives template/schema, current readable sources, attachment refs, registry versions and actor identity.

**Behavior:**

- Require a v2 session in `awaiting_plan` owned by the current tenant/user.
- Build or reuse a frozen candidate source snapshot before hashing the plan.
- Reuse the current complete OutputSpec draft as the proposed, hash-bound version and compile/persist its DocumentPlan proposal idempotently. Do not mint an equivalent OutputSpec version merely because proposal creation is retried.
- Re-authorize the template and all source scope before returning the proposal.
- Return a safe `PlanProposalView`: plan/spec IDs, versions/hashes, deliverables, layout summary, outline/table counts, source identity summary, policies, warnings/blockers and `next_actions`.
- Do not expose source names when policy permits only aggregate counts; never expose evidence text or storage refs.
- If template hash, source authorization or accepted user inputs change, mark the prior proposal stale and require a new proposal.
- A template-free Phase 1 proposal is persisted but has blocking `layout_capability_unavailable`; confirmation is unavailable.
- Move a valid session/task to `awaiting_plan_confirmation`; leave invalid proposals in `awaiting_plan` or `blocked` with actionable issues.

- [ ] **Step 1: Write failing service/API tests**

Cover stable retries, ownership, source snapshot binding, stale template/source/spec changes, a valid template proposal, a non-executable structure proposal and safe response projection.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_plan_proposals.py tests/test_document_work_order_source_snapshots.py tests/test_document_generation_api_sessions.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement proposal orchestration and read API**

Keep the pure compiler isolated. `AppPipeline` resolves live KB/attachment scope and permissions, while `DocumentPlanningService` validates and persists the proposed contracts.

- [ ] **Step 4: Run proposal and permission regressions**

Run: `.venv/bin/pytest tests/test_document_plan_proposals.py tests/test_document_work_order_source_snapshots.py tests/test_document_generation_api_sessions.py tests/test_document_rag_record_authorization.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning src/core/app_pipeline.py src/api/schemas.py src/api/routes/document_generation.py tests/test_document_plan_proposals.py tests/test_document_work_order_source_snapshots.py tests/test_document_generation_api_sessions.py
git commit -m "feat: propose frozen document plans"
```

### Task 10: Confirm plans through a transactional submission outbox

**Files:**

- Create: `src/document_authoring/planning/submissions.py`
- Modify: `src/document_authoring/planning/store.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/document_authoring/job_store.py`
- Modify: `src/workers/main.py`
- Modify: `src/api/schemas.py`
- Modify: `src/api/routes/document_generation.py`
- Create: `tests/test_document_plan_confirmation.py`
- Create: `tests/test_document_plan_submission_worker.py`
- Modify: `tests/test_knowledge_base_document_work_orders.py`
- Modify: `tests/test_document_authoring_job_store.py`

**Endpoint:**

```text
POST /api/v1/document-generation/sessions/{session_id}/confirm-plan?kb=...
```

Request fields: `expected_output_spec_hash`, `expected_plan_hash`, `client_request_id`.

**Transactional boundary:**

In one `document_authoring.db` transaction:

1. re-read and compare the proposed spec/plan hashes and owner scope;
2. verify the candidate snapshot/template/policies still match and the plan is executable;
3. mark OutputSpec and DocumentPlan accepted;
4. bind their versions to GenerationSession and DocumentTask;
5. write an append-only confirmation event with actor and hashes;
6. insert one `document_plan_submissions` outbox row keyed by accepted plan hash.

Do **not** write `auth.db` in this transaction.

**Submission outbox:**

- States: `pending → running → dispatched | waiting_human | retrying → failed/dead_letter`.
- Include only tenant/user/task/session/plan/snapshot/work-order references and hashes.
- Claim/lease/fencing and retry semantics mirror the existing durable job store.
- `HardwareWorker` drains a bounded batch of plan submissions before normal document jobs, so either queue cannot starve the other.
- The worker rebuilds current auth context, revalidates permission, materializes exactly one template-backed Work Order from the accepted plan and proposed snapshot, runs existing ICD/template preflight, and creates the existing `generate_work_order` job only when preflight is ready.
- A preflight human gate records `waiting_human` and the Work Order reference without creating a generation job.
- Replays use `document-plan:{plan_id}:{version}:{plan_hash}` idempotency and cannot fork a second Work Order/job.

**Work Order compatibility:**

- Add optional `output_spec_id/version/hash` and `document_plan_id/version/hash` fields.
- New plan-backed Work Orders use input fingerprint version 3 including those refs.
- Legacy v1/v2 payloads and fingerprints remain byte-compatible; do not backfill fictional refs.
- Add a direct indexed KB idempotency lookup rather than scanning all KB Work Orders.

- [ ] **Step 1: Write failing transaction, worker and compatibility tests**

Cover wrong/stale hash rejection, two concurrent confirmations, transaction rollback, separate-DB dispatch, worker crash/lease adoption, permission revocation, preflight human wait, exactly-one Work Order/job and unchanged legacy fingerprints.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_plan_confirmation.py tests/test_document_plan_submission_worker.py tests/test_knowledge_base_document_work_orders.py tests/test_document_authoring_job_store.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement confirmation, outbox and materialization**

Refactor Work Order construction only enough to accept an already-frozen snapshot and planning refs. Do not change the harness, Writer, validator or renderer. The confirmation HTTP response reports `submission_id/status`; it must not claim a job exists until the outbox worker creates it.

- [ ] **Step 4: Run durability and generation-preflight regressions**

Run: `.venv/bin/pytest tests/test_document_plan_confirmation.py tests/test_document_plan_submission_worker.py tests/test_document_authoring_job_store.py tests/test_knowledge_base_document_work_orders.py tests/test_document_generation_prepare.py tests/test_document_authoring_durable_resume.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/planning src/document_authoring/work_order_store.py src/document_authoring/models.py src/document_authoring/service.py src/document_authoring/job_store.py src/core/app_pipeline.py src/workers/main.py src/api/schemas.py src/api/routes/document_generation.py tests/test_document_plan_confirmation.py tests/test_document_plan_submission_worker.py tests/test_knowledge_base_document_work_orders.py tests/test_document_authoring_job_store.py
git commit -m "feat: submit confirmed document plans durably"
```

### Task 11: Replace silent one-shot generation with proposal/confirmation tools

**Files:**

- Modify: `src/agents/tools/document_authoring_tools.py`
- Modify: `src/agents/runner.py`
- Modify: `tests/test_agent_document_authoring_tools.py`
- Modify: `tests/test_agent_document_intent_routing.py`
- Modify: `tests/test_conversation_orchestrator.py`

**V2 tool surface:**

- `start_document_generation_session` — template optional, creates OutputSpec intake session.
- `answer_clarification` — applies one v2 question answer or legacy answer.
- `propose_document_plan` — compiles/returns the safe plan proposal.
- `confirm_document_plan` — requires current expected hashes and explicit user action.
- `get_document_task_status` — reads the aggregate by task ID.
- Template inspection remains available only with template context.

**Rules:**

- Under `DOCUMENT_PLANNING_V2_ENABLED=true`, `generate_document_from_template` may prepare recommendations and a proposal but must return `waiting_human` with `confirm_document_plan`; it cannot call `auto_confirm_recommended` or create a Work Order.
- `use_recommended_defaults` in compatibility arguments means “populate named recommendations,” never “confirm.”
- Remove low-level `create_document_work_order` from the v2 model tool list; the submission worker owns that transition.
- Rewrite `_DOCUMENT_FLOW_PROMPT` around generic requirements, proposal, explicit confirmation and asynchronous status. Do not mention a template when none exists.
- Keep legacy wrappers under the v2 flag-off path until Phase 5.
- Tool results and cards carry IDs, hashes, summaries and next actions only.

- [ ] **Step 1: Write failing tool/runner tests**

Assert that a direct template generation utterance returns a confirmation proposal and creates zero Work Orders/jobs before explicit confirmation. Cover no-template intake, exact hash forwarding, unavailable layout, legacy flag-off behavior and tool allowlists for template/no-template contexts.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_agent_document_authoring_tools.py tests/test_agent_document_intent_routing.py tests/test_conversation_orchestrator.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement v2 tools and prompt**

Keep each tool a typed, permission-checked adapter over `AppPipeline`; do not let the model construct plan hashes, strategy IDs or Work Order definitions.

- [ ] **Step 4: Run all agent authoring regressions**

Run: `.venv/bin/pytest tests/test_agent_document_authoring_tools.py tests/test_agent_document_intent_routing.py tests/test_conversation_orchestrator.py tests/test_agent_authoring_observability.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agents/tools/document_authoring_tools.py src/agents/runner.py tests/test_agent_document_authoring_tools.py tests/test_agent_document_intent_routing.py tests/test_conversation_orchestrator.py
git commit -m "feat: require explicit document plan confirmation"
```

### Task 12: Project plan state and safe cards through the task aggregate

**Files:**

- Modify: `src/core/app_pipeline.py`
- Modify: `src/agents/tools/document_authoring_tools.py`
- Modify: `src/api/routes/document_generation.py`
- Modify: `tests/test_document_task_projection.py`
- Modify: `tests/test_document_clarification_projection.py`
- Modify: `tests/test_document_status_coverage.py`

**Projection additions:**

- `planning_state`: spec/plan IDs, versions, hashes, proposal status and safe summary.
- `submission`: submission ID/status and Work Order/job refs when created.
- Target user-visible status projection: `draft`, `needs_clarification`, `awaiting_plan_confirmation`, `planned`, `queued`, `running`, `needs_review`, `awaiting_release`, terminal states. An internal session in `awaiting_plan` projects as `draft` with `propose_document_plan` as its next action.
- `next_actions`: `answer_clarification`, `propose_document_plan`, `confirm_document_plan`, `open_document_workbench`, status/review actions.

**Card/event kinds:**

- `requirement_clarification`
- `output_spec_confirmation`
- `generation_status`
- `review_summary`

The existing `generation_session`, `work_order_created` and `work_order_status` kinds remain parseable during migration. Events are stored on the initiating turn and coalesced by `task_id` first, then session/work-order identity. A card summary must not contain raw source names, evidence, paths or approval authority.

- [ ] **Step 1: Write failing projection/card tests**

Cover every pre-Work-Order state, refresh after restart, plan summary redaction, next-action mapping, legacy projection and task/session/work-order association mismatch rejection.

- [ ] **Step 2: Confirm failure**

Run: `.venv/bin/pytest tests/test_document_task_projection.py tests/test_document_clarification_projection.py tests/test_document_status_coverage.py -q`

Expected: FAIL.

- [ ] **Step 3: Extend the aggregate projection and event producer**

Read OutputSpec/DocumentPlan through `DocumentPlanningStore` only after task ownership and KB read permission pass. Treat event projection failure as additive/fail-soft; the authoritative planning transaction must remain committed.

- [ ] **Step 4: Run projection/API regressions**

Run: `.venv/bin/pytest tests/test_document_task_projection.py tests/test_document_clarification_projection.py tests/test_document_status_coverage.py tests/test_document_generation_api_work_orders.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/app_pipeline.py src/agents/tools/document_authoring_tools.py src/api/routes/document_generation.py tests/test_document_task_projection.py tests/test_document_clarification_projection.py tests/test_document_status_coverage.py
git commit -m "feat: project document planning status cards"
```

### Task 13: Add frontend plan contracts, confirmation card and chat actions

**Files:**

- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/documentAuthoring.ts`
- Modify: `frontend/src/api/documentAuthoring.test.ts`
- Modify: `frontend/src/pages/chat/components/documentCardModel.ts`
- Modify: `frontend/src/pages/chat/components/documentCardModel.test.ts`
- Create: `frontend/src/pages/chat/components/OutputSpecConfirmationCard.tsx`
- Create: `frontend/src/pages/chat/components/OutputSpecConfirmationCard.test.tsx`
- Modify: `frontend/src/pages/chat/components/DocumentStatusCard.tsx`
- Modify: `frontend/src/pages/chat/useKbChat.ts`
- Modify: `frontend/src/pages/chat/useKbChat.test.ts`
- Modify: `frontend/src/pages/chat/ChatPage.tsx`
- Modify: `frontend/src/pages/chat/ChatPage.test.tsx`

**Frontend contract:**

- Add typed v2 session, `PlanProposalView`, plan-confirm request/response, planning state and discriminated card kinds.
- `OutputSpecConfirmationCard` shows what will be generated: document type, required deliverables, template/recipe, outline/table counts, target identity, source version summary, missing/inference/approval policies and warnings.
- Provide `确认生成`, `修改需求` and `采用推荐方案` actions. Confirm sends the exact visible spec/plan hashes and a stable client request ID.
- Disable confirmation while blockers exist or a request is in flight.
- A 409 stale response refreshes the proposal and clearly asks for reconfirmation; it never retries confirmation automatically.
- Keep free-text/option clarification in the existing chat card and coalesce updates by task identity.
- Completed/released cards continue to expose authenticated download actions.

- [ ] **Step 1: Write failing API, model and component tests**

Cover safe parsing, unknown kind rejection, summary rendering, blocker-disabled state, one-click recommendation without submission, exact hash confirmation, duplicate-click suppression, stale refresh and legacy card rendering.

- [ ] **Step 2: Confirm failure**

Run: `cd frontend && npx vitest run src/api/documentAuthoring.test.ts src/pages/chat/components/documentCardModel.test.ts src/pages/chat/components/OutputSpecConfirmationCard.test.tsx src/pages/chat/useKbChat.test.ts src/pages/chat/ChatPage.test.tsx`

Expected: FAIL.

- [ ] **Step 3: Implement typed clients and chat UI**

Do not let the frontend derive readiness, hashes or permissions. It renders server fields and sends user actions only. Preserve existing `DocumentStatusCard` behavior for legacy events.

- [ ] **Step 4: Run focused frontend tests and type build**

Run: `cd frontend && npx vitest run src/api/documentAuthoring.test.ts src/pages/chat/components/documentCardModel.test.ts src/pages/chat/components/OutputSpecConfirmationCard.test.tsx src/pages/chat/useKbChat.test.ts src/pages/chat/ChatPage.test.tsx && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api frontend/src/pages/chat
git commit -m "feat: confirm document plans in chat"
```

### Task 14: Resolve task/session/workOrder deep links in the workbench

**Files:**

- Create: `frontend/src/pages/documentGenerationDeepLink.ts`
- Create: `frontend/src/pages/documentGenerationDeepLink.test.ts`
- Modify: `frontend/src/pages/DocumentGenerationPage.tsx`
- Modify: `frontend/src/pages/documentGenerationWorkbench.tsx`
- Modify: `frontend/src/pages/documentGenerationWorkbench.test.tsx`
- Modify: `frontend/src/api/documentAuthoring.ts`
- Modify: `frontend/src/api/types.ts`

**Resolution precedence:**

1. `task` is authoritative: fetch its projection and select session/plan/Work Order from that aggregate.
2. `session`: fetch the owned session, resolve `document_task_id`, then fetch the task projection; remain on clarification/plan view if no Work Order exists.
3. `workOrder`: preserve the existing direct run view.

If more than one parameter is present and their identities conflict, show a scoped error and do not silently select one. `kb` is a display/lookup hint only; the backend response remains authoritative for ownership.

**Workbench behavior:**

- Open the requirement/plan-preparation view for `draft`/`needs_clarification`; a complete draft offers `propose_document_plan` without inventing another task status.
- Open the plan confirmation view for `awaiting_plan_confirmation`.
- Open runs/review for Work Order and later states.
- Preserve the resolved task identity across refresh and internal tab changes.
- A 403/404 stays visible; do not fall back to an unrelated generic workbench.

- [ ] **Step 1: Write failing pure resolver and workbench tests**

Cover task-only, session-only, Work-Order-only, consistent combined refs, conflicting refs, session without Work Order, permission/not-found errors and percent-encoded IDs.

- [ ] **Step 2: Confirm failure**

Run: `cd frontend && npx vitest run src/pages/documentGenerationDeepLink.test.ts src/pages/documentGenerationWorkbench.test.tsx`

Expected: FAIL because the page currently consumes only `kb` and `workOrder`.

- [ ] **Step 3: Implement resolver and page integration**

Keep URL parsing and identity reconciliation in the pure module. `DocumentGenerationPage` should orchestrate fetch/state only; do not add another copy of task-status mapping to the page component.

- [ ] **Step 4: Run workbench and card-link regressions**

Run: `cd frontend && npx vitest run src/pages/documentGenerationDeepLink.test.ts src/pages/documentGenerationWorkbench.test.tsx src/pages/chat/components/documentCardModel.test.ts src/api/documentAuthoring.test.ts && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/documentGenerationDeepLink.ts frontend/src/pages/documentGenerationDeepLink.test.ts frontend/src/pages/DocumentGenerationPage.tsx frontend/src/pages/documentGenerationWorkbench.tsx frontend/src/pages/documentGenerationWorkbench.test.tsx frontend/src/api
git commit -m "feat: restore document tasks from workbench links"
```

### Task 15: Close Phase 0–1 with end-to-end gates and rollout documentation

**Files:**

- Create: `tests/test_conversation_led_document_planning_e2e.py`
- Modify: `tests/test_document_authoring_settings.py`
- Modify: `README.md`
- Modify: `deploy/ragflow/README.md` only if it is the repository's active worker runbook; otherwise create `docs/document-authoring-planning-rollout.md`
- Modify: `docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md` only to append factual implementation status after all gates pass
- Modify: this plan only to record actual verification results after implementation

**End-to-end scenarios:**

1. Template-backed chat request → minimal clarification → plan proposal → explicit confirmation → submission outbox → exactly one Work Order/job.
2. Duplicate messages/proposal/confirmation and worker restart still produce one accepted plan, Work Order and job.
3. Recommended defaults populate the proposal but create no Work Order before confirmation.
4. Template/source/permission change makes a proposal stale or rejects dispatch.
5. Template-free request persists a useful blocked proposal and never enters the current template renderer.
6. Current-answer PDF export creates an ExportJob and no DocumentTask; KB-backed ICD creation creates a DocumentTask and no ExportJob.
7. Refresh/reconnect and each deep-link form restore the same task and current actionable state.
8. Legacy session, direct template tool with v2 disabled and legacy Work Order execution remain compatible.

**Rollout sequence:**

1. Deploy schema/contracts with both flags false.
2. Enable `DOCUMENT_PLANNING_SHADOW_ENABLED` in a test environment; compare plan stability, issue distribution and zero execution drift.
3. Keep v2 false until shadow metrics, migration rehearsal and permission tests pass.
4. Enable `DOCUMENT_PLANNING_V2_ENABLED` for an allowlisted tenant/document type; keep the legacy endpoint available.
5. Expand only after proposal-to-confirmation conversion, stale rate, failure rate and support load meet the recorded threshold policy.
6. Roll back by disabling v2; accepted plans and submissions remain readable and drain safely, while no new v2 session is created.

- [ ] **Step 1: Write the failing end-to-end tests and rollout assertions**

Also assert both flags default false and settings reload behaves deterministically.

- [ ] **Step 2: Confirm failure before final integration fixes**

Run: `.venv/bin/pytest tests/test_conversation_led_document_planning_e2e.py tests/test_document_authoring_settings.py -q`

Expected: FAIL until all Phase 0–1 pieces are connected.

- [ ] **Step 3: Make only integration fixes and write the operator runbook**

Document schema migration, flags, worker ordering, outbox recovery, metrics, stale-plan remediation and rollback. Do not implement Phase 2 behavior to make the E2E test pass.

- [ ] **Step 4: Run the complete verification matrix**

Backend focused:

```bash
.venv/bin/pytest \
  tests/test_document_planning_contracts.py \
  tests/test_document_planning_store.py \
  tests/test_document_planning_registry.py \
  tests/test_document_planning_sources.py \
  tests/test_document_shadow_planner.py \
  tests/test_document_plan_diff.py \
  tests/test_document_planning_shadow_integration.py \
  tests/test_document_output_spec_intake.py \
  tests/test_document_plan_proposals.py \
  tests/test_document_plan_confirmation.py \
  tests/test_document_plan_submission_worker.py \
  tests/test_conversation_led_document_planning_e2e.py -q
```

Backend compatibility:

```bash
.venv/bin/pytest \
  tests/test_generation_sessions.py \
  tests/test_document_tasks.py \
  tests/test_document_task_projection.py \
  tests/test_document_clarification_projection.py \
  tests/test_document_generation_api_sessions.py \
  tests/test_document_generation_api_work_orders.py \
  tests/test_document_generation_prepare.py \
  tests/test_agent_document_authoring_tools.py \
  tests/test_agent_document_intent_routing.py \
  tests/test_intent_planner.py \
  tests/test_conversation_orchestrator.py \
  tests/test_document_work_order_source_snapshots.py \
  tests/test_document_authoring_job_store.py \
  tests/test_document_authoring_durable_resume.py \
  tests/test_full_generation_flow.py -q
```

Backend full suite:

```bash
.venv/bin/pytest tests/ -q
```

Frontend:

```bash
cd frontend
npx vitest run
npm run build
```

Static checks from repository root:

```bash
.venv/bin/python -m py_compile \
  src/document_authoring/planning/*.py \
  src/document_authoring/generation_sessions.py \
  src/document_authoring/tasks.py \
  src/document_authoring/service.py \
  src/core/conversation_orchestrator.py \
  src/core/app_pipeline.py
git diff --check
```

Expected: all tests and builds pass. Record exact counts and any pre-existing warning separately; skipped external-model tests do not prove live provider behavior.

- [ ] **Step 5: Perform manual failure-boundary smoke tests**

- Create a v2 proposal, restart the API/worker, then confirm and verify the same task resumes.
- Stop the worker after confirmation; verify the submission remains durable and dispatches exactly once after restart.
- Revoke KB permission between confirmation and dispatch; verify no Work Order job runs and the task shows an actionable failure.
- Change the template before confirmation; verify stale rejection and a new proposal.
- Confirm the chat card and all three deep links restore the same task.
- Confirm no test or smoke step sends source content to an external model unless explicitly configured and authorized.

- [ ] **Step 6: Commit closeout tests and documentation**

Stage only the files actually changed. If `deploy/ragflow/README.md` is the active runbook, stage that tracked file; otherwise force-add the new ignored `docs/document-authoring-planning-rollout.md`. Do not stage the whole ignored `docs/` tree.

```bash
git add tests/test_conversation_led_document_planning_e2e.py tests/test_document_authoring_settings.py README.md
if [ -f docs/document-authoring-planning-rollout.md ]; then
  git add -f docs/document-authoring-planning-rollout.md
else
  git add deploy/ragflow/README.md
fi
git add docs/superpowers/specs/2026-09-08-conversation-led-document-authoring-design.md docs/superpowers/plans/2026-09-08-conversation-led-document-authoring-phase0-1.md
git commit -m "test: close conversation-led planning phase one"
```

## Phase 0 exit gate

Phase 0 is complete only when:

- identical fixed inputs produce identical semantic plans and hashes;
- all current template-backed Work Orders can produce a shadow plan or an explicit structured issue;
- shadow mode causes zero difference in Work Order fingerprint/status, job count and Artifact bytes;
- planning records and events contain no source text, paths, credentials or raw template content;
- both rollout flags remain disabled by default.

## Phase 1 exit gate

Phase 1 is complete only when:

- a new authoring request can move from conversation to a persisted proposal and explicit confirmation;
- no v2 request creates a Work Order or job before hash-bound confirmation;
- accepted spec/plan/snapshot versions are immutable and stale inputs fail closed;
- the submission outbox survives process failure and creates at most one template-backed Work Order/job;
- template-free requests are representable but cannot reach the current renderer;
- export and authoring routing examples pass without cross-creating task types;
- task/session/workOrder links restore the same authorized task at every pre- and post-Work-Order state;
- legacy sessions, Work Orders, fingerprints and execution tests remain green;
- the full backend suite, full frontend suite and production build pass.

## Handoff to Phase 2

Do not begin Phase 2 automatically. After the Phase 1 exit gate, report the actual plan-quality metrics and migration findings, then write a separate Phase 2 plan for:

1. compiling accepted `DocumentPlan` objects into the persisted execution DAG;
2. native typed-table Writer output and row-key coverage;
3. deterministic aggregation;
4. pre-render and post-render document-level Reviewers;
5. parity and ICD `Pin Definition` quality evaluation against the approved human baseline.
