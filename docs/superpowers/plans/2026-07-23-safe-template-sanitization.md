# Safe Template Sanitization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept active-content-bearing XLSX/XLSM/DOCX files as read-only layout sources and generate only active-content-free output documents.

**Architecture:** A package-level sanitizer creates a hash-bound safe derivative before structural analysis. The original upload and sanitization report are persisted for audit, while all analysis, template approval, rendering, and downloads use the safe derivative only. The renderer retains its existing allowlisted write model and adds a final active-content-free assertion.

**Tech Stack:** Python 3, `zipfile`, `xml.etree.ElementTree`, Pydantic, SQLite, pytest, Streamlit.

## Global Constraints

- Do not load/save workbooks through an Office object model.
- Never send raw OOXML bytes, macro bytes, external targets, or embedded payloads to the LLM.
- Source templates remain immutable and are not used for rendering.
- All generated XLSM-derived artifacts are XLSX; generated DOCX artifacts remain DOCX.
- Macros, external links, embedded OLE/Visio, ActiveX, and form-control package parts must be absent from the safe derivative and final artifact.
- Formula cells, protected cells, hidden cells/sheets, and non-anchor merged cells remain non-writable.
- Do not add the user-provided CAM workbook fixture to Git; test the sanitizer with generated OOXML fixtures and run the CAM file as a documented local acceptance check.

---

### Task 1: Model and persist a source-to-safe-template sanitization record

**Files:**
- Modify: `src/document_authoring/models.py:24-72`
- Modify: `src/document_authoring/work_order_store.py:72-230, 252-350`
- Test: `tests/test_template_sanitization_store.py`

**Interfaces:**
- Produces `TemplateSanitizationReport` with `template_version_id`, `source_format`, `source_content_hash`, `sanitized_format`, `sanitized_content_hash`, `removed_parts`, `removed_relationships`, `status`, and `created_at`.
- Produces `DocumentAuthoringStore.save_sanitized_template(template, source_content, source_format, sanitized_content, security_report, sanitization_report) -> TemplateVersion`.
- Produces `DocumentAuthoringStore.get_template_sanitization_report(template_version_id) -> TemplateSanitizationReport | None`.
- The `TemplateVersion.content_hash`, `format`, and `storage_ref` identify the sanitized derivative; raw source storage is reachable only through the sanitization record.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_save_sanitized_template_keeps_raw_source_separate_from_render_template(tmp_path):
    store = DocumentAuthoringStore(str(tmp_path / "authoring.db"), str(tmp_path / "files"))
    template = _template(content_hash=sha256(b"safe").hexdigest(), format="xlsx")
    report = _sanitization_report(template.template_version_id, source_hash=sha256(b"raw").hexdigest())

    saved = store.save_sanitized_template(
        template, b"raw", "xlsm", b"safe", _clean_security_report(b"safe"), report
    )

    assert store.read_template_content(saved.template_version_id) == b"safe"
    assert Path(store.get_template_sanitization_report(saved.template_version_id).source_storage_ref).read_bytes() == b"raw"
    assert saved.content_hash != report.source_content_hash
```

- [ ] **Step 2: Run the persistence test and verify it fails**

Run: `.venv/bin/python -m pytest tests/test_template_sanitization_store.py -q`

Expected: FAIL because `TemplateSanitizationReport` and `save_sanitized_template` do not exist.

- [ ] **Step 3: Add contracts and storage methods**

```python
class TemplateSanitizationReport(BaseModel):
    template_version_id: str
    source_format: Literal["xlsm", "xlsx", "docx"]
    source_content_hash: str
    source_storage_ref: str
    sanitized_format: Literal["xlsx", "docx"]
    sanitized_content_hash: str
    removed_parts: list[str] = Field(default_factory=list)
    removed_relationships: list[str] = Field(default_factory=list)
    status: Literal["sanitized", "failed"]
    created_at: datetime = Field(default_factory=utc_now)
```

Create `template_sanitization_reports` keyed by `template_version_id`. Write source bytes under `template_sources/`, safe bytes under the existing `templates/` storage path, then insert the template, clean security report, and report in one SQLite transaction. Reject mismatched hashes, unsupported source-to-safe format conversions, or duplicate template IDs.

- [ ] **Step 4: Run persistence tests and the existing template-store tests**

Run: `.venv/bin/python -m pytest tests/test_template_sanitization_store.py tests/test_template_analysis_contracts.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the persistence deliverable**

