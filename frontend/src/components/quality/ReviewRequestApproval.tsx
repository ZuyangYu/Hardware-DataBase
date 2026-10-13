import { useEffect, useState } from 'react';

import { api, ApiError } from '@/api/client';
import type { QualityCase } from '@/api/types';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { ErrorState, LoadingState } from '@/components/admin/StateBlock';

/** Owner-side approval surface used from both chat feedback and the workbench. */
export default function ReviewRequestApproval({ detail, onRefresh }: { detail: QualityCase; onRefresh: () => Promise<void> }) {
  const pending = detail.review_requests?.find((item) => item.status === 'pending');
  const [reviewers, setReviewers] = useState<{ id: number; username: string }[]>([]);
  const [selectedReviewers, setSelectedReviewers] = useState<number[]>([]);
  const [fields, setFields] = useState<string[]>(['query', 'answer']);
  const [evidence, setEvidence] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(detail.row_version);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    setVersion(detail.row_version);
    setSelectedReviewers([]);
    setFields(['query', 'answer']);
    setEvidence([]);
  }, [detail.case_id, pending?.request_id]);

  useEffect(() => {
    if (!pending) return;
    let cancelled = false;
    setLoading(true);
    setLoadError('');
    void api.get<{ id: number; username: string }[]>('/api/v1/quality/reviewers').then((value) => {
      if (!cancelled) setReviewers(value);
    }).catch(() => {
      if (!cancelled) setLoadError('读取同部门复核人员失败');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [detail.case_id, pending?.request_id, attempt]);

  if (!pending) return null;
  const activeRequest = pending;
  const stale = version !== detail.row_version;
  const canShare = detail.source_valid && detail.content_access === 'owner' && detail.manifest.content_available !== false;
  const toggle = <T,>(items: T[], value: T) => items.includes(value) ? items.filter((item) => item !== value) : [...items, value];

  async function approve() {
    if (busy || stale || !canShare || loading || loadError || !selectedReviewers.length || !fields.length) return;
    setBusy(true);
    try {
      await api.post(`/api/v1/quality/cases/${detail.case_id}/review-requests/${activeRequest.request_id}/approve`, {
        row_version: version, reviewer_user_ids: selectedReviewers, fields, evidence_indices: evidence, retention_days: 7,
      });
      notify.success('已批准并共享固定复核材料');
      await onRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '批准复核请求失败');
      if (error instanceof ApiError && error.status === 409) {
        try { await onRefresh(); } catch { /* Keep the draft; a later retry can refresh. */ }
      }
    }
    finally { setBusy(false); }
  }

  async function reject() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/api/v1/quality/cases/${detail.case_id}/review-requests/${activeRequest.request_id}/reject`, { row_version: detail.row_version, decision_note: '暂不共享复核材料' });
      notify.success('已拒绝复核请求');
      await onRefresh();
    } catch (error) { notify.error(error instanceof Error ? error.message : '拒绝复核请求失败'); }
    finally { setBusy(false); }
  }

  return <section className="grid gap-3 rounded-md border border-blue-200 bg-blue-50/60 p-3 text-left">
    <div><h3 className="font-semibold">待处理的复核请求</h3><p className="mt-1 text-sm">管理员请求你选择性共享固定材料。共享副本有效 7 天，可在聊天反馈区撤销；批准将替换现有共享范围。</p>{pending.message && <p className="mt-1 break-words text-xs text-muted-foreground">请求说明：{pending.message}</p>}</div>
    {!canShare && <p className="text-sm">固定材料已不可共享，仍可拒绝请求。</p>}
    {stale && <div className="grid gap-2 text-sm"><p>案例已更新，请重新确认授权范围。</p><Button variant="outline" disabled={busy} onClick={() => { setVersion(detail.row_version); setSelectedReviewers([]); setFields(['query', 'answer']); setEvidence([]); }}>重新确认授权范围</Button></div>}
    {loading && <LoadingState label="正在读取同部门复核人员…" />}
    {loadError && <ErrorState message={loadError} onRetry={() => setAttempt((value) => value + 1)} />}
    <fieldset disabled={busy || stale || !canShare || loading || Boolean(loadError)} className="grid gap-2"><legend className="text-sm font-medium">复核人员</legend>{!loading && !loadError && (reviewers.length ? reviewers.map((reviewer) => <label className="flex items-center gap-2 text-sm" key={reviewer.id}><input type="checkbox" aria-label={reviewer.username} checked={selectedReviewers.includes(reviewer.id)} onChange={() => setSelectedReviewers(toggle(selectedReviewers, reviewer.id))} />{reviewer.username}</label>) : <p className="text-sm text-muted-foreground">暂无可选的同部门复核员工。</p>)}</fieldset>
    <fieldset disabled={busy || stale || !canShare} className="grid gap-2"><legend className="text-sm font-medium">材料范围</legend>{([['query', '固定提问'], ['answer', '固定回答'], ['comment', '反馈补充说明']] as const).map(([value, label]) => <label className="flex items-center gap-2 text-sm" key={value}><input type="checkbox" aria-label={label} checked={fields.includes(value)} onChange={() => setFields(toggle(fields, value))} />{label}</label>)}{(detail.content.evidence ?? []).map((item, index) => <label className="flex min-w-0 items-center gap-2 break-words text-sm" key={index}><input className="shrink-0" type="checkbox" aria-label={`证据 ${index + 1}`} checked={evidence.includes(index)} onChange={() => setEvidence(toggle(evidence, index))} />证据 {index + 1} {String(item.file_name ?? item.title ?? '')}</label>)}</fieldset>
    <div className="flex flex-wrap gap-2"><Button disabled={busy || stale || !canShare || loading || Boolean(loadError) || !selectedReviewers.length || !fields.length} onClick={() => void approve()}>批准并共享复核材料</Button><Button variant="outline" disabled={busy} onClick={() => void reject()}>暂不共享</Button></div>
  </section>;
}
