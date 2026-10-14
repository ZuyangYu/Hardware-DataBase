import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AlertTriangle, CheckCircle2, Clock3, Download, Filter, RefreshCw, SlidersHorizontal, X } from 'lucide-react';

import { api, apiDownload } from '@/api/client';
import type { CostInvocation, CostSummary, CostTask, CostTrends } from '@/api/workbenchTypes';
import type { AuthSession } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { AdminPageShell, AdminSection } from '@/components/admin/AdminPageShell';
import { CopyableTechnicalValue } from '@/components/admin/CopyableTechnicalValue';
import { StatusBadge, type StatusTone } from '@/components/admin/StatusBadge';
import { ErrorState, LoadingState, RefreshingBadge, TableEmptyRow } from '@/components/admin/StateBlock';
import { formatAbsoluteTime, formatMultiCurrencyAmount as amount, formatPercent as percentage, statusTone } from '@/lib/formatters';
import { workbenchErrorMessage } from '@/lib/workbenchErrors';
import { CostBudgetPanel } from './CostBudgetPanel';
import { CostTrendChart, TrendSummary } from './CostTrendChart';

type Call = CostInvocation;
type Summary = CostSummary;
type Trends = CostTrends;
type Task = CostTask & { taskType: string; taskId: string };
type PeriodPreset = 'all' | 'today' | '7d' | '30d' | 'month' | 'custom';

type AppliedFilters = {
  purpose: string;
  start: string;
  end: string;
  modelAlias: string;
  endpointAlias: string;
  projectId: string;
  departmentId: string;
};

const purposes = ['chat', 'authoring', 'memory', 'ingestion', 'evaluation_collection', 'evaluation_judge', 'quality_analysis', 'unattributed'];
const PURPOSE_LABELS: Record<string, string> = {
  chat: '对话生成',
  authoring: '文档编写',
  memory: '记忆提取/整理',
  ingestion: '知识库处理',
  evaluation_collection: '评测采集',
  evaluation_judge: '评测裁判',
  quality_analysis: '质量分析',
  unattributed: '未归属调用',
};

function purposeLabel(purpose: string): string {
  return PURPOSE_LABELS[purpose] ?? purpose;
}

const PERIODS: { key: Exclude<PeriodPreset, 'custom'>; label: string }[] = [
  { key: 'all', label: '全部时间' },
  { key: 'today', label: '今天' },
  { key: '7d', label: '最近 7 天' },
  { key: '30d', label: '最近 30 天' },
  { key: 'month', label: '本月' },
];

const NUMBER_FORMATTER = new Intl.NumberFormat('zh-CN');
const HEALTH_SURFACE: Record<StatusTone, string> = {
  success: 'border-emerald-200 bg-emerald-50',
  warning: 'border-amber-200 bg-amber-50',
  danger: 'border-red-200 bg-red-50',
  info: 'border-teal-200 bg-teal-50',
  neutral: 'border-border bg-muted/30',
  readonly: 'border-border bg-muted/30',
};

function number(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : NUMBER_FORMATTER.format(value);
}

function localInputValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function rangeForPreset(preset: Exclude<PeriodPreset, 'custom'>): { start: string; end: string } {
  if (preset === 'all') return { start: '', end: '' };
  const now = new Date();
  const start = new Date(now);
  if (preset === 'today') {
    start.setHours(0, 0, 0, 0);
  } else if (preset === '7d') {
    start.setDate(start.getDate() - 7);
  } else if (preset === '30d') {
    start.setDate(start.getDate() - 30);
  } else if (preset === 'month') {
    start.setDate(1);
    start.setHours(0, 0, 0, 0);
  }
  return { start: localInputValue(start), end: localInputValue(now) };
}

function tokenValues(summary: Summary | null): Required<NonNullable<Summary['tokens']>> {
  return summary?.tokens ?? { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0 };
}

