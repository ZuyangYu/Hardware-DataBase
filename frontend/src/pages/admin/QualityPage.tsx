import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api } from '@/api/client';
import { createClientRequestId } from '@/api/documentAuthoring';
import type { QualityCase } from '@/api/types';
import type { AuthSession } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { notify } from '@/components/ui/app-toast';
import { AdminPageShell } from '@/components/admin/AdminPageShell';
import { StatusBadge } from '@/components/admin/StatusBadge';
import { LoadingState, RefreshingBadge, TableEmptyRow } from '@/components/admin/StateBlock';
import { CopyIconButton, CopyableTechnicalValue } from '@/components/admin/CopyableTechnicalValue';
import { formatAbsoluteTime, formatRelativeTime } from '@/lib/formatters';
import {
  QUALITY_CASE_FILTER_STATUS, QUALITY_REASONS, QUALITY_STATUS, QUALITY_STATUS_TONE,
  QUALITY_JUDGMENTS, QUALITY_ROOT_CAUSES, QUALITY_CORRECTNESS,
  QUALITY_TRIGGER_LABELS, QUALITY_FEEDBACK_RATING_LABELS, QUALITY_FEEDBACK_RATING_TONE,
  QUALITY_CONTENT_ACCESS_LABELS, QUALITY_CONTENT_ACCESS_TONE, QUALITY_PROJECTION_LABELS,
} from '@/lib/qualityLabels';
import { AdminRemediationGuide, CaseWorkflow, QualityReports, ReviewRequestApproval } from './QualityWorkflowPanel';