```bash
git add src/document_authoring/models.py src/document_authoring/work_order_store.py tests/test_template_sanitization_store.py
git commit -m "feat: persist sanitized template sources"
```

### Task 2: Implement fail-closed OOXML package sanitization

**Files:**
- Create: `src/document_authoring/template_sanitizer.py`
- Test: `tests/test_template_sanitizer.py`

**Interfaces:**
- Consumes `sanitize_template(content: bytes, source_format: Literal["xlsx", "xlsm", "docx"])`.
- Produces `SanitizedTemplate(content: bytes, format: Literal["xlsx", "docx"], removed_parts: list[str], removed_relationships: list[str])`.
- Raises `TemplateSanitizationError` for malformed ZIP/XML, dangling references, missing package roots, or residual active content.

- [ ] **Step 1: Write failing XLSX/XLSM and DOCX sanitizer tests**

```python
def test_sanitize_xlsm_removes_active_parts_and_returns_xlsx():
    result = sanitize_template(_workbook_with_vba_external_link_and_embedded_object(), "xlsm")
    assert result.format == "xlsx"
    assert _active_parts(result.content) == []
    assert _external_relationships(result.content) == []
    assert "xl/vbaProject.bin" in result.removed_parts

def test_sanitize_docx_removes_embedded_object_and_external_relationship():
    result = sanitize_template(_docx_with_external_link_and_ole_object(), "docx")
    assert result.format == "docx"
    assert _active_parts(result.content) == []
    assert _external_relationships(result.content) == []

def test_sanitize_rejects_a_package_with_a_dangling_relationship():
    with pytest.raises(TemplateSanitizationError, match="dangling"):
        sanitize_template(_package_with_dangling_relationship(), "xlsx")
```

- [ ] **Step 2: Run the sanitizer tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_template_sanitizer.py -q`

Expected: FAIL because `template_sanitizer` does not exist.

- [ ] **Step 3: Implement the sanitizer as a package transformation**

```python
SourceFormat = Literal["xlsx", "xlsm", "docx"]
SafeFormat = Literal["xlsx", "docx"]

def sanitize_template(content: bytes, source_format: SourceFormat) -> SanitizedTemplate:
    package = _read_package(content)
    removal = _active_content_removal_set(package, source_format)
    retained = _remove_parts_and_relationships(package, removal)
    _remove_relationship_consumers(retained, removal.relationship_ids)
    _remove_content_type_overrides(retained, removal.parts)
    _normalize_workbook_content_type(retained, source_format)
    sanitized = _write_package(retained)
    _validate_sanitized_package(sanitized, target_format(source_format))
    return SanitizedTemplate(
        content=sanitized,
        format=target_format(source_format),
        removed_parts=sorted(removal.parts),
        removed_relationships=sorted(removal.relationships),
    )
```

Resolve each `.rels` target relative to its owning part. Remove relationships targeting an active part; remove owner XML elements that reference their removed relationship IDs; remove corresponding `[Content_Types].xml` overrides. For XLSM, remove the VBA relationship and replace the macro-enabled workbook content type with the ordinary XLSX content type. Preserve every unaffected ZIP entry and its metadata. Validation must parse all relationship parts, reject missing targets, require `xl/workbook.xml` or `word/document.xml`, and confirm no active part or external relationship remains.

- [ ] **Step 4: Run sanitizer tests and static checks**

Run: `.venv/bin/python -m pytest tests/test_template_sanitizer.py -q && .venv/bin/ruff check src/document_authoring/template_sanitizer.py tests/test_template_sanitizer.py`

Expected: all tests pass and Ruff reports no violations.

- [ ] **Step 5: Commit the sanitizer deliverable**

```bash
git add src/document_authoring/template_sanitizer.py tests/test_template_sanitizer.py
git commit -m "feat: sanitize active content from OOXML templates"
```

### Task 3: Analyze, approve, and render only the safe derivative

**Files:**
- Modify: `src/document_authoring/service.py:142-225, 760-830`
- Modify: `src/document_authoring/renderers/xlsm.py:75-132`
- Modify: `src/document_authoring/renderers/docx.py:82-125`
- Modify: `tests/test_template_upload_service.py`
- Modify: `tests/test_template_authoring_integration.py`

**Interfaces:**
- Consumes `SanitizedTemplate` from Task 2 and `save_sanitized_template` from Task 1.
- `DocumentGenerationService.analyze_uploaded_template` returns a `TemplateAnalysis` over the safe format/content hash.
- Generated artifact validation raises `ValueError("generated artifact contains active content")` when inspection finds active content.

- [ ] **Step 1: Write failing service and integration tests**

```python
def test_active_xlsm_upload_is_analyzed_as_a_safe_xlsx_template(authoring_service, author_ctx):
    analysis = authoring_service.analyze_uploaded_template(
        author_ctx, filename="review.xlsm", content=_workbook_with_vba_external_link_and_embedded_object(), template_name="review"
    )
    template = authoring_service.store.get_template(analysis.template_version_id)
    report = authoring_service.store.get_template_sanitization_report(template.template_version_id)
    assert analysis.status == "ready_for_confirmation"
    assert analysis.format == template.format == "xlsx"
    assert report.source_format == "xlsm"
    assert authoring_service.workbook_renderer.inspect(authoring_service.store.read_template_content(template.template_version_id)).active_content_status == "clean"

