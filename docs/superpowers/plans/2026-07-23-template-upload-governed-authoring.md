# Template Upload and Governed Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Streamlit upload, rule-and-LLM-assisted analysis, one-click controlled activation, DOCX/XLSX/XLSM candidate generation, and durable live run status for project-baseline-bound document authoring.

**Architecture:** Format-specific analyzers create a safe, format-neutral structural inventory. A model may propose semantics only from that inventory; the service validates every proposed write location against the inventory before persisting the approved template/schema. The existing project-scoped Harness continues to retrieve evidence and create drafts, while independent OOXML renderers perform only allowlisted mutations.

**Tech Stack:** Python 3.12+, Pydantic 2, SQLite, standard-library `zipfile`/`xml.etree.ElementTree`, Streamlit, existing `LLMClient`, pytest.

## Global Constraints

- Support exactly `xlsx`, `xlsm`, and `docx`; do not add Markdown, MCP, REST, CLI, batch, scheduled, ProjectFact, ProjectSnapshot, or external-agent execution.
- Preserve ProjectBaseline, SourceSetSnapshot, Evidence validation, Harness, WorkOrder, approval, audit, and existing XLSM safeguards.
- Store the uploaded bytes immutably and bind analysis, activation, rendering, and approval to the exact SHA-256 content hash.
- The LLM receives only the normalized structural inventory and returns Pydantic-validated JSON; it never receives binary bytes, arbitrary paths, database access, or raw project sources.
- Template analysis and model-backed document writing reuse the intelligent-chat `LLMClient` and its existing `AGENT_*` URL, model, timeout, and API-key configuration; do not add document-specific provider settings or secret storage.
- Formula, macro, external-link, embedded-object, signature, protected, human-input, and human-approval locations are non-writable by machine execution.
- Use red-green-refactor. Run new tests with `.venv/bin/pytest`; never stage the user-owned `docs/ADAS/` directory or CAM XLSM file.
- The rollback task produces a reviewable inventory only. Do not delete, rewrite history, or change retained behavior until the user approves an exact deletion list.

---

### Task 1: Produce the read-only rollback inventory

**Files:**
- Create: `docs/superpowers/audits/2026-07-23-document-authoring-rollback-inventory.md`
- Test: no automated test; this is a read-only classification report.

**Interfaces:**
- Consumes: implementation introduced in commit `18f2d68` and the approved design spec.
- Produces: file-level `retain`, `deletion_candidate`, or `requires_user_approval` classification with dependency/test impact.

- [ ] **Step 1: Collect file ownership and live references**

Run:

```bash
git diff --name-only 979d545..HEAD
rg -n "document_authoring|src.projects|DocumentGenerationService" src tests streamlit_app.py
```

Expected: identifies the feature foundation, direct callers, and existing tests.

- [ ] **Step 2: Write the inventory**

Use this table:

```markdown
| Path | Classification | Direct callers/tests | User-visible impact | Action before approval |
| --- | --- | --- | --- | --- |
| src/document_authoring/harness/runtime.py | retain | service.py, test_document_authoring_p2a.py | evidence-bound drafting and durable status | none |
```

Classify `src/projects/**`, Document Authoring models/service/store/validator/harness/renderers/worker, `src/core/app_pipeline.py`, `src/ui/document_generation_page.py`, and their tests as `retain`. Classify unimplemented P3/P4/P5 capabilities as `not_present`. Classify isolated, unreferenced dead code as `deletion_candidate`; any proposed removal coupled to Streamlit, source snapshots, templates, Harness, or approval is `requires_user_approval`.

- [ ] **Step 3: Verify no destructive command appears**

Run:

```bash
rg -n "git reset|git checkout|rm -rf|delete source" docs/superpowers/audits/2026-07-23-document-authoring-rollback-inventory.md
```

Expected: no matches.

- [ ] **Step 4: Commit only the inventory**

```bash
git add -f docs/superpowers/audits/2026-07-23-document-authoring-rollback-inventory.md
git commit -m "docs: audit document authoring rollback scope"
```

### Task 2: Add durable template-analysis contracts and storage

