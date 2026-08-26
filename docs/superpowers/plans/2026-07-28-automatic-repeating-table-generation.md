# Automatic Repeating-Table Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compile ordinary workbook tables into typed schemas, activate them without manual mapping review, generate structured rows, and render them safely while removing legacy examples.

**Architecture:** A new deterministic table compiler converts batch-local LLM proposals into global scalar and table mappings. Activation persists typed table schemas, the writer produces row arrays, and the controlled OOXML renderer fills registered table regions. One bounded LLM repair pass handles proposal errors; deterministic validation remains authoritative.

**Tech Stack:** Python 3.12, Pydantic 2, SQLite, OOXML via `zipfile` and `xml.etree.ElementTree`, pytest, Ruff.

## Global Constraints

- Never send raw OOXML bytes, storage paths, formulas, credentials, or hidden workbook content to the LLM.
- LLM output never grants overwrite authority or bypasses deterministic validation.
- Missing evidence clears legacy example values and writes `未找到依据`.
- Existing scalar-only XLSX/XLSM and all DOCX behavior remain backward compatible.
- Existing dirty worktree changes belong to the user and must not be reset or overwritten.
- Every production change follows red-green-refactor TDD.

---

### Task 1: Typed table analysis and schema models

**Files:**
- Modify: `src/document_authoring/template_analysis.py`
- Modify: `src/document_authoring/models.py`
- Create: `src/document_authoring/template_table_compiler.py`
- Test: `tests/test_template_table_compiler.py`

**Interfaces:**
- Produces: `CompiledTemplateTable`, `WorkbookTableSchema`, `WorkbookTableColumnSchema`.
- Produces: `compile_template_tables(analysis: TemplateAnalysis) -> TemplateTableCompilation`.
- Consumes: existing `TemplateAnalysisUnit` geometry and `TemplateAnalysisSuggestion`.

- [ ] **Step 1: Write failing model and compiler tests**

```python
def test_compiler_merges_column_and_row_fragments_into_one_table():
    result = compile_template_tables(icd_fragmented_analysis())
    assert len(result.tables) == 1
    table = result.tables[0]
    assert table.first_data_row == 16
    assert table.last_template_row == 34
    assert [column.column_letter for column in table.columns] == ["A", "B", "C", "D", "E"]
    assert len({item.semantic_unit_id for item in result.scalar_suggestions}) == len(result.scalar_suggestions)


def test_compiler_rejects_true_scalar_table_overlap():
    with pytest.raises(TemplateTableCompilationError, match="scalar_table_overlap"):
        compile_template_tables(overlapping_analysis())
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_template_table_compiler.py`

Expected: collection fails because `template_table_compiler` and table models do not exist.

- [ ] **Step 3: Add backward-compatible analysis models**

```python
class CompiledTemplateTableColumn(BaseModel):
    column_id: str
    label: str
    column_letter: str
    value_type: str = "text"
    required: bool = False


class CompiledTemplateTable(BaseModel):
    semantic_unit_id: str
    label: str
    sheet_name: str
    header_row: int
    first_data_row: int
    last_template_row: int
    style_source_row: int
    columns: list[CompiledTemplateTableColumn]
    target_unit_ids: list[str]
    confidence: float = Field(ge=0.0, le=1.0)


class TemplateAnalysis(BaseModel):
    # existing fields remain
    compiled_tables: list[CompiledTemplateTable] = Field(default_factory=list)
    compilation_diagnostics: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Add persisted workbook table models**

```python
class WorkbookTableColumnSchema(BaseModel):
    column_id: str
    label: str
    column_letter: str
    value_type: str = "text"
    required: bool = False


class WorkbookTableSchema(BaseModel):
    table_region_id: str
    semantic_unit_id: str
    sheet_name: str
    header_row: int
    first_data_row: int
    last_template_row: int
    style_source_row: int
    min_output_rows: int = 1
    max_output_rows: int
    columns: list[WorkbookTableColumnSchema]
    expected_value_hashes: dict[str, str | None]
    allow_example_region_replacement: bool = True