def test_rendered_artifact_from_sanitized_template_contains_no_active_content(tmp_path):
    pipeline, ctx, baseline, retrieve = _pipeline_with_approved_project_source(tmp_path)
    analysis = pipeline.analyze_document_template(
        ctx, filename="review.docx", content=_docx_with_external_link_and_ole_object(), template_name="Review",
    )
    template = pipeline.confirm_document_template(ctx, analysis_id=analysis.analysis_id, display_name="Review")
    order = pipeline.create_document_work_order(
        ctx, project_id=baseline.project_id, baseline_id=baseline.baseline_id,
        template_version_id=template.template_version_id,
        document_schema_id=template.template_schema_id,
        document_schema_version=template.template_schema_version,
        harness_policy_id="deterministic-docx-writer",
    )
    candidate = pipeline.run_internal_document_harness(ctx, order.work_order_id, retrieve=retrieve)
    assert _active_parts(pipeline.download_document_artifact(ctx, candidate.artifact_id)) == []
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_template_upload_service.py tests/test_template_authoring_integration.py -q`

Expected: FAIL because uploads still analyze the original content and active content returns `requires_human`.

- [ ] **Step 3: Integrate sanitization at the service boundary**

```python
sanitized = sanitize_template(content, suffix)
report = self._inspect(sanitized.content, sanitized.format)
if report.active_content_status != "clean":
    raise ValueError("sanitized template still contains active content")
template = TemplateVersion(
    template_version_id=template_version_id,
    template_id=template_name.strip() or filename,
    format=sanitized.format,
    content_hash=hashlib.sha256(sanitized.content).hexdigest(),
    template_schema_id=template_schema_id,
    template_schema_version="1",
    renderer_policy_id=policy.renderer_policy_id,
)
template = self.store.save_sanitized_template(template, content, suffix, sanitized.content, report, sanitization_report)
analysis = analyze_template(sanitized.content, sanitized.format).model_copy(update={
    "analysis_id": f"analysis-{uuid.uuid4().hex}",
    "template_version_id": template.template_version_id,
})
```

Create renderer policies with `macro_policy="strip"`, `external_link_policy="strip"`, `embedded_object_policy="strip"`, and an empty active-content allowlist. Before saving any candidate or released artifact, inspect it and reject it unless `active_content_status == "clean"`; keep the existing changed-parts integrity checks.

- [ ] **Step 4: Run focused regression tests**

Run: `.venv/bin/python -m pytest tests/test_template_upload_service.py tests/test_template_authoring_integration.py tests/test_docx_renderer.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the safe-generation deliverable**

```bash
git add src/document_authoring/service.py src/document_authoring/renderers/xlsm.py src/document_authoring/renderers/docx.py tests/test_template_upload_service.py tests/test_template_authoring_integration.py
git commit -m "feat: generate from sanitized templates only"
```

### Task 4: Show sanitization results in the Streamlit upload workflow

**Files:**
- Modify: `src/document_authoring/service.py:142-225`
- Modify: `src/core/app_pipeline.py:351-360`
- Modify: `src/ui/document_generation_page.py:87-141`
- Modify: `tests/test_document_generation_page.py`

**Interfaces:**
- Produces `DocumentGenerationService.get_template_sanitization_report(template_version_id)` and `AppPipeline.get_document_template_sanitization_report(ctx, template_version_id)`.
- Consumes `pipeline.get_document_template_sanitization_report(ctx, template_version_id)`.
- Displays removed asset counts and the safe output format before template confirmation.
- Does not render raw part names, external URLs, or source bytes.

