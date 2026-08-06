# RAGAS Evaluation Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the 5175 React RAGAS evaluation screen into a trusted, total-overview-first dashboard with threshold charts, historical comparison, and sample diagnostics.

**Architecture:** Keep evaluation execution and files unchanged. The run-detail API safely exposes parsed `results.jsonl` rows beside the existing state and summary; a small frontend data-model module derives labels, chart rows, credibility, and filterable diagnostics. React presentation components consume that derived model using native HTML/CSS/SVG, so no chart runtime dependency is required.

**Tech Stack:** FastAPI, Pydantic v2, pytest, React 18, TypeScript, Vite 6, Tailwind CSS 4, Vitest.

## Global Constraints

- Retain `require_system_admin` protection for all evaluation result data.
- Do not change RAGAS scoring, `DEFAULT_THRESHOLDS`, run lifecycle, or evaluation artifact formats.
- Do not add a production chart package; use semantic HTML/CSS/SVG and text/table alternatives.
- Draw score charts only for finite scores in the inclusive `0..1` domain; render missing values as unavailable rather than zero.
- Preserve unknown metric names while placing them after the established evaluation metric order.
- Read `results.jsonl` only in the selected-run detail route, never in the run-list route.
- Preserve the existing create-run and control-run behavior and do not touch unrelated dirty worktree files.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/api/routes/evaluation.py` | Safely load `results.jsonl` and add diagnostics fields to the existing run-detail response. |
| `tests/test_evaluation_api.py` | Regression tests for diagnostics loading, missing/corrupt artifacts, and detail-response integration. |
| `frontend/src/api/types.ts` | TypeScript contracts for run sample results and diagnostic read errors. |
| `frontend/src/pages/admin/evaluationDashboard.ts` | Pure metric sorting/labels, threshold/credibility calculations, and sample classification/filtering. |
| `frontend/src/pages/admin/evaluationDashboard.test.ts` | Vitest coverage for the frontend pure data model. |
| `frontend/src/pages/admin/EvaluationDashboard.tsx` | Accessible total-overview cards, native SVG bar charts, gate details, and expandable diagnostic rows. |
| `frontend/src/pages/admin/EvaluationPage.tsx` | Integrate the dashboard into the selected-run detail and retain run controls. |
| `frontend/package.json`, `frontend/package-lock.json` | Add the test command and Vitest dev dependency. |

## Task 1: Safely expose selected-run sample diagnostics

**Files:**
- Modify: `src/api/routes/evaluation.py:26-28, 136-343`
- Create: `tests/test_evaluation_api.py`

**Interfaces:**
- Consumes: `SampleResult.model_validate_json(line)` and the selected run directory `<output_root>/<run_id>/results.jsonl`.
- Produces: `_load_sample_results(run_dir: Path) -> tuple[list[dict[str, Any]], str]`; `GET /evaluation/runs/{run_id}` gains `sample_results: list[dict[str, Any]]` and `sample_results_error: str`.
- Guarantees: Missing or invalid results artifacts do not prevent `status` or `summary` from being returned.

- [ ] **Step 1: Write the failing loader tests**

Create `tests/test_evaluation_api.py` with tests that write valid and invalid JSONL artifacts to `tmp_path`:

```python
import json
from src.api.routes.evaluation import _load_sample_results


def test_load_sample_results_serializes_valid_results(tmp_path):
    (tmp_path / "results.jsonl").write_text(
        json.dumps({
            "sample_id": "case-1", "question": "Q", "reference_answer": "R",
            "response": "A", "retrieved_contexts": ["context"], "critical": True,
            "metrics": [{"sample_id": "case-1", "metric_name": "faithfulness", "score": 0.8}],
        }) + "\n",
        encoding="utf-8",
    )

    rows, error = _load_sample_results(tmp_path)

    assert error == ""
    assert rows == [{
        "sample_id": "case-1", "question": "Q", "reference_answer": "R",
        "response": "A", "scored_response": "", "retrieved_contexts": ["context"],
        "critical": True, "snapshot_status": "success",
        "metrics": [{"sample_id": "case-1", "metric_name": "faithfulness", "score": 0.8,
                     "status": "success", "reason": "", "details": {}}], "metadata": {},
    }]