export default function QualityPage({ auth }: { auth: AuthSession; onLogout: () => void }) {
  const [params, setParams] = useSearchParams();
  const caseId = params.get('case');
  const [rows, setRows] = useState<QualityCase[]>([]);
  const [detail, setDetail] = useState<QualityCase | null>(null);
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [judgment, setJudgment] = useState('confirmed');
  const [correctness, setCorrectness] = useState('not_evaluated');
  const [rootCause, setRootCause] = useState('unknown');
  const [referenceAnswer, setReferenceAnswer] = useState('');
  const [notes, setNotes] = useState('');
  const [rubric, setRubric] = useState('');
  const [reviewKey, setReviewKey] = useState(createClientRequestId);
  const retryBody = useRef<{ key: string; body: Record<string, unknown> } | null>(null);
  // 草稿保护:轮询刷新只用服务器值回填未编辑过的字段;用户已动过的表单不被覆盖,
  // 改为提示案例有新版本,由用户保存或主动刷新后载入。保存成功或切换案例时解锁。
  const draftDirty = useRef(false);
  const detailVersion = useRef<number | null>(null);
  const [staleDetail, setStaleDetail] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    try { setRows(await api.get(`/api/v1/quality/cases?limit=50&offset=${offset}${status ? `&status=${status}` : ''}`)); }
    catch (error) { notify.error(error instanceof Error ? error.message : '加载质量案例失败'); }
    finally { setLoading(false); }
  }, [status, offset]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setReviewKey(createClientRequestId());
    setReferenceAnswer(''); setRubric(''); setNotes('');
    draftDirty.current = false;
    detailVersion.current = null;
    setStaleDetail(false);
    if (!caseId) return;
    let sequence = 0;
    const refresh = () => {
      const requestSequence = ++sequence;
      return api.get<QualityCase>(`/api/v1/quality/cases/${caseId}`).then((value) => {
        if (!cancelled && requestSequence === sequence) {
          if (draftDirty.current) {
            // 用户正在编辑:仅更新详情,不回填表单;仅当案例版本真的变化时提示。
            if (detailVersion.current !== null && value.row_version !== detailVersion.current) setStaleDetail(true);
          } else {
            const latest = value.reviews.filter((review) => !value.reviews.some((next) => next.supersedes === review.review_id)).slice(-1)[0];
            setReferenceAnswer(latest?.content.reference_answer ?? '');
            setRubric(latest?.content.rubric ?? '');
            setNotes(latest?.content.notes ?? '');
            setJudgment(latest?.judgment ?? 'confirmed');
            setCorrectness(latest?.correctness ?? 'not_evaluated');
            setRootCause(latest?.root_cause ?? 'unknown');
            setStaleDetail(false);
          }
          detailVersion.current = value.row_version;
          setDetail(value);
        }
      }).catch((error) => { if (!cancelled && requestSequence === sequence) { if (!draftDirty.current) setDetail(null); notify.error(error instanceof Error ? error.message : '加载案例失败'); } });
    };
    void refresh();
    const interval = window.setInterval(() => void refresh(), 15000);
    return () => { cancelled = true; window.clearInterval(interval); };
  }, [caseId]);
  async function reloadLatestVersion() {
    if (!caseId) return;
    try {
      const value = await api.get<QualityCase>(`/api/v1/quality/cases/${caseId}`);
      const latest = value.reviews.filter((review) => !value.reviews.some((next) => next.supersedes === review.review_id)).slice(-1)[0];
      setReferenceAnswer(latest?.content.reference_answer ?? '');
      setRubric(latest?.content.rubric ?? '');
      setNotes(latest?.content.notes ?? '');
      setJudgment(latest?.judgment ?? 'confirmed');
      setCorrectness(latest?.correctness ?? 'not_evaluated');
      setRootCause(latest?.root_cause ?? 'unknown');
      draftDirty.current = false;
      setStaleDetail(false);
      setReviewKey(createClientRequestId());
      setDetail(value);
      notify.success('已载入最新版本，未保存的草稿已被替换');
    } catch (error) { notify.error(error instanceof Error ? error.message : '载入最新版本失败'); }
  }
  async function saveReview() {
    if (!detail) return;
    setBusy(true);
    try {
      if (retryBody.current?.key !== reviewKey) retryBody.current = { key: reviewKey, body: { row_version: detail.row_version, client_request_id: reviewKey,
        judgment, correctness, root_cause: rootCause, reference_answer: referenceAnswer, notes, rubric,
        supersedes: detail.reviews[detail.reviews.length - 1]?.review_id ?? null } };
      await api.post(`/api/v1/quality/cases/${detail.case_id}/reviews`, retryBody.current.body);
      draftDirty.current = false;
      setStaleDetail(false);
      setReviewKey(createClientRequestId());
      try { setDetail(await api.get(`/api/v1/quality/cases/${detail.case_id}`)); } catch { setDetail(null); }
      await load();
      notify.success('已保存复核结论');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '复核提交失败');
      try { setDetail(await api.get(`/api/v1/quality/cases/${detail.case_id}`)); } catch { setDetail(null); }
    } finally { setBusy(false); }
  }
  return <div className="flex min-h-0 flex-1 flex-col">
    <AdminPageShell>
      <AppHeader title="质量工作台" description="生产反馈与案例复核:定位积压案例、提交复核结论并生成质量报告。" />
       <div className="flex flex-wrap items-center gap-3"><div className="min-w-0"><h2 className="font-semibold">案例队列</h2><p className="mt-1 text-xs text-muted-foreground">按状态定位积压案例，打开案例查看问题概览和修复门禁。</p></div>
         <div className="ml-auto flex flex-wrap items-center gap-2"><label className="flex items-center gap-2 text-sm">状态<select aria-label="案例状态" className="rounded-md border bg-white p-2 text-sm" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}><option value="">全部状态</option>{Object.entries(QUALITY_CASE_FILTER_STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
         {loading && rows.length > 0 && <RefreshingBadge label="正在更新列表" />}<Button variant="outline" disabled={loading} onClick={() => void load()}>{loading ? '刷新中…' : '刷新'}</Button></div>
       </div>
      <p className="text-sm text-muted-foreground">反馈与随机抽样分别记录。复核正文仅对原会话用户或被明确授权的员工开放。</p>
        <div className="overflow-x-auto rounded-lg border"><table className="min-w-[900px] w-full text-left text-sm">
         <thead className="bg-muted"><tr>{['案例', ...(auth.user.role === 'system_admin' ? ['部门'] : []), '触发来源', '用户反馈', '状态', '内容访问', '更新时间'].map((title) => <th className="p-3" key={title}>{title}</th>)}</tr></thead>
         <tbody>{loading && !rows.length ? <tr><td className="p-0" colSpan={auth.user.role === 'system_admin' ? 7 : 6}><LoadingState label="正在加载质量案例…" /></td></tr> : rows.map((r) => <tr className="border-t transition-colors hover:bg-muted/40" key={r.case_id}>
           <td className="group p-3"><button className="font-mono text-xs text-blue-700 hover:underline" title={r.case_id} onClick={() => setParams({ case: r.case_id })}>{r.case_id.slice(0, 17)}…</button><CopyIconButton value={r.case_id} label="案例 ID" /></td>
           {auth.user.role === 'system_admin' && <td className="p-3 whitespace-nowrap">{r.department_id == null ? '全局' : `部门 ${r.department_id}`}</td>}
          <td className="p-3">{r.triggers.map((t) => QUALITY_TRIGGER_LABELS[t] ?? t).join('、')}{r.sampling.probability != null && <div className="text-xs text-muted-foreground">入样概率 {r.sampling.probability * 100}%</div>}</td>
          <td className="p-3">{r.feedback ? <StatusBadge tone={QUALITY_FEEDBACK_RATING_TONE[r.feedback.rating] ?? 'neutral'}>{QUALITY_FEEDBACK_RATING_LABELS[r.feedback.rating] ?? r.feedback.rating}</StatusBadge> : '—'}<div className="mt-1 text-xs text-muted-foreground">{r.feedback?.reason_codes.map((c) => QUALITY_REASONS[c]).join('、')}</div></td>
          <td className="p-3"><StatusBadge tone={QUALITY_STATUS_TONE[r.status] ?? 'neutral'}>{QUALITY_STATUS[r.status] ?? r.status}</StatusBadge></td>
          <td className="p-3"><StatusBadge tone={QUALITY_CONTENT_ACCESS_TONE[r.content_access] ?? 'neutral'}>{QUALITY_CONTENT_ACCESS_LABELS[r.content_access] ?? r.content_access}</StatusBadge></td>
          <td className="p-3 whitespace-nowrap" title={formatAbsoluteTime(r.updated_at)}>{formatRelativeTime(r.updated_at)}</td>
        </tr>)}{!rows.length && !loading && <TableEmptyRow colSpan={auth.user.role === 'system_admin' ? 7 : 6} title="当前范围暂无案例">调整状态筛选或稍后刷新</TableEmptyRow>}</tbody>
      </table></div>
        <p className="mt-1 text-xs text-muted-foreground md:hidden">左右滑动查看全部列</p>
       <div className="flex flex-wrap items-center gap-3"><Button variant="outline" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><span className="text-sm text-muted-foreground">第 {offset / 50 + 1} 页 · 当前页 {rows.length} 条</span><Button variant="outline" disabled={rows.length < 50 || loading} onClick={() => setOffset(offset + 50)}>下一页</Button></div>
      <QualityReports />
    </AdminPageShell>
    <Dialog open={Boolean(caseId)} onOpenChange={(open) => { if (!open) setParams({}); }}><DialogContent className="flex h-[min(900px,calc(100dvh-32px))] max-h-[calc(100dvh-32px)] w-[min(960px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-none">
      <DialogHeader className="shrink-0 border-b border-[#edf0f4] px-[24px] py-[18px] pr-[56px]"><DialogTitle>固定案例与复核</DialogTitle></DialogHeader>
      {!detail ? <p className="p-[24px]">正在读取授权材料…</p> : <div className="min-h-0 flex-1 overflow-y-auto px-[24px] py-[20px]"><div className="grid gap-4 text-sm">
        <details className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground"><summary className="cursor-pointer font-medium">技术信息(默认折叠,可复制)</summary><div className="mt-2 grid gap-1">
          <div className="flex min-w-0 gap-2"><span className="w-16 shrink-0">案例</span><CopyableTechnicalValue value={detail.case_id} /></div>
          <div className="flex min-w-0 gap-2"><span className="w-16 shrink-0">快照</span><CopyableTechnicalValue value={detail.snapshot_id} /></div>
          <div className="flex min-w-0 gap-2"><span className="w-16 shrink-0">回答版本</span><CopyableTechnicalValue value={detail.answer_revision} /></div>
          <div className="flex min-w-0 gap-2"><span className="w-16 shrink-0">执行 attempt</span><CopyableTechnicalValue value={String(detail.manifest.execution_attempt_id || '历史记录未保存')} /></div>
          <div className="flex min-w-0 gap-2"><span className="w-16 shrink-0">Trace</span><CopyableTechnicalValue value={String(detail.manifest.trace_id || '无采样 trace')} /></div>
        </div></details>
        <div>状态：{QUALITY_STATUS[detail.status] ?? detail.status} · 版本 {detail.row_version}</div>
        {detail.projection_status && <p className="text-xs text-muted-foreground">Phoenix 标注：{detail.projection_status.map((s) => `${QUALITY_PROJECTION_LABELS[s.status] ?? s.status} ${s.count}`).join(' · ') || '暂无标注'}</p>}
         {detail.content_access === 'hidden' && <p className="rounded-md bg-muted p-3">当前仅有元数据访问权限，正文与证据未共享或共享已失效。</p>}
          {auth.user.role === 'system_admin' && <AdminRemediationGuide key={detail.case_id} detail={detail} />}
          {auth.user.role === 'employee' && detail.content_access === 'owner' && <ReviewRequestApproval key={`${detail.case_id}:${detail.review_requests?.find((item) => item.status === 'pending')?.request_id ?? ''}`} detail={detail} onRefresh={async () => { setDetail(await api.get<QualityCase>(`/api/v1/quality/cases/${detail.case_id}`)); await load(); }} />}
          <section className="rounded-md border border-amber-200 bg-amber-50/60 p-3">
           <h3 className="font-semibold">问题概览（管理员可见）</h3>
           {detail.feedback ? <div className="mt-2 grid gap-1">
             <p><span className="font-medium">用户反馈：</span>{QUALITY_FEEDBACK_RATING_LABELS[detail.feedback.rating] ?? detail.feedback.rating}</p>
             <p><span className="font-medium">反馈指出：</span>{detail.feedback.reason_codes.length ? detail.feedback.reason_codes.map((code) => QUALITY_REASONS[code]).join('、') : '未选择分类'}</p>
              <p className="text-xs text-muted-foreground">反馈时间：{formatAbsoluteTime(detail.feedback.created_at)}</p>
             {detail.content_access === 'hidden' && <p className="text-xs text-muted-foreground">补充说明原文仅对原用户或明确授权的员工开放。</p>}
           </div> : <p className="mt-2">本案例由{detail.triggers.map((trigger) => QUALITY_TRIGGER_LABELS[trigger] ?? trigger).join('、')}触发，尚无用户反馈。</p>}
           {(detail.feedback_history?.length ?? 0) > 1 && <div className="mt-3 border-t border-amber-200 pt-3"><p className="font-medium">反馈历史（仅元数据）</p><div className="mt-1 grid gap-1">{detail.feedback_history?.map((event) => <p className="text-xs" key={event.event_id}>第 {event.sequence} 次：{QUALITY_FEEDBACK_RATING_LABELS[event.rating] ?? event.rating} · {event.reason_codes.length ? event.reason_codes.map((code) => QUALITY_REASONS[code]).join('、') : '未选择分类'} · {formatAbsoluteTime(event.created_at)}</p>)}</div></div>}
           {detail.reviews.length > 0 && <div className="mt-3 border-t border-amber-200 pt-3">
             <p className="font-medium">当前复核结论</p>
             {detail.reviews.slice(-1).map((review) => <div className="mt-1 grid gap-1" key={review.review_id}>
               <p>判断：{QUALITY_JUDGMENTS[review.judgment] ?? review.judgment} · 根因：{QUALITY_ROOT_CAUSES[review.root_cause] ?? review.root_cause}</p>
               {review.correctness && <p>事实正确性：{QUALITY_CORRECTNESS[review.correctness] ?? review.correctness}</p>}
               <p className="text-xs text-muted-foreground">复核结论已记录；原始复核说明、参考答案和评分要点按权限保护。</p>
             </div>)}
           </div>}
           {detail.content_access === 'hidden' && <p className="mt-3 text-xs text-muted-foreground">需要判断具体问答内容时，请由原用户把选定的提问、回答或证据共享给同部门授权员工复核；管理员继续负责状态、报表和门禁监督。</p>}
         </section>
         {detail.content.query != null && <section><h3 className="font-semibold">固定提问</h3><p className="whitespace-pre-wrap break-words">{detail.content.query}</p></section>}
        {detail.content.answer != null && <section><h3 className="font-semibold">固定回答</h3><p className="whitespace-pre-wrap break-words">{detail.content.answer}</p></section>}
        {detail.feedback?.comment && <section><h3 className="font-semibold">补充说明</h3><p className="whitespace-pre-wrap">{detail.feedback.comment}</p></section>}
        {detail.content.evidence?.map((e, i) => <section className="rounded-md border p-3" key={i}><h3 className="font-semibold">复核证据 {i + 1}：{String(e.file_name ?? e.title ?? '')}</h3><p className="whitespace-pre-wrap break-words">{String(e.text ?? e.text_preview ?? '')}</p></section>)}
        {detail.reviews.map((r) => <section className="rounded-md border p-3" key={r.review_id}><h3 className="font-semibold">复核：{QUALITY_JUDGMENTS[r.judgment] ?? r.judgment} · {QUALITY_ROOT_CAUSES[r.root_cause] ?? r.root_cause}</h3><p className="whitespace-pre-wrap">{r.content.notes}</p><p className="whitespace-pre-wrap">{r.content.reference_answer}</p><span className="text-xs text-muted-foreground" title={formatAbsoluteTime(r.created_at)}>{formatRelativeTime(r.created_at)}</span></section>)}
        {detail.source_valid && detail.content.answer != null && auth.user.role === 'employee' && <fieldset disabled={busy} className="grid gap-3 border-t pt-4"><legend className="font-semibold">提交复核结论</legend>
          {staleDetail && <p className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">案例数据已更新（可能有新的复核被提交），你正在编辑的草稿未被覆盖。<button className="ml-1 underline" onClick={() => void reloadLatestVersion()}>载入最新版本</button>（将替换未保存草稿）</p>}
          <label>判断 <select className="rounded border p-2" value={judgment} onChange={(e) => { draftDirty.current = true; setJudgment(e.target.value); setReviewKey(createClientRequestId()); }}>{Object.entries(QUALITY_JUDGMENTS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>事实正确性 <select className="rounded border p-2" value={correctness} onChange={(e) => { draftDirty.current = true; setCorrectness(e.target.value); setReviewKey(createClientRequestId()); }}>{Object.entries(QUALITY_CORRECTNESS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label>根因 <select className="rounded border p-2" value={rootCause} onChange={(e) => { draftDirty.current = true; setRootCause(e.target.value); setReviewKey(createClientRequestId()); }}>{Object.entries(QUALITY_ROOT_CAUSES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label className="grid gap-1">参考答案（经确认后填写）<textarea className="rounded border p-2" maxLength={20000} value={referenceAnswer} onChange={(e) => { draftDirty.current = true; setReferenceAnswer(e.target.value); setReviewKey(createClientRequestId()); }} /></label>
          <label className="grid gap-1">评分要点 / rubric<textarea className="rounded border p-2" maxLength={4000} value={rubric} onChange={(e) => { draftDirty.current = true; setRubric(e.target.value); setReviewKey(createClientRequestId()); }} /></label>
          <label className="grid gap-1">复核说明<textarea className="rounded border p-2" maxLength={4000} value={notes} onChange={(e) => { draftDirty.current = true; setNotes(e.target.value); setReviewKey(createClientRequestId()); }} /></label>
          <Button onClick={() => void saveReview()}>保存复核结论</Button>
        </fieldset>}
        {detail.source_valid && detail.content_access !== 'hidden' && auth.user.role === 'employee' && <CaseWorkflow detail={detail} onRefresh={async () => { setDetail(await api.get(`/api/v1/quality/cases/${detail.case_id}`)); await load(); }} />}
      </div></div>}
    </DialogContent></Dialog>
  </div>;
}
