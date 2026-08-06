import { useMemo, useState } from 'react';

import type {
  EvaluationCompareResponse,
  EvaluationSampleResult,
  EvaluationSummary,
} from '../../api/types';
import { StatCard } from '@/components/StatCard';
import { cn } from '@/lib/utils';

import {
  buildCredibility,
  classifySampleResult,
  compareMetricRows,
  filterSampleResults,
  metricRows,
  type SampleStatus,
} from './evaluationDashboard';

type Props = {
  summary: EvaluationSummary;
  sampleResults: EvaluationSampleResult[];
  sampleResultsError: string;
  compare: EvaluationCompareResponse | null;
};

const SAMPLE_FILTERS: Array<SampleStatus | '全部'> = [
  '全部',
  '采集失败',
  '评分失败',
  '关键样本待复核',
  '无检索证据',
  '正常完成',
];

function scoreText(score: number): string {
  return score.toFixed(3);
}

function GatePill({ summary }: { summary: EvaluationSummary }) {
  if (!summary.gate) return <span className="rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">未执行 Gate</span>;
  return (
    <span className={cn('rounded-full px-[8px] py-[2px] text-[11px]', summary.gate.passed ? 'bg-[#e6f6ec] text-[#138a55]' : 'bg-[#fce7e7] text-[#d20b0b]')}>
      Gate {summary.gate.passed ? '通过' : '未通过'}
    </span>
  );
}

function MetricBar({ label, score, threshold, tone = 'current' }: {
  label: string;
  score: number;
  threshold?: number;
  tone?: 'current' | 'baseline' | 'pass' | 'fail' | 'neutral';
}) {
  const color = {
    current: '#18181a', baseline: '#a6adba', pass: '#138a55', fail: '#d20b0b', neutral: '#858b9c',
  }[tone];
  return (
    <div className="grid grid-cols-[minmax(120px,0.9fr)_minmax(120px,2fr)_44px] items-center gap-[10px] text-[12px]">
      <span className="truncate text-[#464c5e]">{label}</span>
      <div
        className="relative h-[12px] rounded-full bg-[#edf0f4]"
        role="progressbar"
        aria-label={`${label} 得分 ${scoreText(score)}${threshold == null ? '' : `，门禁阈值 ${threshold.toFixed(2)}`}`}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={score}
      >
        <div className="h-full rounded-full" style={{ width: `${score * 100}%`, backgroundColor: color }} />
        {threshold != null && <span aria-hidden="true" className="absolute top-[-3px] bottom-[-3px] w-[2px] bg-[#464c5e]" style={{ left: `${threshold * 100}%` }} />}
      </div>
      <span className="text-right font-medium text-[#18181a]">{scoreText(score)}</span>
    </div>
  );
}

function CurrentMetricChart({ summary }: { summary: EvaluationSummary }) {
  const rows = metricRows(summary);
  return (
    <section className="rounded-[12px] border border-[#edf0f4] p-[14px]">
      <div className="mb-[12px] flex flex-wrap items-center justify-between gap-[8px]">
        <div>
          <h4 className="text-[14px] font-semibold text-[#18181a]">当前评估效果</h4>
          <p className="mt-[2px] text-[11px] text-[#858b9c]">得分固定在 0–1 区间；竖线表示门禁阈值。</p>
        </div>
        <div className="flex gap-[9px] text-[11px] text-[#858b9c]">
          <span><i className="mr-[4px] inline-block size-[7px] rounded-full bg-[#138a55]" />达标</span>
          <span><i className="mr-[4px] inline-block size-[7px] rounded-full bg-[#d20b0b]" />未达标</span>
          <span><i className="mr-[4px] inline-block h-[10px] w-[2px] bg-[#464c5e] align-middle" />阈值</span>
        </div>
      </div>
      {rows.length === 0 ? <p className="py-[20px] text-[12px] text-[#858b9c]">暂无可展示的有效评分指标。</p> : (
        <div className="grid gap-[12px]">
          {rows.map((row) => <MetricBar key={row.metric} label={row.label} score={row.score} threshold={row.threshold} tone={row.meetsThreshold == null ? 'neutral' : row.meetsThreshold ? 'pass' : 'fail'} />)}
        </div>
      )}
    </section>
  );
}

