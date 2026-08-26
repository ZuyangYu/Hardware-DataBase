# Evaluation, Circuit Index, and History Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound interactive RAGAS scoring time, automatically display finalized reports, make EDF graph/vector artifacts part of real ingestion and deterministic retrieval, and safely import 26 complete historical reports for cohort-compatible comparison.

**Architecture:** Keep `CircuitStore` as the authoritative EDF state and make `CircuitIndexService` coordinate its derived graph and vector indexes with explicit indexed/degraded outcomes. Route structural hardware questions to circuit evidence deterministically before document fallback. Add read-only history metadata helpers plus an atomic report importer, then let Streamlit compare only runs whose sorted sample-ID fingerprints match.

**Tech Stack:** Python 3.11+, Pydantic v2, unittest/pytest, Streamlit fragments/session state, NetworkX with the existing dictionary fallback, Chroma through the existing `CircuitVectorIndex`, SHA-256, filesystem atomic rename.

## Global Constraints

- Follow TDD for every task: add the failing test, run it and confirm the expected failure, implement the smallest behavior, then rerun the focused suite.
- Preserve unrelated dirty worktree changes. In particular, inspect and layer changes into the already modified `src/evaluation/ragas_adapter.py`, `src/core/app_pipeline.py`, `.env.example`, and their tests; do not revert or wholesale replace them.
- Keep `CircuitStore` structured state authoritative. A parser/validation failure must leave a previously indexed design untouched; graph or configured-vector failures may only mark the new result degraded.
- Do not create a component-to-component clique. Persist `component -> pin -> net` topology and include stable node IDs so high-fanout power/ground nets remain linear in connection count.
- The historical source root is read-only. Copy `summary.json` and `results.jsonl` plus existing optional `summary.csv` and `report.html`; never copy snapshots, run states, source manifests, worker logs, uploads, or checkpoints.
- Never overwrite a target run directory containing different content. Publish imports only with a same-filesystem atomic rename after validation and hash verification.
- Do not include API keys, prompts, upstream response bodies, or absolute source paths in rendered report content. The local import sidecar may retain the absolute source path for auditability.
- Commit only the files named in each task. Do not stage unrelated files with `git add -A` or `git add .`.

---

### Task 1: Give interactive scoring bounded evaluation-specific defaults

**Files:**
- Modify: `src/evaluation/config.py`
- Test: `tests/evaluation/test_eval_config.py`

**Interfaces:**
- `EvaluationConfig` defaults become `llm_max_tokens=4096`, `max_contexts_per_sample=4`, `max_context_chars=2000`, `max_context_chars_per_item=800`, `scoring_max_budget_attempts=1`, `timeout_seconds=60`, and `max_retries=0`.
- `EvaluationConfig.from_environment()` reads `EVAL_TIMEOUT_SECONDS` with a literal 60-second default and never falls back to `AGENT_TIMEOUT_SECONDS`.
- All existing `EVAL_*` overrides remain supported and continue appearing in `public_metadata()`.

- [ ] Change `test_scoring_budgets_have_safe_defaults` to assert the bounded values above, including `timeout_seconds == 60` and `max_retries == 0`.
- [ ] Add `test_evaluation_timeout_does_not_inherit_agent_timeout`: set `AGENT_TIMEOUT_SECONDS=300` without `EVAL_TIMEOUT_SECONDS` and assert the evaluation timeout remains 60.
- [ ] Add `test_evaluation_runtime_overrides_remain_explicit`: set `EVAL_TIMEOUT_SECONDS=90` and `EVAL_MAX_RETRIES=1`, then assert both override the defaults.
- [ ] Run `pytest tests/evaluation/test_eval_config.py -q`; confirm failures show the current 8192/4000/1200/3/120/2 defaults and agent-timeout inheritance.
- [ ] Update the dataclass defaults and `from_environment()` literals. Use `_positive_int_env` for timeout/workers and a small non-negative integer parser for retry count so invalid values raise `EvaluationConfigurationError` consistently.
- [ ] Run `pytest tests/evaluation/test_eval_config.py tests/evaluation/test_ragas_adapter.py tests/evaluation/test_ragas_runtime.py -q`; expect all tests to pass and existing per-metric failure handling to remain intact.
- [ ] Commit only the config and config-test files with `fix: bound interactive evaluation scoring`.

### Task 2: Automatically leave the active status fragment when a report finalizes

**Files:**
- Modify: `src/ui/evaluation_page.py`
- Test: `tests/test_evaluation_page.py`

