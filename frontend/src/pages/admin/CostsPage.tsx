import { useCallback, useEffect, useRef, useState } from 'react';
import { api, apiDownload } from '@/api/client';
import type { AuthSession } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { StatCard } from '@/components/StatCard';
import { notify } from '@/components/ui/app-toast';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { CostBudgetPanel } from './CostBudgetPanel';
import { AdminPageShell, AdminPageTitle } from '@/components/admin/AdminPageShell';
import { StatusBadge } from '@/components/admin/StatusBadge';
import { LoadingState, RefreshingBadge, TableEmptyRow } from '@/components/admin/StateBlock';
import { CopyableTechnicalValue } from '@/components/admin/CopyableTechnicalValue';

type Call = {
  invocation_id: string; task_type: string; task_id: string; purpose: string; started_at: string;
  requested_model: string; response_model: string; endpoint_alias: string; status: string;
  input_tokens: number | null; output_tokens: number | null; cost_status: string; cost_amount: string | null;
  currency: string | null; attempt_visibility: string; price_version: string | null;
};
type Summary = {
  calls: number; known_amounts: Record<string, string>; unknown_cost_calls: number;
  usage_coverage: number | null; price_coverage: number | null; logical_only_calls: number;
  unfinished_calls: number; unattributed_calls: number; updated_at: string | null; scope: string;
  by_purpose: Record<string, { calls: number; known_amounts: Record<string, string> }>;
};
type Trends = {
  buckets: (Summary & { bucket_start: string })[];
  rankings_by_currency: Record<string, { endpoint_alias: string; model_alias: string; requested_model: string; response_model: string; known_amount: string; calls: number; price_coverage: number | null }[]>;
};
type Task = { summary: Summary; invocations: Call[]; limit: number; offset: number; taskType: string; taskId: string };
const purposes = ['chat', 'authoring', 'memory', 'ingestion', 'evaluation_collection', 'evaluation_judge', 'quality_analysis', 'unattributed'];
const amount = (values: Record<string, string>) => Object.entries(values).map(([currency, n]) => `${currency} ${n}`).join(' · ') || '无已知金额';
const percentage = (value: number | null | undefined) => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const STATUS_TONE: Record<string, 'success' | 'danger' | 'warning' | 'info' | 'neutral'> = {
  success: 'success', succeeded: 'success', complete: 'success', completed: 'success', ok: 'success',
  failed: 'danger', error: 'danger', timeout: 'danger',
  pending: 'warning', running: 'info', unfinished: 'warning', unknown: 'neutral',
};
const statusTone = (status: string) => STATUS_TONE[status] ?? 'neutral';

