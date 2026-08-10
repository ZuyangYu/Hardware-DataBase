# Parallel Document Harness Design

## Goal

Reduce document-generation latency by executing independent semantic units in
parallel, without widening a work order's frozen sources, changing its
evidence rules, or making the rendered workbook nondeterministic.

## Measured service capacity

On 2026-08-10, against the active `ADAS` knowledge base and its configured
LLM, a read-only probe completed successfully at each tested concurrency
level.  The probe used a short CAN-transceiver query restricted to three
frozen sources and a minimal JSON-only LLM request.

| Concurrency | RAG wall time | LLM wall time | Errors |
| --- | ---: | ---: | ---: |
| 1 | 1.95 s | 4.48 s | 0 |
| 2 | 3.82 s | 5.18 s | 0 |
| 3 | 2.23 s | 10.64 s | 0 |
| 4 | 4.44 s | 5.34 s | 0 |

The isolated probe is not a capacity guarantee.  A three-unit default gives
headroom for normal interactive use and the currently running job; four is a
validated hard maximum and administrator-selectable setting.

## Design

The graph first creates all information requirements, then submits one
independent unit task for each semantic unit to a bounded executor.  A task
performs the current ordered chain: retrieve (including rewrite/retry),
validate and rerank evidence, draft, validate the draft, and contamination or
fit checks.  The coordinator collects completed task results, preserves the
schema's original unit order when assembling drafts and evidence-matrix rows,
then runs the existing cross-unit validation exactly once.

`HarnessPolicy` gains a frozen `max_parallel_units` value.  New automatically
created policies use `3`; validation limits it to `1..4`.  A work order's run
manifest records it, so a later settings change cannot alter a started run.

Only the coordinator updates the durable HarnessRun/Checkpoint progress.
Workers return immutable per-unit results.  Step and retrieval counts are
summed by the coordinator, while the persisted current node is
`parallel_units` and includes completion progress for the UI.  This avoids
concurrent SQLite writers mutating the same checkpoint cursor.

The per-run cross-unit evidence cache is made thread-safe.  The retrieval
registry, writer node receipts, frozen-source checks, reranker, and validators
remain unchanged in behaviour.  A cancelled or paused run does not submit new
unit tasks; already in-flight external calls may finish but their fenced
receipts cannot overwrite a newer controller state.

## Current serial run migration

Running Python processes cannot safely turn an already executing serial graph
into a parallel graph.  Its partial in-memory graph state is not checkpointed
per unit.  After the parallel implementation is deployed and verified, the
authorized migration is to cancel the active serial HarnessRun and create a
new run from the same immutable work order, template, policy inputs, and
knowledge-base source snapshot.  No source or template data is changed; work
performed only in memory by the serial run is discarded.

## Error handling and observability

Per-unit retrieval and writer failures retain the existing status mapping and
do not fail unrelated units.  A worker exception becomes an issue on only its
unit.  The run still reaches `waiting_human` when any unit requires review.
The run status API will expose parallelism, completed units, total units, and
active nodes, while retaining `step_count` and `retrieval_round_count` for
backward compatibility.

## Tests

Tests are written before implementation and cover: bounded concurrency,
stable schema-order output despite out-of-order completion, cross-unit
validation after all tasks finish, progress aggregation, cancellation fencing,
policy/manifest freezing, and serial compatibility at parallelism one.
