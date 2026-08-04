# Safe Automatic Template Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically activate and render ordinary spreadsheet templates with explicit scalar placeholders, while preserving ambiguous templates for actionable human correction and preventing destructive scalar fan-out or fixed-content overwrites.

**Architecture:** Extend the immutable template analysis into a content-aware workbook inventory, then pass every AI proposal through a deterministic activation policy. Persist the decision with the analysis, freeze only safe mappings, and independently enforce scalar cardinality and baseline cell-value guards during binding and rendering.

**Tech Stack:** Python 3.11+, Pydantic v2, OOXML/ZIP/XML (`zipfile`, `xml.etree.ElementTree`), SQLite JSON persistence, pytest, Streamlit.

## Global Constraints

- Preserve existing DOCX behavior.
- Do not replace the existing deterministic authoring workflow with LangGraph or DeepAgents.
- Normal templates are defined conservatively as templates with explicit, unambiguous scalar placeholders.
- Scalar mappings have exactly one target.
- Repeating-table proposals remain `requires_human` until an explicit table schema exists.
- Fixed labels, formulas, hidden/protected cells, merged non-anchor cells, and unmarked layout blanks cannot auto-activate.
- Existing unsafe persisted mappings must be rejected at generation or rendering time.
- Template and correction decisions remain bound to the immutable sanitized content hash.
- Preserve all unrelated dirty-worktree changes.

---

## File Structure

- `src/document_authoring/template_analysis.py`: persisted content-aware units, suggestion shape, risk metrics, activation decision, and correction contracts.
- `src/document_authoring/template_analyzers.py`: safe OOXML value/style/neighborhood extraction and conservative role hints.
- `src/document_authoring/template_activation.py`: pure deterministic automatic-activation policy and reason codes.
- `src/document_authoring/template_suggester.py`: content-aware prompt payload and strict `value_shape` response parsing.
- `src/document_authoring/service.py`: policy invocation, automatic activation gate, correction workflow, scalar binding guard, and baseline region creation.
- `src/document_authoring/work_order_store.py`: immutable analysis revision history while retaining the current-analysis lookup.
- `src/document_authoring/models.py`: workbook region baseline and overwrite authorization.
- `src/document_authoring/renderers/xlsm.py`: baseline verification, duplicate target/value guard, and exact cell-change manifest.
- `src/ui/document_generation_page.py`: distinguish automatic acceptance from human-review routing and expose review reasons.
- `tests/test_template_analyzers.py`: content-aware OOXML inventory tests.
- `tests/test_template_activation.py`: deterministic policy unit tests.
- `tests/test_template_suggester.py`: prompt and response contract tests.
- `tests/test_template_upload_service.py`: auto-activation and correction lifecycle tests.
- `tests/test_document_authoring_safety.py`: binding and fill-plan defense-in-depth tests.
- `tests/test_xlsm_renderer_safety.py`: baseline, overwrite, duplicate-value, and manifest tests.
- `tests/test_icd_template_regression.py`: real-template regression against the 241-cell corruption.

---

### Task 1: Content-Aware Workbook Inventory

**Files:**
- Modify: `src/document_authoring/template_analysis.py`
- Modify: `src/document_authoring/template_analyzers.py`
- Test: `tests/test_template_analyzers.py`

**Interfaces:**
- Produces: `TemplateNeighbor`, enriched `TemplateAnalysisUnit`, `value_shape` on `TemplateAnalysisSuggestion`.
- Consumes: sanitized OOXML bytes already passed to `analyze_template(content, format)`.

- [ ] **Step 1: Write failing analyzer tests**

Add tests that create inline-string, shared-string, numeric, formula, explicit `{{project_summary}}`, and styled-empty cells, then assert:

```python
assert units["sheet:Review!A1"].value_preview == "Project"
assert units["sheet:Review!A1"].value_kind == "text"
assert units["sheet:Review!A1"].structural_role_hint == "fixed_label"
assert units["sheet:Review!B1"].structural_role_hint == "placeholder"
assert units["sheet:Review!C1"].structural_role_hint == "layout_blank"
assert units["sheet:Review!D1"].value_kind == "formula"
assert units["sheet:Review!D1"].writable is False
assert units["sheet:Review!B1"].neighborhood
```

- [ ] **Step 2: Run the analyzer tests and verify RED**

Run:

```bash
pytest -q tests/test_template_analyzers.py
```

Expected: failures report missing `value_preview`, `value_kind`, `structural_role_hint`, or `neighborhood`.

