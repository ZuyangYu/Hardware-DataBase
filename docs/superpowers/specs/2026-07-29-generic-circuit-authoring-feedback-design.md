# Generic Circuit Evidence for Governed Document Authoring — Design

## Goal

Allow governed document authoring to retrieve complete, source-scoped circuit
facts from EDF/EDIF designs, render them into review candidates without
project-specific rules, and collect feedback before publication. The design
must support the current ICD case and unrelated projects whose reference
designators, connector names, nets, and templates differ.

## Scope

- Add circuit retrieval to both knowledge-base and project authoring paths.
- Preserve every parsed pin for an explicitly requested component or
  connector; do not special-case X1900, X1902, PGND, or any project name.
- Normalize OrCAD presentation artifacts (a leading `&` in a pin name) only
  for display, while retaining the raw value in evidence metadata.
- Represent an absent EDF net as `NC (no net declared in source)` rather than
  silently dropping the pin.
- Add post-generation preview and auditable feedback before a candidate is
  published.

The change does not infer whether a connected pin is an interface, shield, or
mounting pin. That decision belongs to a template's schema and to human review
because a net named `PGND` is not universally a mounting pin.

## Architecture

### 1. Capability selection

Document information requirements retain their schema-declared capabilities.
A generic, vocabulary-based enrichment adds `relationship_lookup` for fields
or tables referring to pins, connectors, nets, or connections (including
Chinese and English terms), and `entity_lookup` for component identity/model
requests. It is additive: explicit schema capabilities remain intact, and no
project identifier or fixed refdes is embedded in the rule.

The existing harness allowlist continues to accept both capabilities. A field
with neither circuit semantic nor declared circuit capability will not trigger
a circuit lookup.

### 2. Circuit fact rendering

`CircuitQueryEngine.get_instance_detail` remains the lossless data source: it
returns every stored pin, including ones without a net. `CircuitEvidenceMapper`
converts that result into an evidence envelope with:

- a human-readable, deterministic pin-to-net table for the writer;
- normalized display pin names (leading `&` removed);
- an explicit connection state for every pin (`connected` or `no_net`);
- the raw pin name, normalized name, net name, connection state, refdes, and
  source design identifier in metadata for traceability.

No pin is omitted merely because its net is blank. A `no_net` pin is displayed
as `NC (no net declared in source)`. A connected `PGND` pin remains a connected
`PGND` fact. The preview and writer therefore receive complete physical source
facts without claiming a product-specific interface interpretation.

### 3. Retrieval routes and scope

The knowledge-base retriever always performs its existing RAGFlow lookup and
additively invokes `CircuitQueryTool` for `entity_lookup` and
`relationship_lookup`. Circuit hits are discarded unless `source_name` belongs
to the frozen knowledge-base source set.

The project retriever applies the same capability dispatch per frozen source
version. It discards circuit hits whose source name differs from the logical
document title and wraps accepted evidence with that version, its processing
artifact, role, revision, and approval state. This preserves the existing
fail-closed project-scope validation.

### 4. Candidate preview, feedback, and release

Generation always ends with a `review_candidate`; it does not auto-release a
fully generated document. `approve_document_artifact` remains the sole
publication path and keeps its content-hash, validation-report, source-snapshot,
permission, and approval-event checks.

The document-generation UI will provide a read-only, bounded preview for a
candidate after it is generated:

- XLSX/XLSM: a bounded sheet/cell table;
- DOCX: bounded paragraph/table text;
- unsupported or failed previews: a clear warning plus the existing candidate
  download control.

Preview extraction is read-only and authorization-gated like candidate
download. The user can submit a comment as `DocumentHumanEvent(event_type=
"feedback")`. The event binds the candidate content hash and frozen source
snapshot. Feedback is immutable audit data: it cannot mutate a candidate,
modify source evidence, satisfy an approval, or bypass a new generation when
content needs revision.

## Data Flow

```text
template semantic unit
  -> declared capabilities + generic semantic enrichment
  -> frozen-scope RAGFlow retrieval + eligible CircuitQueryTool lookup
  -> complete, normalized circuit evidence
  -> governed writer and renderer
  -> review_candidate
  -> read-only preview + optional feedback event
  -> explicit approval -> approved_release
```

## Error Handling and Safety

- Missing circuit index, no eligible capability, missing component, or no
  source-scoped hit yields no circuit evidence; the ordinary retrievers retain
  their current behavior.
- Preview parsing errors do not alter the artifact and do not prevent download.
- A feedback event requires the same document-context authorization as other
  non-approval human events and is content-hash-bound to the candidate.
- Candidate feedback never auto-edits or releases a document.
- Existing source-set filtering and project evidence validation remain
  authoritative; feedback cannot broaden scope.

## Testing

1. **Circuit evidence tests** prove that a connector's evidence retains all
   pins, displays leading-`&` pin names without the prefix, and emits blank-net
   pins as `NC (no net declared in source)` with raw provenance metadata.
2. **Knowledge-base and project retriever tests** prove capability-gated
   circuit dispatch, frozen-source filtering, correct project binding, and no
   circuit call when the capability or service is absent.
3. **End-to-end ICD regression** uses a deterministic controlled writer that
   consumes its supplied circuit evidence (rather than hard-coded rows). It
   verifies full X1900 pin facts, the complete source facts for X1902, model
   data, normalized pin labels, and no-net pin rendering in the rendered
   candidate.
4. **Cross-project regression** uses different reference designators, nets,
   connector model, and template labels to prove that the same capability
   enrichment and retrieval/rendering path remains data-driven.
5. **Candidate review tests** prove user-facing preview extraction is bounded
   and non-mutating, feedback is persisted and cannot approve a candidate, and
   generated documents are not automatically released.

## Acceptance Criteria

- Any authorized EDF/EDIF source in a frozen document set can contribute
  complete connector pin evidence to a semantic unit that requires it.
- The system makes no product-specific pin filtering decision.
- No-net and OrCAD-prefix cases are visible, traceable, and test-covered.
- A generated document is reviewable and feedback-capable before publication.
- Tests demonstrate both the ICD case and an unrelated circuit/template case.
