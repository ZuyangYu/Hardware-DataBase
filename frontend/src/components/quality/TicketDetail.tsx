import { useRef, useState } from 'react';
import { api, ApiError } from '@/api/client';
import { createClientRequestId } from '@/api/documentAuthoring';
import type { FeedbackTicketDetail, TicketAction } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { StatusBadge } from '@/components/admin/StatusBadge';
import { notify } from '@/components/ui/app-toast';
import { formatAbsoluteTime } from '@/lib/formatters';
import { QUALITY_REASONS } from '@/lib/qualityLabels';
import { TICKET_EVENT, TICKET_STATUS, TICKET_TONE } from '@/lib/ticketLabels';

export default function TicketDetail({ detail, admin, assignees, onChanged, onReload, onDeleted }: {
  detail: FeedbackTicketDetail; admin: boolean; assignees: { id: number; username: string }[];
  onChanged: (value: FeedbackTicketDetail) => void; onReload: () => Promise<void>; onDeleted: () => void;
}) {
  const [message, setMessage] = useState('');
  const [assigneeId, setAssigneeId] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<{ action: 'close' | 'delete'; version: number } | null>(null);
  const retry = useRef<{ signature: string; client_request_id: string } | null>(null);
  const active = detail.status !== 'closed' && detail.status !== 'resolved';
  const writable = detail.consent_active && detail.content_available;
  async function act(action: TicketAction, version = detail.row_version) {
    if (busy) return;
    if (['reply', 'wait', 'resolve'].includes(action) && !message.trim()) { notify.error('请填写补充说明或解决办法'); return; }
    const body = { row_version: version, action, message: message.trim(), ...(action === 'assign' ? { assignee_user_id: Number(assigneeId) } : {}) };
    const signature = JSON.stringify(body);
    if (retry.current?.signature !== signature) retry.current = { signature, client_request_id: createClientRequestId() };
    setBusy(true);
    try {
      const result = await api.post<FeedbackTicketDetail>(`/api/v1/quality/tickets/${encodeURIComponent(detail.ticket_id)}/actions`, { ...body, client_request_id: retry.current.client_request_id });
      retry.current = null;
      onChanged(result); setMessage(''); setConfirmation(null);
      notify.success(action === 'resolve' ? '已记录解决办法' : '工单已更新');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { retry.current = null; setConfirmation(null); await onReload(); }
      notify.error(error instanceof Error ? error.message : '更新工单失败');
    } finally { setBusy(false); }
  }
  async function remove() {
    if (!confirmation || busy) return;
    setBusy(true);
    try {
      await api.delete(`/api/v1/quality/tickets/${encodeURIComponent(detail.ticket_id)}?row_version=${confirmation.version}`);
      setConfirmation(null); onDeleted(); notify.success('工单已删除');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) { setConfirmation(null); await onReload(); }
      notify.error(error instanceof Error ? error.message : '删除工单失败');
    } finally { setBusy(false); }
  }
  return <div className="grid min-w-0 gap-5 text-sm">
    <div className="flex flex-wrap items-center gap-3"><StatusBadge tone={TICKET_TONE[detail.status]}>{TICKET_STATUS[detail.status]}</StatusBadge><span>处理人：{detail.assignee_username || '尚未认领'}</span><Button variant="outline" size="sm" onClick={() => void onReload()}>刷新工单</Button></div>
    <dl className="grid gap-2 rounded-lg border bg-muted/20 p-3 sm:grid-cols-2">
      <div><dt className="text-xs text-muted-foreground">反馈员工</dt><dd>{detail.requester_username}</dd></div>
      <div><dt className="text-xs text-muted-foreground">提交部门</dt><dd>{detail.department_name || (detail.department_id == null ? '未设置部门' : `部门 ${detail.department_id}`)}</dd></div>
      <div><dt className="text-xs text-muted-foreground">联系方式</dt><dd className="whitespace-pre-wrap break-words">{detail.contact || '可通过工单回复联系'}</dd></div>
      <div><dt className="text-xs text-muted-foreground">提交时间</dt><dd>{formatAbsoluteTime(detail.created_at)}</dd></div>
    </dl>
    {!writable && <p className="rounded-md bg-muted p-3">反馈已撤销或原始材料已失效，正文不可查看，工单停止继续处理。</p>}
    {detail.reason_codes.length > 0 && <p>问题分类：{detail.reason_codes.map(code => QUALITY_REASONS[code]).join('、')}</p>}
    {detail.comment && <section><h3 className="mb-2 font-semibold">补充说明</h3><p className="whitespace-pre-wrap break-words">{detail.comment}</p></section>}
    {detail.content.query != null && <section><h3 className="mb-2 font-semibold">本次提问</h3><p className="whitespace-pre-wrap break-words">{detail.content.query}</p></section>}
    {detail.content.answer != null && <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">查看原回答</summary><p className="mt-3 whitespace-pre-wrap break-words">{detail.content.answer}</p></details>}
    {detail.resolution && <section className="rounded-lg border border-green-200 bg-green-50 p-3"><h3 className="mb-2 font-semibold">解决办法</h3><p className="whitespace-pre-wrap break-words">{detail.resolution}</p></section>}
    <section className="grid gap-3"><h3 className="font-semibold">沟通与处理记录</h3>{detail.events.map(event => <article className="rounded-lg border p-3" key={event.event_id}><div className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>{event.actor_username} · {event.actor_role === 'system_admin' ? '管理员' : '员工'} · {TICKET_EVENT[event.kind] ?? event.kind}</span><time>{formatAbsoluteTime(event.created_at)}</time></div>{event.message && <p className="mt-2 whitespace-pre-wrap break-words">{event.message}</p>}</article>)}</section>
    {writable && <fieldset disabled={busy} className="grid gap-3 border-t pt-4">
      {detail.status !== 'closed' && <label className="grid gap-2">回复或解决说明<textarea className="min-h-28 rounded-md border p-3" maxLength={4000} value={message} onChange={event => setMessage(event.target.value)} placeholder={admin ? '记录联系情况、需要补充的信息或解决办法' : '补充问题细节，或说明为什么仍未解决'} /></label>}
      <div className="flex flex-wrap gap-2">
        {active && <Button disabled={!message.trim()} onClick={() => void act('reply')}>{admin ? '发送回复' : '发送补充'}</Button>}
        {admin && active && <><Button variant="outline" onClick={() => void act('claim')}>认领工单</Button><Button variant="outline" disabled={!message.trim()} onClick={() => void act('wait')}>请求补充</Button><Button disabled={!message.trim()} onClick={() => void act('resolve')}>标记已解决</Button></>}
        {(detail.status === 'resolved' || (admin && detail.status === 'closed')) && <Button onClick={() => void act('reopen')}>重新打开</Button>}
        {detail.status !== 'closed' && <Button variant="outline" onClick={() => setConfirmation({ action: 'close', version: detail.row_version })}>关闭工单</Button>}
      </div>
      {admin && active && <div className="flex flex-wrap items-center gap-2"><label>转派管理员 <select className="rounded border p-2" value={assigneeId} onChange={event => setAssigneeId(event.target.value)}><option value="">选择管理员</option>{assignees.map(user => <option key={user.id} value={user.id}>{user.username}</option>)}</select></label><Button variant="outline" disabled={!assigneeId} onClick={() => void act('assign')}>转派</Button></div>}
    </fieldset>}
    {admin && <div className="flex border-t pt-4"><Button variant="outline" disabled={busy} className="text-red-600" onClick={() => setConfirmation({ action: 'delete', version: detail.row_version })}>删除工单</Button></div>}
    <ConfirmDialog open={confirmation !== null} onOpenChange={open => { if (!open) setConfirmation(null); }} loading={busy}
      title={confirmation?.action === 'delete' ? '删除这条工单？' : '关闭这条工单？'}
      description={confirmation?.action === 'delete' ? '删除后员工和管理员均无法查看此工单，工单中的补充说明、联系方式和处理内容将被清除。原始问答和质量记录保留。' : '关闭后不能继续补充。如需继续处理，请联系管理员重新打开。'}
      confirmText={confirmation?.action === 'delete' ? '确认删除' : '确认关闭'} destructive={confirmation?.action === 'delete'}
      onConfirm={() => { if (confirmation?.action === 'delete') void remove(); else if (confirmation) void act('close', confirmation.version); }} />
  </div>;
}
