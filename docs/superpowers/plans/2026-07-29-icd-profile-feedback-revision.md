# ICD Profile and Feedback Revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably recognize formal ICD templates, generate them from frozen circuit facts, and turn feedback into bounded revisions that cannot bypass release checks.

**Architecture:** Add a pure template-profile classifier and use it at work-order execution boundaries. ICD work orders retain the existing frozen scope decision and deterministic front-view overlay, but gain formal-template and header-slot gates. Feedback creates a bound revision request; the continuation path consumes only the original frozen sources and rejects approval until the request is resolved.

**Tech Stack:** Python 3.12, Pydantic, openpyxl-backed XLSX parser, pytest, Streamlit.

## Global Constraints

- No project name, RefDes, pin count, or workbook coordinate may be hard-coded.
- EDF/EDIF is the authority for ICD pins; requirements/FPT/HSI are the authority for approved semantic fields.
- Datasheet evidence is optional and cannot prove a project connector mapping.
- Generic document generation behavior must remain unchanged.
- Work only with frozen source snapshots; new source material requires a new revision/work order.
- Use test-driven development: run every new test red before implementation and green after.

---

### Task 1: Add a data-driven ICD template-profile classifier

**Files:**
- Create: `src/document_authoring/icd_profile.py`
- Create: `tests/test_icd_profile.py`
- Modify: `src/document_authoring/icd_generation.py`

**Interfaces:**
- Consumes: template bytes and format.
- Produces: `IcdTemplateProfile(kind, reasons, connector_blocks, issues)` where `kind` is `generic`, `icd`, or `icd_sample`.
- `connector_refdes_from_front_view_template(content)` remains the parser for actual front-view slot identifiers.

- [ ] **Step 1: Write the failing classifier tests**

```python
def test_formal_icd_requires_identity_connector_and_pin_definition_contract():
    profile = classify_icd_template(_formal_icd_bytes(), "xlsx")
    assert profile.kind == "icd"
    assert profile.connector_blocks

def test_example_only_workbook_is_not_releasable_icd_template():
    profile = classify_icd_template(_example_only_bytes(), "xlsx")
    assert profile.kind == "icd_sample"
    assert "formal_connector_block_missing" in _codes(profile.issues)

def test_normal_spreadsheet_remains_generic():
    assert classify_icd_template(_generic_bytes(), "xlsx").kind == "generic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_icd_profile.py`

Expected: FAIL because `src.document_authoring.icd_profile` does not exist.

- [ ] **Step 3: Implement the pure classifier**

```python
@dataclass(frozen=True)
class IcdTemplateProfile:
    kind: Literal["generic", "icd", "icd_sample"]
    reasons: list[str]
    connector_blocks: list[IcdConnectorBlock]
    issues: list[dict[str, str]]

def classify_icd_template(content: bytes, target_format: str) -> IcdTemplateProfile:
    """Classify only from template labels and geometry; never from project data."""
```

Parse only XLSX/XLSM. Exclude sheets named as examples, identify a pin table by bilingual Pin Number/Pin Definition headers, identify the nearest Location Number and board connector model labels, and return `icd_sample` when ICD labels exist but no formal connector block remains. Reuse the front-view slot parser rather than duplicate connector parsing.

- [ ] **Step 4: Run classifier tests to verify they pass**

Run: `uv run python -m pytest -q tests/test_icd_profile.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/icd_profile.py src/document_authoring/icd_generation.py tests/test_icd_profile.py
git commit -m "feat: classify formal ICD templates"
```

### Task 2: Gate ICD work orders on the formal profile and frozen scope

**Files:**
- Modify: `src/core/app_pipeline.py`
- Modify: `src/document_authoring/service.py`
- Modify: `tests/test_icd_scope_pipeline.py`
- Modify: `tests/test_icd_login_flow_regression.py`

**Interfaces:**
- Consumes: `IcdTemplateProfile`, `DocumentWorkOrder`, frozen `IcdScopeReview`.
- Produces: `scope_review_required` for actionable source gaps, and a blocking validation issue for `icd_sample` templates.

- [ ] **Step 1: Write failing integration tests**

```python
def test_icd_sample_template_returns_a_template_contract_stop(pipeline, ctx):
    result = pipeline.auto_generate_knowledge_base_document(
        ctx, knowledge_base_name="hardware", template_version_id="example-template", ...
    )
    assert result["stage"] == "template_contract_review_required"
    assert result["issues"][0]["code"] == "icd_formal_template_required"

def test_formal_icd_retrieves_only_profile_connector_refdes(pipeline, ctx):
    pipeline.auto_generate_knowledge_base_document(ctx, knowledge_base_name="hardware", ...)
    pipeline.circuit_service.list_pin_mapping_evidence.assert_called_once_with(
        "hardware", ANY, ctx, refdes=["J1", "J2"]
    )
```