**Files:**
- Create: `src/document_authoring/template_analysis.py`
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/__init__.py`
- Create: `tests/test_template_analysis_contracts.py`

**Interfaces:**
- Produces `TemplateAnalysis`, `TemplateAnalysisUnit`, `TemplateAnalysisSuggestion`, and `DocxRegionSchema`.
- `DocumentAuthoringStore.save_template_analysis(analysis)` persists one analysis for a template hash; `get_template_analysis(template_version_id)` returns it.

- [ ] **Step 1: Write failing contract and persistence tests**

```python
def test_template_analysis_rejects_suggested_location_not_in_inventory():
    analysis = TemplateAnalysis(
        analysis_id="analysis-1", template_version_id="template-1", content_hash="a" * 64,
        format="docx", status="ready_for_confirmation",
        units=[TemplateAnalysisUnit(unit_id="paragraph-1", locator={"paragraph_index": 1}, writable=True)],
        suggestions=[TemplateAnalysisSuggestion(
            semantic_unit_id="summary", label="摘要", target_unit_ids=["paragraph-99"], confidence=0.9,
        )],
    )
    with pytest.raises(ValueError, match="unknown analysis unit"):
        analysis.validate_suggestions()


def test_store_round_trips_hash_bound_template_analysis(tmp_path):
    store = DocumentAuthoringStore(db_path=str(tmp_path / "authoring.db"), artifact_root=str(tmp_path / "artifacts"))
    saved = store.save_template_analysis(_docx_analysis("template-1"))
    assert store.get_template_analysis("template-1").content_hash == saved.content_hash
```

- [ ] **Step 2: Run the tests and verify red**

Run: `.venv/bin/pytest -q tests/test_template_analysis_contracts.py`

Expected: import/attribute failures for missing contracts and store methods.

- [ ] **Step 3: Implement the contracts and store**

```python
class TemplateAnalysisUnit(BaseModel):
    unit_id: str
    locator: dict[str, Any]
    label: str = ""
    writable: bool = False
    blocked_reason: str | None = None

class TemplateAnalysisSuggestion(BaseModel):
    semantic_unit_id: str
    label: str
    target_unit_ids: list[str]
    retrieval_terms: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

class TemplateAnalysis(BaseModel):
    analysis_id: str
    template_version_id: str
    content_hash: str
    format: Literal["xlsx", "xlsm", "docx"]
    status: Literal["ready_for_confirmation", "requires_human", "failed"]
    units: list[TemplateAnalysisUnit]
    suggestions: list[TemplateAnalysisSuggestion] = Field(default_factory=list)

    def validate_suggestions(self) -> None:
        units = {unit.unit_id: unit for unit in self.units}
        for suggestion in self.suggestions:
            for unit_id in suggestion.target_unit_ids:
                if unit_id not in units:
                    raise ValueError(f"suggestion references unknown analysis unit: {unit_id}")
                if not units[unit_id].writable:
                    raise PermissionError(f"suggestion targets non-writable analysis unit: {unit_id}")
```

Add a `template_analyses` table keyed by `template_version_id`, storing the model JSON and content hash. Its getter must compare the saved analysis hash with its `TemplateVersion.content_hash`. Add `DocxRegionSchema(region_id, locator, role, write_policy, value_type)` and reject protected roles for writable regions.

- [ ] **Step 4: Run green and commit**

Run: `.venv/bin/pytest -q tests/test_template_analysis_contracts.py`

Expected: PASS.

```bash
git add src/document_authoring/template_analysis.py src/document_authoring/models.py src/document_authoring/work_order_store.py src/document_authoring/__init__.py tests/test_template_analysis_contracts.py
git commit -m "feat: persist hash-bound template analyses"
```

### Task 3: Build safe analyzers and constrained LLM suggestions

**Files:**
- Create: `src/document_authoring/template_analyzers.py`
- Create: `src/document_authoring/template_suggester.py`
- Create: `tests/test_template_analyzers.py`
- Create: `tests/test_template_suggester.py`

**Interfaces:**
- `analyze_template(content: bytes, format: Literal["xlsx", "xlsm", "docx"]) -> TemplateAnalysis`.
- `TemplateSuggestionProvider.suggest(analysis: TemplateAnalysis) -> list[TemplateAnalysisSuggestion]`.
- `LLMTemplateSuggestionProvider(client: LLMClient)` sends only `analysis.model_dump_json()` and parses a JSON array.

- [ ] **Step 1: Write failing safe-structure tests**

```python
def test_xlsm_analysis_never_marks_formula_or_active_content_cell_writable():
    analysis = analyze_template(_xlsm_with_formula_and_vba(), "xlsm")
    by_id = {unit.unit_id: unit for unit in analysis.units}
    assert by_id["sheet:Review!A1"].writable is False
    assert by_id["sheet:Review!A1"].blocked_reason == "formula"
    assert analysis.status == "requires_human"


