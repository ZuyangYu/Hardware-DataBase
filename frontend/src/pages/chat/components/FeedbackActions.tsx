import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError } from '@/api/client';
import { createClientRequestId } from '@/api/documentAuthoring';
import type { MessageView, QualityCase, QualityFeedbackState, QualityRating, QualityReason } from '@/api/types';
import ReviewRequestApproval from '@/components/quality/ReviewRequestApproval';
import { QUALITY_REASONS, QUALITY_STATUS } from '@/lib/qualityLabels';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { notify } from '@/components/ui/app-toast';

// 标签单一来源迁移到 src/lib/qualityLabels:此处 re-export 保持既有
// import 路径兼容(聊天与后台工作台共用同一份中文标签与状态口径)。
export { QUALITY_REASONS, QUALITY_STATUS } from '@/lib/qualityLabels';

export default function FeedbackActions({ msg }: { msg: MessageView }) {
  const [state, setState] = useState<QualityFeedbackState | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [negativeOpen, setNegativeOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [reasons, setReasons] = useState<QualityReason[]>([]);
  const [comment, setComment] = useState('');
  const [reviewers, setReviewers] = useState<{ id: number; username: string }[]>([]);
  const [reviewCase, setReviewCase] = useState<QualityCase | null>(null);
  const [reviewCaseError, setReviewCaseError] = useState(false);
  const [reviewCaseAttempt, setReviewCaseAttempt] = useState(0);
  const [selectedReviewers, setSelectedReviewers] = useState<number[]>([]);
  const [fields, setFields] = useState<string[]>(['query', 'answer']);
  const [evidence, setEvidence] = useState<number[]>([]);
  const retry = useRef<{ signature: string; client_request_id: string } | null>(null);
  const path = `/api/v1/chat/turns/${encodeURIComponent(msg.turn_id ?? '')}/feedback`;
  const eligible = Boolean(msg.turn_id && msg.id > 0 && !msg.redacted && !msg.edited_at);
  const pendingRequestId = state?.review_requests?.find((item) => item.status === 'pending')?.request_id;

  useEffect(() => {
    let cancelled = false;
    setState(null);
    setReviewCase(null);
    setLoadError('');
    setLoading(false);
    if (!eligible) return;
    setLoading(true);
    api.get<QualityFeedbackState>(path).then((value) => {
      if (!cancelled && value.assistant_message_id === msg.id) setState(value);
    }).catch((error) => {
      if (!cancelled) setLoadError(feedbackLoadError(error));
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [path, msg.id, eligible, loadAttempt]);

  useEffect(() => {
    let cancelled = false;
    setReviewCase(null);
    setReviewCaseError(false);
    if (!state?.case_id || !pendingRequestId) return () => { cancelled = true; };
    void api.get<QualityCase>(`/api/v1/quality/cases/${encodeURIComponent(state.case_id)}`).then((value) => {
      if (!cancelled) setReviewCase(value);
    }).catch(() => {
      if (!cancelled) setReviewCaseError(true);
    });
    return () => { cancelled = true; };
  }, [state?.case_id, pendingRequestId, reviewCaseAttempt]);

  async function refresh() { setState(await api.get<QualityFeedbackState>(path)); }
  async function submit(rating: QualityRating) {
    if (!state || busy) return;
    setBusy(true);
    const body = { assistant_message_id: msg.id, answer_revision: state.answer_revision, rating,
      reason_codes: rating === 'unhelpful' ? reasons : [], comment: rating === 'unhelpful' ? comment : '' };
    const signature = JSON.stringify(body);
    if (retry.current?.signature !== signature) retry.current = { signature, client_request_id: createClientRequestId() };
    try {
      await api.post(path, { ...body, client_request_id: retry.current.client_request_id });
      await refresh();
      retry.current = null;
      setNegativeOpen(false);
    } catch (error) { notify.error(error instanceof Error ? error.message : '提交反馈失败'); }
    finally { setBusy(false); }
  }

  async function openShare() {
    setBusy(true);
    try {
      await refresh();
      setReviewers(await api.get('/api/v1/quality/reviewers'));
      setSelectedReviewers([]);
      setFields(['query', 'answer']);
      setEvidence([]);
      setShareOpen(true);
    } catch (error) { notify.error(error instanceof Error ? error.message : '加载复核人员失败'); }
    finally { setBusy(false); }
  }
  async function saveShare(revoke = false) {
    if (!state?.case_id || !state.row_version) return;
    setBusy(true);
    try {
      await api.post(`/api/v1/quality/cases/${state.case_id}/share${revoke ? '/revoke' : ''}`, revoke
        ? { row_version: state.row_version }
        : { row_version: state.row_version, reviewer_user_ids: selectedReviewers, fields, evidence_indices: evidence, retention_days: 7 });
      await refresh();
      setShareOpen(false);
      notify.success(revoke ? '已撤销复核材料共享' : '已向指定员工共享固定复核副本（7 天）');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新复核共享失败');
      try { await refresh(); } catch { /* A later reload restores server state. */ }
    }
    finally { setBusy(false); }
  }
  if (!eligible) return null;
  if (!state) return <div className="flex flex-wrap items-center gap-3 text-[11px] text-[#858b9c]">
    <span role="status">{loading ? '正在加载回答反馈…' : loadError || '该回答暂未提供反馈入口'}</span>
    {!loading && <button type="button" className="hover:underline" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>重试加载反馈</button>}
  </div>;
  const activeShare = state.share.active && (state.share.expires_at ?? '') > new Date().toISOString();
  const toggle = <T,>(items: T[], value: T) => items.includes(value) ? items.filter((item) => item !== value) : [...items, value];
  return (
    <div className="flex flex-wrap items-center gap-3 text-[11px] text-[#858b9c]">
      <button disabled={busy} type="button" aria-pressed={state.rating === 'helpful'} className={state.rating === 'helpful' ? 'font-semibold text-blue-700' : ''} onClick={() => void submit(state.rating === 'helpful' ? 'withdrawn' : 'helpful')}>有帮助</button>
      <button disabled={busy} type="button" aria-pressed={state.rating === 'unhelpful'} className={state.rating === 'unhelpful' ? 'font-semibold text-blue-700' : ''} onClick={() => { setReasons(state.reason_codes); setComment(state.comment); setNegativeOpen(true); }}>没帮助</button>
      {state.rating && state.rating !== 'withdrawn' && <button disabled={busy} type="button" onClick={() => void submit('withdrawn')}>撤销反馈</button>}
      {state.case_id && <>
        <Link className="hover:underline" to={`/admin/quality?case=${state.case_id}`}>{QUALITY_STATUS[state.status ?? ''] ?? state.status}</Link>
        {state.content_available && <button disabled={busy} type="button" onClick={() => void openShare()}>提交复核材料</button>}
        {activeShare && <button disabled={busy} type="button" onClick={() => void saveShare(true)}>撤销材料共享</button>}
        {pendingRequestId && !reviewCase && (reviewCaseError
          ? <span role="status">读取复核请求失败，<button type="button" className="underline" onClick={() => setReviewCaseAttempt((value) => value + 1)}>重试读取复核请求</button></span>
          : <span role="status">正在加载复核请求…</span>)}
        {reviewCase && <ReviewRequestApproval detail={reviewCase} onRefresh={async () => {
          const updated = await api.get<QualityCase>(`/api/v1/quality/cases/${encodeURIComponent(reviewCase.case_id)}`);
          setReviewCase(updated);
          await refresh();
        }} />}
      </>}
      <Dialog open={negativeOpen} onOpenChange={setNegativeOpen}>
        <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[min(560px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-none">
          <DialogHeader className="shrink-0 border-b border-[#edf0f4] px-[24px] py-[18px] pr-[56px]"><DialogTitle>这个回答为什么没帮助？</DialogTitle></DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-[24px] py-[20px]">
            <div className="grid gap-2">{Object.entries(QUALITY_REASONS).map(([value, label]) => <label key={value} className="flex min-h-[32px] items-center gap-2 text-sm"><input type="checkbox" checked={reasons.includes(value as QualityReason)} onChange={() => setReasons(toggle(reasons, value as QualityReason))} />{label}</label>)}</div>
            <label className="mt-5 grid gap-2 text-sm">补充说明（可选）<textarea className="min-h-[128px] resize-y rounded-md border p-3 leading-6 outline-none focus:border-blue-500" maxLength={4000} value={comment} onChange={(e) => setComment(e.target.value)} /></label>
            <p className="mt-3 text-xs leading-5 text-muted-foreground">反馈绑定这次生成的固定回答。问答、证据和补充说明的复核共享可单独选择。</p>
          </div>
          <div className="flex shrink-0 justify-end border-t border-[#edf0f4] px-[24px] py-[14px]"><Button disabled={busy} onClick={() => void submit('unhelpful')}>提交反馈</Button></div>
        </DialogContent>
      </Dialog>
      <Dialog open={shareOpen} onOpenChange={setShareOpen}>
        <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[min(640px,calc(100vw-32px))] max-w-none flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-none">
          <DialogHeader className="shrink-0 border-b border-[#edf0f4] px-[24px] py-[18px] pr-[56px]"><DialogTitle>共享固定复核副本</DialogTitle></DialogHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-[24px] py-[20px]">
            <p className="mb-5 text-sm leading-6 text-muted-foreground">选择同部门复核员工及材料。副本有效期 7 天，可随时撤销共享。</p>
            <fieldset className="grid gap-2 rounded-[10px] border border-[#edf0f4] p-4"><legend className="px-1 text-sm font-semibold">复核人员</legend>
              {reviewers.length === 0 && <p className="text-sm">暂无可选同部门复核员工。</p>}
              <div className="grid max-h-[220px] gap-2 overflow-y-auto">{reviewers.map((r) => <label key={r.id} className="flex min-h-[32px] items-center gap-2 text-sm"><input type="checkbox" checked={selectedReviewers.includes(r.id)} onChange={() => setSelectedReviewers(toggle(selectedReviewers, r.id))} />{r.username}</label>)}</div>
            </fieldset>
            <fieldset className="mt-4 grid gap-2 rounded-[10px] border border-[#edf0f4] p-4"><legend className="px-1 text-sm font-semibold">材料范围</legend>
              {Object.entries({ query: '原始提问', answer: '原始回答', comment: '补充说明' }).map(([value, label]) => <label key={value} className="flex min-h-[32px] items-center gap-2 text-sm"><input type="checkbox" checked={fields.includes(value)} onChange={() => setFields(toggle(fields, value))} />{label}</label>)}
              <div className="grid max-h-[280px] gap-2 overflow-y-auto">{Array.from({ length: state.evidence_count }, (_, i) => <label key={i} className="flex min-h-[32px] items-center gap-2 text-sm"><input type="checkbox" checked={evidence.includes(i)} onChange={() => setEvidence(toggle(evidence, i))} />固定参考来源 [{i + 1}]</label>)}</div>
            </fieldset>
          </div>
          <div className="flex shrink-0 justify-end border-t border-[#edf0f4] px-[24px] py-[14px]"><Button disabled={busy || !selectedReviewers.length || !fields.length} onClick={() => void saveShare()}>共享所选材料</Button></div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function feedbackLoadError(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return '该回答尚无可用质量记录；历史回答需由管理员补采';
  if (error instanceof ApiError && error.status === 503) return '回答反馈功能当前未启用';
  return '回答反馈加载失败，请重试';
}