- [ ] **Step 1: Write failing page-helper tests**

```python
def test_sanitization_summary_exposes_only_asset_counts_and_safe_format():
    summary = _safe_sanitization_summary(_report(removed_parts=["xl/vbaProject.bin", "xl/externalLinks/a.xml"]))
    assert summary == {"已移除宏": 1, "已移除外链": 1, "安全模板格式": "xlsx"}
    assert "vbaProject.bin" not in str(summary)
```

- [ ] **Step 2: Run the page tests and verify they fail**

Run: `.venv/bin/python -m pytest tests/test_document_generation_page.py -q`

Expected: FAIL because `_safe_sanitization_summary` does not exist.

- [ ] **Step 3: Implement the safe upload summary and pipeline getter**

```python
def get_template_sanitization_report(self, template_version_id: str):
    return self.store.get_template_sanitization_report(template_version_id)

def get_document_template_sanitization_report(self, ctx, template_version_id: str):
    return self.document_generation.get_template_sanitization_report(template_version_id)

summary = _safe_sanitization_summary(pipeline.get_document_template_sanitization_report(ctx, analysis.template_version_id))
st.success("已生成无活动内容的安全模板；后续分析与生成仅使用该副本。")
st.json(summary, expanded=False)
```

Count macros, external links, and embedded/control parts from the report; display zero values only when all are zero. Keep the existing safe analysis table and confirmation action. Replace the `requires_human` upload dead-end only for successfully sanitized files; malformed sanitization failures remain errors.

- [ ] **Step 4: Run page and API-contract tests**

Run: `.venv/bin/python -m pytest tests/test_document_generation_page.py tests/test_template_upload_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the upload UX deliverable**

```bash
git add src/document_authoring/service.py src/core/app_pipeline.py src/ui/document_generation_page.py tests/test_document_generation_page.py
git commit -m "feat: show sanitized template upload results"
```

### Task 5: Verify the CAM workbook locally and document the operator path

**Files:**
- Modify: `docs/Hardware-DataBase_Agentic-RAG改造方案.md`
- Test: `tests/test_template_sanitizer.py`

**Interfaces:**
- Uses the committed synthetic fixtures from Task 2 in CI.
- Uses the local, untracked file `docs/ADAS/22_825504681 825504682_CAM_硬件原理图设计评审检查单.xlsm` for a non-CI acceptance command.

- [ ] **Step 1: Document the user workflow and local CAM acceptance command**

```bash
cd /home/user/workspace/Hardware-DataBase-integrate-develop/.worktrees/template-upload-governed-authoring
.venv/bin/python -c "from pathlib import Path; from src.document_authoring.template_sanitizer import sanitize_template; p=Path('../docs/ADAS/22_825504681 825504682_CAM_硬件原理图设计评审检查单.xlsm').resolve(); r=sanitize_template(p.read_bytes(), 'xlsm'); print(r.format, len(r.removed_parts))"
```

Document that the original is retained for audit, that generated XLSM-derived output is XLSX, and that no manual source-template cleanup is required.

- [ ] **Step 2: Run the CAM local acceptance command and full relevant verification**

Run:

```bash
.venv/bin/python -c "from pathlib import Path; from src.document_authoring.template_sanitizer import sanitize_template; p=Path('../docs/ADAS/22_825504681 825504682_CAM_硬件原理图设计评审检查单.xlsm').resolve(); r=sanitize_template(p.read_bytes(), 'xlsm'); assert r.format == 'xlsx'; assert r.removed_parts; print('CAM sanitize passed', len(r.removed_parts))"
.venv/bin/python -m pytest tests/test_template_sanitizer.py tests/test_template_sanitization_store.py tests/test_template_upload_service.py tests/test_template_authoring_integration.py tests/test_document_generation_page.py tests/test_docx_renderer.py -q
.venv/bin/ruff check src/document_authoring src/core/app_pipeline.py src/ui/document_generation_page.py tests/test_template_sanitizer.py tests/test_template_sanitization_store.py tests/test_document_generation_page.py
```

Expected: all tests pass and Ruff reports no violations.

- [ ] **Step 3: Commit documentation and final verification changes**

```bash
git add docs/Hardware-DataBase_Agentic-RAG改造方案.md tests/test_template_sanitizer.py
git commit -m "docs: describe sanitized template workflow"
```