def test_docx_analysis_exposes_paragraph_and_table_cells_but_protects_external_relationships():
    analysis = analyze_template(_docx_with_paragraph_table_and_external_link(), "docx")
    assert any(unit.locator == {"paragraph_index": 0} for unit in analysis.units)
    assert any(unit.locator == {"table_index": 0, "row_index": 0, "cell_index": 0} for unit in analysis.units)
    assert all(unit.writable is False for unit in analysis.units if unit.blocked_reason == "external_relationship")
```

- [ ] **Step 2: Run red**

Run: `.venv/bin/pytest -q tests/test_template_analyzers.py`

Expected: `ModuleNotFoundError` for `template_analyzers`.

- [ ] **Step 3: Implement package-only analyzers**

Use `zipfile.ZipFile` and `ElementTree` only. Reuse the existing workbook relationship resolution logic to enumerate sheets/cells. A workbook cell is writable only when it has no formula, is not protected, hidden, or a merged non-anchor, and is not in an active-content workbook. For DOCX read `word/document.xml`, `word/_rels/document.xml.rels`, and package relationships; emit paragraph and table-cell locators. Mark units protected if the package has external relations, embeddings, macros, signatures, or document protection. Return `requires_human` whenever active content is present.

- [ ] **Step 4: Write and run failing constrained-suggester test**

```python
def test_llm_suggester_only_sends_structural_inventory_and_rejects_invalid_json():
    client = RecordingClient(response='[{"semantic_unit_id":"summary","label":"摘要","target_unit_ids":["p-1"],"confidence":0.9}]')
    suggestions = LLMTemplateSuggestionProvider(client).suggest(_safe_docx_analysis())
    assert "content_hash" in client.messages[1]["content"]
    assert "PK" not in client.messages[1]["content"]
    assert suggestions[0].semantic_unit_id == "summary"
```

Run: `.venv/bin/pytest -q tests/test_template_suggester.py`

Expected: import failure.

- [ ] **Step 5: Implement strict suggestion parsing**

Use a system prompt requiring a JSON array with exactly `semantic_unit_id`, `label`, `target_unit_ids`, `retrieval_terms`, and `confidence`. Parse `json.loads(client.chat(..., usage_stage="template_analysis"))`, validate each suggestion, then call `analysis.validate_suggestions()`. Convert model/network/JSON errors into `analysis.status="requires_human"` with an operator-visible error; do not infer a writable locator.

- [ ] **Step 6: Run green and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_template_analyzers.py tests/test_template_suggester.py
git add src/document_authoring/template_analyzers.py src/document_authoring/template_suggester.py tests/test_template_analyzers.py tests/test_template_suggester.py
git commit -m "feat: analyze templates with constrained LLM suggestions"
```

Expected: tests PASS before the commit.

### Task 4: Add controlled DOCX rendering and format-aware FillPlans

**Files:**
- Create: `src/document_authoring/renderers/docx.py`
- Modify: `src/document_authoring/renderers/__init__.py`
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_docx_renderer.py`

**Interfaces:**
- `DocxFillPlan(template_version_id: str, fills: list[DocxFill])` uses explicit paragraph/table-cell/content-control locators.
- `DocxRenderer.inspect(content)` and `DocxRenderer.render(content, regions, fill_plan, policy, security_approved=False)`.
- `DocumentGenerationService._render_fill_plan(template, fill_plan) -> tuple[bytes, dict]`.

- [ ] **Step 1: Write failing DOCX integrity tests**

```python
def test_docx_renderer_changes_only_approved_paragraph_part():
    source = _docx_with_text("old")
    output = DocxRenderer().render(source, [_docx_region("p-0")], _docx_fill_plan("p-0", "new"), _clean_policy())
    assert _paragraph_text(output.content) == "new"
    assert output.integrity_manifest["changed_parts"] == ["word/document.xml"]