- [ ] **Step 3: Add backward-compatible analysis contracts**

Add Pydantic fields with defaults:

```python
class TemplateNeighbor(BaseModel):
    relative_row: int
    relative_column: int
    value_preview: str


class TemplateAnalysisUnit(BaseModel):
    unit_id: str
    locator: dict[str, Any]
    label: str = ""
    writable: bool = False
    blocked_reason: str | None = None
    value_preview: str | None = None
    value_kind: Literal["blank", "text", "number", "boolean", "formula", "error"] = "blank"
    style_fingerprint: str = ""
    neighborhood: list[TemplateNeighbor] = Field(default_factory=list)
    structural_role_hint: Literal[
        "unknown", "fixed_label", "placeholder", "value",
        "table_header", "table_body", "layout_blank",
    ] = "unknown"


class TemplateAnalysisSuggestion(BaseModel):
    ...
    value_shape: Literal["scalar", "repeating_table"] = "scalar"
```

- [ ] **Step 4: Extract bounded workbook semantics**

Implement shared-string parsing, inline-string parsing, typed values, a stable style fingerprint, two-cell-radius non-empty neighbors, and conservative role hints. Use `{{name}}`, `${name}`, and `<<name>>` as explicit placeholder syntax. Formula text is never copied to `value_preview`.

- [ ] **Step 5: Run analyzer and contract tests and verify GREEN**

Run:

```bash
pytest -q tests/test_template_analyzers.py tests/test_template_analysis_contracts.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/document_authoring/template_analysis.py src/document_authoring/template_analyzers.py tests/test_template_analyzers.py
git commit -m "feat: build content-aware workbook inventory"
```

---

### Task 2: Deterministic Automatic-Activation Policy

**Files:**
- Create: `src/document_authoring/template_activation.py`
- Modify: `src/document_authoring/template_analysis.py`
- Test: `tests/test_template_activation.py`

**Interfaces:**
- Consumes: `TemplateAnalysis`.
- Produces: `decide_template_activation(analysis, policy=None) -> TemplateActivationDecision`.
- Produces: `TemplateRiskMetrics`, `TemplateActivationDecision`, and stable reason codes stored on `TemplateAnalysis.activation_decision`.

- [ ] **Step 1: Write failing policy tests**

Cover these exact cases:

```python
assert decide_template_activation(explicit_scalar).status == "auto_accepted"
assert "scalar_target_fanout" in decide_template_activation(multitarget_scalar).reason_codes
assert "layout_blank_target" in decide_template_activation(layout_blank).reason_codes
assert "nonempty_target_not_placeholder" in decide_template_activation(fixed_label).reason_codes
assert "repeating_table_requires_schema" in decide_template_activation(table_shape).reason_codes
assert "low_mapping_confidence" in decide_template_activation(low_confidence).reason_codes
assert "missing_semantic_context" in decide_template_activation(coordinate_only).reason_codes
```

- [ ] **Step 2: Run policy tests and verify RED**

Run:

```bash
pytest -q tests/test_template_activation.py
```

Expected: import failure for `src.document_authoring.template_activation`.

- [ ] **Step 3: Implement the pure policy**

Use a frozen policy value object:

```python
@dataclass(frozen=True)
class TemplateActivationPolicy:
    min_mapping_confidence: float = 0.90
    max_target_ratio: float = 0.20
    max_nonempty_overwrite_ratio: float = 0.0


def decide_template_activation(
    analysis: TemplateAnalysis,
    policy: TemplateActivationPolicy | None = None,
) -> TemplateActivationDecision:
    ...
```

Deduplicate reason codes in deterministic order and compute target, non-empty overwrite, and confidence metrics. Apply strict workbook rules and preserve current DOCX activation semantics.

- [ ] **Step 4: Run policy tests and verify GREEN**

Run:

```bash
pytest -q tests/test_template_activation.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/template_activation.py src/document_authoring/template_analysis.py tests/test_template_activation.py
git commit -m "feat: gate template activation with deterministic policy"
```

---

### Task 3: Content-Aware AI Suggestion Contract

**Files:**
- Modify: `src/document_authoring/template_suggester.py`
- Test: `tests/test_template_suggester.py`

**Interfaces:**
- Consumes: enriched `TemplateAnalysisUnit`.
- Produces: strict suggestions with `value_shape`.

- [ ] **Step 1: Write failing prompt and parser tests**

Assert each request unit contains:

```python
{
    "unit_id",
    "label",
    "value_preview",
    "value_kind",
    "style_fingerprint",
    "structural_role_hint",
    "neighborhood",
}
```

Assert a response without `value_shape` remains backward compatible as `scalar`, while an explicit `repeating_table` value parses and unknown values fail.

- [ ] **Step 2: Run suggester tests and verify RED**

Run:

```bash
pytest -q tests/test_template_suggester.py
```

Expected: prompt inventory lacks semantic context or parser rejects the new shape contract.

- [ ] **Step 3: Update the constrained prompt and batching**

Tell the model to preserve fixed labels, emit one target for scalar fields, and use `repeating_table` for repeated structures. Send only bounded, non-executable unit metadata and no OOXML bytes or filesystem paths.

- [ ] **Step 4: Update strict parsing**

Accept the existing five fields plus optional `value_shape`; reject all other keys. Construct:

```python
TemplateAnalysisSuggestion(
    semantic_unit_id=semantic_unit_id,
    label=label,
    target_unit_ids=target_unit_ids,
    retrieval_terms=retrieval_terms,
    confidence=float(confidence),
    value_shape=value_shape,
)
```

- [ ] **Step 5: Run suggester tests and verify GREEN**

Run:

```bash
pytest -q tests/test_template_suggester.py
```

Expected: all selected tests pass, including batching and retry behavior.

- [ ] **Step 6: Commit only intended hunks**

Because this file contains pre-existing user changes, inspect and stage only the new semantic-context hunks:

```bash
git diff -- src/document_authoring/template_suggester.py tests/test_template_suggester.py
git add -p src/document_authoring/template_suggester.py tests/test_template_suggester.py
git commit -m "feat: constrain content-aware template suggestions"
```

---

### Task 4: Safe Activation and Human Correction Lifecycle

**Files:**
- Modify: `src/document_authoring/service.py`
- Modify: `src/document_authoring/work_order_store.py`
- Test: `tests/test_template_upload_service.py`

**Interfaces:**
- Consumes: `decide_template_activation`.
- Produces: `correct_template_analysis(ctx, *, correction: TemplateMappingCorrection) -> TemplateAnalysis`.
- Changes: `analyze_uploaded_template` persists the activation decision and sets `requires_human` for unsafe workbook mappings.

- [ ] **Step 1: Write failing lifecycle tests**

Add tests proving:

```python
analysis = service.analyze_uploaded_template(...explicit_placeholder_xlsx...)
assert analysis.status == "ready_for_confirmation"
assert analysis.activation_decision.status == "auto_accepted"

analysis = service.analyze_uploaded_template(...ambiguous_xlsx...)
assert analysis.status == "requires_human"
assert "scalar_target_fanout" in analysis.activation_decision.reason_codes

corrected = service.correct_template_analysis(ctx, correction=correction)
assert corrected.analysis_id != analysis.analysis_id
assert corrected.status == "ready_for_confirmation"
```

Also assert a correction with a different `expected_content_hash`, unknown target, duplicate target, or unlocked fixed-label overwrite is rejected.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run:

```bash
pytest -q tests/test_template_upload_service.py
```

Expected: missing activation decision or `correct_template_analysis`.

- [ ] **Step 3: Invoke and persist the policy**

After provider suggestions validate, call `decide_template_activation`, copy it into the analysis, and set workbook status to `ready_for_confirmation` only for `auto_accepted`.

- [ ] **Step 4: Implement immutable correction**

Load by `analysis_id`, verify tenant capability through the template access path, verify `expected_content_hash`, replace suggestions and allowed locked/overwrite metadata, re-run validation and activation policy, assign a new `analysis_id`, and save the revision through the existing content-hash-bound store.

- [ ] **Step 5: Preserve analysis revisions**

Add a content-hash-bound `template_analysis_revisions` table keyed by `analysis_id`. On every save, insert the immutable revision and update the existing per-template current-analysis row. Resolve `get_template_analysis_by_id` from the revision table, then run the same template/content/format integrity checks as the current-analysis lookup.

- [ ] **Step 6: Gate automatic activation**

Require `analysis.activation_decision.status == "auto_accepted"` for spreadsheet auto-activation. Keep `requires_human` as an auditable draft with reason codes.

- [ ] **Step 7: Run lifecycle tests and verify GREEN**

Run:

```bash
pytest -q tests/test_template_upload_service.py
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/document_authoring/service.py src/document_authoring/work_order_store.py tests/test_template_upload_service.py
git commit -m "feat: add safe activation and correction lifecycle"
```

---

### Task 5: Binding and Fill-Plan Cardinality Guards

**Files:**
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/service.py`
- Create: `tests/test_document_authoring_safety.py`

**Interfaces:**
- Adds: `WorkbookRegionSchema.expected_value_hash: str | None`.
- Adds: `WorkbookRegionSchema.allow_nonempty_overwrite: bool = False`.
- Enforces: one target region for each field/scalar binding.

- [ ] **Step 1: Write failing defense-in-depth tests**

Directly call `_regions_and_bindings` and `_semantic_fills` with unsafe persisted data:

```python
with pytest.raises(ValueError, match="scalar.*one target"):
    service._regions_and_bindings(template, unsafe_analysis)

with pytest.raises(ValueError, match="scalar.*one target"):
    service._semantic_fills(template, drafts, statuses, bindings)
```

Assert safe workbook regions store a baseline hash and overwrite permission derived from the confirmed unit.

- [ ] **Step 2: Run safety tests and verify RED**

Run:

```bash
pytest -q tests/test_document_authoring_safety.py
```

Expected: unsafe fan-out currently succeeds or creates multiple fills.

- [ ] **Step 3: Add baseline fields and scalar guards**

Reject `value_shape != "scalar"` in Phase 1 and reject every scalar suggestion or field binding whose target count is not exactly one. For workbook regions calculate the expected baseline hash from a canonical JSON representation of the analyzed value and set non-empty overwrite permission only for an explicit placeholder or reviewed correction.

- [ ] **Step 4: Run safety tests and verify GREEN**

Run:

```bash
pytest -q tests/test_document_authoring_safety.py tests/test_document_authoring_p2a.py
```

Expected: selected tests pass and existing direct workbook workflows remain compatible when legacy regions omit the optional baseline.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/models.py src/document_authoring/service.py tests/test_document_authoring_safety.py
git commit -m "fix: reject scalar fanout in document generation"
```

---

### Task 6: Renderer Baseline and Cell-Diff Guards

**Files:**
- Modify: `src/document_authoring/renderers/xlsm.py`
- Modify: `src/document_authoring/validator.py`
- Create: `tests/test_xlsm_renderer_safety.py`

**Interfaces:**
- Consumes: `expected_value_hash`, `allow_nonempty_overwrite`.
- Produces: `integrity_manifest["cell_changes"]`.

- [ ] **Step 1: Write failing renderer tests**

Cover:

```python
with pytest.raises(PermissionError, match="baseline"):
    renderer.render(changed_template, regions, plan, policy, security_approved=True)

with pytest.raises(PermissionError, match="non-empty"):
    renderer.render(nonempty_template, unconfirmed_regions, plan, policy, security_approved=True)

with pytest.raises(ValueError, match="duplicate"):
    renderer.render(template, regions, duplicate_region_plan, policy, security_approved=True)
```

Also assert a valid placeholder replacement returns one manifest entry with sheet, cell, baseline/generated hashes, semantic unit ID, and region ID.

- [ ] **Step 2: Run renderer safety tests and verify RED**

Run:

```bash
pytest -q tests/test_xlsm_renderer_safety.py
```

Expected: baseline and duplicate protections are absent.

- [ ] **Step 3: Validate each target before mutation**

Parse current worksheet values with the same canonical value reader used by analysis. Reject duplicate region IDs, duplicate sheet/cell locators, baseline mismatches, formula replacement, and unauthorized non-empty overwrite.

- [ ] **Step 4: Add the exact cell-change manifest**

Append entries shaped as:

```python
{
    "sheet_name": region.sheet_name,
    "cell": ref,
    "baseline_value_hash": baseline_hash,
    "generated_value_hash": content_hash(value),
    "baseline_empty": baseline_value is None,
    "semantic_unit_id": fill.semantic_unit_id,
    "region_id": region.region_id,
}
```

Include `cell_changes` when computing the manifest hash.

- [ ] **Step 5: Validate cell-change issues independently**

Teach `DocumentValidator.validate` to turn manifest entries marked with baseline mismatch, unauthorized overwrite, unexpected target, scalar fan-out, or duplicate long-value violations into `renderer_integrity` issues. This keeps artifact approval fail-closed even when a custom renderer supplies the manifest.

- [ ] **Step 6: Run renderer tests and verify GREEN**

Run:

