# Safe Automatic Template Authoring Design

## Goal

Make normal spreadsheet templates activate and generate documents automatically,
while routing ambiguous or structurally unsafe templates to a correctable human
review. The system must prevent the failure observed with `icd_example.xlsx`:
fixed template content was overwritten, styled blank cells were treated as
fields, and one scalar draft was copied into 224 cells.

## Scope

Delivery is split into two compatible phases.

### Phase 1: Safety and review

- Build a content-aware workbook inventory instead of exposing only cell
  coordinates.
- Classify automatic activation as `auto_accepted` or `requires_human` using
  deterministic policy after AI suggestions.
- Reject unsafe scalar fan-out, unauthorized non-empty overwrites, layout-only
  blank cells, and destructive change ratios.
- Persist review reasons and corrected mappings as versioned template schema
  data.
- Add artifact cell-diff validation before approval and release.
- Preserve existing DOCX, deterministic authoring, security sanitization,
  artifact hashing, and approval behavior.

### Phase 2: Repeating tables

- Add explicit repeating-table schema with header range, prototype row, column
  bindings, and row expansion policy.
- Generate typed row arrays rather than one scalar for a rectangular region.
- Render each record into its corresponding row and column.
- Validate table dimensions, types, formulas, and row expansion before
  rendering.

Phase 1 must define stable interfaces that Phase 2 can extend without changing
the safety decision or renderer guard contracts.

## Non-goals

- A general-purpose agent with filesystem or shell access will not control
  rendering or publication.
- The implementation will not train a model online from human corrections.
- The implementation will not replace the existing authoring runtime with
  LangGraph in this change.
- The implementation will not infer that every blank or styled cell is a
  placeholder.

## Current failure

The current workbook analyzer inventories every serialized cell and labels it
only with its address. The suggestion model receives labels such as
`Sheet1!A1`, so it must guess semantics from coordinates. For the observed
artifact it proposed four fields:

- `header_row`: 7 targets
- `subheader_row2`: 5 targets
- `subtotal_row3`: 5 targets
- `data_rows`: 224 targets

All 241 targets became writable `semantic_draft` regions. The fill-plan builder
then copied each field's single draft value into every bound region. Validation
checked evidence status and OOXML integrity but did not check destructive
overwrites, scalar fan-out, duplicate content, or semantic fit to the template.

## Architecture

The existing deterministic authoring workflow remains the orchestration
backbone. Template onboarding gains a compiler-like pipeline:

```text
sanitize package
  -> build WorkbookIR
  -> propose semantic mapping
  -> validate proposal
  -> decide automatic activation
       -> safe: freeze TemplateSchema
       -> ambiguous: persist ReviewPackage and pause
  -> retrieve evidence per schema field
  -> generate typed document data
  -> construct guarded FillPlan
  -> render allowlisted cells
  -> validate cell/package diff
  -> approve and release
```

AI is allowed to propose mappings, retrieval terms, and grounded content. It is
not allowed to bypass the template policy, renderer policy, diff validator, or
approval state machine.

## Data contracts

### Workbook cell inventory

`TemplateAnalysisUnit` remains the persisted atomic locator and gains safe
semantic context:

```python
class TemplateAnalysisUnit(BaseModel):
    unit_id: str
    locator: dict[str, Any]
    label: str
    writable: bool
    blocked_reason: str | None
    value_preview: str | None
    value_kind: Literal["blank", "text", "number", "boolean", "formula", "error"]
    style_fingerprint: str
    neighborhood: list[TemplateNeighbor]
    structural_role_hint: Literal[
        "unknown",
        "fixed_label",
        "placeholder",
        "value",
        "table_header",
        "table_body",
        "layout_blank",
    ]
```

`value_preview` is length-bounded and comes from the sanitized workbook.
Formula source text is never sent as writable content. Rich style objects are
reduced to stable, non-executable features.

### Mapping proposal

Every AI proposal declares its shape:

```python
class TemplateAnalysisSuggestion(BaseModel):
    semantic_unit_id: str
    label: str
    target_unit_ids: list[str]
    retrieval_terms: list[str]
    confidence: float
    value_shape: Literal["scalar", "repeating_table"] = "scalar"
```

In Phase 1, `scalar` is automatically activatable only when it has exactly one
target. A `repeating_table` proposal is persisted for review but cannot
auto-activate until Phase 2's table schema is available.

