import { useEffect, useState } from 'react';
import { api } from '@/api/client';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { AdminSection } from '@/components/admin/AdminPageShell';
import { StatusBadge, type StatusTone } from '@/components/admin/StatusBadge';
import { EmptyState } from '@/components/admin/StateBlock';
import { CopyableTechnicalValue } from '@/components/admin/CopyableTechnicalValue';
import { formatAbsoluteTime, formatRelativeTime } from '@/lib/formatters';

type Budget = {
  policy: { budget_version: string; department_id: number | null; purpose: string | null; currency: string; amount_limit: string; effective_from: string; effective_to: string };
  active: boolean; known_amount: string; status: string; period_status: string;
  summary: { calls: number; unknown_cost_calls: number; price_coverage: number | null };
};
const statusLabels: Record<string, string> = { exceeded_known_amount: '已知金额超预算', no_recorded_calls: '无已记录调用', insufficient_cost_coverage: '未知费用，覆盖不足', below_limit_known_amount: '已知金额低于限额' };
const STATUS_TONE: Record<string, StatusTone> = { exceeded_known_amount: 'danger', insufficient_cost_coverage: 'warning', no_recorded_calls: 'neutral', below_limit_known_amount: 'success' };
const PERIOD: Record<string, { label: string; tone: StatusTone }> = {
  active: { label: '生效中', tone: 'success' },
  future: { label: '未生效', tone: 'warning' },
  ended: { label: '已结束', tone: 'neutral' },
};

/** 后端错误转中文提示:409/重复版本/时间重叠是最常见的注册失败。 */
function budgetErrorMessage(failure: unknown, fallback: string): string {
  const message = failure instanceof Error ? failure.message : '';
  if (/409|conflict|duplicate|already exists|已存在/i.test(message)) return '预算版本已存在，或与现有版本的有效期重叠；请更换版本号或调整有效期。';
  if (/overlap|重叠|range|interval/i.test(message)) return '有效期与已有预算版本重叠，请调整开始/结束时间。';
  if (/400|422|invalid/i.test(message)) return `预算参数无效:${message || '请检查版本号、币种、金额和有效期'}`;
  return message || fallback;
}

