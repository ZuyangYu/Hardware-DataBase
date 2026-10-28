import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError } from '@/api/client';
import { createClientRequestId } from '@/api/clientRequestId';
import type { MessageView, QualityFeedbackState, QualityRating, QualityReason } from '@/api/types';
import { QUALITY_REASONS } from '@/lib/qualityLabels';
import { TICKET_STATUS } from '@/lib/ticketLabels';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { notify } from '@/components/ui/app-toast';

export default function FeedbackActions({ msg }: { msg: MessageView }) {
  const [state, setState] = useState<QualityFeedbackState | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [reasons, setReasons] = useState<QualityReason[]>([]);
  const [comment, setComment] = useState('');
  const [contact, setContact] = useState('');
  const retry = useRef<{ signature: string; client_request_id: string } | null>(null);
  const path = `/api/v1/chat/turns/${encodeURIComponent(msg.turn_id ?? '')}/feedback`;
  const eligible = Boolean(msg.turn_id && msg.id > 0 && !msg.redacted && !msg.edited_at);
  useEffect(() => {
    let cancelled = false;
    setState(null); setLoadError('');
    if (!eligible) return;
    setLoading(true);
    void api.get<QualityFeedbackState>(path).then(value => { if (!cancelled) setState(value); })
      .catch(error => { if (!cancelled) setLoadError(feedbackLoadError(error)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [path, eligible, loadAttempt]);
  async function submit(rating: QualityRating) {
    if (!state || busy) return;
    setBusy(true);
    const body = {
      assistant_message_id: msg.id, answer_revision: state.answer_revision, rating,
      reason_codes: rating === 'unhelpful' ? reasons : [], comment: rating === 'unhelpful' ? comment : '',
      share_with_support: rating === 'unhelpful', support_contact: rating === 'unhelpful' ? contact.trim() : '',
    };
    const signature = JSON.stringify(body);
    if (retry.current?.signature !== signature) retry.current = { signature, client_request_id: createClientRequestId() };
    try {
      const result = await api.post<{ ticket_id?: string | null; case_id: string }>(path, { ...body, client_request_id: retry.current.client_request_id });
      setState(previous => previous && { ...previous, rating, reason_codes: body.reason_codes, comment: body.comment, case_id: result.case_id, ticket_id: result.ticket_id ?? previous.ticket_id });
      retry.current = null; setOpen(false);
      try { setState(await api.get<QualityFeedbackState>(path)); } catch { /* Submission already committed; preserve its receipt. */ }
      if (rating === 'unhelpful') notify.success('工单已提交，可在“我的反馈”查看处理进度');
      else if (rating === 'withdrawn') notify.success('反馈已撤销，关联工单已停止处理');
    } catch (error) { notify.error(error instanceof Error ? error.message : '提交反馈失败'); }
    finally { setBusy(false); }
  }
  if (!eligible) return null;
  if (!state) return <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><span>{loading ? '正在加载反馈…' : loadError}</span>{loadError && <button type="button" className="underline" onClick={() => setLoadAttempt(value => value + 1)}>重试加载反馈</button>}</div>;
  return <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
    <button disabled={busy} type="button" aria-pressed={state.rating === 'helpful'} onClick={() => void submit(state.rating === 'helpful' ? 'withdrawn' : 'helpful')}>有帮助</button>
    <button disabled={busy} type="button" aria-pressed={state.rating === 'unhelpful'} className={state.rating === 'unhelpful' ? 'font-semibold text-blue-700' : ''} onClick={() => { setReasons(state.reason_codes); setComment(state.comment); setContact(state.support_contact ?? ''); setOpen(true); }}>回答有问题</button>
    {state.ticket_id && <Link className="text-blue-700 hover:underline" to={`/admin/quality?ticket=${encodeURIComponent(state.ticket_id)}`}>查看工单 · {state.ticket_status ? TICKET_STATUS[state.ticket_status] : '待处理'}</Link>}
    {state.rating && state.rating !== 'withdrawn' && <button disabled={busy} type="button" title="撤销反馈将关闭关联工单并撤销本次材料授权" onClick={() => void submit('withdrawn')}>撤销反馈</button>}
    <Dialog open={open} onOpenChange={value => { if (!busy) setOpen(value); }}><DialogContent className="flex max-h-[calc(100dvh-32px)] w-[min(560px,calc(100vw-32px))] max-w-none flex-col overflow-hidden sm:max-w-none"><DialogHeader className="shrink-0"><DialogTitle>反馈回答问题</DialogTitle></DialogHeader>
      <div className="min-h-0 overflow-y-auto">
        <p className="mb-3 text-sm text-muted-foreground">说明哪里存在问题，管理员会跟进处理。分类和补充信息均可选填。</p>
        <div className="grid gap-2 sm:grid-cols-2">{Object.entries(QUALITY_REASONS).map(([value, label]) => <label key={value} className="flex min-h-8 items-center gap-2 text-sm"><input type="checkbox" checked={reasons.includes(value as QualityReason)} onChange={() => setReasons(items => items.includes(value as QualityReason) ? items.filter(item => item !== value) : [...items, value as QualityReason])} />{label}</label>)}</div>
        <label className="mt-4 grid gap-2 text-sm">补充说明（可选）<textarea className="min-h-28 resize-y rounded-md border p-3" maxLength={4000} value={comment} onChange={event => setComment(event.target.value)} placeholder="哪里有问题？你期望得到什么结果？" /></label>
        <label className="mt-4 grid gap-2 text-sm">联系方式（可选）<input className="rounded-md border p-3" maxLength={200} value={contact} onChange={event => setContact(event.target.value)} placeholder="企业微信、邮箱等；也可直接通过工单沟通" /></label>
        <p className="mt-4 rounded-md bg-muted p-3 text-xs leading-5">提交后，处理管理员可查看本次提问、回答和补充说明，用于联系你解决问题。</p>
      </div>
      <div className="flex shrink-0 justify-end gap-2 border-t pt-3"><Button variant="outline" disabled={busy} onClick={() => setOpen(false)}>取消</Button><Button disabled={busy} onClick={() => void submit('unhelpful')}>{busy ? '提交中…' : '提交工单'}</Button></div>
    </DialogContent></Dialog>
  </div>;
}

function feedbackLoadError(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return '该回答尚无可用质量记录；历史回答需由管理员补采';
  if (error instanceof ApiError && error.status === 503) return '回答反馈功能当前未启用';
  return '回答反馈加载失败，请重试';
}