- [ ] **Step 2: Run the integration tests to verify they fail**

Run: `uv run python -m pytest -q tests/test_icd_scope_pipeline.py tests/test_icd_login_flow_regression.py`

Expected: FAIL because template profile is not evaluated at work-order creation.

- [ ] **Step 3: Enforce the profile before retrieval and rendering**

```python
profile = classify_icd_template(template_content, template.format)
if profile.kind == "icd_sample":
    return _template_contract_stop(order.work_order_id, profile.issues)
if profile.kind == "icd":
    connector_refdes = _profile_and_evidence_connector_refdes(profile, supporting_evidences)
```

The pipeline must use Profile connector blocks before broad evidence candidates, keep the frozen-source filter, and preserve `generic` behavior. The service must append a blocking `icd_formal_template_required` issue if an invalid ICD template reaches finalization by another entry point.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `uv run python -m pytest -q tests/test_icd_profile.py tests/test_icd_scope_pipeline.py tests/test_icd_login_flow_regression.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core/app_pipeline.py src/document_authoring/service.py tests/test_icd_scope_pipeline.py tests/test_icd_login_flow_regression.py
git commit -m "fix: gate ICD generation on formal template profile"
```

### Task 3: Validate ICD header slots and their evidence before release

**Files:**
- Create: `src/document_authoring/icd_field_validation.py`
- Modify: `src/document_authoring/service.py`
- Modify: `tests/test_icd_validation.py`
- Create: `tests/test_icd_field_validation.py`

**Interfaces:**
- Consumes: rendered workbook bytes, template profile, final evidence matrix, and frozen scope review.
- Produces: validation issues `icd_location_invalid`, `icd_required_header_missing`, and `icd_header_untraceable`.

- [ ] **Step 1: Write failing validation tests**

```python
def test_location_slot_rejects_retrieval_paragraph():
    issues = validate_icd_header_slots(_workbook_with_location("MCU Component: long text"), _profile())
    assert issues == [{"code": "icd_location_invalid", "severity": "blocking", "field": "location_number"}]

def test_missing_board_connector_model_is_blocking():
    assert "icd_required_header_missing" in _codes(validate_icd_header_slots(_missing_model(), _profile()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_icd_field_validation.py`

Expected: FAIL because header validation does not exist.

- [ ] **Step 3: Implement header and provenance checks**

```python
def validate_icd_header_slots(content: bytes, profile: IcdTemplateProfile, evidence_matrix: Iterable[Any]) -> list[dict[str, str]]:
    """Validate required profile slots without writing or inferring values."""
```

Allow Location Number only when it matches a token-like connector identifier and is represented in the frozen mapping. Require every profile-declared board/harness/product slot to be non-empty and require its evidence matrix row to cite an allowed source role. Append issues in `_finalize_internal_harness_result` and reuse `_has_icd_blocking_issue` for approval.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `uv run python -m pytest -q tests/test_icd_field_validation.py tests/test_icd_validation.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/icd_field_validation.py src/document_authoring/service.py tests/test_icd_field_validation.py tests/test_icd_validation.py
git commit -m "fix: validate ICD header slots and sources"
```

### Task 4: Turn feedback into a bounded, source-frozen revision request

**Files:**
- Modify: `src/document_authoring/models.py`
- Modify: `src/document_authoring/work_order_store.py`
- Modify: `src/document_authoring/service.py`
- Modify: `src/core/app_pipeline.py`
- Modify: `src/ui/document_generation_page.py`
- Modify: `tests/test_document_generation_feedback.py`
- Create: `tests/test_document_feedback_revision.py`

**Interfaces:**
- Consumes: candidate artifact hash, feedback comment, category, optional field identifier.
- Produces: persisted `DocumentRevisionRequest` with `open`, `rendered`, or `blocked` state and `revision_count <= 2`.

- [ ] **Step 1: Write failing revision tests**

```python
def test_feedback_opens_a_bound_revision_and_blocks_approval(service, ctx):
    request = service.submit_document_feedback(ctx, "candidate-1", comment="补充板端型号", category="missing_evidence")
    assert request.status == "open"
    with pytest.raises(ValueError, match="open feedback revision"):
        service.approve_document_artifact(ctx, "candidate-1")

def test_third_feedback_revision_becomes_a_manual_todo(service, ctx):
    service._save_two_revisions("candidate-1")
    request = service.submit_document_feedback(ctx, "candidate-1", comment="仍不正确", category="format")
    assert request.status == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest -q tests/test_document_feedback_revision.py tests/test_document_generation_feedback.py`

