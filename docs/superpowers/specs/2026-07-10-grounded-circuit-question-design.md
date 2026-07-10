# Grounded Circuit Question Design

## Goal

Answer open-ended circuit questions with traceable results: derive connectivity and topology from EDF/EDIF, retrieve component capabilities from the knowledge-base documents through RAGFlow, and never promote inference to a confirmed conclusion.

## Scope

The first increment covers connection paths, pull-up/pull-down networks, power paths, and protection-related questions. It does not perform electrical simulation, infer undocumented IC functionality, or fetch public datasheets.

## Architecture

`CircuitIndexService` receives a normalized question plan and calls deterministic topology queries over `CircuitDesign`. The agent keeps the circuit query and document RAG as separate tools. Circuit evidence exposes exact part-number candidates; the retrieval orchestrator issues bounded, derived RAGFlow queries for those parts when the intent requires a datasheet claim.

## Question and Evidence Model

The deterministic question analyzer emits one or more operations: `connection`, `bias`, `power_path`, and `protection`, plus `requires_datasheet`. An LLM may choose/rewrite calls at the agent boundary but it may only select validated operations and cannot create facts.

Circuit evidence has `evidence_kind` equal to `circuit_fact` or `derived_topology`; document evidence has `datasheet_claim`. Every topology row contains its design ID, involved refdes, pin/net pairs, and candidate part numbers. Results carry `certainty` and `confidence`; a 0-ohm link, divider, or feedback branch is never labelled a pull-up.

## Topology Rules

- Bias: a non-zero resistor from a signal net to a named power net is a pull-up; the corresponding ground case is a pull-down. Resistors between two power nets or an identified feedback net are reported as other topology.
- Protection: identify candidate TVS/ESD, fuse/PTC, diode, MOSFET, load-switch, and power-management devices from refdes, library cell, part number, and pin/net connectivity. This proves topology only.
- Capability: a conclusion such as short-circuit, over-current, thermal, short-to-ground, or short-to-battery protection requires a RAGFlow document passage associated with a discovered candidate part number.

## Orchestration and Answer Rules

Questions containing connectivity/topology terms require `circuit_design`; questions containing capability or compliance terms require both `circuit_design` and `document_text`. After circuit retrieval, at most four unique part-number queries are sent to `document_rag`. Existing department, KB, source and record filters apply unchanged.

The answer prompt must use: **confirmed** only for direct circuit facts plus any required datasheet claim; **topology observed** for circuit-only evidence; and **not confirmable** when the component, target network, or manual clause is absent. It must cite the source evidence IDs.

## Failure Handling

No circuit or part match returns no evidence rather than a guessed answer. A document retrieval failure preserves circuit evidence and marks the capability unconfirmed. Unknown Chinese phrasing falls back to ordinary circuit/entity retrieval; it never turns into a claim.

## Acceptance Tests

1. A Chinese pull-up question returns pull-up evidence including resistor, signal net and rail.
2. A whole-board query returns separate rails rather than claiming a global shared rail.
3. A protection question returns a grounded TVS/ESD topology, but does not call it short-circuit protection without a datasheet claim.
4. A circuit hit with a part number causes a bounded RAGFlow follow-up query when the question needs a datasheet capability.
5. Existing scope filtering and ordinary net/instance queries remain unchanged.
