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

export function canLoadSampleDiagnostics(status: string, hasSummary: boolean): boolean {
  return hasSummary && status !== 'queued';
}

/** Background polling must not clear an already-rendered view. */
export function shouldResetEvaluationLoading(silent: boolean): boolean {
  return !silent;
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
  const collectionFailures = results.filter((result) => result.snapshot_status === 'failed').length;
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