```

Model validators enforce unique columns, positive ordered rows, bounded output,
and a rectangular baseline hash inventory.

- [ ] **Step 5: Implement deterministic table compilation**

`compile_template_tables` must:

1. assign provider-independent unique scalar identities;
2. group `repeating_table` targets by sheet and adjacent/overlapping row spans;
3. derive columns from cell coordinates;
4. merge fragments into one rectangular table;
5. reject formula, protected, scalar/table overlap, missing-column, and
   incompatible-geometry cases using stable diagnostic codes;
6. return scalar suggestions, compiled tables, and diagnostics without mutating
   uploaded bytes.

- [ ] **Step 6: Run compiler and model tests**

Run: `uv run pytest -q tests/test_template_table_compiler.py tests/test_template_activation.py`

Expected: all pass.

---

### Task 2: Global LLM identities and one-pass automatic repair

**Files:**
- Modify: `src/document_authoring/template_suggester.py`
- Modify: `src/document_authoring/template_progress.py`
- Test: `tests/test_template_suggester_safety.py`
- Test: `tests/test_template_suggester.py`

**Interfaces:**
- Consumes: `compile_template_tables`.
- Produces: compiled tables on `TemplateAnalysis`.
- Produces: `_repair_suggestions(...) -> list[TemplateAnalysisSuggestion]`.

- [ ] **Step 1: Write failing cross-batch identity and repair tests**

```python
def test_provider_namespaces_batch_local_semantic_ids_before_compilation():
    analysis = multi_batch_analysis()
    LLMTemplateSuggestionProvider(client_with_repeated_su_ids, max_request_chars=900).suggest(analysis)
    ids = [item.semantic_unit_id for item in analysis.suggestions]
    assert len(ids) == len(set(ids))


def test_provider_repairs_one_repairable_compilation_error():
    provider.suggest(analysis)
    assert client.call_count == 2
    assert analysis.compilation_diagnostics == []
    assert len(analysis.compiled_tables) == 1
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_template_suggester_safety.py tests/test_template_suggester.py`

Expected: repeated IDs remain and no repair request occurs.

- [ ] **Step 3: Namespace batch-local IDs**

After each batch is parsed, replace its local ID with a provider-owned identity:

```python
suggestion.model_copy(update={
    "semantic_unit_id": f"batch_{batch_index}_{slug(suggestion.semantic_unit_id)}"
})
```

The final compiler later derives stable geometry-based IDs. External providers
that bypass `LLMTemplateSuggestionProvider` retain existing duplicate-ID
fail-closed behavior.

- [ ] **Step 4: Compile and repair once**

After normalization:

```python
compilation = compile_template_tables(candidate)
if compilation.repairable_diagnostics:
    repaired = self._repair_suggestions(candidate, suggestions, compilation)
    compilation = compile_template_tables(
        candidate.model_copy(update={"suggestions": repaired})
    )
analysis.suggestions = compilation.scalar_suggestions
analysis.compiled_tables = compilation.tables
analysis.compilation_diagnostics = compilation.diagnostics
```

The repair prompt includes only safe unit payloads, prior suggestions, and
stable diagnostic codes. Exactly one repair request is allowed.

- [ ] **Step 5: Add progress events**

Add `llm_repair_started`, `llm_repair_completed`, and `llm_repair_failed` with
safe counts and error types; update the Streamlit label mapping without
displaying prompt content.

- [ ] **Step 6: Run suggestion tests**

Run: `uv run pytest -q tests/test_template_suggester.py tests/test_template_suggester_safety.py tests/test_document_generation_page.py`

Expected: all pass.

---

### Task 3: Automatic activation and table-schema persistence

**Files:**
- Modify: `src/document_authoring/template_activation.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/models.py`
- Test: `tests/test_template_activation.py`
- Test: `tests/test_template_activation_service.py`
- Create: `tests/test_workbook_table_schema_store.py`

**Interfaces:**
- Produces: `save_workbook_table_schemas(...)` and `list_workbook_table_schemas(...)`.
- Extends: `activate_template_analysis(..., table_schemas=[])`.
- Consumes: `TemplateAnalysis.compiled_tables`.

- [ ] **Step 1: Write failing activation and persistence tests**

```python
def test_compiled_table_resolves_cell_level_review_reasons():
    decision = decide_template_activation(compiled_icd_analysis())
    assert decision.status == "auto_accepted"
    assert decision.reason_codes == []