**Interfaces:**
- Add `TERMINAL_RERUN_GUARD_PREFIX = "evaluation_terminal_rerun:"`.
- `_render_run_status(...) -> EvaluationRunState | None` returns the state it rendered.
- `_render_active_status(...)` requests one app-level `st.rerun()` only when the observed state is terminal and `should_render_evaluation_summary(run_dir)` confirms the completion marker/report.
- The session-state guard is scoped to run ID and is cleared when that run becomes active again or a different run is selected.

- [ ] Update existing `_render_run_status` tests to assert the returned state and `None` on a load error.
- [ ] Add `test_active_fragment_reruns_app_once_when_terminal_report_is_finalized` with a fake fragment/session state, terminal state, `summary.json`, `results.jsonl`, and `report_complete.json`; assert exactly one app rerun request and the guard key is set.
- [ ] Add `test_active_fragment_does_not_rerun_for_terminal_state_without_finalized_report`.
- [ ] Add `test_active_fragment_terminal_rerun_guard_prevents_loop` by invoking the fragment twice against the same session state.
- [ ] Run `pytest tests/test_evaluation_page.py -q`; confirm the new tests fail because the current fragment discards the state and never reruns the outer page.
- [ ] Return loaded state from `_render_run_status`, inspect it in `status_panel`, and perform the guarded rerun only after a final report is visible. Keep the two-second polling interval and existing pause/resume/cancel behavior.
- [ ] Run `pytest tests/test_evaluation_page.py -q`; expect all tests to pass.
- [ ] Commit only the UI file and UI test with `fix: refresh finalized evaluation reports`.

### Task 3: Replace the quadratic connectivity clique with component-pin-net topology

**Files:**
- Modify: `src/circuit/graph_store.py`
- Create: `tests/test_circuit_graph_store.py`

**Interfaces:**
- Stable node IDs: `component:{refdes}`, `pin:{refdes}.{pin}`, and `net:{net_name}`.
- NetworkX graph type: `nx.Graph`; node attributes include `kind`, `design_id`, `source_name`, and relevant EDF fields. Edges use `relation="contains"` or `relation="on_net"`.
- The dictionary fallback exposes the same node IDs/attributes and an `edges` list with `src`, `dst`, and `relation`; remove pairwise component adjacency semantics.
- Add `GraphIndexResult(path: str, node_count: int, edge_count: int)` and make `GraphStore.save(...)` return it; the existing orchestrator may continue ignoring the return value.
- Add `GraphStore.connected_entities(graph, *, refdes="", net_name="", pin="") -> list[dict[str, Any]]` so retrieval can consume either representation without depending on NetworkX APIs.

- [ ] Build a minimal design fixture with two components sharing a net and one component on a second net.
- [ ] Add `test_graph_uses_component_pin_net_nodes_without_component_clique`; assert exact node/edge kinds, no direct component-component edge, and edge count equals `2 * connection_count`.
- [ ] Add `test_high_fanout_net_graph_size_is_linear`; create 100 connections and assert 200 edges instead of 4,950 component pairs.
- [ ] Add `test_graph_preserves_multiple_nets_between_the_same_components` and ensure both net nodes remain distinguishable.
- [ ] Patch `_try_import_networkx` to return `None` and add `test_fallback_graph_has_equivalent_component_pin_net_semantics`.
- [ ] Add query tests for `connected_entities` by refdes, pin, and net name for both NetworkX and fallback data.
- [ ] Run `pytest tests/test_circuit_graph_store.py -q`; confirm failures show the existing pairwise graph shape and absent query API.
- [ ] Implement a shared node/edge projection used by both NetworkX and fallback builders, preserving component part number/value and net type/source metadata.
- [ ] Run `pytest tests/test_circuit_graph_store.py -q`; expect all tests to pass.
- [ ] Commit only graph storage and its test with `refactor: persist linear circuit connectivity graph`.

### Task 4: Make real EDF ingestion coordinate structured, graph, and vector indexes

**Files:**
- Modify: `src/circuit/index_service.py`
- Modify: `src/circuit/vector_index.py`
- Test: `tests/test_circuit_index_service.py`
- Test: `tests/test_circuit_vector_detachment.py`