def test_docx_renderer_rejects_human_only_region():
    with pytest.raises(PermissionError, match="machine-written"):
        DocxRenderer().render(_docx_with_text("old"), [_human_only_docx_region()], _docx_fill_plan("human", "new"), _clean_policy())
```

- [ ] **Step 2: Run red**

Run: `.venv/bin/pytest -q tests/test_docx_renderer.py`

Expected: import failure for `DocxRenderer`.

- [ ] **Step 3: Implement byte-preserving DOCX renderer**

Copy every ZIP entry into a new package and replace only `word/document.xml`. Resolve paragraph/table/content-control locators. Reject an unknown region, non-writable policy, or protected role. Change only `w:t` text nodes so run properties survive. Hash every input/output part, require all relationship parts to be identical, and permit only `word/document.xml` in `changed_parts`. Reuse active-content policy rejection for macro, external-link, and embedded-object packages.

- [ ] **Step 4: Dispatch rendering by frozen template format**

```python
def _render_fill_plan(self, template: TemplateVersion, fill_plan):
    content = self.store.read_template_content(template.template_version_id)
    policy = self._policy(template)
    if template.format in {"xlsx", "xlsm"}:
        result = self.workbook_renderer.render(
            content, self.store.list_workbook_regions(template.template_schema_id, template.template_schema_version),
            fill_plan, policy, security_approved=True,
        )
    elif template.format == "docx":
        result = self.docx_renderer.render(
            content, self.store.list_docx_regions(template.template_schema_id, template.template_schema_version),
            fill_plan, policy, security_approved=True,
        )
    else:
        raise ValueError(f"unsupported controlled output format: {template.format}")
    return result.content, result.integrity_manifest
```

Make `_semantic_fills` return `WorkbookFillPlan` for workbooks and `DocxFillPlan` for DOCX.

- [ ] **Step 5: Run green and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_docx_renderer.py tests/test_document_authoring_p2a.py
git add src/document_authoring/renderers src/document_authoring/models.py src/document_authoring/service.py tests/test_docx_renderer.py
git commit -m "feat: render approved DOCX template regions"
```

Expected: tests PASS before the commit.

### Task 5: Add upload-to-enable APIs and default Harness configuration

**Files:**
- Modify: `src/document_authoring/service.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/document_authoring/work_order_store.py`
- Create: `tests/test_template_upload_service.py`

**Interfaces:**
- `DocumentGenerationService.analyze_uploaded_template(ctx, *, filename, content, template_name) -> TemplateAnalysis`.
- `DocumentGenerationService.confirm_template_analysis(ctx, *, analysis_id, display_name) -> TemplateVersion`.
- `AppPipeline.analyze_document_template(...)` and `AppPipeline.confirm_document_template(...)`.

- [ ] **Step 1: Write failing upload/confirmation tests**

```python
def test_confirmed_docx_analysis_creates_hash_bound_approved_template_and_schema(authoring_service, author_ctx):
    analysis = authoring_service.analyze_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("Project Summary"), template_name="Review",
    )
    template = authoring_service.confirm_template_analysis(author_ctx, analysis_id=analysis.analysis_id, display_name="Review")
    assert template.format == "docx"
    assert template.status == "approved"
    assert authoring_service.store.get_template_analysis(template.template_version_id).content_hash == template.content_hash


def test_confirmation_rejects_when_template_hash_changes(authoring_service, author_ctx):
    analysis = authoring_service.analyze_uploaded_template(
        author_ctx, filename="review.docx", content=_docx_with_text("A"), template_name="Review",
    )
    _replace_template_bytes(authoring_service.store, analysis.template_version_id, _docx_with_text("B"))
    with pytest.raises(ValueError, match="content hash"):
        authoring_service.confirm_template_analysis(author_ctx, analysis_id=analysis.analysis_id, display_name="Review")
```