Expected: FAIL because feedback returns only `DocumentHumanEvent`.

- [ ] **Step 3: Persist and continue bounded revisions**

```python
class DocumentRevisionRequest(BaseModel):
    revision_request_id: str
    artifact_id: str
    work_order_id: str
    source_snapshot_hash: str
    category: Literal["missing_evidence", "format", "semantic", "layout"]
    status: Literal["open", "rendered", "blocked"] = "open"
    revision_count: int

def continue_document_feedback_revision(self, ctx, revision_request_id: str) -> DocumentArtifact:
    """Re-run the original work order with its frozen snapshot and bounded feedback context."""
```

`submit_document_feedback` must still save the immutable human event, then create the revision request. The pipeline continuation uses its existing frozen retriever and the request context only for affected units; it must not expand sources. Approval rejects an artifact with an open request. The UI lets the user choose one of four categories with concise help and exposes “生成修订候选” only for open requests.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `uv run python -m pytest -q tests/test_document_feedback_revision.py tests/test_document_generation_feedback.py tests/test_document_generation_page.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/document_authoring/models.py src/document_authoring/work_order_store.py src/document_authoring/service.py src/core/app_pipeline.py src/ui/document_generation_page.py tests/test_document_generation_feedback.py tests/test_document_feedback_revision.py tests/test_document_generation_page.py
git commit -m "feat: create bounded revisions from document feedback"
```

### Task 5: Prove the ICD release gate with comparison and regression tests

**Files:**
- Modify: `tests/test_icd_artifact_comparison.py`
- Modify: `tests/test_icd_template_regression.py`
- Modify: `tests/test_icd_login_flow_regression.py`
- Modify: `docs/superpowers/specs/2026-07-29-icd-profile-feedback-revision-design.md`

**Interfaces:**
- Consumes: the approved/manual ICD fixture and generated formal-ICD fixture.
- Produces: 100% pin matching/coverage for the controlled fixture, plus explicit rejection of the X302 example artifact.

- [ ] **Step 1: Write failing acceptance tests**

```python
def test_formal_icd_fixture_matches_reference_pin_set():
    result = compare_workbooks(_reference(), _generated_from_frozen_scope())
    assert result["summary"]["exact_match_rate"] == 1.0
    assert result["summary"]["reference_coverage"] == 1.0

def test_x302_example_artifact_is_not_releasable_against_formal_icd_contract():
    assert "icd_formal_template_required" in _codes(_report_for_example_artifact())
```

- [ ] **Step 2: Run acceptance tests to verify they fail**

Run: `uv run python -m pytest -q tests/test_icd_artifact_comparison.py tests/test_icd_template_regression.py tests/test_icd_login_flow_regression.py`

Expected: FAIL until the generated fixture uses the formal profile and frozen mapping.

- [ ] **Step 3: Add portable fixture construction and regression assertions**

Use the existing static ICD payload/EDF evidence fixtures, not developer-local storage or production output files. Assert connector identities, board/harness models, Pin Definition values, and front-view slots in addition to the pin comparison summary. Update the design document only if a tested interface differs from this plan.

- [ ] **Step 4: Run full relevant verification**

Run:

```bash
uv run python -m pytest -q \
  tests/test_icd_profile.py \
  tests/test_icd_scope_decision.py \
  tests/test_icd_scope_review_service.py \
  tests/test_icd_scope_pipeline.py \
  tests/test_icd_validation.py \
  tests/test_icd_field_validation.py \
  tests/test_icd_front_view.py \
  tests/test_icd_login_flow_regression.py \
  tests/test_icd_artifact_comparison.py \
  tests/test_icd_template_regression.py \
  tests/test_document_generation_feedback.py \
  tests/test_document_feedback_revision.py \
  tests/test_document_generation_page.py
uv run ruff check src/core/app_pipeline.py src/document_authoring tests/test_icd_profile.py tests/test_icd_field_validation.py tests/test_document_feedback_revision.py
```

Expected: all tests pass and Ruff reports no violations.

- [ ] **Step 5: Commit**

```bash
git add tests/test_icd_artifact_comparison.py tests/test_icd_template_regression.py tests/test_icd_login_flow_regression.py docs/superpowers/specs/2026-07-29-icd-profile-feedback-revision-design.md
git commit -m "test: prove governed ICD generation and revisions"
```
