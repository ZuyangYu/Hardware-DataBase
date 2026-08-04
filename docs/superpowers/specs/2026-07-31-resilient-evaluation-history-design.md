# Resilient Evaluation History Design

## Goal

Make RAGAS evaluation runs terminate into viewable history even when the
upstream evaluator times out or the user cancels. Preserve successful metric
scores, record failed metrics with a safe diagnostic, and make partial results
clear in the existing Streamlit history screen.

## Scope

This change covers the existing evaluation controller, RAGAS adapter, report
writers, and evaluation page. It does not add an external queue, change the
chosen evaluator model, or alter normal completed-run scoring semantics.

## Design

### Incremental scoring checkpoint

`EvaluationService.score` will accept a progress callback. The service will
invoke it after each RAGAS metric group has been converted to `MetricResult`
objects and attached to its in-memory sample results. The callback receives a
serializable partial result set and the number of completed scoring groups.

`EvaluationRunController.execute` will use that callback while its state is in
the `scoring` stage. It will persist a checkpoint report in the run directory
without publishing it as the final report. It will also update run-state
scoring progress so the active status panel has a meaningful, advancing value.

### Control behavior

At each metric-group checkpoint, the controller checks for pause or cancel.
When requested, it stops starting new groups and writes a final partial report
from the checkpointed results.

* A cancelled evaluation finishes with status `cancelled` and retains its
  partial report.
* A paused evaluation finishes with status `paused` and retains its partial
  report. Resuming it starts a new scoring pass from its snapshots; it does not
  merge a previous partial score with a fresh score.
* A normally completed evaluation remains `completed` and writes the same
  final report artifacts as today.

The controller cannot interrupt an in-flight HTTP request. A cancel request is
therefore acknowledged at the next completed metric-group boundary rather than
after an unbounded wait.

### Timeout and partial outcomes

The RAGAS adapter already turns an upstream `TimeoutError` into failed
`MetricResult` entries. It will retain that behavior and include a concise,
safe error type/message in the result metadata. A timed-out metric group is a
completed group for checkpointing purposes: subsequent groups can still run,
and the final report lists both scores and failures.

If an unexpected exception escapes scoring, the controller will publish the
latest checkpoint as a partial report before marking the run `failed`. This
replaces the current behavior that deletes every report artifact and leaves the
user without any history.

### Report contract

Reports will carry a `run_outcome` metadata block with:

* `kind`: `completed`, `partial_cancelled`, `partial_paused`, or `partial_failed`;
* completed and total RAGAS metric-group counts; and
* an optional safe failure summary.

`summary.json`, `results.jsonl`, `summary.csv`, and `report.html` remain the
history artifact names. Checkpoints use separate temporary names and are only
promoted atomically when a run ends or needs to display a partial outcome.
Completed reports retain their existing schema plus the additive metadata.

### Existing stranded runs

When the history page discovers a non-terminal run with no live worker, it
will mark it as paused and offer its snapshots as a recoverable history entry.
If it has no scoring checkpoint, the page shows collection progress and a
clear “scoring did not complete; resume or rerun” explanation rather than
pretending scores exist. A fresh run after this change will have a partial
report whenever at least one scoring group finishes.

### User interface

The active panel displays a distinct scoring-progress indicator in addition to
collection progress. The history screen renders partial reports using the
existing summary and per-sample table, preceded by a warning explaining the
outcome, the count of completed scoring groups, and failures. Terminal partial
runs appear in the run selector just like completed runs.

## Error Handling

No API key, prompt, or upstream response body is written to the report. UI
messages use the existing safe diagnostic fields. Atomic report promotion
prevents a history page from reading a half-written final report.

## Tests

Tests will demonstrate that a timeout creates failed metric entries while
other score groups are retained; a cancel or pause at a checkpoint publishes a
viewable partial report; an unexpected scoring failure preserves the most
recent checkpoint; the active-state data exposes scoring progress; and the
history page selects and labels partial reports correctly. Existing completed
and no-report collection-only behavior will remain covered.
