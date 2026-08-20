# Deterministic Circuit Authoring Fast Path Design

## Goal

Reduce generation time for unambiguous circuit facts while preserving the frozen-source, evidence-grounded document workflow.  The 5175 status page must show progress as individual parallel units finish.

## Context

The current internal Harness runs semantic units concurrently, but each unit can still perform an LLM evidence rerank and LLM draft generation.  It also publishes progress only after every parallel future has completed.  On a 66-unit template this makes a healthy job look stuck at `create_information_requirements` and makes direct EDF/datasheet facts unnecessarily expensive.

## Design

### Deterministic fast path

The graph will classify a unit as deterministic only after retrieval and evidence validation.  The classifier requires all of the following:

1. The unit is an electrical-fact field: its label, description, or retrieval terms identify a reference designator, pin, net, connector, part/model number, device parameter, MCU, CAN, or similar circuit attribute.
2. Exactly one validated evidence item remains after field evidence selection.
3. That evidence is from a structured circuit/EDF result or explicitly contains an assignment for the requested query anchor.

When all three conditions hold, the graph uses `DeterministicEvidenceWriter` directly.  It skips the optional LLM reranker and configured managed-writer call, then keeps the existing validation, contamination detection, typed-field validation, and evidence ledger.  If any condition is not met, the existing path is unchanged: rerank when enabled, then use the configured writer.

The fast path never broadens a knowledge-base snapshot, never uses a non-frozen source, and never turns a low-confidence recovery into an automatic result.  Low-confidence recovery evidence still follows the existing human-review outcome.

### Parallel progress

Parallel workers remain side-effect free with respect to Harness persistence.  The coordinating graph thread consumes unit futures in completion order.  After each future completes it advances a coordinator-only `parallel_units` progress state with aggregate steps/retrieval rounds and `completed_units`/`total_units`, then calls the runtime progress callback.  Final drafts and matrix rows are still merged in template-schema order, so document output stays deterministic.

The runtime persists the new counters on both `HarnessRun` and `HarnessCheckpoint`; existing records remain compatible because the fields default to zero.  No worker writes status rows, avoiding fencing-token and concurrent-store races.

## Error handling

Worker exceptions are propagated as today.  Budget violations remain handled by the graph.  Per-unit progress is emitted only for successfully returned unit results, so persisted counters never claim a failed result was completed.  Fast-path eligibility is deliberately conservative: uncertain evidence uses the existing LLM path.

## Testing

Tests will first prove that an unambiguous structured pin fact bypasses reranking and the configured writer, while ordinary text evidence still invokes the configured writer.  A parallel test will prove that the progress callback observes one completed unit before the slowest unit finishes and that final draft order remains schema order.  The focused Harness test suites will then be run.

## Non-goals

This change does not alter template parsing, source snapshot selection, retrieval policy, evidence validation rules, output rendering, or the public 5175 route contract.  It does not make all electrical prose deterministic; only explicitly grounded facts qualify.
