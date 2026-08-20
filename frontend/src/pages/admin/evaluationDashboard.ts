import type { EvaluationSampleResult, EvaluationSummary } from '../../api/types';

const METRIC_LABELS: Record<string, string> = {
  completeness: '完整性',
  evidence_consistency: '证据一致性',
  answer_correctness: '答案正确性',
  faithfulness: '忠实性',
  answer_relevancy: '答案相关性',
  context_precision: '上下文精确率',
  context_recall: '上下文召回率',
  missing_information_honesty: '缺失信息诚实性',
  conflict_disclosure: '冲突披露',
};

const METRIC_ORDER = Object.keys(METRIC_LABELS);

const THRESHOLDS: Record<string, number> = {
  answer_correctness: 0.75,
  faithfulness: 0.75,
  completeness: 0.75,
  evidence_consistency: 0.75,
  answer_relevancy: 0.7,
  context_precision: 0.7,
  context_recall: 0.7,
  missing_information_honesty: 0.9,
  conflict_disclosure: 0.9,
};

type SummaryMetrics = Pick<EvaluationSummary, 'metric_scores' | 'metric_counts' | 'metric_failures'>;
type CredibilitySummary = Pick<EvaluationSummary, 'failed_samples' | 'metric_scores'> & Partial<Pick<EvaluationSummary, 'metric_counts'>>;
type SampleClassificationInput = Pick<EvaluationSampleResult, 'sample_id' | 'snapshot_status' | 'metrics' | 'retrieved_contexts' | 'critical'>;

export type SampleStatus = '采集失败' | '评分失败' | '关键样本待复核' | '无检索证据' | '正常完成';

export type MetricRow = {
  metric: string;
  label: string;
  score: number;
  threshold?: number;
  meetsThreshold: boolean | null;
  count: number;
  failures: number;
};

export function canLoadSampleDiagnostics(_status: string, hasSummary: boolean): boolean {
  // The API exposes checkpoint summary/results while a run is active. Those
  // incremental rows are needed for the live evidence and scored counters.
  return hasSummary;
}

function metricSort(left: string, right: string): number {
  const leftIndex = METRIC_ORDER.indexOf(left);
  const rightIndex = METRIC_ORDER.indexOf(right);
  const normalizedLeft = leftIndex === -1 ? METRIC_ORDER.length : leftIndex;
  const normalizedRight = rightIndex === -1 ? METRIC_ORDER.length : rightIndex;
  return normalizedLeft - normalizedRight || left.localeCompare(right);
}

function hasDisplayableScore(score: number): boolean {
  return Number.isFinite(score) && score >= 0 && score <= 1;
}

export function metricLabel(metric: string): string {
  const label = METRIC_LABELS[metric];
  return label ? `${label} (${metric})` : metric;
}

export function metricRows(summary: SummaryMetrics): MetricRow[] {
  return Object.entries(summary.metric_scores)
    .filter(([, score]) => hasDisplayableScore(score))
    .sort(([left], [right]) => metricSort(left, right))
    .map(([metric, score]) => {
      const threshold = THRESHOLDS[metric];
      return {
        metric,
        label: metricLabel(metric),
        score,
        threshold,
        meetsThreshold: threshold == null ? null : score >= threshold,
        count: summary.metric_counts[metric] ?? 0,
        failures: summary.metric_failures[metric] ?? 0,
      };
    });
}

export function compareMetricRows(
  current: Pick<EvaluationSummary, 'metric_scores'>,
  baseline: Pick<EvaluationSummary, 'metric_scores'>,
) {
  return Object.keys(current.metric_scores)
    .filter((metric) => metric in baseline.metric_scores)
    .filter((metric) => hasDisplayableScore(current.metric_scores[metric]) && hasDisplayableScore(baseline.metric_scores[metric]))
    .sort(metricSort)
    .map((metric) => ({
      metric,
      label: metricLabel(metric),
      current: current.metric_scores[metric],
      baseline: baseline.metric_scores[metric],
      delta: Number((current.metric_scores[metric] - baseline.metric_scores[metric]).toFixed(3)),
    }));
}

export function classifySampleResult(result: SampleClassificationInput): SampleStatus {
  if (result.snapshot_status === 'failed') return '采集失败';
  if (result.metrics.some((metric) => metric.status === 'failed')) return '评分失败';
  if (result.critical && result.retrieved_contexts.length === 0) return '关键样本待复核';
  if (result.retrieved_contexts.length === 0) return '无检索证据';
  return '正常完成';
}

