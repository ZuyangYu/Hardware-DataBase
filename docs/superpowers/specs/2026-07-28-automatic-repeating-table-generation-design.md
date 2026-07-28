# Automatic Repeating-Table Generation Design

## Goal

Generate ordinary scalar and repeating-table workbook templates without manual
mapping review. The system must infer a typed schema, repair model proposals,
replace legacy example content, render structured rows, and verify the result
without allowing the model to bypass deterministic safety controls.

## Scope

This phase covers XLSX and XLSM templates whose repeated data occupies a
rectangular worksheet region with a stable set of columns and a reusable style
row. It fixes the observed ICD failure modes:

- semantic IDs such as `su_1` and `pin_table` collide across LLM batches;
- one logical table is fragmented into unrelated row and column proposals;
- blank table-body cells are classified as unsafe layout blanks;
- example rows are treated as arbitrary destructive overwrites;
- no typed table schema or structured row renderer exists.

DOCX behavior is unchanged. Formula-bearing table bodies, irreducible merged
cell layouts, overlapping tables, and damaged workbook packages fail
automatically with a technical diagnostic; they do not request business-user
mapping decisions.

## Architecture

The implementation remains a bounded Python workflow. LangGraph or a
multi-agent framework is not required because the stages are synchronous,
finite, and already owned by `DocumentGenerationService`.

The workflow is:

1. Build one immutable workbook inventory.
2. Ask the LLM for scalar and table-column proposals in bounded batches.
3. Assign provider-owned globally unique proposal IDs.
4. Compile batch-local proposals into scalar fields and logical table schemas.
5. Apply deterministic normalization and safety validation.
6. If compilation fails with repairable diagnostics, send the diagnostics and
   safe inventory back to the LLM once, then compile and validate again.
7. Persist the compiled analysis and activate it automatically when validation
   succeeds.
8. Generate scalar drafts and structured table-row drafts from frozen evidence.
9. Render only registered scalar cells and registered table regions.
10. Reinspect the output and reject any unregistered or unsafe workbook change.

The LLM proposes semantics and repairs its own proposal. Deterministic code owns
identity, geometry, overwrite authority, formula protection, output bounds, and
activation.

## Compiled Table Schema

Add a workbook table schema that is separate from scalar
`WorkbookRegionSchema`:

- `table_region_id`: stable content-derived identifier;
- `semantic_unit_id`: globally unique semantic identity;
- `sheet_name`;
- `header_row`;
- `first_data_row`;
- `last_template_row`;
- `style_source_row`;
- `min_output_rows` and `max_output_rows`;
- ordered columns, each containing:
  - `column_id`;
  - `label`;
  - `column_letter`;
  - `value_type`;
  - `required`;
- frozen baseline hashes for every cell in the replaceable template region;
- formula and merged-cell diagnostics;
- `allow_example_region_replacement`.

A table proposal is valid only when its cells can be compiled into one
rectangular region with unique columns, continuous data rows, and no protected
formula cells. Blank cells inside a validated table rectangle are table-body
targets, not `layout_blank` targets.

The existing scalar schema remains unchanged. Scalar and table semantic IDs
share one global namespace.

## Cross-Batch Identity and Merge

LLM-provided IDs are descriptive hints, not authoritative identifiers. The
provider assigns a batch namespace during parsing and the compiler derives
stable final IDs from normalized labels plus workbook geometry.

The compiler groups repeating proposals using:

- worksheet identity;
- overlapping or adjacent row intervals;
- compatible header context;
- common rectangular geometry.

Column fragments and consecutive row fragments are merged into one logical
table. Duplicate model IDs do not create a mapping conflict by themselves.
True overlapping scalar/table targets, incompatible column definitions, and
two tables claiming the same cell remain deterministic conflicts.

## Automatic Overwrite Policy

The system grants overwrite authority only from structural evidence:

- Placeholder cells are writable.
- Empty scalar targets are writable only when they are not isolated
  `layout_blank` cells.
- A non-empty scalar value is automatically replaceable when it is a labeled
  sample value and the label relationship is structurally unambiguous.
- A validated table example region is replaceable as a whole.
- Fixed titles, instructions, section headings, formulas, signatures, approval
  fields, and cells outside a compiled region are never writable.

`destructive_target_ratio` is calculated separately for scalar targets and
compiled table regions. Replacing one validated example table is not treated
as dozens of unrelated destructive scalar writes.

## Automatic Repair

Compilation produces structured diagnostics, including:

- duplicate semantic identity;
- unresolved scalar/table overlap;
- missing or duplicated table column;
- discontinuous table geometry;
- unsafe formula or merge;
- unknown or non-writable target;
- ambiguous labeled sample value.