```bash
pytest -q tests/test_xlsm_renderer_safety.py tests/test_document_authoring_p2a.py
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit only intended hunks**

This file contains pre-existing worksheet namespace-preservation changes. Stage only the safety additions:

```bash
git diff -- src/document_authoring/renderers/xlsm.py src/document_authoring/validator.py tests/test_xlsm_renderer_safety.py
git add -p src/document_authoring/renderers/xlsm.py
git add src/document_authoring/validator.py tests/test_xlsm_renderer_safety.py
git commit -m "fix: enforce workbook render baselines"
```

---

### Task 7: Real ICD Regression and Review Status

**Files:**
- Create: `tests/test_icd_template_regression.py`
- Modify: `src/ui/document_generation_page.py`
- Test: `tests/test_document_generation_page.py`

**Interfaces:**
- Consumes: persisted `TemplateActivationDecision`.
- Produces: UI status containing the analysis ID and reason codes for human review.

- [ ] **Step 1: Write failing ICD regression**

Load `docs/ADAS/icd_example.xlsx`, feed the observed four suggestions with 7, 5, 5, and 224 targets, and assert:

```python
assert analysis.status == "requires_human"
assert "scalar_target_fanout" in analysis.activation_decision.reason_codes
assert store.list_templates()[0].status == "draft"
assert store.list_workbook_regions(template_schema_id, "1") == []
```

Add a renderer-level assertion that the same long scalar cannot be written to 224 cells.

- [ ] **Step 2: Write failing UI status test**

Assert an abnormal upload renders a review-required message containing its stable reason codes and analysis ID without calling confirmation.

- [ ] **Step 3: Run regression and UI tests and verify RED**

Run:

```bash
pytest -q tests/test_icd_template_regression.py tests/test_document_generation_page.py
```

Expected: destructive mapping is not policy-gated or review metadata is absent from the UI.

- [ ] **Step 4: Surface actionable review metadata**

Catch the existing automatic-activation review path, load the persisted analysis, and show `analysis_id`, `reason_codes`, affected targets, and original value previews. Do not expose source storage paths or unsanitized bytes.

- [ ] **Step 5: Run regression and UI tests and verify GREEN**

Run:

```bash
pytest -q tests/test_icd_template_regression.py tests/test_document_generation_page.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit only intended hunks**

```bash
git diff -- src/ui/document_generation_page.py tests/test_document_generation_page.py
git add -p src/ui/document_generation_page.py tests/test_document_generation_page.py
git add tests/test_icd_template_regression.py
git commit -m "fix: route unsafe workbook templates to review"
```

---

### Task 8: Full Compatibility and Acceptance Verification

**Files:**
- Modify only if a verified regression requires an in-scope compatibility fix.

**Interfaces:**
- Verifies every preceding interface together.

- [ ] **Step 1: Run focused safety suite**

```bash
pytest -q \
  tests/test_template_analyzers.py \
  tests/test_template_analysis_contracts.py \
  tests/test_template_activation.py \
  tests/test_template_suggester.py \
  tests/test_template_upload_service.py \
  tests/test_document_authoring_safety.py \
  tests/test_xlsm_renderer_safety.py \
  tests/test_icd_template_regression.py \
  tests/test_document_generation_page.py
```

Expected: zero failures.

- [ ] **Step 2: Run document-authoring regression suite**

```bash
pytest -q tests/test_document_authoring_p2a.py tests/test_docx_renderer.py tests/test_template_authoring_integration.py
```

Expected: zero failures.

- [ ] **Step 3: Run static checks available in the repository**

```bash
python -m compileall -q src tests
git diff --check
```

Expected: both commands exit with status 0.

- [ ] **Step 4: Prove the original failure is blocked**

Run the real ICD regression alone with verbose output:

```bash
pytest -vv tests/test_icd_template_regression.py
```

Expected: the 224-target scalar suggestion is rejected before region creation or rendering, and the template remains a reviewable draft.

- [ ] **Step 5: Inspect scope**

```bash
git status --short
git diff --stat HEAD~7..HEAD
```

Expected: no unrelated user-owned files are staged or committed by this implementation.

- [ ] **Step 6: Request code review and address findings**

Invoke the `requesting-code-review` skill with the confirmed specification, plan, commit range, and verification evidence. Fix any critical or important findings with a failing test first.

- [ ] **Step 7: Final verification**

Repeat Steps 1–4 after review fixes. Report exact pass/fail counts and any pre-existing unrelated dirty files.