### Activation decision

```python
class TemplateActivationDecision(BaseModel):
    status: Literal["auto_accepted", "requires_human"]
    reason_codes: list[str]
    suggestion_ids: list[str]
    metrics: TemplateRiskMetrics
```

Reason codes are stable machine-readable values:

- `nonempty_target_not_placeholder`
- `scalar_target_fanout`
- `layout_blank_target`
- `repeating_table_requires_schema`
- `low_mapping_confidence`
- `mapping_conflict`
- `destructive_target_ratio`
- `missing_semantic_context`

The decision and metrics are stored with the template analysis so the UI and
audit log explain why automation stopped.

### Review correction

Human correction replaces the proposal, not workbook bytes:

```python
class TemplateMappingCorrection(BaseModel):
    analysis_id: str
    expected_content_hash: str
    suggestions: list[TemplateAnalysisSuggestion]
    locked_unit_ids: list[str]
    actor_id: str
    comment: str
```

The service revalidates corrected targets against the immutable analysis and
template hash. A correction creates a new analysis revision and does not mutate
an approved schema in place.

### Renderer baseline

Writable regions retain the expected pre-render state:

```python
class WorkbookRegionSchema(BaseModel):
    ...
    expected_value_hash: str
    allow_nonempty_overwrite: bool = False
```

Before writing, the renderer verifies that the current cell value matches the
expected hash and rejects non-empty replacement unless the confirmed region
explicitly permits it.

## Workbook analysis

The analyzer reads the sanitized OOXML package without loading executable
content. It resolves:

- shared strings and inline strings;
- numeric, boolean, error, and formula cell kinds;
- merged ranges and merge anchors;
- visible sheet, row, and column state;
- style indices and reduced style fingerprints;
- named ranges and Excel tables when present;
- neighboring non-empty cell previews;
- contiguous style/value regions and repeated row signatures.

A cell being technically writable means only that Office protection does not
forbid a write. It does not make the cell a semantic target.

Deterministic role hints follow conservative rules:

- Formula, hidden, protected, and merged non-anchor cells are blocked.
- Non-empty label-like cells are `fixed_label` unless explicitly marked as a
  placeholder or confirmed as replaceable.
- Blank cells with no marker and no semantic label are `layout_blank`.
- Defined names and supported placeholder syntax are strong placeholder
  signals.
- Repeated headers followed by homogeneous row patterns are table candidates,
  not scalar groups.

## AI proposal and review

The proposal prompt receives batched units with content and structure. It must:

- assign meaningful domain field names;
- distinguish fixed labels from values;
- output one target for scalar fields;
- declare table-shaped regions rather than grouping them as a scalar;
- use only inventory unit IDs;
- return confidence for every mapping.

The implementation supports a second reviewer provider through an interface,
but Phase 1 may use deterministic review when no independent reviewer model is
configured. Automatic activation never depends on confidence alone.

## Automatic activation policy

An analysis is automatically accepted only if all conditions hold:

1. Every target exists and is technically writable.
2. Every scalar suggestion has exactly one target.
3. No target is a formula, fixed label, human-only cell, merged non-anchor, or
   unmarked layout blank.
4. A non-empty target is an explicit placeholder or has a confirmed overwrite
   policy.
5. No repeating-table proposal lacks a valid repeating-table schema.
6. Mapping confidence meets the configured threshold.
7. Suggestions do not conflict or overlap.
8. Target and overwrite ratios remain within configured safety budgets.
9. Semantic context is present; coordinate-only proposals cannot auto-activate.

Failure of any condition produces `requires_human`. It is not treated as a
technical failure and does not discard the proposal.

`analyze_and_activate_uploaded_template` activates only `auto_accepted`
analyses. For `requires_human`, it returns the persisted draft through the
existing progress/status interface and does not create an approved schema.

## Fill-plan guards

Phase 1 adds defense in depth:

- A scalar binding with more than one target is rejected before FillPlan
  construction.
- Duplicate cell targets are rejected.
- A renderer fill must match its region's value shape.
- The renderer verifies the baseline value hash.
- Non-empty overwrites require an explicit confirmed permission.
- Formula-like text and formula replacement remain prohibited.
- A configurable duplicate-value detector rejects the same long value written
  across an abnormal number or ratio of cells.