- [ ] **Step 2: Run red**

Run: `.venv/bin/pytest -q tests/test_template_upload_service.py`

Expected: missing upload/confirmation APIs.

- [ ] **Step 3: Implement the service flow**

Infer the format from the lower-case suffix and reject all other suffixes. On upload calculate SHA-256; create a draft `TemplateVersion`; inspect/persist immutable bytes; run deterministic analysis then constrained suggestions; save the analysis. On confirmation load analysis/template; verify matching hashes; validate suggested targets; create regions/bindings and a draft `DocumentSchema`; approve schema/template in one SQLite transaction. Do not enable `requires_human` analyses until exception corrections specify allowed targets. Active-content templates require exact-hash allowlisting in `RendererPolicy`.

When no approved policy exists, create one approved `HarnessPolicy` using the existing five-tool allowlist and a managed provider that calls the same `LLMClient` configuration used by intelligent chat. It must turn the model response into `DocumentUnitDraft` and reject malformed, unsupported, or ungrounded output. The deterministic evidence writer remains the test/offline provider. Do not add document-specific URL, model, or API-key settings. An explicit approved policy still takes precedence.

- [ ] **Step 4: Add and run automatic Harness-selection test**

```python
def test_semantic_template_work_order_uses_frozen_internal_harness_policy(pipeline, author_ctx, approved_project_baseline):
    template = _upload_and_confirm_semantic_template(pipeline, author_ctx)
    order = pipeline.create_document_work_order(
        author_ctx, project_id=approved_project_baseline.project_id, baseline_id=approved_project_baseline.baseline_id,
        template_version_id=template.template_version_id, document_schema_id=template.template_schema_id,
        document_schema_version="1",
    )
    assert order.execution_mode == "internal_harness"
    assert order.harness_policy_id
```

Run: `.venv/bin/pytest -q tests/test_template_upload_service.py tests/test_document_authoring_p2a.py`

Expected: PASS.

- [ ] **Step 5: Commit upload-to-enable APIs**

```bash
git add src/document_authoring/service.py src/document_authoring/work_order_store.py src/core/app_pipeline.py tests/test_template_upload_service.py
git commit -m "feat: activate analyzed templates through document service"
```

### Task 6: Replace the Streamlit page with simplified live workflow

**Files:**
- Modify: `src/ui/document_generation_page.py`
- Create: `tests/test_document_generation_page.py`

**Interfaces:**
- `render_document_generation_page(st, pipeline, ctx)` has `上传模板`, `新建生成任务`, and `任务与下载` tabs.
- `_run_timeline(status: dict) -> list[tuple[str, str]]` maps durable WorkOrder/HarnessRun state to display stages.

- [ ] **Step 1: Write failing pure UI-helper tests**

```python
def test_run_timeline_marks_current_harness_node_and_terminal_error():
    timeline = _run_timeline({"status": "retrieving", "harness_run": {"current_node": "draft_ready_unit", "status": "running", "error": None}})
    assert ("撰写", "active") in timeline
    assert ("渲染", "pending") in timeline


def test_run_timeline_exposes_failed_run_error():
    timeline = _run_timeline({"status": "blocked", "harness_run": {"current_node": "failed", "status": "failed", "error": {"message": "writer unavailable"}}})
    assert ("失败：writer unavailable", "error") in timeline
```

- [ ] **Step 2: Run red**

Run: `.venv/bin/pytest -q tests/test_document_generation_page.py`

Expected: import failure for `_run_timeline`.

- [ ] **Step 3: Implement the three tabs and polling**

Use `st.file_uploader("上传模板", type=["xlsx", "xlsm", "docx"])`; call only `pipeline.analyze_document_template`; show the safe summary and a single `确认并启用模板` action when ready. Show exception-correction controls only for `requires_human` analysis. List only approved templates/baselines in New generation and create one idempotency key per click. In Runs and downloads render the timeline, checkpoint, retries, per-unit state, validation errors, downloads, pause/cancel, and approval.