def test_load_sample_results_returns_safe_empty_data_for_missing_or_invalid_artifact(tmp_path):
    assert _load_sample_results(tmp_path) == ([], "")
    (tmp_path / "results.jsonl").write_text('{"sample_id": "broken"}\n', encoding="utf-8")

    rows, error = _load_sample_results(tmp_path)

    assert rows == []
    assert "样本诊断不可用" in error
```

Add an integration-style test that patches `_check_output_root` and `_controller`, writes a valid `run_state.json`, `summary.json`, and `results.jsonl`, calls `get_run("run-1", output_root=str(tmp_path))`, and asserts that existing `summary` remains present while the two new diagnostics keys are included.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_evaluation_api.py -q`

Expected: collection fails because `_load_sample_results` cannot be imported.

- [ ] **Step 3: Implement a tolerant results loader and attach it to the detail response**

Update the evaluation route import and add this helper after `_state_dict`:

```python
from src.evaluation.schemas import EvaluationSample, EvaluationSummary, EvaluationRunState, SampleResult


def _load_sample_results(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    results_path = run_dir / "results.jsonl"
    if not results_path.is_file():
        return [], ""
    try:
        rows = [
            SampleResult.model_validate_json(line).model_dump(mode="json")
            for line in results_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    except (OSError, ValidationError, ValueError) as exc:
        return [], f"样本诊断不可用：{exc}"
    return rows, ""
```

At the end of `get_run`, after the current summary handling, add:

```python
    result["sample_results"], result["sample_results_error"] = _load_sample_results(
        Path(output_root) / run_id
    )
```

Do not alter `list_runs`; it must stay lightweight.

- [ ] **Step 4: Run the API regression tests to verify they pass**

Run: `pytest tests/test_evaluation_api.py -q`

Expected: all new tests pass.

- [ ] **Step 5: Run the established evaluation suite**

Run: `pytest tests/test_evaluation_page.py tests/evaluation/test_presentation.py -q`

Expected: all tests pass, confirming compatibility with existing evaluation serialization and presentation logic.

- [ ] **Step 6: Commit the API slice**

```bash
git add src/api/routes/evaluation.py tests/test_evaluation_api.py
git commit -m "feat: expose evaluation sample diagnostics"
```

## Task 2: Add testable frontend evaluation-dashboard data modeling

**Files:**
- Modify: `frontend/package.json`, `frontend/package-lock.json`, `frontend/src/api/types.ts:477-555`
- Create: `frontend/src/pages/admin/evaluationDashboard.ts`, `frontend/src/pages/admin/evaluationDashboard.test.ts`

**Interfaces:**
- Consumes: `EvaluationSummary` and `EvaluationSampleResult` from `frontend/src/api/types.ts`.
- Produces: `metricRows(summary)`, `compareMetricRows(current, baseline)`, `buildCredibility(summary, results)`, `classifySampleResult(result)`, and `filterSampleResults(results, state)`.
- Guarantees: Metric display order and threshold values match `src/evaluation/presentation.py` and `src/evaluation/gates.py`.

- [ ] **Step 1: Add the failing Vitest tests for data derivation**

Create `frontend/src/pages/admin/evaluationDashboard.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { buildCredibility, classifySampleResult, metricRows } from './evaluationDashboard';

describe('evaluation dashboard data', () => {
  it('orders known metrics, exposes their threshold state, and keeps unknown metrics last', () => {
    const rows = metricRows({ metric_scores: { unknown: 0.5, faithfulness: 0.8, answer_correctness: 0.7 }, metric_counts: {}, metric_failures: {} });
    expect(rows.map((row) => row.metric)).toEqual(['answer_correctness', 'faithfulness', 'unknown']);
    expect(rows[0]).toMatchObject({ threshold: 0.75, meetsThreshold: false });
    expect(rows[1]).toMatchObject({ threshold: 0.75, meetsThreshold: true });
  });

  it('prioritizes collection and scoring failures when classifying samples', () => {
    expect(classifySampleResult({ sample_id: 'a', snapshot_status: 'failed', metrics: [], retrieved_contexts: [], critical: false })).toBe('采集失败');
    expect(classifySampleResult({ sample_id: 'b', snapshot_status: 'success', metrics: [{ sample_id: 'b', metric_name: 'faithfulness', status: 'failed', score: null, reason: '', details: {} }], retrieved_contexts: ['e'], critical: false })).toBe('评分失败');
  });

  it('marks technical failure as not interpretable ahead of score coverage', () => {
    expect(buildCredibility({ failed_samples: 1, metric_scores: { faithfulness: 0.8 } }, [])).toMatchObject({ status: '存在技术失败' });
  });
});
```