def test_table_schemas_round_trip_with_template_schema_version(tmp_path):
    store.save_workbook_table_schemas("schema", "1", [table])
    assert store.list_workbook_table_schemas("schema", "1") == [table]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_template_activation.py tests/test_template_activation_service.py tests/test_workbook_table_schema_store.py`

Expected: compiled table still triggers repeating/layout/destructive reasons and
the store methods do not exist.

- [ ] **Step 3: Add SQLite storage**

Create an idempotent `workbook_table_schemas` table:

```sql
CREATE TABLE IF NOT EXISTS workbook_table_schemas (
    table_region_id TEXT NOT NULL,
    template_schema_id TEXT NOT NULL,
    template_schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(template_schema_id, template_schema_version, table_region_id)
);
```

Add transactional save/list methods and include table schemas in atomic template
activation.

- [ ] **Step 4: Compile activation artifacts**

Extend `_regions_and_bindings` to return:

```python
tuple[
    list[WorkbookRegionSchema] | list[DocxRegionSchema],
    list[WorkbookTableSchema],
    list[TemplateUnitBinding],
]
```

Each compiled table becomes one `DocumentFieldSchema` with
`value_type="table_rows"` and one binding whose target ID is the table region
ID. Baseline hashes come from immutable analysis units.

- [ ] **Step 5: Make risk policy table-aware**

Scalar checks continue unchanged. Compiled table targets:

- may contain blank body cells;
- may replace their frozen example region;
- count as one logical target in the destructive ratio;
- remain rejected for formulas, unsupported merges, overlaps, or missing
  geometry.

Uncompiled `repeating_table` suggestions still produce
`repeating_table_requires_schema`.

- [ ] **Step 6: Run activation and store tests**

Run: `uv run pytest -q tests/test_template_activation.py tests/test_template_activation_service.py tests/test_workbook_table_schema_store.py tests/test_template_upload_service.py`

Expected: all pass.

---

### Task 4: Structured table drafting and fill-plan compilation

**Files:**
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/writers/provider.py`
- Modify: `src/document_authoring/writers/managed.py`
- Modify: `src/document_authoring/validator.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_managed_writer_table_rows.py`
- Create: `tests/test_document_table_fill_plan.py`

**Interfaces:**
- Produces: `WorkbookTableFill`.
- Extends: `WorkbookFillPlan.table_fills`.
- Consumes: `WorkbookTableSchema` and `DocumentUnitDraft.proposed_value`.

- [ ] **Step 1: Write failing structured draft tests**

```python
def test_table_writer_accepts_strict_row_array():
    draft = writer._parse_and_validate(response, table_request)
    assert draft.proposed_value == [{"pin": "X302-1", "signal": "NC"}]


def test_fill_plan_uses_missing_evidence_row_instead_of_legacy_examples():
    plan = service._semantic_fills(template, [missing_table_draft], statuses, bindings)
    assert plan.table_fills[0].rows == [{"pin": "未找到依据", "signal": "未找到依据"}]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest -q tests/test_managed_writer_table_rows.py tests/test_document_table_fill_plan.py`

Expected: table row arrays are not validated or represented in fill plans.

- [ ] **Step 3: Add table fill models**

```python
class WorkbookTableFill(BaseModel):
    table_region_id: str
    semantic_unit_id: str
    rows: list[dict[str, str]]


class WorkbookFillPlan(BaseModel):
    template_version_id: str
    fills: list[WorkbookFill] = Field(default_factory=list)
    table_fills: list[WorkbookTableFill] = Field(default_factory=list)
```

- [ ] **Step 4: Add strict table writer parsing**

Extend `WriterRequest` with backward-compatible `value_type: str = "text"` and
`value_schema: dict[str, Any] = Field(default_factory=dict)`. When
`WriterRequest.value_type == "table_rows"`, the prompt includes ordered
column IDs and requires `proposed_value` to be a JSON array. Validation rejects
unknown/missing columns, nested values, formula-like text, and excessive rows.
The deterministic fallback returns one missing-evidence row instead of copying
paragraphs into every cell.

- [ ] **Step 5: Compile table drafts into fill plans**

Resolve a table binding against `WorkbookTableSchema`; normalize missing
evidence to `未找到依据`; keep scalar fills unchanged; reject a table draft bound
to a scalar region or vice versa.

- [ ] **Step 6: Run writer, validator, and fill-plan tests**

Run: `uv run pytest -q tests/test_managed_writer_table_rows.py tests/test_document_table_fill_plan.py tests/test_document_authoring_p2a.py`

Expected: all pass.

---

### Task 5: Controlled OOXML table renderer

**Files:**
- Modify: `src/document_authoring/renderers/xlsm.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_workbook_table_renderer.py`
- Modify: `tests/test_template_upload_service.py`

**Interfaces:**
- Extends: `XlsmRenderer.render(..., table_schemas=[])`.
- Consumes: `WorkbookFillPlan.table_fills`.
- Produces: cell and row-operation entries in the integrity manifest.

- [ ] **Step 1: Write failing fixed-capacity rendering tests**

```python
def test_renderer_replaces_registered_example_rows_and_clears_surplus():
    result = renderer.render(template, [], plan, policy, table_schemas=[schema])
    values = workbook_values(result.content)
    assert values["A16"] == "X302-20"
    assert values["B16"] == "CAN3"
    assert values["A17"] is None
    assert unchanged_parts_except_sheet(result.content, template)
```

- [ ] **Step 2: Write failing bounded row-expansion tests**

```python
def test_renderer_clones_style_row_for_safe_expansion():
    result = renderer.render(template, [], three_row_plan, policy, table_schemas=[two_row_schema])
    assert style_id(result.content, "A18") == style_id(template, "A17")
    assert result.integrity_manifest["table_row_operations"] == [{
        "table_region_id": schema.table_region_id,
        "operation": "expand",
        "from_rows": 2,
        "to_rows": 3,
    }]
```