function MetricTableAndGate({ summary }: { summary: EvaluationSummary }) {
  const rows = metricRows(summary);
  return (
    <section className="rounded-[12px] border border-[#edf0f4] p-[14px]">
      <h4 className="mb-[10px] text-[14px] font-semibold text-[#18181a]">指标与门禁</h4>
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-[12px]">
            <thead className="text-[#858b9c]"><tr className="border-b border-[#edf0f4]"><th className="pb-[7px] font-medium">指标</th><th className="pb-[7px] text-right font-medium">得分</th><th className="pb-[7px] text-right font-medium">适用样本</th><th className="pb-[7px] text-right font-medium">评分失败</th><th className="pb-[7px] text-right font-medium">阈值</th></tr></thead>
            <tbody>{rows.map((row) => <tr key={row.metric} className="border-b border-[#f3f4f6] last:border-0"><td className="py-[8px] font-medium text-[#18181a]">{row.label}</td><td className="py-[8px] text-right text-[#464c5e]">{scoreText(row.score)}</td><td className="py-[8px] text-right text-[#464c5e]">{row.count}</td><td className={cn('py-[8px] text-right', row.failures ? 'text-[#d20b0b]' : 'text-[#464c5e]')}>{row.failures}</td><td className="py-[8px] text-right text-[#464c5e]">{row.threshold == null ? '—' : row.threshold.toFixed(2)}</td></tr>)}</tbody>
          </table>
        </div>
      )}
      {summary.gate?.failures?.length ? <div className="mt-[12px] rounded-[8px] bg-[#fff7f7] px-[10px] py-[9px]"><p className="mb-[4px] text-[12px] font-medium text-[#d20b0b]">门禁失败原因</p><ul className="grid list-disc gap-[3px] pl-[18px] text-[12px] text-[#8a3030]">{summary.gate.failures.map((failure) => <li key={failure}>{failure}</li>)}</ul></div> : null}
    </section>
  );
}

function BaselineChart({ compare }: { compare: EvaluationCompareResponse | null }) {
  if (!compare) return null;
  const rows = compareMetricRows(compare.current, compare.baseline);
  return (
    <section className="rounded-[12px] border border-[#edf0f4] p-[14px]">
      <div className="mb-[12px]"><h4 className="text-[14px] font-semibold text-[#18181a]">历史对比</h4><p className="mt-[2px] text-[11px] text-[#858b9c]">当前运行与基线仅比较两次都具备的指标。</p></div>
      {rows.length === 0 ? <p className="py-[20px] text-[12px] text-[#858b9c]">两次运行没有可直接比较的同名指标。</p> : <div className="grid gap-[14px]">{rows.map((row) => <div key={row.metric}><div className="mb-[5px] flex items-center justify-between gap-[10px]"><span className="truncate text-[12px] text-[#464c5e]">{row.label}</span><span className={cn('shrink-0 text-[12px] font-medium', row.delta >= 0 ? 'text-[#138a55]' : 'text-[#d20b0b]')}>{row.delta >= 0 ? '+' : ''}{scoreText(row.delta)}</span></div><div className="grid gap-[4px]"><MetricBar label="当前" score={row.current} tone="current" /><MetricBar label="基线" score={row.baseline} tone="baseline" /></div></div>)}</div>}
    </section>
  );
}

function DiagnosticDetails({ result }: { result: EvaluationSampleResult }) {
  const retrievalSummary = result.metadata.retrieval_summary;
  const retrievalSummaryText = retrievalSummary == null ? '' : JSON.stringify(retrievalSummary, null, 2);
  return (
    <details className="group border-t border-[#edf0f4] px-[10px] py-[9px]">
      <summary className="cursor-pointer text-[12px] font-medium text-[#464c5e] marker:text-[#858b9c]">展开样本诊断</summary>
      <div className="mt-[10px] grid gap-[10px] text-[12px] leading-[18px] text-[#464c5e]">
        <DiagnosticText title="问题" value={result.question} />
        <DiagnosticText title="参考答案" value={result.reference_answer} />
        <DiagnosticText title="实际回答" value={result.response} />
        <DiagnosticText title="检索上下文" value={result.retrieved_contexts.length ? result.retrieved_contexts.join('\n\n') : '无'} />
        {retrievalSummaryText && <DiagnosticText title="检索与证据诊断" value={retrievalSummaryText} mono />}
        {result.metrics.length > 0 && <div><p className="mb-[4px] font-medium text-[#18181a]">单样本指标</p>{result.metrics.map((metric) => <p key={metric.metric_name} className="rounded-[6px] bg-[#fafbfc] px-[8px] py-[5px]"><span className="font-medium text-[#18181a]">{metric.metric_name}</span> · {metric.status}{metric.score == null ? '' : ` · ${scoreText(metric.score)}`}{metric.reason ? ` · ${metric.reason}` : ''}</p>)}</div>}
      </div>
    </details>
  );
}

function DiagnosticText({ title, value, mono = false }: { title: string; value: string; mono?: boolean }) {
  return <div><p className="mb-[3px] font-medium text-[#18181a]">{title}</p><p className={cn('whitespace-pre-wrap break-words rounded-[6px] bg-[#fafbfc] px-[8px] py-[6px]', mono && 'max-h-[240px] overflow-auto font-mono text-[11px]')}>{value || '—'}</p></div>;
}