Only repairable proposal errors are returned to the LLM. The repair request
contains the safe structural inventory, prior proposal, and diagnostics, never
raw OOXML bytes or storage paths. One repair attempt is allowed. The repaired
proposal is recompiled from scratch and must pass the same deterministic
validator.

Technical transport failures use the existing retry policy. A persistent
transport failure or deterministic non-repairable layout failure records a
failed analysis with actionable diagnostics; it does not silently activate the
template.

## Structured Drafting

`DocumentFieldSchema.value_type` supports a table-row-array type whose ordered
columns reference the compiled table schema. A table unit draft stores
`proposed_value` as a list of row objects.

The managed writer prompt includes the exact column schema and requests strict
JSON. The validator rejects:

- non-array table values;
- non-object rows;
- unknown columns;
- missing required columns;
- non-scalar cell values;
- row counts above the schema maximum;
- formula-like strings.

The deterministic fallback groups normalized evidence into the same row shape.
It must never copy one long evidence paragraph into every table cell.

## Missing Evidence

Legacy example values are never retained as generated project facts.

When evidence is missing for a scalar field, the generated value is
`未找到依据`. When evidence is missing for a table or a required table column,
the renderer clears the registered legacy example region and writes a single
row whose available cells contain `未找到依据`. The draft and integrity
manifest record the missing-evidence outcome.

## Rendering

Extend the controlled OOXML renderer instead of loading and saving through a
generic workbook object model.

For each registered table:

1. Verify all frozen baseline hashes.
2. Reject protected formulas or unsupported merged ranges.
3. Clone the registered style source row when more rows are required.
4. Remove or clear surplus registered example rows when fewer rows are
   required.
5. Write only registered table columns.
6. Update worksheet dimensions and row/cell references when row count changes.
7. Preserve formulas and references outside the registered table.
8. Record every changed cell and structural row operation in the integrity
   manifest.

If safe row insertion would require rewriting external references, drawings,
tables, validations, conditional formatting, or unsupported formula ranges,
the renderer fails closed. A fixed-capacity table may still be generated by
clearing and filling its registered rows without structural insertion.

## Activation and User Experience

An analysis that compiles and validates is activated automatically. The former
human-review reason codes are resolved as follows:

- `nonempty_target_not_placeholder`: resolved by labeled-sample or table-example
  overwrite policy;
- `repeating_table_requires_schema`: resolved by compiled table schema;
- `layout_blank_target`: ignored only inside a validated table body;
- `mapping_conflict`: resolved through provider IDs and table merge, or rejected
  as a technical layout error;
- `destructive_target_ratio`: calculated by logical regions instead of raw
  table cell count.

The UI displays automatic-analysis and automatic-repair diagnostics for audit.
It does not ask the user to edit mappings or authorize individual targets.
Unrecoverable templates show a technical failure and preserve their immutable
analysis record.

## Persistence and Compatibility

Persist table schemas and their columns in dedicated tables keyed by immutable
template schema ID and version. Existing scalar-only schemas, analyses, fill
plans, and artifacts remain readable. New model fields use empty-list defaults
so old JSON records continue to validate.

Corrected analyses already stored in the database are not mutated. Re-uploading
or explicitly rerunning analysis creates a new immutable analysis revision that
uses the automatic compiler.

## Testing

Tests must cover:

- globally unique identities across multiple LLM batches;
- column and row fragment merging into one table;
- true scalar/table overlaps still failing closed;
- blank table-body cells not producing `layout_blank_target`;
- labeled scalar samples and table examples receiving automatic overwrite
  authority;
- fixed headings and instructions remaining protected;
- structured writer parsing and validation;
- missing evidence replacing legacy values with `未找到依据`;
- fixed-capacity table fill and safe row expansion with style preservation;
- formula, merged-cell, and output-row-limit rejection;
- integrity manifests listing every changed cell and row operation;
- the real ICD template compiling and activating without mapping review;
- the generated ICD artifact preserving workbook structure while replacing
  registered scalar and table example values;
- existing XLSX, XLSM, DOCX, activation, and document-generation regression
  suites remaining green.

## Acceptance Criteria

- A fresh analysis of the ICD workbook does not contain any of the five
  human-review reason codes reported by the user.
- The analysis contains globally unique scalar and table semantic IDs.
- The repeated pin rows compile into one typed table schema rather than
  fragmented suggestions.
- Template activation completes without mapping controls or overwrite
  checkboxes.
- Generated output contains evidence-backed scalar and table values.
- Missing evidence clears legacy examples and writes `未找到依据`.
- Fixed labels, formulas, unsupported merged layouts, and all workbook parts
  outside registered regions remain protected.
- All targeted and full regression tests pass.