**Interfaces:**
- Inject `graph_store: GraphStore | None` and `vector_index: CircuitVectorIndex | None` into `CircuitIndexService`.
- Add `CircuitVectorIndexStatus(available: bool, indexed_count: int, error: str = "")` and `reindex_design_with_status(design)`. Keep `reindex_design(design) -> int` as a compatibility wrapper.
- `CircuitIndexResult.status` is `indexed` when graph succeeds and vector succeeds or is explicitly unavailable; it is `degraded` when graph or an available vector index fails. `ok` remains true for a valid structured design.
- Result `stats` adds `graph_node_count`, `graph_edge_count`, and `vector_document_count`; warnings identify the failing derived index without exposing payloads.
- Persist metadata only after the structured design is saved, using `store.design_dir(..., create=True)` as the graph destination.

- [ ] Extend the existing successful ingestion test with graph/vector fakes and assert call order, design identity, graph artifact path, counts, and `status == "indexed"`.
- [ ] Add `test_index_file_treats_unconfigured_vector_index_as_explicitly_unavailable` and assert no degraded warning.
- [ ] Add `test_index_file_returns_degraded_when_graph_persistence_fails` and `test_index_file_returns_degraded_when_configured_vector_index_fails`; assert structured state and metadata remain queryable.
- [ ] Add `test_parser_failure_preserves_previous_design_and_derived_artifacts`: index once, record hashes, make the parser raise, and assert those hashes do not change.
- [ ] Add a vector-status compatibility test proving the old integer API delegates to the new status API.
- [ ] Run `pytest tests/test_circuit_index_service.py tests/test_circuit_vector_detachment.py -q`; confirm failures because the coordinator currently saves only structured state and vector failures collapse into integer zero.
- [ ] Implement the injected coordinator and explicit vector result. Do not catch parser/validation failures inside the derived-index warning block.
- [ ] Run the focused tests plus `pytest tests/test_circuit_ingestion_orchestrator.py tests/test_circuit_pipeline_handler.py tests/test_circuit_pipeline_routing.py -q`; expect all to pass.
- [ ] Commit only the four task files with `feat: build derived circuit indexes during ingestion`.

### Task 5: Deterministically route and retrieve the five hardware evaluation patterns

**Files:**
- Modify: `src/circuit/question_analysis.py`
- Modify: `src/circuit/index_service.py`
- Modify: `src/agents/graph.py`
- Test: `tests/test_circuit_question_analysis.py`
- Test: `tests/test_circuit_index_service.py`
- Test: `tests/test_agentic_runner.py`

**Interfaces:**
- `analyze_question()` recognizes refdes/net/pin/connection, pull-up/down and value, placement, oscillator/crystal/frequency/clock, enable/inhibit/wakeup, I2C, component-selection, and power-path signals.
- Agent `_expected_evidence(question)` includes `circuit_design` for those deterministic structural patterns even when an LLM omits it.
- `CircuitIndexService.query()` retrieval order is exact structured evidence, graph relationship evidence, optional vector evidence, then its existing keyword fallback. Evidence is deduplicated by stable `Evidence.id`; structured/graph scores remain above semantic scores.
- The graph lookup uses the persisted graph for pin/net relationships and never makes graph availability a prerequisite for structured lookup.

- [ ] Parameterize question-analysis tests with all five lines from `evaluation/datasets/ai_database_test.jsonl`; assert structural questions select circuit operations and component-selection requests retain document plus circuit requirements.
- [ ] Add agent-routing regressions asserting the five questions create at least one `circuit_query` call whenever an indexed circuit source appears in the catalog, including the LLM-planner completion path.
- [ ] Extend the index-service fixture with `Y900=20MHz`, `Y600=30MHz`, `R1205=100K`, and `U1600=LN10046FSQ1LQR` plus representative pin/net relations.
- [ ] Add exact-query tests proving each of those refdes/value facts is returned without invoking semantic search, and graph tests proving enable/pull-up/net neighbors are returned with entity/pin/net locators.
- [ ] Add a semantic fallback test where no exact/graph hit exists and assert the mapped vector evidence scores below direct EDF evidence.
- [ ] Run `pytest tests/test_circuit_question_analysis.py tests/test_circuit_index_service.py tests/test_agentic_runner.py -q`; confirm failures isolate missing patterns, graph lookup, and semantic integration.
- [ ] Extend deterministic patterns and source-plan completion in `src/agents/graph.py`; do not change the document RAG tool contract.
- [ ] Add graph/vector stages and stable deduplication to `CircuitIndexService.query()` while retaining department and source filters.
- [ ] Run the focused tests plus `pytest tests/test_circuit_agent_planning.py tests/test_circuit_agent_tool.py tests/test_circuit_query_engine.py tests/test_circuit_topology_query.py -q`; expect all to pass.
- [ ] Run the five queries against the existing `storage/circuits/ADAS_new/825504380_ADAS_SCH_TCN2` structured state in a read-only diagnostic and record returned locator IDs in the implementation notes.
- [ ] Commit only the files named in this task with `feat: route structural hardware questions to circuit facts`.