For queued/running/retrying runs call `st.autorefresh(interval=2000, key=f"document-run-refresh-{work_order_id}")` when available; otherwise show `刷新状态`. Every rerun calls `pipeline.get_document_run_status`; never start work from session state.

- [ ] **Step 4: Run green and commit**

Run:

```bash
.venv/bin/pytest -q tests/test_document_generation_page.py tests/test_template_upload_service.py
git add src/ui/document_generation_page.py tests/test_document_generation_page.py
git commit -m "feat: add live template upload authoring page"
```

Expected: tests PASS before the commit.

### Task 7: Prove end-to-end governance and run regressions

**Files:**
- Create: `tests/test_template_authoring_integration.py`
- Modify: `docs/document_authoring_p2a.md`
- Modify: `docs/document_authoring_p2b.md`

**Interfaces:**
- The integration test uses a deterministic template-suggestion provider, deterministic evidence writer, and project-scoped retrieval callback.
- Documentation describes the supported formats, hash-bound confirmation, renderer policy, and durable status.

- [ ] **Step 1: Write the failing end-to-end test**

```python
def test_uploaded_docx_is_analyzed_confirmed_written_from_project_evidence_and_downloaded(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx, filename="review.docx", content=_docx_with_text("Controller"), template_name="Review",
    )
    template = pipeline.confirm_document_template(ctx, analysis_id=analysis.analysis_id, display_name="Review")
    order = pipeline.create_document_work_order(
        ctx, project_id=baseline.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id, document_schema_id=template.template_schema_id,
        document_schema_version="1",
    )
    candidate = pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)
    assert b"STM32H743" in pipeline.download_document_artifact(ctx, candidate.artifact_id)
```

- [ ] **Step 2: Run red and complete only exposed integration gaps**

Run: `.venv/bin/pytest -q tests/test_template_authoring_integration.py`

Expected: failure at the first incomplete public workflow boundary. The retrieval callback must return a `RetrievalOutcome` whose snapshot ID, source versions, processing artifacts, region policies, and evidence project ID match the frozen order. Do not add chat-agent fallback, global source search, arbitrary file access, or a model-specific test dependency.

- [ ] **Step 3: Run focused regression and static checks**

```bash
.venv/bin/pytest -q tests/test_template_analysis_contracts.py tests/test_template_analyzers.py tests/test_template_suggester.py tests/test_docx_renderer.py tests/test_template_upload_service.py tests/test_document_generation_page.py tests/test_template_authoring_integration.py tests/test_document_authoring_p2a.py tests/test_claim_evidence.py tests/test_app_pipeline_scope.py tests/test_ragflow_metadata_fallback.py
.venv/bin/ruff check src/document_authoring src/ui/document_generation_page.py tests/test_template_analysis_contracts.py tests/test_template_analyzers.py tests/test_template_suggester.py tests/test_docx_renderer.py tests/test_template_upload_service.py tests/test_document_generation_page.py tests/test_template_authoring_integration.py
```

Expected: both commands exit 0.

- [ ] **Step 4: Update docs and commit**

Document rule-backed/LLM-assisted analysis, hash-bound confirmation, allowlisted DOCX edits, and live state from persistent run records. Keep P3 external-agent/MCP limitations unchanged.

```bash
git add tests/test_template_authoring_integration.py docs/document_authoring_p2a.md docs/document_authoring_p2b.md
git commit -m "test: cover governed uploaded-template authoring"
```

## Plan Self-Review

- Spec coverage: Tasks 2–5 implement immutable upload, rule/LLM analysis, controlled confirmation, Harness use, and both OOXML output formats. Task 6 implements simplified real-time Streamlit workflow. Task 7 verifies the governed end-to-end path. Task 1 isolates the requested rollback audit from source deletion.
- Placeholder scan: every task identifies concrete files, public method names, assertions, and commands.
- Type consistency: `TemplateAnalysis` flows from analyzer through store/service to Streamlit. `DocxFillPlan` stays separate from `WorkbookFillPlan`; renderer dispatch uses frozen `TemplateVersion.format`.