function tokenPair(summary: Summary | null): string {
  if (!summary) return '—';
  const tokens = tokenValues(summary);
  return `${number(tokens.input_tokens)} / ${number(tokens.output_tokens)}`;
}

function tokenDetail(row: Pick<Call, 'input_tokens' | 'output_tokens' | 'cached_input_tokens' | 'reasoning_tokens'>): ReactNode {
  return (
    <>
      <span>{row.input_tokens == null ? '未知' : number(row.input_tokens)} 输入 / {row.output_tokens == null ? '未知' : number(row.output_tokens)} 输出</span>
      {(row.cached_input_tokens != null || row.reasoning_tokens != null) && (
        <span className="block text-xs text-muted-foreground">
          缓存 {row.cached_input_tokens == null ? '未知' : number(row.cached_input_tokens)} · reasoning {row.reasoning_tokens == null ? '未知' : number(row.reasoning_tokens)}
        </span>
      )}
    </>
  );
}

function healthFor(summary: Summary | null): { label: string; detail: string; tone: StatusTone } {
  if (!summary) return { label: '读取中', detail: '正在读取账本健康度', tone: 'neutral' };
  if (!summary.calls) return { label: '暂无记录', detail: '当前范围没有已记录调用', tone: 'neutral' };
  if (summary.unknown_cost_calls > 0) return { label: '需要关注', detail: `${number(summary.unknown_cost_calls)} 次调用费用未知，未知不等于 0`, tone: 'warning' };
  if (summary.usage_coverage == null || summary.price_coverage == null || summary.usage_coverage < 1 || summary.price_coverage < 1) {
    return { label: '部分覆盖', detail: `Usage ${percentage(summary.usage_coverage)} · 计价 ${percentage(summary.price_coverage)}`, tone: 'warning' };
  }
  return { label: '覆盖完整', detail: '当前已记录调用均有用量和价格信息', tone: 'success' };
}

function costStatusLabel(status: Call['cost_status']): string {
  if (status === 'unknown_usage') return '费用未知：缺少用量';
  if (status === 'unknown_price') return '费用未知：未匹配价格';
  return '已计价';
}

function invocationStatusLabel(status: string): string {
  if (status === 'success') return '成功';
  if (status === 'error') return '失败';
  if (status === 'started') return '进行中';
  if (status === 'cancelled') return '已取消';
  return status;
}

function modelLabel(row: Call): string {
  const values = [row.model_alias, row.response_model, row.requested_model];
  return values.find((value) => value && value !== 'unregistered') || '未登记模型';
}

function endpointLabel(endpoint: string): string {
  return endpoint === 'unregistered' ? '未登记端点' : endpoint;
}

function scopeLabel(scope: string | undefined): string {
  if (scope === 'global') return '全局';
  if (scope === 'department_and_owned_tasks') return '本部门及本人任务';
  return scope || '当前权限范围';
}

function MetricTile({ label, value, detail, tone = 'default' }: { label: string; value: ReactNode; detail: ReactNode; tone?: 'default' | 'success' | 'warning' | 'danger' }) {
  const surface = tone === 'success' ? 'border-emerald-200 bg-emerald-50/70' : tone === 'warning' ? 'border-amber-200 bg-amber-50/70' : tone === 'danger' ? 'border-red-200 bg-red-50/70' : 'bg-white';
  return (
    <div className={`min-w-0 rounded-xl border p-4 shadow-sm ${surface}`}>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-2 break-words text-2xl font-semibold leading-tight tracking-tight">{value}</p>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{detail}</p>
    </div>
  );
}