Also test rejection for formulas, merged cells, output above maximum, duplicate
table fills, baseline changes, and formula-like generated values.

- [ ] **Step 3: Run renderer tests and verify RED**

Run: `uv run pytest -q tests/test_workbook_table_renderer.py`

Expected: renderer has no table schema/fill support.

- [ ] **Step 4: Implement fixed-capacity table replacement**

Validate schemas and frozen hashes, clear the entire registered example
rectangle, then write row objects by column ID. Reuse `_patch_worksheet` and
existing inline-string safety. Record every changed cell.

- [ ] **Step 5: Implement safe bounded expansion**

Clone only the registered style source row. Rewrite row and cell references
inside `sheetData`, update worksheet dimension, and reject expansion when the
affected worksheet contains unsupported formulas, merged ranges crossing the
table boundary, drawings, validations, conditional formatting, or table
relationships that require reference rewrites.

- [ ] **Step 6: Extend integrity verification**

Add `table_row_operations`; allow only the registered worksheet parts; preserve
all unknown package members byte-for-byte.

- [ ] **Step 7: Run renderer and upload regression tests**

Run: `uv run pytest -q tests/test_workbook_table_renderer.py tests/test_template_upload_service.py tests/test_document_authoring_p2a.py`

Expected: all pass.

---

### Task 6: ICD end-to-end automatic activation and generation

**Files:**
- Modify: `tests/test_icd_template_regression.py`
- Modify: `tests/test_document_auto_generation.py`
- Modify: `src/ui/document_generation_page.py`
- Modify: `docs/superpowers/specs/2026-07-28-automatic-repeating-table-generation-design.md` only if implementation reveals a clarified constraint

**Interfaces:**
- Exercises the full upload → analyze → compile → activate → draft → render flow.

- [ ] **Step 1: Write failing real-template regression**

```python
def test_icd_template_compiles_and_activates_without_human_review():
    template = service.analyze_and_activate_uploaded_template(
        ctx,
        filename="icd_example.xlsx",
        content=ICD_TEMPLATE.read_bytes(),
        template_name="ICD",
    )
    analysis = store.get_template_analysis(template.template_version_id)
    assert analysis.status == "ready_for_confirmation"
    assert analysis.activation_decision.reason_codes == []
    assert len(analysis.compiled_tables) == 1
```

Add artifact assertions that fixed headings are unchanged, scalar samples and
pin-table examples are replaced, unused legacy rows are cleared, and missing
evidence produces `未找到依据`.

- [ ] **Step 2: Run ICD tests and verify RED**

Run: `uv run pytest -q tests/test_icd_template_regression.py tests/test_document_auto_generation.py`

Expected: current analysis returns the five review reasons.

- [ ] **Step 3: Remove mapping-review UI from the ordinary compiled path**

Display audit diagnostics and automatic repair status. A successfully compiled
template activates immediately. Non-repairable technical layouts show an error
with analysis ID but no mapping editor or overwrite checkboxes.

- [ ] **Step 4: Run focused feature regression**

Run:

```bash
uv run pytest -q \
  tests/test_template_table_compiler.py \
  tests/test_template_suggester_safety.py \
  tests/test_template_activation.py \
  tests/test_template_activation_service.py \
  tests/test_workbook_table_schema_store.py \
  tests/test_document_table_fill_plan.py \
  tests/test_workbook_table_renderer.py \
  tests/test_icd_template_regression.py \
  tests/test_document_auto_generation.py \
  tests/test_document_generation_review_ui.py
```

Expected: all pass.

- [ ] **Step 5: Run static verification**

Run:

```bash
uv run ruff check \
  src/document_authoring/template_analysis.py \
  src/document_authoring/template_table_compiler.py \
  src/document_authoring/template_suggester.py \
  src/document_authoring/template_activation.py \
  src/document_authoring/models.py \
  src/document_authoring/work_order_store.py \
  src/document_authoring/service.py \
  src/document_authoring/writers/provider.py \
  src/document_authoring/writers/managed.py \
  src/document_authoring/validator.py \
  src/document_authoring/renderers/xlsm.py \
  src/ui/document_generation_page.py \
  tests/test_template_table_compiler.py \
  tests/test_workbook_table_schema_store.py \
  tests/test_document_table_fill_plan.py \
  tests/test_workbook_table_renderer.py
```

Expected: `All checks passed!`

Run: `git diff --check`

Expected: no output, exit 0.

- [ ] **Step 6: Run the full regression suite**

Run: `uv run pytest -q`

Expected: zero failures.