- [ ] **Step 2: Configure and run the tests to verify failure**

Add this script and development dependency, then regenerate the lockfile with npm:

```json
"test": "vitest run",
"vitest": "^3.0.0"
```

Run: `npm test -- --run frontend/src/pages/admin/evaluationDashboard.test.ts`

Expected: FAIL because `evaluationDashboard.ts` does not exist.

- [ ] **Step 3: Add frontend API contracts and pure derivation functions**

In `frontend/src/api/types.ts`, add exact contracts beside `EvaluationSummary`:

```ts
export interface EvaluationMetricResult {
  sample_id: string;
  metric_name: string;
  score: number | null;
  status: 'success' | 'failed' | 'not_applicable';
  reason: string;
  details: Record<string, unknown>;
}

export interface EvaluationSampleResult {
  sample_id: string;
  question: string;
  reference_answer: string;
  response: string;
  scored_response: string;
  retrieved_contexts: string[];
  critical: boolean;
  snapshot_status: 'success' | 'failed';
  metrics: EvaluationMetricResult[];
  metadata: Record<string, unknown>;
}
```

Extend `EvaluationRunDetail` with:

```ts
sample_results: EvaluationSampleResult[];
sample_results_error: string;
```

Implement `evaluationDashboard.ts` with a `METRIC_LABELS` map, `METRIC_ORDER`, and a `THRESHOLDS` map exactly matching the Python constants. Use `Number.isFinite(score) && score >= 0 && score <= 1` before producing a chartable row. Implement classification with this fixed precedence: failed snapshot, failed metric, critical with no contexts, no contexts, normal completion. `buildCredibility` must return collection failures, metric failures, evidence samples, scored samples, and status in this precedence: technical failure, insufficient scoring coverage, interpretable.

- [ ] **Step 4: Run frontend data tests to verify they pass**

Run: `npm test -- --run src/pages/admin/evaluationDashboard.test.ts`

Expected: all Vitest assertions pass.

- [ ] **Step 5: Commit the frontend data slice**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/api/types.ts \
  frontend/src/pages/admin/evaluationDashboard.ts frontend/src/pages/admin/evaluationDashboard.test.ts
git commit -m "feat: add evaluation dashboard data model"
```

## Task 3: Render the total-overview-first dashboard and diagnostics

**Files:**
- Create: `frontend/src/pages/admin/EvaluationDashboard.tsx`
- Modify: `frontend/src/pages/admin/EvaluationPage.tsx:1-12, 576-688`
- Test: `frontend/src/pages/admin/evaluationDashboard.test.ts`

**Interfaces:**
- Consumes: `EvaluationRunDetail`, `EvaluationSummary`, and the pure functions from `evaluationDashboard.ts`.
- Produces: `EvaluationDashboard({ run, compare }: { run: EvaluationRunDetail; compare: EvaluationCompareResponse | null })`.
- Guarantees: The parent keeps polling and action controls; the new component is display-only and does not issue API requests.

- [ ] **Step 1: Extend the failing frontend test with chart and compare cases**

Append these cases to `frontend/src/pages/admin/evaluationDashboard.test.ts` before presentation code exists:

```ts
import { compareMetricRows, metricRows } from './evaluationDashboard';

it('omits unavailable scores instead of rendering them as zero', () => {
  expect(metricRows({ metric_scores: { faithfulness: Number.NaN }, metric_counts: {}, metric_failures: {} })).toEqual([]);
});