function SampleDiagnostics({ results, error }: { results: EvaluationSampleResult[]; error: string }) {
  const [filter, setFilter] = useState<SampleStatus | '全部'>('全部');
  const filtered = useMemo(() => filterSampleResults(results, filter), [filter, results]);
  return (
    <section className="rounded-[12px] border border-[#edf0f4] p-[14px]">
      <div className="mb-[10px] flex flex-wrap items-center justify-between gap-[8px]"><div><h4 className="text-[14px] font-semibold text-[#18181a]">样本诊断</h4><p className="mt-[2px] text-[11px] text-[#858b9c]">优先查看采集失败、评分失败和关键待复核样本。</p></div><label className="text-[12px] text-[#464c5e]">状态筛选 <select className="ml-[4px] h-[30px] rounded-[7px] border border-[#e3e7f1] bg-white px-[7px] text-[12px]" value={filter} onChange={(event) => setFilter(event.target.value as SampleStatus | '全部')}>{SAMPLE_FILTERS.map((status) => <option key={status}>{status}</option>)}</select></label></div>
      {error ? <p className="mb-[10px] rounded-[8px] bg-[#fff7e7] px-[10px] py-[8px] text-[12px] text-[#9a610d]">{error}</p> : null}
      {!error && results.length === 0 ? <p className="py-[20px] text-[12px] text-[#858b9c]">暂无样本诊断数据。</p> : <div className="overflow-hidden rounded-[8px] border border-[#edf0f4]"><div className="grid grid-cols-[minmax(110px,1fr)_100px_70px_90px] gap-[8px] bg-[#fafbfc] px-[10px] py-[8px] text-[11px] text-[#858b9c] max-[700px]:grid-cols-[minmax(110px,1fr)_100px_60px]"><span>样本</span><span>状态</span><span className="text-right">证据</span><span className="text-right max-[700px]:hidden">评分指标</span></div>{filtered.map((result) => <div key={result.sample_id}><div className="grid grid-cols-[minmax(110px,1fr)_100px_70px_90px] gap-[8px] px-[10px] py-[8px] text-[12px] max-[700px]:grid-cols-[minmax(110px,1fr)_100px_60px]"><span className="truncate font-medium text-[#18181a]">{result.sample_id}</span><span className="text-[#464c5e]">{classifySampleResult(result)}</span><span className="text-right text-[#464c5e]">{result.retrieved_contexts.length}</span><span className="text-right text-[#464c5e] max-[700px]:hidden">{result.metrics.filter((metric) => metric.status === 'success' && metric.score != null).length}</span></div><DiagnosticDetails result={result} /></div>)}{filtered.length === 0 ? <p className="px-[10px] py-[20px] text-center text-[12px] text-[#858b9c]">没有符合该状态的样本。</p> : null}</div>}
    </section>
  );
}

export default function EvaluationDashboard({ summary, sampleResults, sampleResultsError, compare }: Props) {
  const credibility = buildCredibility(summary, sampleResults);
  const statusClass = credibility.status === '结果可解读' ? 'bg-[#eef8f2] text-[#246d4a]' : credibility.status === '存在技术失败' ? 'bg-[#fff1f1] text-[#a63c3c]' : 'bg-[#fff7e7] text-[#9a610d]';
  return (
    <div className="grid gap-[14px] border-t border-[#f0f1f4] pt-[14px]">
      <section><div className="mb-[10px] flex flex-wrap items-center gap-[8px]"><h3 className="text-[14px] font-semibold text-[#18181a]">评估总览</h3><GatePill summary={summary} /></div><div className="grid grid-cols-5 gap-[10px] max-[1000px]:grid-cols-3 max-[640px]:grid-cols-1"><StatCard label="样本" value={summary.sample_count} /><StatCard label="采集成功" value={summary.successful_samples} tone={summary.failed_samples ? 'default' : 'green'} /><StatCard label="有检索证据" value={`${credibility.evidenceSamples} / ${sampleResults.length || summary.sample_count}`} /><StatCard label="已评分" value={`${credibility.scoredSamples} / ${sampleResults.length || summary.sample_count}`} /><StatCard label="评分失败" value={credibility.metricFailures} tone={credibility.metricFailures ? 'red' : 'green'} /></div></section>
      <p className={cn('rounded-[8px] px-[10px] py-[8px] text-[12px]', statusClass)}>结果状态：{credibility.status}；采集失败 {credibility.collectionFailures} 条，评分失败 {credibility.metricFailures} 条。{credibility.status === '存在技术失败' ? '请先排除技术失败，再用分数作结论。' : credibility.status === '评分覆盖不足' ? '当前没有足够的有效评分用于比较。' : '分数仍应结合适用样本数、证据和门禁原因解读。'}</p>
      <div className="grid gap-[14px] xl:grid-cols-2"><CurrentMetricChart summary={summary} /><MetricTableAndGate summary={summary} /></div>
      <BaselineChart compare={compare} />
      <SampleDiagnostics results={sampleResults} error={sampleResultsError} />
    </div>
  );
}
