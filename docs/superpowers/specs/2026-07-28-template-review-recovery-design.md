# Template Review Recovery Design

## Goal

Complete the safe automatic-template workflow so ordinary explicit scalar
templates activate without intervention, while an ambiguous workbook such as
`icd_example2` is routed to an actionable review that can be corrected and
confirmed without uploading the workbook again.

## Scope

This change closes two observed gaps:

1. The LLM proposal is accepted structurally even when it maps fixed
   instructions, separators, or isolated layout blanks.
2. The UI tells the user to revise a mapping but provides no review or
   correction controls.

Explicit repeating-table rendering is a separate capability. Until a
column-aware table schema and typed row writer exist, repeating-table mappings
remain review-only. The review UI must explain that limitation and allow the
reviewer to remove or replace those mappings with supported scalar mappings.

## Design

### Deterministic proposal normalization

After parsing the LLM response and resolving duplicate targets, normalize the
proposal against the immutable workbook inventory:

- Remove scalar mappings to `layout_blank` cells.
- Remove scalar mappings to obvious fixed instructions, headings, and
  separator symbols.
- Preserve non-empty value cells next to a label as reviewable overwrite
  candidates; do not auto-authorize their overwrite.
- Preserve repeating-table proposals for review, but never auto-activate them
  without a table schema.
- Never let model-provided confidence override these rules.

Normalization is deterministic and produces auditable dropped-target
diagnostics on the analysis revision.

### Actionable review

When an upload returns `TemplateRequiresHumanReview`, Streamlit stores the
analysis ID in session state and renders a review panel. The panel shows:

- stable reason codes and risk metrics;
- each proposed field, shape, confidence, target cells, role, and value preview;
- controls to include or remove each proposal;
- scalar target selection;
- explicit non-empty overwrite consent;
- locked targets;
- correction comment.

Submitting creates `TemplateMappingCorrection` against the original content
hash. A successful correction returns a new immutable analysis revision. If it
is safe, the same panel offers confirmation and activates the template without
another upload. If unsupported repeating tables remain, the panel stays in
review and explains which proposals must be removed or converted.

### Safety

- Fixed labels are not silently converted to writable regions.
- Non-empty overwrite consent is per target.
- Corrections are hash-bound and actor-bound through the existing service.
- Duplicate, unknown, locked, or non-writable targets continue to fail closed.
- DOCX behavior is unchanged.

## Acceptance Criteria

- The observed ICD analysis no longer proposes standalone fields for
  `E2`, `E7:E12`, `E31`, `G18`, or `F19:G22`.
- A review user can load an analysis by ID, remove unsupported table mappings,
  retain scalar mappings, authorize intended example-value overwrite, submit a
  correction, and confirm the resulting revision.
- A correction retaining `repeating_table` remains blocked with a clear
  explanation.
- An explicit placeholder-only workbook still auto-activates.
- Existing safety, template upload, renderer, and DOCX tests remain green.