it('compares only metrics present in both summaries', () => {
  expect(compareMetricRows(
    { metric_scores: { faithfulness: 0.8, answer_relevancy: 0.7 } },
    { metric_scores: { faithfulness: 0.6, context_recall: 0.9 } },
  )).toEqual([expect.objectContaining({ metric: 'faithfulness', current: 0.8, baseline: 0.6, delta: 0.2 })]);
});
```

- [ ] **Step 2: Run the targeted test to verify it fails**

Run: `npm test -- --run src/pages/admin/evaluationDashboard.test.ts`

Expected: FAIL because the new filtering/comparison behavior is not yet implemented.

- [ ] **Step 3: Implement the dashboard presentation components**

Create `frontend/src/pages/admin/EvaluationDashboard.tsx` with these focused components:

- `CredibilityOverview`: five summary cards (sample count, collection success, evidence samples, scored samples, gate) and a semantic status notice plus action-count chips.
- `MetricBarChart`: maps `metricRows(summary)` to labelled rows with a 0–1 CSS/SVG bar, numeric score, threshold tick, threshold text, applicable count and failures. Add an `aria-label` that includes metric name, score and threshold status.
- `MetricTableAndGate`: accessible table of score/count/failure/threshold and the existing gate failure messages.
- `BaselineBarChart`: renders paired current/baseline bars only from `compareMetricRows`; show the existing table-style values and a no-common-metrics empty state.
- `SampleDiagnostics`: maintains only local filter/expanded-id state. Render filter buttons for `全部`, `采集失败`, `评分失败`, `关键样本待复核`, `无检索证据`, and `正常完成`; render answer, context, retrieval summary keys (`claim_coverage`, `retrieval_ledger`, `evidence_quality`) and metric statuses only after a user expands a sample.

Use explicit empty messages from the design. Do not interpolate raw HTML from answers, contexts, metadata, or errors.

In `EvaluationPage.tsx`, import `EvaluationDashboard` and replace the current `SummaryPanel` invocation in `RunDetailPanel` with:

```tsx
{run.summary && <EvaluationDashboard run={run} compare={null} />}
```

Move the historical compare card so it passes the loaded `compare` response to a dashboard comparison section or preserve it as a sibling with `EvaluationDashboard` receiving the compare response. Remove duplicate `SummaryPanel` and `SummaryCompare` render paths only after the new component renders every existing numeric field and gate failure.

- [ ] **Step 4: Run frontend data tests to verify they pass**

Run: `npm test -- --run src/pages/admin/evaluationDashboard.test.ts`

Expected: all dashboard derivation tests pass.

- [ ] **Step 5: Build the frontend**

Run: `npm run build`

Expected: `tsc -b && vite build` exits with code 0 and reports a generated production bundle.

- [ ] **Step 6: Manually verify a completed evaluation run at port 5175**

Start the frontend if it is not already running:

```bash
npm run dev
```

As a system administrator, open `/admin/evaluation`, select a completed run, then verify:

1. The overview appears before metric detail and displays the gate and technical-failure warning when applicable.
2. A current-score chart shows score values and threshold markers without relying only on color.
3. Selecting a baseline renders paired current/baseline bars for shared metrics only.
4. Filtering and expanding a failed sample reveals its answer, contexts, retrieval summary, and metric failure reason.
5. A run with no `results.jsonl` retains summary and charts while showing the diagnostic empty state.

- [ ] **Step 7: Commit the dashboard presentation slice**

```bash
git add frontend/src/pages/admin/EvaluationDashboard.tsx frontend/src/pages/admin/EvaluationPage.tsx \
  frontend/src/pages/admin/evaluationDashboard.ts frontend/src/pages/admin/evaluationDashboard.test.ts
git commit -m "feat: visualize RAGAS evaluation diagnostics"
```

## Task 4: Verify the complete change set

**Files:**
- Modify only if verification reveals a defect in files from Tasks 1–3.

**Interfaces:**
- Consumes: all completed API and frontend dashboard changes.
- Produces: evidence that the route, pure data model, TypeScript build, and existing evaluation behavior remain compatible.

- [ ] **Step 1: Run the complete relevant Python test set**

Run: `pytest tests/test_evaluation_api.py tests/test_evaluation_page.py tests/evaluation/test_presentation.py tests/evaluation/test_run_control.py -q`

Expected: all selected tests pass.

- [ ] **Step 2: Run all frontend dashboard tests and production build**

Run: `npm test -- --run src/pages/admin/evaluationDashboard.test.ts && npm run build`

Expected: Vitest and Vite both exit with code 0.

- [ ] **Step 3: Inspect the diff and static Python checks**

Run: `ruff check src/api/routes/evaluation.py tests/test_evaluation_api.py && git diff --check HEAD~3..HEAD && git status --short`

Expected: no Ruff diagnostics, no whitespace errors, and only intentional tracked changes or user-pre-existing changes.

- [ ] **Step 4: Commit any verification fixes only if needed**

```bash
git add src/api/routes/evaluation.py tests/test_evaluation_api.py frontend
git commit -m "fix: verify RAGAS dashboard integration"
```

Do not create this commit when the verification commands require no source changes.