export default function CostsPage(props: { auth: AuthSession; onLogout: () => void }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rows, setRows] = useState<Call[]>([]);
  const [loadError, setLoadError] = useState('');
  const [purpose, setPurpose] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [modelAlias, setModelAlias] = useState('');
  const [endpointAlias, setEndpointAlias] = useState('');
  const [projectId, setProjectId] = useState('');
  const [departmentId, setDepartmentId] = useState('');
  const [periodPreset, setPeriodPreset] = useState<PeriodPreset>('all');
  const [appliedFilters, setAppliedFilters] = useState<AppliedFilters>({ purpose: '', start: '', end: '', modelAlias: '', endpointAlias: '', projectId: '', departmentId: '' });
  const [trends, setTrends] = useState<Trends | null>(null);
  const [trendError, setTrendError] = useState('');
  const [trendBucket, setTrendBucket] = useState<'day' | 'hour'>('day');
  const [showDiagnosticRows, setShowDiagnosticRows] = useState(false);
  const [offset, setOffset] = useState(0);
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const requestSequence = useRef(0);

  const query = useMemo(() => {
    const params = new URLSearchParams();
    if (appliedFilters.purpose) params.set('purpose', appliedFilters.purpose);
    if (appliedFilters.start) params.set('start', new Date(appliedFilters.start).toISOString());
    if (appliedFilters.end) params.set('end', new Date(appliedFilters.end).toISOString());
    if (appliedFilters.modelAlias) params.set('model_alias', appliedFilters.modelAlias);
    if (appliedFilters.endpointAlias) params.set('endpoint_alias', appliedFilters.endpointAlias);
    if (appliedFilters.projectId) params.set('project_id', appliedFilters.projectId);
    if (appliedFilters.departmentId) params.set('department_id', appliedFilters.departmentId);
    return params.toString();
  }, [appliedFilters]);

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setBusy(true);
    try {
      const trendQuery = query ? `${query}&bucket=${trendBucket}` : `bucket=${trendBucket}`;
      const [data, calls, series] = await Promise.all([
        api.get<Summary>(`/api/v1/costs/summary${query ? `?${query}` : ''}`),
        api.get<Call[]>(`/api/v1/costs/invocations${query ? `?${query}&` : '?'}limit=50&offset=${offset}`),
        api.get<Trends>(`/api/v1/costs/trends?${trendQuery}`).catch((error) => {
          if (sequence === requestSequence.current) setTrendError(workbenchErrorMessage(error, '读取趋势失败'));
          return null;
        }),
      ]);
      if (sequence === requestSequence.current) {
        setSummary(data);
        setRows(calls);
        setTrends(series);
        setLoadError('');
        if (series) setTrendError('');
      }
    } catch (error) {
      if (sequence === requestSequence.current) setLoadError(workbenchErrorMessage(error, '读取费用账本失败'));
    } finally {
      if (sequence === requestSequence.current) setBusy(false);
    }
  }, [offset, query, trendBucket]);

  useEffect(() => { void load(); }, [load]);

  function applyFilters() {
    if (start && end && new Date(start) >= new Date(end)) {
      notify.error('结束时间必须晚于开始时间');
      return;
    }
    setOffset(0);
    setPeriodPreset('custom');
    setAppliedFilters({ purpose, start, end, modelAlias, endpointAlias, projectId, departmentId });
  }

  function applyPreset(preset: Exclude<PeriodPreset, 'custom'>) {
    const range = rangeForPreset(preset);
    setPeriodPreset(preset);
    setStart(range.start);
    setEnd(range.end);
    setOffset(0);
    setAppliedFilters((current) => ({ ...current, start: range.start, end: range.end }));
  }

  function resetFilters() {
    setPurpose(''); setStart(''); setEnd(''); setModelAlias(''); setEndpointAlias(''); setProjectId(''); setDepartmentId('');
    setPeriodPreset('all');
    setOffset(0);
    setAppliedFilters({ purpose: '', start: '', end: '', modelAlias: '', endpointAlias: '', projectId: '', departmentId: '' });
  }

  async function exportCsv() {
    try {
      await apiDownload.blob(`/api/v1/costs/export${query ? `?${query}` : ''}`, 'llm-costs.csv');
    } catch (error) {
      setLoadError(workbenchErrorMessage(error, '导出失败'));
    }
  }

  async function showTask(row: Pick<Call, 'task_type' | 'task_id'>, taskOffset = 0) {
    if (!row.task_id) return;
    try {
      const detail = await api.get<Omit<Task, 'taskType' | 'taskId'>>(`/api/v1/costs/tasks/${encodeURIComponent(row.task_type)}/${encodeURIComponent(row.task_id)}?limit=100&offset=${taskOffset}`);
      setTask({ ...detail, taskType: row.task_type, taskId: row.task_id });
    } catch (error) {
      setLoadError(workbenchErrorMessage(error, '任务费用读取失败'));
    }
  }

  const activeFilters = [
    appliedFilters.purpose ? `用途：${purposeLabel(appliedFilters.purpose)}` : '',
    appliedFilters.modelAlias ? `模型：${appliedFilters.modelAlias}` : '',
    appliedFilters.endpointAlias ? `端点：${appliedFilters.endpointAlias}` : '',
    appliedFilters.projectId ? `项目：${appliedFilters.projectId}` : '',
    appliedFilters.departmentId ? `部门：${appliedFilters.departmentId}` : '',
    appliedFilters.start || appliedFilters.end ? `时间：${appliedFilters.start || '不限'} — ${appliedFilters.end || '不限'}` : '',
  ].filter(Boolean);
  const health = healthFor(summary);
  const tokens = tokenValues(summary);
  const purposeRows = Object.entries(summary?.by_purpose ?? {}).sort(([, left], [, right]) => right.calls - left.calls);
  const maxPurposeCalls = Math.max(...purposeRows.map(([, item]) => item.calls), 0);
  const diagnosticRows = rows.filter((row) => row.usage_status === 'missing');
  const visibleRows = showDiagnosticRows ? rows : rows.filter((row) => row.usage_status !== 'missing');

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <AdminPageShell>
        <AppHeader title="成本与用量" description="共享模型调用账本 · 应用侧 Token 估算 · 按币种分别统计" />
        <section className="grid min-w-0 gap-4 rounded-xl border bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <h2 className="font-semibold">查询范围</h2>
              <p className="mt-1 text-xs text-muted-foreground">先选时间范围，再按用途、模型或归属缩小观测范围。本页只统计已捕获的模型/Embedding provider 调用；记忆读写等非模型操作不计入。当前页面不管理用户 API Key，也不代表供应商账户余额。</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {busy && <RefreshingBadge label="正在更新账本" />}
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void load()}><RefreshCw className="size-3.5" />刷新</Button>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void exportCsv()}><Download className="size-3.5" />导出 CSV</Button>
            </div>
          </div>
          <div className="flex flex-wrap gap-2" role="group" aria-label="时间范围">
            {PERIODS.map((period) => (
              <Button key={period.key} size="sm" variant={periodPreset === period.key ? 'secondary' : 'outline'} aria-pressed={periodPreset === period.key} disabled={busy} onClick={() => applyPreset(period.key)}>
                {period.label}
              </Button>
            ))}
            {periodPreset === 'custom' && <StatusBadge tone="info"><Clock3 className="size-3" />自定义范围</StatusBadge>}
          </div>
          <details className="rounded-lg border bg-muted/20 p-3">
            <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium"><SlidersHorizontal className="size-4" />高级筛选<span className="text-xs font-normal text-muted-foreground">（可选）</span></summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              <label className="grid gap-1 text-sm">开始时间<input className="min-w-0 rounded border bg-white p-2 text-sm" type="datetime-local" value={start} onChange={(event) => { setStart(event.target.value); setPeriodPreset('custom'); }} /></label>
              <label className="grid gap-1 text-sm">结束时间<input className="min-w-0 rounded border bg-white p-2 text-sm" type="datetime-local" value={end} onChange={(event) => { setEnd(event.target.value); setPeriodPreset('custom'); }} /></label>
              <label className="grid gap-1 text-sm">调用用途<select aria-label="调用用途" className="rounded border bg-white p-2 text-sm" value={purpose} onChange={(event) => setPurpose(event.target.value)}><option value="">全部用途</option>{purposes.map((item) => <option key={item} value={item}>{purposeLabel(item)}</option>)}</select></label>
              <label className="grid gap-1 text-sm">模型别名<input className="min-w-0 rounded border bg-white p-2 text-sm" value={modelAlias} onChange={(event) => setModelAlias(event.target.value)} /></label>
              <label className="grid gap-1 text-sm">端点别名<input className="min-w-0 rounded border bg-white p-2 text-sm" value={endpointAlias} onChange={(event) => setEndpointAlias(event.target.value)} /></label>
              <label className="grid gap-1 text-sm">项目 ID<input className="min-w-0 rounded border bg-white p-2 text-sm" value={projectId} onChange={(event) => setProjectId(event.target.value)} /></label>
              {props.auth.user.role === 'system_admin' && <label className="grid gap-1 text-sm">部门 ID<input type="number" min="1" className="min-w-0 rounded border bg-white p-2 text-sm" value={departmentId} onChange={(event) => setDepartmentId(event.target.value)} /></label>}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button size="sm" disabled={busy} onClick={applyFilters}><Filter className="size-3.5" />应用筛选</Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={resetFilters}>重置</Button>
            </div>
          </details>
          {activeFilters.length > 0 && <div className="flex flex-wrap items-center gap-2 text-xs"><span className="text-muted-foreground">已应用：</span>{activeFilters.map((item) => <span className="inline-flex items-center gap-1 rounded-full border bg-muted px-2 py-1" key={item}>{item}<X className="size-3 text-muted-foreground" aria-hidden /></span>)}</div>}
          <p className="text-xs text-muted-foreground">时间按本地输入转换为 UTC 查询；趋势图按 UTC 时间桶展示。</p>
        </section>

        {loadError && <ErrorState message={loadError} onRetry={() => void load()} />}

        <AdminSection title="观测摘要" description="先看当前权限范围内的消耗和数据完整性；金额是应用侧 Token 估算，不是供应商结算账单。">
          <div className={`flex flex-wrap items-start gap-3 rounded-lg border p-3 ${HEALTH_SURFACE[health.tone]}`}>
            {health.tone === 'success' ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-700" aria-hidden /> : <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />}
            <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><StatusBadge tone={health.tone}>{health.label}</StatusBadge><span className="text-sm font-medium">账本健康度</span></div><p className="mt-1 text-xs text-muted-foreground">{health.detail}。覆盖率分母为已记录调用，队列丢失不能据此推断为零。</p></div>
          </div>
          <div className="grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <MetricTile label="已知费用" value={summary ? amount(summary.known_amounts) : '—'} detail="应用 Token 估算 · 多币种分别展示" />
            <MetricTile label="调用数" value={summary ? number(summary.calls) : '—'} detail={summary ? `${number(summary.unfinished_calls)} 次未结束 · ${number(summary.unattributed_calls)} 次未归属` : '当前范围内已记录调用'} />
            <MetricTile label="Token 用量" value={summary ? `${tokenPair(summary)}` : '—'} detail={summary ? `输入 / 输出 · 缓存 ${number(tokens.cached_input_tokens)} · reasoning ${number(tokens.reasoning_tokens)}` : '输入 / 输出 Token'} />
            <MetricTile label="账本健康" value={health.label} detail={health.detail} tone={health.tone === 'success' ? 'success' : health.tone === 'warning' ? 'warning' : health.tone === 'danger' ? 'danger' : 'default'} />
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
            <span>权限范围：{scopeLabel(summary?.scope)}</span>
            <span>更新时间：{formatAbsoluteTime(summary?.updated_at)}</span>
            <span>未知费用：{summary ? number(summary.unknown_cost_calls) : '—'}</span>
            <span>Usage 覆盖：{percentage(summary?.usage_coverage)}</span>
            <span>计价覆盖：{percentage(summary?.price_coverage)}</span>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">未知费用不等于 0；reasoning 已包含在 output 时不重复计价；预算和费用均不代表供应商账户余额。</p>
        </AdminSection>

        <CostBudgetPanel canRegister={props.auth.user.role === 'system_admin'} />

        <AdminSection
          title="趋势与消耗来源"
          description="用图表快速定位消耗变化，再按用途和模型查看来源。图表只绘制已知金额，不把未知费用或缺失时间桶推定为零。"
          actions={<div className="flex items-center gap-1" role="group" aria-label="趋势粒度"><Button size="sm" variant={trendBucket === 'day' ? 'secondary' : 'outline'} aria-pressed={trendBucket === 'day'} disabled={busy} onClick={() => setTrendBucket('day')}>按日</Button><Button size="sm" variant={trendBucket === 'hour' ? 'secondary' : 'outline'} aria-pressed={trendBucket === 'hour'} disabled={busy} onClick={() => setTrendBucket('hour')}>按小时</Button></div>}
        >
          {trendError && <ErrorState message={trendError} onRetry={() => void load()} />}
          <TrendSummary trends={trends} />
          <CostTrendChart trends={trends} />
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)]">
            <div className="min-w-0 rounded-lg border p-3">
              <div className="mb-3"><h3 className="font-medium">按用途</h3><p className="mt-1 text-xs text-muted-foreground">按调用数显示占比，金额仍按币种分别展示。</p></div>
              <div className="grid gap-3">
                {purposeRows.map(([name, item]) => <div key={name} className="grid gap-1.5"><div className="flex flex-wrap justify-between gap-2 text-sm"><span>{purposeLabel(name)}</span><span className="text-right text-muted-foreground">{number(item.calls)} 次 · {amount(item.known_amounts)}</span></div><div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary/70" style={{ width: `${maxPurposeCalls ? (item.calls / maxPurposeCalls) * 100 : 0}%` }} /></div></div>)}
                {!purposeRows.length && <p className="py-6 text-center text-sm text-muted-foreground">当前范围暂无用途数据</p>}
              </div>
            </div>
            <div className="min-w-0 rounded-lg border p-3"><div className="mb-3"><h3 className="font-medium">模型已知费用排行</h3><p className="mt-1 text-xs text-muted-foreground">按币种排序；价格覆盖不足时，排行只代表已知金额下界。</p></div><div className="grid gap-3">{Object.entries(trends?.rankings_by_currency ?? {}).map(([currency, ranking]) => <div className="overflow-x-auto rounded border" key={currency}><table className="min-w-[620px] w-full text-left text-sm"><caption className="bg-muted p-2 text-left font-medium">{currency}</caption><thead className="bg-muted/60"><tr><th className="p-2">模型 / 端点</th><th className="p-2 text-right">已知金额</th><th className="p-2 text-right">调用</th><th className="p-2 text-right">计价覆盖</th></tr></thead><tbody>{ranking.map((item) => <tr className="border-t" key={`${item.endpoint_alias}:${item.model_alias}:${item.requested_model}`}><td className="p-2"><div>{item.model_alias || item.response_model || item.requested_model}</div><div className="text-xs text-muted-foreground">{item.endpoint_alias}</div></td><td className="p-2 text-right whitespace-nowrap">{currency} {item.known_amount}</td><td className="p-2 text-right">{number(item.calls)}</td><td className="p-2 text-right">{percentage(item.price_coverage)}</td></tr>)}</tbody></table></div>)}{!Object.keys(trends?.rankings_by_currency ?? {}).length && <p className="py-6 text-center text-sm text-muted-foreground">当前范围暂无模型排行</p>}</div></div>
          </div>
          <details className="rounded-lg border">
            <summary className="cursor-pointer list-none px-3 py-2 text-sm font-medium">查看趋势数据表</summary>
            <div className="overflow-x-auto border-t"><table className="min-w-[620px] w-full text-left text-sm"><thead className="bg-muted"><tr><th className="p-2">UTC 时间桶</th><th className="p-2 text-right">调用</th><th className="p-2 text-right">已知金额</th><th className="p-2 text-right">未知费用</th><th className="p-2 text-right">计价覆盖</th></tr></thead><tbody>{trends?.buckets.map((item) => <tr className="border-t" key={item.bucket_start}><td className="p-2 whitespace-nowrap">{item.bucket_start}</td><td className="p-2 text-right">{number(item.calls)}</td><td className="p-2 text-right whitespace-nowrap">{amount(item.known_amounts)}</td><td className="p-2 text-right">{number(item.unknown_cost_calls)}</td><td className="p-2 text-right">{percentage(item.price_coverage)}</td></tr>)}{!trends?.buckets.length && <TableEmptyRow colSpan={5} title="当前范围暂无趋势数据" />}</tbody></table></div>
          </details>
        </AdminSection>

        <AdminSection
          title="调用明细"
          description="默认展示已返回 Token 用量的模型调用；用量缺失的失败或逻辑记录归入诊断记录，可按需展开。明细不包含提示词、回答正文或 API Key。"
          actions={(
            <label className="inline-flex items-center gap-2 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={showDiagnosticRows}
                onChange={(event) => setShowDiagnosticRows(event.target.checked)}
              />
              显示诊断记录{diagnosticRows.length ? `（${diagnosticRows.length}）` : ''}
            </label>
          )}
        >
          <div className="overflow-x-auto rounded-lg border"><table className="min-w-[1080px] w-full text-left text-sm"><thead className="bg-muted"><tr>{['时间 / 调用', '用途 / 任务', '模型 / 端点', 'Token 用量', '状态 / 数据质量', '应用侧费用'].map((title) => <th className="whitespace-nowrap p-3" key={title}>{title}</th>)}</tr></thead><tbody>
            {busy && !rows.length ? <tr><td className="p-0" colSpan={6}><LoadingState label="正在读取调用明细…" /></td></tr> : visibleRows.map((row) => <tr className="border-t transition-colors hover:bg-muted/30" key={row.invocation_id}>
              <td className="p-3"><span className="whitespace-nowrap">{formatAbsoluteTime(row.started_at)}</span><div className="mt-1"><CopyableTechnicalValue value={row.invocation_id} display={`${row.invocation_id.slice(0, 17)}…`} label="调用" /></div></td>
              <td className="p-3"><div>{purposeLabel(row.purpose)}</div><div className="mt-1">{row.task_id ? <button className="break-all text-left font-mono text-xs text-blue-700 hover:underline" title={`查看任务 ${row.task_id}`} onClick={() => void showTask(row)}>{row.task_id}</button> : <span className="text-xs text-muted-foreground">未归属任务</span>}</div></td>
              <td className="p-3"><div>{modelLabel(row)}</div><div className="text-xs text-muted-foreground">{endpointLabel(row.endpoint_alias)}</div></td>
              <td className="p-3 whitespace-nowrap">{tokenDetail(row)}</td>
              <td className="p-3"><div className="flex flex-wrap gap-1"><StatusBadge tone={statusTone(row.status)}>{invocationStatusLabel(row.status)}</StatusBadge>{row.usage_status !== 'reported' && <StatusBadge tone="warning">{row.usage_status === 'partial' ? '用量不完整' : '用量未知'}</StatusBadge>}{row.attempt_visibility === 'logical_only' && <StatusBadge tone="info">逻辑调用</StatusBadge>}</div><div className="mt-1">{row.price_version ? <CopyableTechnicalValue value={row.price_version} label="价格版本" /> : <StatusBadge tone="warning">{costStatusLabel(row.cost_status)}</StatusBadge>}</div></td>
              <td className="p-3 whitespace-nowrap">{row.cost_amount != null ? `${row.currency ?? ''} ${row.cost_amount}` : <StatusBadge tone="warning" title="未匹配价格或缺少用量记录；未知费用不等于 0">{costStatusLabel(row.cost_status)}</StatusBadge>}</td>
            </tr>)}
            {!busy && !visibleRows.length && <TableEmptyRow colSpan={6} title={diagnosticRows.length ? '当前页没有已返回用量的调用' : '当前范围没有调用记录'}>{diagnosticRows.length ? `当前页有 ${diagnosticRows.length} 条用量缺失的诊断记录，打开上方“显示诊断记录”查看。` : '调整时间范围或重置筛选后重试'}</TableEmptyRow>}
          </tbody></table></div>
          <div className="flex flex-wrap items-center gap-3"><Button variant="outline" disabled={!offset || busy} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><span className="text-sm text-muted-foreground">第 {offset / 50 + 1} 页 · 显示 {visibleRows.length} 条{diagnosticRows.length ? ` · 诊断记录 ${diagnosticRows.length} 条` : ''}</span><Button variant="outline" disabled={rows.length < 50 || busy} onClick={() => setOffset(offset + 50)}>下一页</Button></div>
        </AdminSection>

        <Dialog open={Boolean(task)} onOpenChange={(open) => { if (!open) setTask(null); }}>
          <DialogContent className="flex h-[min(760px,calc(100dvh-32px))] max-h-[calc(100dvh-32px)] w-[min(960px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-none">
            <DialogHeader className="shrink-0 border-b px-6 py-4 pr-14"><DialogTitle>任务执行尝试</DialogTitle></DialogHeader>
            {task && <div className="min-h-0 flex-1 overflow-y-auto p-6"><p className="break-all text-sm text-muted-foreground">{task.taskType} / {task.taskId}</p><h2 className="my-3 font-semibold">{number(task.summary.calls)} 次调用 · {amount(task.summary.known_amounts)}</h2><p className="mb-3 text-sm text-muted-foreground">未知费用：{number(task.summary.unknown_cost_calls)} · 当前页 {task.offset + 1}–{task.offset + task.invocations.length}</p><div className="overflow-x-auto rounded border"><table className="min-w-[760px] w-full text-left text-sm"><thead className="bg-muted"><tr><th className="p-2">调用</th><th className="p-2">时间</th><th className="p-2">状态</th><th className="p-2">用量</th><th className="p-2">费用</th></tr></thead><tbody>{task.invocations.map((row) => <tr className="border-t" key={row.invocation_id}><td className="max-w-[280px] break-all p-2 text-xs"><CopyableTechnicalValue value={row.invocation_id} /></td><td className="whitespace-nowrap p-2">{formatAbsoluteTime(row.started_at)}</td><td className="p-2"><StatusBadge tone={statusTone(row.status)}>{row.status}</StatusBadge></td><td className="p-2">{tokenDetail(row)}</td><td className="p-2">{row.cost_amount == null ? <StatusBadge tone="warning">{costStatusLabel(row.cost_status)}</StatusBadge> : `${row.currency ?? ''} ${row.cost_amount}`}</td></tr>)}</tbody></table></div><div className="mt-4 flex flex-wrap gap-2"><Button variant="outline" disabled={!task.offset || busy} onClick={() => void showTask({ task_type: task.taskType, task_id: task.taskId }, Math.max(0, task.offset - task.limit))}>上一页</Button><Button variant="outline" disabled={task.invocations.length < task.limit || busy} onClick={() => void showTask({ task_type: task.taskType, task_id: task.taskId }, task.offset + task.limit)}>下一页</Button></div></div>}
          </DialogContent>
        </Dialog>
      </AdminPageShell>
    </div>
  );
}