export function filterSampleResults<T extends SampleClassificationInput>(results: T[], status: SampleStatus | '全部'): T[] {
  return status === '全部' ? results : results.filter((result) => classifySampleResult(result) === status);
}

export function buildCredibility(summary: CredibilitySummary, results: SampleClassificationInput[]) {
  const collectionFailures = Math.max(
    summary.failed_samples,
    results.filter((result) => result.snapshot_status === 'failed').length,
  );
  const metricFailures = results.filter((result) => result.metrics.some((metric) => metric.status === 'failed')).length;
  const evidenceSamples = results.filter((result) => result.retrieved_contexts.length > 0).length;
  const scoredSamples = results.filter((result) => result.metrics.some((metric) => metric.status === 'success' && metric.score != null)).length;
  const hasScores = Object.values(summary.metric_scores).some(hasDisplayableScore);
  const status = summary.failed_samples > 0 || collectionFailures > 0 || metricFailures > 0
    ? '存在技术失败'
    : !hasScores || (results.length > 0 && scoredSamples === 0)
      ? '评分覆盖不足'
      : '结果可解读';
  return { status, collectionFailures, metricFailures, evidenceSamples, scoredSamples };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

export function buildScoringCaveats(
  summary: Pick<EvaluationSummary, 'metadata' | 'metric_failures'>,
  results: Array<Pick<EvaluationSampleResult, 'snapshot_status' | 'retrieved_contexts' | 'metrics' | 'metadata'>>,
) {
  const stored = asRecord(asRecord(summary.metadata)?.scoring_diagnostics);
  const storedWarnings = Array.isArray(stored?.warnings)
    ? stored.warnings.filter((item): item is string => typeof item === 'string')
    : [];
  if (storedWarnings.length > 0) {
    return {
      status: typeof stored?.status === 'string' ? stored.status : 'interpret_with_caution',
      warnings: uniqueStrings(storedWarnings),
    };
  }

  const warnings: string[] = [];
  const noEvidence = results.filter((result) => result.snapshot_status === 'success' && result.retrieved_contexts.length === 0).length;
  const partialRetrieval = results.filter((result) => {
    const retrieval = asRecord(asRecord(result.metadata)?.retrieval_summary);
    const status = retrieval?.status;
    return typeof status === 'string' && status !== 'success' && status !== 'unknown';
  }).length;
  const truncated = results.filter((result) => asRecord(asRecord(result.metadata)?.ragas_scoring)?.contexts_truncated === true).length;
  const metricFailureRows = results.reduce(
    (total, result) => total + result.metrics.filter((metric) => metric.status === 'failed').length,
    0,
  );
  const metricFailures = Math.max(summary.metric_failures ? Object.values(summary.metric_failures).reduce((a, b) => a + b, 0) : 0, metricFailureRows);
  const scoresByMetric = new Map<string, number[]>();
  for (const result of results) {
    for (const metric of result.metrics) {
      if (metric.status !== 'success' || metric.score == null) continue;
      const values = scoresByMetric.get(metric.metric_name) ?? [];
      values.push(metric.score);
      scoresByMetric.set(metric.metric_name, values);
    }
  }
  const allZeroMetrics = [...scoresByMetric.entries()]
    .filter(([, scores]) => scores.length > 0 && scores.every((score) => score === 0))
    .map(([metric]) => metric)
    .sort();

  if (noEvidence) warnings.push(`${noEvidence} 个成功样本没有检索证据；上下文相关指标不应按普通低分解读。`);
  if (partialRetrieval) warnings.push(`${partialRetrieval} 个样本的检索状态不是 success，可能导致答案和上下文指标被低估。`);
  if (truncated) warnings.push(`${truncated} 个样本的评分上下文经过了数量或字符裁剪；请结合上下文选择诊断解读分数。`);
  if (metricFailures) warnings.push(`有 ${metricFailures} 条评分任务失败，失败项不应当当作 0 分。`);
  if (allZeroMetrics.length) warnings.push(`以下指标的有效评分样本全部为 0：${allZeroMetrics.join('、')}；优先检查参考答案/上下文对齐和评估模型兼容性。`);

  return {
    status: metricFailures ? 'technical_failure' : warnings.length ? 'interpret_with_caution' : 'ready',
    warnings: uniqueStrings(warnings),
  };
}