These checks apply even to old persisted schemas so a previously approved
unsafe binding cannot recreate the observed artifact.

## Validation and approval

The renderer produces an exact cell-change manifest containing:

- sheet and cell;
- baseline and generated value hashes;
- whether the baseline was empty;
- semantic unit ID;
- region ID.

The validator fails the artifact on:

- a changed cell outside registered regions;
- an unauthorized non-empty overwrite;
- baseline hash mismatch;
- scalar fan-out;
- abnormal duplicate-value count or ratio;
- changed-cell or overwrite budget violation;
- formula, active-content, or package-integrity violation.

`approve_document_artifact` continues to verify artifact and validation hashes.
The downloaded artifact hash is surfaced to the UI so users can compare it with
the registered release hash.

## Human review

The Streamlit review surface shows:

- sheet/cell and original value preview;
- proposed field and value shape;
- confidence and risk reason codes;
- fixed/placeholder/table role;
- target cells and destructive-change estimate.

Reviewers may lock cells, change field names and retrieval terms, remove or add
targets, and select scalar or repeating-table shape. Phase 1 accepts corrected
scalar mappings. Repeating-table mappings are preserved for Phase 2 and remain
non-activatable until their column schema is complete.

Once confirmed, the immutable schema version is reused for later work orders.
A changed template content hash requires reanalysis; corrections are never
silently applied to different bytes.

## Error handling

- Malformed OOXML remains `failed`.
- Safe but ambiguous structure becomes `requires_human`.
- LLM transport or invalid JSON remains a technical failure and is retriable.
- Policy rejection includes stable reason codes and persisted metrics.
- Renderer guard failure prevents artifact creation.
- Artifact diff failure creates a failed validation report and cannot be
  approved.

## Compatibility and migration

- Existing model payloads load with defaults for newly introduced fields.
- DOCX analysis and rendering behavior is unchanged.
- Existing safe scalar schemas continue to render.
- Existing multi-target scalar bindings are rejected at generation time and
  require schema correction.
- Previously uploaded workbook templates must be reanalyzed before automatic
  activation under the new policy.
- Existing artifact bytes and audit records are never rewritten.

## Configuration

Safety defaults are fail-closed:

```text
DOCUMENT_TEMPLATE_AUTO_ACTIVATION=true
DOCUMENT_TEMPLATE_MIN_MAPPING_CONFIDENCE=0.90
DOCUMENT_TEMPLATE_MAX_TARGET_RATIO=0.20
DOCUMENT_TEMPLATE_MAX_NONEMPTY_OVERWRITE_RATIO=0.00
DOCUMENT_TEMPLATE_MAX_DUPLICATE_LONG_VALUE_CELLS=1
```

Confirmed mappings may explicitly authorize non-empty overwrites. Global
configuration cannot turn a fixed label into a writable region.

## Testing strategy

Tests follow red-green-refactor and cover:

1. Analyzer resolves shared and inline strings and includes bounded semantic
   context.
2. Styled blank cells without markers are not automatically writable targets.
3. Coordinate-only suggestions require human review.
4. Multi-target scalar suggestions require human review and cannot activate.
5. Non-empty fixed labels cannot auto-activate.
6. A simple explicit scalar placeholder template auto-activates and renders.
7. Existing unsafe persisted scalar bindings are rejected by FillPlan or
   renderer guards.
8. `icd_example.xlsx` cannot generate the prior 241-cell destructive artifact.
9. The same long value cannot be written into 224 cells.
10. Cell-diff validation rejects unauthorized changes.
11. Corrected scalar mappings activate only against the same template hash.
12. Existing DOCX, sanitization, artifact integrity, and deterministic
    authoring tests remain green.

## Acceptance criteria

- `icd_example.xlsx` is never automatically activated with the observed
  four-field/241-target mapping.
- The 224-target `data_rows` scalar binding is rejected before rendering.
- No artifact can overwrite all 86 original non-empty cells without 86
  explicit confirmed overwrite permissions.
- A normal template with explicit scalar placeholders completes without human
  review.
- An abnormal template persists actionable reason codes and can be corrected
  without reuploading the same bytes.
- Renderer and validator independently prevent destructive output even if an
  unsafe schema reaches them.
- Existing DOCX and deterministic workflows remain compatible.