### Task 6: Model historical run origin and cohort compatibility without rewriting reports

**Files:**
- Create: `src/evaluation/history.py`
- Modify: `src/ui/evaluation_page.py`
- Create: `tests/evaluation/test_history.py`
- Modify: `tests/test_evaluation_page.py`

**Interfaces:**
- `cohort_fingerprint(sample_ids: Iterable[str]) -> str` returns SHA-256 of canonical JSON for sorted unique nonblank IDs.
- `load_history_run(run_dir: Path) -> EvaluationHistoryRun` parses `summary.json` and `results.jsonl`, derives sample IDs/count/fingerprint, and optionally reads `import_manifest.json` for origin. Legacy runs without a sidecar remain valid with origin `local`.
- `compatible_baselines(selected, candidates)` returns only different runs with the same nonempty fingerprint.
- Streamlit run labels show directory run name, origin (`本地`/`导入`), and sample count. The selected report header shows import origin and validation warnings. Numerical baseline options include only compatible cohorts.

- [ ] Add history unit tests for fingerprint order/duplicate stability, different-cohort inequality, legacy sidecar absence, imported origin parsing, malformed results rejection, and compatible-baseline filtering.
- [ ] Replace the UI history test that accepts any completed run with `test_history_view_only_offers_same_cohort_baselines`; create one same-cohort and one different-cohort report and assert only the former reaches the baseline selector.
- [ ] Add UI tests for imported/local selector labels and a selected imported report's origin/sample-count caption.
- [ ] Run `pytest tests/evaluation/test_history.py tests/test_evaluation_page.py -q`; confirm failures because no history model exists and the page currently offers every finalized run.
- [ ] Implement the pure history helper, then make the UI load it once per candidate. Pass the already parsed summary/results into `_render_summary` to avoid rereading files.
- [ ] If the selected run has no compatible baseline, display a short explanation that other runs use a different sample cohort; do not calculate deltas.
- [ ] Run the focused history/UI tests; expect all to pass, including legacy reports without new metadata.
- [ ] Commit only the four task files with `feat: compare matching evaluation cohorts`.

### Task 7: Add a dry-run-first, atomic one-time history importer

**Files:**
- Create: `src/evaluation/history_import.py`
- Create: `scripts/import_evaluation_history.py`
- Create: `tests/evaluation/test_history_import.py`

**Interfaces:**
- `discover_imports(source_root, target_root) -> ImportPlan` classifies candidates as `copy`, `skip_equal`, `conflict`, or `invalid`.
- Eligibility requires parseable `summary.json` and nonempty parseable `results.jsonl`. Those two files are mandatory; `summary.csv` and `report.html` are copied when present.
- `apply_import_plan(plan) -> ImportResult` recomputes source hashes before copying, copies into `target_root/.import-<run>-<nonce>`, verifies copied hashes, writes `import_manifest.json` and `report_complete.json`, fsyncs files/directories where supported, and atomically renames into the final run directory.
- `import_manifest.json` records schema version, source root/path, source directory name, `summary.run_id`, UTC import timestamp, per-file SHA-256 hashes, sorted sample IDs, cohort fingerprint, and validation warnings.
- CLI defaults to dry-run and requires `--apply` to mutate the target. Any conflict or apply failure returns a nonzero exit code; invalid/incomplete source directories are reported but do not block copying valid runs.

- [ ] Add tests proving discovery accepts a valid report with the two mandatory artifacts, copies optional CSV/HTML when present, and rejects missing summary, missing results, empty results, invalid JSON, and schema-invalid rows.
- [ ] Add `test_dry_run_does_not_mutate_source_or_target` using recursive path/hash snapshots before and after discovery and CLI dry-run.
- [ ] Add equal-content and different-content collision tests; assert equal is skipped and different is a conflict that is never overwritten.
- [ ] Add `test_apply_publishes_canonical_report_and_sidecars_atomically`; assert no temporary directory remains and all final hashes match the source.
- [ ] Inject copy/hash/rename failures and assert the final directory is absent, the source snapshot is unchanged, and a temporary directory is cleaned up.
- [ ] Add a run-directory/`summary.run_id` mismatch test and assert both identities are retained with a warning rather than renaming the historical run.
- [ ] Run `pytest tests/evaluation/test_history_import.py -q`; confirm failures because the importer modules do not yet exist.
- [ ] Implement pure discovery/validation first, then atomic application, then a thin `argparse` CLI. Use `shutil.copy2` only for the explicit mandatory/optional report files and never traverse arbitrary nested source content.
- [ ] Run `pytest tests/evaluation/test_history_import.py tests/evaluation/test_history.py -q`; expect all tests to pass.
- [ ] Run `python scripts/import_evaluation_history.py --help` and a temporary-directory dry-run smoke test.
- [ ] Commit only the importer module, CLI, and tests with `feat: import complete evaluation reports safely`.

