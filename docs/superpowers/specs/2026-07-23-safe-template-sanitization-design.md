# Safe Template Sanitization and Active-Content-Free Output Design

## Purpose

Allow uploaded XLSX, XLSM, and DOCX files to serve as read-only layout and
structure templates even when they contain macros, external links, embedded
objects, ActiveX controls, or form controls. Generated output must contain
none of those active-content assets.

The source file is evidence for formatting and structural analysis, not an
executable input. No LLM is given raw OOXML bytes or active-content payloads.

## Scope

This change replaces the current upload-time behavior that marks a template as
`requires_human` solely because it contains active content. It does not permit
the generated file to preserve active content, and it does not make formulas,
hidden content, protected locations, or non-anchor merged cells writable.

## Alternatives Considered

1. **OOXML package sanitization (selected).** Create a derivative package by
   removing active parts and their references while retaining unaffected OOXML
   parts. This retains layout more faithfully and is deterministic.
2. **Office-suite conversion.** Re-save through Excel or LibreOffice. It is
   host/version dependent and may preserve unwanted relationships or alter
   formatting.
3. **Rebuild a workbook or document from the analysis model.** This is safest
   but would lose complex merged-cell, style, and page-layout fidelity.

## Data Flow

```text
Original upload (immutable, read-only)
  -> inspect active parts and relationships
  -> sanitize OOXML package
  -> validate safe template package
  -> structural analysis and constrained LLM suggestions
  -> hash-bound confirmation
  -> allowlisted renderer writes safe regions only
  -> validate active-content-free generated artifact
```

The original upload, sanitization report, and sanitized template receive
separate content hashes. Analysis, region registration, and rendering bind to
the sanitized hash only.

## Sanitization Rules

### XLSX and XLSM

- Remove VBA/macro parts and output the sanitized derivative as XLSX.
- Remove external-link parts and their relationship references.
- Remove embedded OLE/Visio, ActiveX, and control-property parts and their
  relationship references.
- Remove matching content-type overrides and references from package, workbook,
  drawing, worksheet, and relationship parts as applicable.
- Preserve workbook sheets, styles, merged ranges, formulas, ordinary cell
  values, print settings, and unaffected drawing/layout parts.

### DOCX

- Remove VBA, embedded OLE/Visio, ActiveX/form-control, and external
  relationship parts and their relationship references.
- Remove matching content-type declarations.
- Preserve document body, styles, tables, headers/footers, and unaffected
  layout parts.

## Structural Analysis and Rendering

The structural analyzer operates on the sanitized derivative. It inventories
the layout and classifies cells/regions as writable or protected. Formula
cells, hidden cells/sheets, protected cells, and non-anchor merged cells remain
read-only; they may inform formatting but cannot become generation targets.

The LLM receives only the safe structural inventory and project evidence. It
returns constrained semantic bindings and draft text, never OOXML locations or
raw source bytes.

The renderer starts from the sanitized template and changes only approved
worksheet parts or the approved DOCX body regions. It rejects formula-like
values and protected regions as it does today.

## User Experience

The upload page shows a sanitization summary rather than asking the user to
edit the source template manually. It reports the count and type of removed
assets, then displays structural analysis and LLM suggestions for the safe
template. A single confirmation action enables the sanitized template.

Generated downloads always come from the sanitized derivative. They contain no
macros, external links, embedded objects, ActiveX controls, or form controls.

## Failure Handling and Auditability

Sanitization fails closed when any of the following is true:

- package XML cannot be parsed;
- a removed relationship leaves a dangling reference;
- the required workbook/document part is missing;
- validation still detects active-content parts; or
- the sanitized package cannot be read as the declared format.

No work order starts on failure. Persist the original template metadata,
sanitization report, failure reason, and hash values for audit. Do not expose
raw active-content bytes in the UI or LLM prompt.

## Verification

- Unit tests for XLSX/XLSM and DOCX sanitization, including relationships and
  content-type cleanup.
- Regression fixture for the CAM checklist: analysis accepts its sanitized
  derivative, and the derivative has zero active-content parts.
- Rendering tests verify only allowlisted body/worksheet parts change and the
  final artifact has no active content.
- API/persistence tests verify the original and sanitized hashes cannot be
  confused or substituted.
