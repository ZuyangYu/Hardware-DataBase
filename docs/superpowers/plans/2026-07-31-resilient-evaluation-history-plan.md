# Resilient Evaluation History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make RAGAS runs checkpoint metric groups, stop cleanly at pause/cancel boundaries, and expose completed or partial score reports in Streamlit history.

**Architecture:** Keep report schemas backward compatible and add run-outcome metadata plus scoring counters to the existing Pydantic models. Score one RAGAS metric group at a time, call a service progress callback after each group, and let the run controller persist a private checkpoint before publishing a final/partial report. The existing history page will treat any terminal report as viewable and label partial outcomes.

**Tech Stack:** Python 3.11+, Pydantic, unittest/pytest, Streamlit, existing RAGAS adapter and atomic JSON/JSONL report writer.

## Global Constraints

- Do not modify or stage unrelated pre-existing worktree changes.
- Preserve the existing `summary.json`, `results.jsonl`, `summary.csv`, and `report.html` names and additive schema compatibility.
- Never write API keys, prompts, or upstream response bodies into diagnostics.
- A cancel/pause may take effect only after the in-flight evaluator request returns.
- Follow TDD: each behavior gets a failing test before production code.

### Task 1: Add scoring progress and outcome schema primitives

**Files:**
- Modify: `src/evaluation/schemas.py`
- Modify: `src/evaluation/service.py`
- Test: `tests/evaluation/test_service.py`

**Interfaces:**
- Add `EvaluationSummary.metadata["run_outcome"]` fields: `kind`, `completed_groups`, `total_groups`, and optional `failure_summary`.
- Add `EvaluationRunState.scoring_completed_groups` and `scoring_total_groups` integer fields with zero defaults.
- Extend `EvaluationService.score(..., progress_callback: Callable[[EvaluationSummary, list[SampleResult], int, int], bool] | None = None)`; return the final/partial summary and results after the callback first returns `False`.

- [ ] Write `test_score_invokes_progress_callback_after_each_ragas_metric_group` using a fake adapter that records metric-name batches and assert it receives one callback per metric group and the returned results contain both groups.
- [ ] Write `test_score_stops_after_callback_returns_false` and assert the second metric group is not sent to the adapter while the first group remains in the returned results.
- [ ] Run `pytest tests/evaluation/test_service.py -q`; observe failures because scoring currently sends all metric names in one adapter call and has no callback.
- [ ] Refactor the retrieval scoring block to call `adapter.score(..., [metric_name])` per metric group, merge each group into `sample_results`, build a partial summary through a shared helper, and stop when the callback returns `False`.
- [ ] Add a summary-building helper that computes gate/count/failure fields from the current sample results and accepts additive run-outcome metadata.
- [ ] Run the two focused tests and the full service test module; expect all to pass.
- [ ] Commit only `src/evaluation/schemas.py`, `src/evaluation/service.py`, and `tests/evaluation/test_service.py` with `feat: checkpoint evaluation scoring`.

### Task 2: Persist checkpoints and publish partial terminal reports

**Files:**
- Modify: `src/evaluation/run_control.py`
- Modify: `src/evaluation/reporters.py`
- Test: `tests/evaluation/test_run_control.py`
- Test: `tests/evaluation/test_reporters.py`

**Interfaces:**
- Add `RunStateStore.update_scoring_progress(completed_groups, total_groups)`.
- Add `RunStateStore.finish_partial(status, report_path, outcome_kind, error_message="")` for `paused`, `cancelled`, and `failed` states.
- `write_reports(..., metadata: dict[str, Any] | None = None)` merges additive metadata into `EvaluationSummary.metadata` before writing all four artifacts.

- [ ] Write `test_cancel_at_scoring_checkpoint_publishes_partial_report` with a fake service callback that requests cancellation after group one; assert final status is `cancelled`, `report_path` points to an existing HTML report, and summary metadata says `partial_cancelled`.
- [ ] Write `test_scoring_exception_publishes_latest_checkpoint` with a fake service that emits one callback then raises; assert status `failed` and the report contains the checkpointed first-group score.
- [ ] Write `test_report_writer_adds_run_outcome_metadata` and assert the JSON summary and HTML include the outcome label without changing existing report filenames.
- [ ] Run the focused controller/reporter tests; observe failures because the controller currently waits for one monolithic score call and removes all artifacts on exceptions.
- [ ] Add a private checkpoint writer under `<run_dir>/.checkpoint/` using the existing atomic `write_reports`; retain the latest in-memory summary/results in the worker closure.
- [ ] Pass a controller callback to `EvaluationService.score` that writes the checkpoint, updates scoring counters, and returns `False` when state is `pause_requested` or `cancel_requested`.
- [ ] On a stop request, write a final report with `partial_paused` or `partial_cancelled`, then transition the requested state without deleting that report. On unexpected exceptions, publish the latest checkpoint as `partial_failed` before marking failed; preserve the existing cleanup behavior when no checkpoint exists.
- [ ] Keep normal completed runs on the existing final-report path and ensure checkpoint artifacts are not selected as history runs.
- [ ] Run `pytest tests/evaluation/test_run_control.py tests/evaluation/test_reporters.py -q`; expect all to pass.
- [ ] Commit the controller/reporter changes and tests with `feat: publish partial evaluation reports`.

### Task 3: Render scoring progress and all terminal reports in history

**Files:**
- Modify: `src/ui/evaluation_page.py`
- Test: `tests/test_evaluation_page.py`

**Interfaces:**
- `should_render_evaluation_summary(run_dir)` returns `True` whenever a final `summary.json` exists and the run state is terminal (`completed`, `paused`, `cancelled`, or `failed`).
- `_render_run_status` displays `scoring_completed_groups / scoring_total_groups` when `stage == "scoring"`.
- `_render_summary` displays the additive `run_outcome` warning and its completed/total group counts.

- [ ] Write `test_should_render_partial_terminal_summary` for paused, cancelled, and failed state files with summary artifacts; assert each renders as history, while a running state still renders the active panel.
- [ ] Write `test_summary_outcome_text_includes_partial_failure` against the UI helper used to form the outcome caption; assert it contains the Chinese partial-result warning and group counts.
- [ ] Run the focused UI tests; observe failures because only `completed` summaries are currently accepted and no scoring counters/outcome caption exists.
- [ ] Update summary selection and active rendering with terminal-state checks, add an outcome warning before charts, and show scoring progress in the active status panel.
- [ ] Add a compact history caption for partial reports; retain existing metric charts, sample filtering, baseline comparison, and completed-run copy.
- [ ] Run `pytest tests/test_evaluation_page.py -q`; expect all to pass.
- [ ] Commit the UI changes and tests with `feat: show partial evaluation history`.

### Task 4: Regression verification and stranded-run compatibility

**Files:**
- Modify: `src/evaluation/run_control.py` only if compatibility fixes are required.
- Test: `tests/evaluation/test_run_control.py`, `tests/evaluation/test_service.py`, `tests/evaluation/test_reporters.py`, `tests/test_evaluation_page.py`.

- [ ] Add a regression test that loads an old completed `summary.json` without `run_outcome` and still renders it normally.
- [ ] Add a regression test that an orphaned running state remains recoverable as paused and does not fabricate score results when no checkpoint exists.
- [ ] Run the complete evaluation/UI suite: `pytest tests/evaluation tests/test_evaluation_page.py -q`.
- [ ] Run the project test suite: `pytest -q`.
- [ ] Run `git diff --check` and inspect `git status --short`; verify only intended feature files differ from the pre-existing dirty worktree.
- [ ] Request a code review against the feature commits and fix all critical/important findings before reporting completion.