export default function CostsPage(props: { auth: AuthSession; onLogout: () => void }) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rows, setRows] = useState<Call[]>([]);
  const [purpose, setPurpose] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [modelAlias, setModelAlias] = useState('');
  const [projectId, setProjectId] = useState('');
  const [departmentId, setDepartmentId] = useState('');
  const [appliedFilters, setAppliedFilters] = useState({ purpose: '', start: '', end: '', modelAlias: '', projectId: '', departmentId: '' });
  const [trends, setTrends] = useState<Trends | null>(null);
  const [trendError, setTrendError] = useState('');
  const [offset, setOffset] = useState(0);
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const requestSequence = useRef(0);
  const params = new URLSearchParams();
  if (appliedFilters.purpose) params.set('purpose', appliedFilters.purpose);
  if (appliedFilters.start) params.set('start', new Date(appliedFilters.start).toISOString());
  if (appliedFilters.end) params.set('end', new Date(appliedFilters.end).toISOString());
  if (appliedFilters.modelAlias) params.set('model_alias', appliedFilters.modelAlias);
  if (appliedFilters.projectId) params.set('project_id', appliedFilters.projectId);
  if (appliedFilters.departmentId) params.set('department_id', appliedFilters.departmentId);
  const query = params.toString();
  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setBusy(true);
    // 刷新不清空已有摘要/表格:保留旧数据避免"暂无调用"假空态,由 busy
    // 标记显示"正在更新";仅首次加载(尚无数据)走骨架/— 空态。
    try {
      const [data, calls, series] = await Promise.all([api.get<Summary>(`/api/v1/costs/summary?${query}`), api.get<Call[]>(`/api/v1/costs/invocations?${query}&limit=50&offset=${offset}`),
        api.get<Trends>(`/api/v1/costs/trends?${query}`).catch((error) => { if (sequence === requestSequence.current) setTrendError(error instanceof Error ? error.message : '趋势读取失败'); return null; })]);
      if (sequence === requestSequence.current) { setSummary(data); setRows(calls); setTrends(series); }
    } catch (error) { if (sequence === requestSequence.current) notify.error(error instanceof Error ? error.message : '读取费用账本失败'); }
    finally { if (sequence === requestSequence.current) setBusy(false); }
  }, [query, offset]);
  useEffect(() => { void load(); }, [load]);
  async function exportCsv() {
    try {
      await apiDownload.blob(`/api/v1/costs/export?${query}`, 'llm-costs.csv');
    } catch (error) { notify.error(error instanceof Error ? error.message : '导出失败'); }
  }
  function applyFilters() {
    if (start && end && new Date(start) >= new Date(end)) {
      notify.error('结束时间必须晚于开始时间');
      return;
    }
    setOffset(0);
    setAppliedFilters({ purpose, start, end, modelAlias, projectId, departmentId });
  }
  function resetFilters() {
    setPurpose(''); setStart(''); setEnd(''); setModelAlias(''); setProjectId(''); setDepartmentId(''); setOffset(0);
    setAppliedFilters({ purpose: '', start: '', end: '', modelAlias: '', projectId: '', departmentId: '' });
  }
  async function showTask(row: Pick<Call, 'task_type' | 'task_id'>, taskOffset = 0) {
    if (!row.task_id) return;
    try {
      const detail = await api.get<Omit<Task, 'taskType' | 'taskId'>>(`/api/v1/costs/tasks/${encodeURIComponent(row.task_type)}/${encodeURIComponent(row.task_id)}?limit=100&offset=${taskOffset}`);
      setTask({ ...detail, taskType: row.task_type, taskId: row.task_id });
    }
    catch (error) { notify.error(error instanceof Error ? error.message : '任务费用读取失败'); }
  }
  return <div className="flex min-h-0 flex-1 flex-col">
    <AppHeader title="成本与用量" description="共享调用账本 · 应用 token 费用测算 · 按币种分别合计" />
    <AdminPageShell>
    <section className="my-5 rounded-xl border bg-white p-4 shadow-sm"><div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div><h2 className="font-semibold">筛选调用账本</h2><p className="text-xs text-muted-foreground">先填写筛选条件，再点击应用；避免每次输入都刷新账本。</p></div><span className="text-xs text-muted-foreground">时间按本地输入转换为 UTC 查询</span></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <label className="grid gap-1 text-sm">开始时间<input className="min-w-0 rounded border p-2 text-sm" type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">结束时间<input className="min-w-0 rounded border p-2 text-sm" type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">调用用途<select aria-label="调用用途" className="rounded border bg-white p-2 text-sm" value={purpose} onChange={(e) => setPurpose(e.target.value)}><option value="">全部用途</option>{purposes.map((p) => <option key={p}>{p}</option>)}</select></label>
        <label className="grid gap-1 text-sm">模型别名<input className="min-w-0 rounded border p-2 text-sm" value={modelAlias} onChange={(e) => setModelAlias(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">项目 ID<input className="min-w-0 rounded border p-2 text-sm" value={projectId} onChange={(e) => setProjectId(e.target.value)} /></label>
        {props.auth.user.role === 'system_admin' && <label className="grid gap-1 text-sm">部门 ID<input type="number" min="1" className="min-w-0 rounded border p-2 text-sm" value={departmentId} onChange={(e) => setDepartmentId(e.target.value)} /></label>}
      </div>
      <div className="mt-4 flex flex-wrap gap-2"><Button disabled={busy} onClick={applyFilters}>应用筛选</Button><Button variant="outline" disabled={busy} onClick={resetFilters}>重置</Button><Button variant="outline" disabled={busy} onClick={() => void load()}>刷新</Button><Button variant="outline" disabled={busy} onClick={() => void exportCsv()}>导出 CSV</Button></div>
      {(query || busy) && <p className="mt-3 text-xs text-muted-foreground">{busy ? '正在读取已应用筛选…' : `当前筛选：${[appliedFilters.purpose, appliedFilters.modelAlias, appliedFilters.projectId, appliedFilters.departmentId].filter(Boolean).join(' · ') || '时间范围'}`}</p>}
    </section>
    <div className="mb-3 flex items-center justify-between gap-3">
      <AdminPageTitle title="账本摘要" description="全部金额为应用 token 测算,尚未与供应商结算账单对账;覆盖率为已记录调用口径。" />
      {busy && summary && <RefreshingBadge label="正在更新账本" />}
    </div>
    <div className="flex flex-wrap gap-3">
      <StatCard label="已知金额（未对账）" value={summary ? amount(summary.known_amounts) : '—'} title="已记录调用的应用 token 测算金额,尚未与供应商结算账单对账" />
      <StatCard label="调用数" value={summary?.calls ?? '—'} title="当前筛选范围内已记录的调用" />
      <StatCard label="未知费用调用" value={summary?.unknown_cost_calls ?? '—'} title="未匹配价格或缺少用量记录、金额未知的调用;未知不等于 0" />
      <StatCard label="Usage 覆盖率" value={summary ? percentage(summary.usage_coverage) : '—'} title="有用量记录的调用占比;分母为已记录调用,不含被队列丢弃的调用" />
      <StatCard label="计价覆盖率" value={summary ? percentage(summary.price_coverage) : '—'} title="匹配到当前价格版本的调用占比" />
    </div>
    <p className="my-4 text-xs text-muted-foreground">统计范围：{summary?.scope ?? '读取中'} · 更新时间：{summary?.updated_at ? new Date(summary.updated_at).toLocaleString() : '无数据'} · SDK 内部重试未逐请求可见：{summary?.logical_only_calls ?? 0} · 未结束：{summary?.unfinished_calls ?? 0} · 未归属：{summary?.unattributed_calls ?? 0}</p>
    <p className="mb-4 text-sm text-muted-foreground">覆盖率分母为已记录调用；队列丢弃告警代表仍有未记录调用，不能据此断言全量覆盖。未知费用保留为空。reasoning 已包含在 output 时不重复计价；多个币种分别显示。该账本尚未与供应商结算账单对账。</p>
    <div className="mb-5 flex flex-wrap gap-3">{Object.entries(summary?.by_purpose ?? {}).map(([p, item]) => <div className="rounded border p-3 text-sm" key={p}>{p}：{item.calls} 调用 · {amount(item.known_amounts)}</div>)}</div>
    <section className="mb-5 grid gap-4 rounded-xl border bg-white p-4 shadow-sm"><div><h2 className="font-semibold">费用趋势与模型排行</h2><p className="text-xs text-muted-foreground">趋势使用 UTC 日桶；空桶省略，不把未记录用量推定为零。最多展示 10000 次调用。</p></div>{trendError && <p className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-700">{trendError}</p>}<div className="grid gap-5 xl:grid-cols-2"><div className="min-w-0"><h3 className="mb-2 text-sm font-medium">每日趋势</h3><div className="overflow-x-auto rounded border"><table className="min-w-[620px] w-full text-left text-sm"><thead className="bg-muted"><tr><th className="p-2">UTC 日期</th><th className="p-2 text-right">调用</th><th className="p-2 text-right">已知金额</th><th className="p-2 text-right">未知</th><th className="p-2 text-right">计价覆盖</th></tr></thead><tbody>{trends?.buckets.map((item) => <tr className="border-t" key={item.bucket_start}><td className="p-2">{item.bucket_start.slice(0, 10)}</td><td className="p-2 text-right">{item.calls}</td><td className="p-2 text-right whitespace-nowrap">{amount(item.known_amounts)}</td><td className="p-2 text-right">{item.unknown_cost_calls}</td><td className="p-2 text-right">{percentage(item.price_coverage)}</td></tr>)}{!trends?.buckets.length && <tr><td className="p-6 text-center text-muted-foreground" colSpan={5}>当前范围暂无趋势数据</td></tr>}</tbody></table></div></div><div className="min-w-0"><h3 className="mb-2 text-sm font-medium">模型已知费用排行（按币种）</h3><div className="grid gap-3">{Object.entries(trends?.rankings_by_currency ?? {}).map(([currency, ranking]) => <div className="overflow-x-auto rounded border" key={currency}><table className="min-w-[620px] w-full text-left text-sm"><caption className="bg-muted p-2 text-left font-medium">{currency}</caption><thead className="bg-muted/60"><tr><th className="p-2">模型</th><th className="p-2">已知金额</th><th className="p-2">调用</th><th className="p-2">计价覆盖</th></tr></thead><tbody>{ranking.map((item, index) => <tr className="border-t" key={`${item.endpoint_alias}:${item.requested_model}:${item.response_model}`}><td className="max-w-[280px] p-2"><span className="mr-2 text-muted-foreground">{index + 1}.</span>{item.model_alias} / {item.response_model || item.requested_model}</td><td className="p-2">{currency} {item.known_amount}</td><td className="p-2">{item.calls}</td><td className="p-2">{percentage(item.price_coverage)}</td></tr>)}</tbody></table></div>)}{!Object.keys(trends?.rankings_by_currency ?? {}).length && <p className="rounded border p-6 text-center text-muted-foreground">当前范围暂无排行数据</p>}</div></div></div></section>
    <CostBudgetPanel canRegister={props.auth.user.role === 'system_admin'} />
    <div className="overflow-x-auto rounded-xl border bg-white shadow-sm"><table className="min-w-[1000px] w-full text-left text-sm"><thead className="bg-muted"><tr>{['时间 / 调用', '用途 / 任务', '模型 / 端点', '输入 / 输出', '状态 / 价格版本', '费用'].map((v) => <th className="whitespace-nowrap p-3" key={v}>{v}</th>)}</tr></thead><tbody>
      {busy && !rows.length ? <tr><td className="p-0" colSpan={6}><LoadingState label="正在读取调用明细…" /></td></tr> : rows.map((r) => {
        const usage = `${r.input_tokens ?? '未知'} / ${r.output_tokens ?? '未知'}`;
        return <tr className="border-t" key={r.invocation_id}>
          <td className="p-3">{new Date(r.started_at).toLocaleString()}<div className="mt-1"><CopyableTechnicalValue value={r.invocation_id} display={`${r.invocation_id.slice(0, 17)}…`} label="调用" /></div></td>
          <td className="p-3">{r.purpose}<div className="mt-1">{r.task_id ? <button className="font-mono text-xs text-blue-700 hover:underline" title={`查看任务 ${r.task_id}`} onClick={() => void showTask(r)}>{r.task_id}</button> : <span className="text-xs text-muted-foreground" title="该调用未归属到任务">未归属</span>}</div></td>
          <td className="p-3">{r.response_model || r.requested_model}<div className="text-xs text-muted-foreground">{r.endpoint_alias}</div></td>
          <td className="p-3 whitespace-nowrap">{usage}</td>
          <td className="p-3"><StatusBadge tone={statusTone(r.status)}>{r.status}</StatusBadge><div className="mt-1">{r.price_version ? <CopyableTechnicalValue value={r.price_version} label="价格版本" /> : <span className="text-xs text-muted-foreground">无匹配价格</span>}</div></td>
          <td className="p-3 whitespace-nowrap">{r.cost_amount != null ? `${r.currency ?? ''} ${r.cost_amount}` : <StatusBadge tone="warning" title="未匹配价格或缺少用量记录;未知费用不等于 0 元">{r.cost_status || '未知费用'}</StatusBadge>}</td>
        </tr>;
      })}
      {!busy && !rows.length && <TableEmptyRow colSpan={6} title="当前范围没有调用记录">调整时间范围或重置筛选后重试</TableEmptyRow>}</tbody></table></div>
    <p className="mt-1 text-xs text-muted-foreground md:hidden">左右滑动查看全部列</p>
    <div className="mt-4 flex gap-3"><Button variant="outline" disabled={!offset || busy} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><span>第 {offset / 50 + 1} 页</span><Button variant="outline" disabled={rows.length < 50 || busy} onClick={() => setOffset(offset + 50)}>下一页</Button></div>
    <Dialog open={Boolean(task)} onOpenChange={(open) => { if (!open) setTask(null); }}><DialogContent className="flex h-[min(760px,calc(100dvh-32px))] max-h-[calc(100dvh-32px)] w-[min(960px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-none"><DialogHeader className="shrink-0 border-b px-6 py-4 pr-14"><DialogTitle>任务执行尝试</DialogTitle></DialogHeader>{task && <div className="min-h-0 flex-1 overflow-y-auto p-6"><p className="break-all text-sm text-muted-foreground">{task.taskType} / {task.taskId}</p><h2 className="my-3 font-semibold">{task.summary.calls} 调用 · {amount(task.summary.known_amounts)}</h2><p className="mb-3 text-sm">未知费用调用：{task.summary.unknown_cost_calls} · 当前页 {task.offset + 1}–{task.offset + task.invocations.length}</p><div className="overflow-x-auto rounded border"><table className="min-w-[760px] w-full text-left text-sm"><thead className="bg-muted"><tr><th className="p-2">调用</th><th className="p-2">时间</th><th className="p-2">状态</th><th className="p-2">用量</th><th className="p-2">费用</th></tr></thead><tbody>{task.invocations.map((r) => <tr className="border-t" key={r.invocation_id}><td className="max-w-[280px] break-all p-2 text-xs">{r.invocation_id}</td><td className="whitespace-nowrap p-2">{new Date(r.started_at).toLocaleString()}</td><td className="p-2">{r.status}</td><td className="p-2">{r.input_tokens ?? '未知'} / {r.output_tokens ?? '未知'}</td><td className="p-2">{r.cost_amount == null ? r.cost_status : `${r.currency} ${r.cost_amount}`}</td></tr>)}</tbody></table></div><div className="mt-4 flex flex-wrap gap-2"><Button variant="outline" disabled={!task.offset || busy} onClick={() => void showTask({ task_type: task.taskType, task_id: task.taskId }, Math.max(0, task.offset - task.limit))}>上一批尝试</Button><Button variant="outline" disabled={task.offset + task.invocations.length >= task.summary.calls || busy} onClick={() => void showTask({ task_type: task.taskType, task_id: task.taskId }, task.offset + task.limit)}>下一批尝试</Button></div></div>}</DialogContent></Dialog>
    </AdminPageShell>
  </div>;
}