### Task 8: Execute the approved import and verify the integrated feature

**Files:**
- Create: `storage/evaluations/<26 historical run directories>/summary.json`
- Create: `storage/evaluations/<26 historical run directories>/results.jsonl`
- Create: `storage/evaluations/<26 historical run directories>/summary.csv`
- Create: `storage/evaluations/<26 historical run directories>/report.html`
- Create: `storage/evaluations/<26 historical run directories>/import_manifest.json`
- Create: `storage/evaluations/<26 historical run directories>/report_complete.json`
- Modify production/test files only if verification exposes an in-scope defect; add a regression test before each correction.

**Commands and acceptance criteria:**

- [ ] Snapshot the source tree with path, size, and SHA-256 for every regular file. Run the importer without `--apply`:
  `python scripts/import_evaluation_history.py --source /home/user/workspace/Hardware-DataBase-integrate-develop/storage/evaluations --target /home/user/workspace/Hardware-DataBase-integrate-develop/.worktrees/template-upload-governed-authoring/storage/evaluations`
- [ ] Confirm dry-run reports exactly 26 `copy` candidates, excludes incomplete/snapshot-only directories, and reports no conflicts with the two existing target runs.
- [ ] Run the same command with `--apply`. Recompute the source snapshot and prove it is byte-for-byte unchanged.
- [ ] Confirm the target now has 28 viewable reports, every imported directory has all four canonical report files plus both sidecars, and no `.import-*` directory remains.
- [ ] Re-run the importer in dry-run mode and confirm all 26 historical runs are `skip_equal` and zero are `copy` or `conflict`.
- [ ] Run focused verification:
  `pytest tests/evaluation/test_eval_config.py tests/evaluation/test_history.py tests/evaluation/test_history_import.py tests/test_evaluation_page.py tests/test_circuit_graph_store.py tests/test_circuit_index_service.py tests/test_circuit_vector_detachment.py tests/test_circuit_question_analysis.py tests/test_agentic_runner.py -q`
- [ ] Run surrounding regressions:
  `pytest tests/evaluation tests/test_evaluation_page.py tests/test_circuit_agent_planning.py tests/test_circuit_agent_tool.py tests/test_circuit_ingestion_orchestrator.py tests/test_circuit_pipeline_handler.py tests/test_circuit_pipeline_routing.py tests/test_circuit_query_engine.py tests/test_circuit_topology_query.py -q`
- [ ] Run `pytest -q`. If the known unrelated ICD regression still fails because `frozen_icd_pins_only=False` is present, report it separately with the exact failing test; do not alter unrelated ICD code.
- [ ] Run `git diff --check`, inspect `git status --short`, and verify no unrelated dirty file was staged or overwritten.
- [ ] Inspect one imported 15-sample report and one local 5-sample report in the history model; prove they are both viewable but are not numerical baselines for one another.
- [ ] Use the `verification-before-completion` skill, then the `requesting-code-review` skill. Fix every critical or important in-scope finding and rerun affected tests.
- [ ] Commit imported report artifacts by explicit run-directory paths with `data: import complete evaluation history`. If repository policy intentionally ignores `storage/evaluations`, force-add only the 26 enumerated import directories after reviewing their contents and sizes.

## Final Evidence to Report

- Evaluation default timeout/retry/context/output values and the `EVAL_*` override behavior.
- Proof that terminal reports trigger one guarded page rerun and then render automatically.
- Graph node/edge counts for the existing ADAS EDF and vector availability/indexed count.
- Direct evidence locators returned for `Y900`, `Y600`, `R1205`, and `U1600`.
- Import summary: eligible/copied/skipped/conflict/invalid counts, source immutability hash result, target viewable-report count, and idempotent second-run result.
- Cohort fingerprints/sample counts showing why old and new suites are viewable but not mutually comparable.
- Focused, surrounding, and full-suite test totals, including any explicitly unrelated pre-existing failure.
