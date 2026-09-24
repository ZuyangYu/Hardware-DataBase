import { describe, expect, it } from 'vitest';

import {
  buildCredibility,
  buildScoringCaveats,
  canLoadSampleDiagnostics,
  classifySampleResult,
  compareMetricRows,
  metricRows,
  shouldResetEvaluationLoading,
} from './evaluationDashboard';

describe('evaluation dashboard data', () => {
  it('keeps existing content mounted during background refreshes', () => {
    expect(shouldResetEvaluationLoading(false)).toBe(true);
    expect(shouldResetEvaluationLoading(true)).toBe(false);
  });

  it('orders known metrics, exposes threshold state, and keeps unknown metrics last', () => {
    const rows = metricRows({
      metric_scores: { unknown: 0.5, faithfulness: 0.8, answer_correctness: 0.7 },
      metric_counts: {},
      metric_failures: {},
    });

    expect(rows.map((row) => row.metric)).toEqual(['answer_correctness', 'faithfulness', 'unknown']);
    expect(rows[0]).toMatchObject({ threshold: 0.75, meetsThreshold: false });
    expect(rows[1]).toMatchObject({ threshold: 0.75, meetsThreshold: true });
  });

  it('prioritizes collection and scoring failures when classifying samples', () => {
    expect(classifySampleResult({
      sample_id: 'a',
      snapshot_status: 'failed',
      metrics: [],
      retrieved_contexts: [],
      critical: false,
    })).toBe('采集失败');
    expect(classifySampleResult({
      sample_id: 'b',
      snapshot_status: 'success',
      metrics: [{ sample_id: 'b', metric_name: 'faithfulness', status: 'failed', score: null, reason: '', details: {} }],
      retrieved_contexts: ['e'],
      critical: false,
    })).toBe('评分失败');
  });

  it('marks technical failure as not interpretable ahead of score coverage', () => {
    expect(buildCredibility({ failed_samples: 1, metric_scores: { faithfulness: 0.8 } }, [])).toMatchObject({
      status: '存在技术失败',
    });
  });

  it('omits unavailable scores instead of rendering them as zero', () => {
    expect(metricRows({ metric_scores: { faithfulness: Number.NaN }, metric_counts: {}, metric_failures: {} })).toEqual([]);
  });

  it('compares only metrics present in both summaries', () => {
    expect(compareMetricRows(
      { metric_scores: { faithfulness: 0.8, answer_relevancy: 0.7 } },
      { metric_scores: { faithfulness: 0.6, context_recall: 0.9 } },
    )).toEqual([expect.objectContaining({ metric: 'faithfulness', current: 0.8, baseline: 0.6, delta: 0.2 })]);
  });

  it('shows checkpoint diagnostics while a run is active', () => {
    expect(canLoadSampleDiagnostics('completed', true)).toBe(true);
    expect(canLoadSampleDiagnostics('running', true)).toBe(true);
    expect(canLoadSampleDiagnostics('completed', false)).toBe(false);
  });

  it('explains why a completed low score needs technical review first', () => {
    const result = {
      snapshot_status: 'success' as const,
      retrieved_contexts: [],
      critical: false,
      metadata: {
        retrieval_summary: { status: 'partial_failure' },
        ragas_scoring: { contexts_truncated: true },
      },
      metrics: [
        { sample_id: 'q1', metric_name: 'context_precision', status: 'success' as const, score: 0, reason: '', details: {} },
        { sample_id: 'q1', metric_name: 'faithfulness', status: 'failed' as const, score: null, reason: '', details: {} },
      ],
    };

    const caveats = buildScoringCaveats(
      { metadata: {}, metric_failures: { faithfulness: 1 } },
      [result],
    );

    expect(caveats.status).toBe('technical_failure');
    expect(caveats.warnings.join(' ')).toContain('没有检索证据');
    expect(caveats.warnings.join(' ')).toContain('检索状态不是 success');
    expect(caveats.warnings.join(' ')).toContain('context_precision');
  });
});