export function CostBudgetPanel({ canRegister }: { canRegister: boolean }) {
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [version, setVersion] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [amount, setAmount] = useState('');
  const [department, setDepartment] = useState('');
  const [purpose, setPurpose] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function load() {
    try { setBudgets(await api.get('/api/v1/costs/budgets')); setError(''); }
    catch (failure) { setError(budgetErrorMessage(failure, '读取预算失败')); }
  }
  useEffect(() => { void load(); }, []);
  async function register() {
    if (!start || !end || new Date(start) >= new Date(end)) {
      notify.error('预算结束时间必须晚于开始时间');
      return;
    }
    if (!amount || Number(amount) <= 0) {
      notify.error('预算金额必须大于 0');
      return;
    }
    setBusy(true);
    try {
      await api.post('/api/v1/costs/budgets', { budget_version: version, currency, amount_limit: amount,
        department_id: department ? Number(department) : null, purpose: purpose || null,
        effective_from: new Date(start).toISOString(), effective_to: new Date(end).toISOString() });
      await load();
      setVersion(''); setAmount(''); setDepartment(''); setPurpose(''); setStart(''); setEnd('');
      notify.success('预算版本已注册');
    } catch (failure) { notify.error(budgetErrorMessage(failure, '注册预算失败')); }
    finally { setBusy(false); }
  }
  return <AdminSection
    className="mb-5"
    title="版本化费用预算"
    description="预算只用于观测和告警，不会自动停用模型或中断请求。每个策略固定统计时间范围；已知金额为已记录调用的下界。未知费用和队列丢失时不能断言预算内，币种不换算。"
    actions={<Button variant="outline" disabled={busy} onClick={() => void load()}>刷新预算</Button>}
  >
    {error && <p className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700" role="alert">{error}</p>}
    {!budgets.length && !error && <EmptyState title="当前权限范围内尚未注册预算">预算用于观测与告警,不注册不影响调用</EmptyState>}
    {budgets.map((b) => {
      const period = PERIOD[b.period_status] ?? { label: b.period_status, tone: 'neutral' as StatusTone };
      const limit = Number(b.policy.amount_limit);
      const known = Number(b.known_amount);
      const utilization = Number.isFinite(limit) && limit > 0 && Number.isFinite(known) ? Math.min(known / limit, 1) : null;
      const utilizationLabel = utilization == null ? '—' : `${(utilization * 100).toFixed(1)}%`;
      const barTone = utilization == null ? 'bg-muted' : utilization >= 0.9 ? 'bg-red-500' : utilization >= 0.7 ? 'bg-amber-500' : 'bg-emerald-500';
      return <div className="grid gap-1 rounded-lg border p-3 text-sm" key={b.policy.budget_version}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-medium">{b.policy.budget_version}</h3>
          <StatusBadge tone={STATUS_TONE[b.status] ?? 'neutral'}>{statusLabels[b.status] ?? b.status}</StatusBadge>
          <StatusBadge tone={period.tone}>{period.label}</StatusBadge>
          <CopyableTechnicalValue className="ml-auto" value={b.policy.budget_version} label="版本号" />
        </div>
        <p>{b.policy.currency} {b.known_amount} / {b.policy.amount_limit} · 已知金额利用率 {utilizationLabel} · {b.summary.calls} 调用 · 未知费用 {b.summary.unknown_cost_calls}</p>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" role="presentation"><div className={cnBar(barTone)} style={{ width: `${(utilization ?? 0) * 100}%` }} /></div>
        <p className="text-xs text-muted-foreground">利用率为"已知金额/限额"的下界口径,未知费用补齐前可能低估 · {b.policy.department_id == null ? '全局' : `部门 ${b.policy.department_id}`} · {b.policy.purpose ?? '全部用途'} · <span title={formatAbsoluteTime(b.policy.effective_from)}>{formatRelativeTime(b.policy.effective_from)}</span> — {formatAbsoluteTime(b.policy.effective_to)}</p>
      </div>;
    })}
    {canRegister && <details><summary className="cursor-pointer font-medium">注册预算版本（管理员）</summary><form className="mt-3 grid gap-3 rounded-lg border bg-muted/20 p-4" onSubmit={(event) => { event.preventDefault(); void register(); }}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <label className="grid gap-1 text-sm">预算版本<input required className="min-w-0 rounded border p-2 text-sm" value={version} onChange={(e) => setVersion(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">预算币种<input required pattern="[A-Z]{3}" className="min-w-0 rounded border p-2 text-sm uppercase" value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} /></label>
        <label className="grid gap-1 text-sm">预算金额<input required inputMode="decimal" className="min-w-0 rounded border p-2 text-sm" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">预算部门 ID（空为全局）<input type="number" min="1" className="min-w-0 rounded border p-2 text-sm" value={department} onChange={(e) => setDepartment(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">预算用途（空为全部）<input className="min-w-0 rounded border p-2 text-sm" value={purpose} onChange={(e) => setPurpose(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">预算开始<input required type="datetime-local" className="min-w-0 rounded border p-2 text-sm" value={start} onChange={(e) => setStart(e.target.value)} /></label>
        <label className="grid gap-1 text-sm">预算结束<input required type="datetime-local" className="min-w-0 rounded border p-2 text-sm" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
      </div>
      <p className="text-xs text-muted-foreground">预算维度固定为"部门 + 用途 + 币种";本阶段不支持按模型(model_alias)限定,需要时请在治理需求中单独提出。注册后不可变:更新请注册新版本。</p>
      <div><Button disabled={busy} type="submit">{busy ? '注册中…' : '注册不可变预算'}</Button></div>
    </form></details>}
  </AdminSection>;
}

/** 利用率进度条底色类(基于后端无关的前端分档,仅视觉提示)。 */
function cnBar(toneClass: string): string {
  return `h-full rounded-full transition-[width] ${toneClass}`;
}
