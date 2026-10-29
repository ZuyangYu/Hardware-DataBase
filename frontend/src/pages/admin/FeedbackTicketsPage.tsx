import { useCallback, useEffect, useRef, useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { api } from '@/api/client';
import type { FeedbackTicket, FeedbackTicketDetail, TicketStatus } from '@/api/types';
import type { AuthSession } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { AdminPageShell } from '@/components/admin/AdminPageShell';
import { ErrorState, LoadingState } from '@/components/admin/StateBlock';
import { StatusBadge } from '@/components/admin/StatusBadge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import TicketDetail from '@/components/quality/TicketDetail';
import { TICKET_STATUS, TICKET_TONE } from '@/lib/ticketLabels';
import { formatAbsoluteTime } from '@/lib/formatters';

export default function FeedbackTicketsPage({ auth }: { auth: AuthSession; onLogout: () => void }) {
  const admin = auth.user.role === 'system_admin';
  const [params, setParams] = useSearchParams();
  const ticketId = params.get('ticket');
  const legacyCase = params.get('case');
  const [rows, setRows] = useState<FeedbackTicket[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<TicketStatus | ''>('');
  const [search, setSearch] = useState('');
  const [searchInput, setSearchInput] = useState('');
  const [department, setDepartment] = useState('');
  const [departments, setDepartments] = useState<{ id: number; name: string }[]>([]);
  const [assignees, setAssignees] = useState<{ id: number; username: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<FeedbackTicketDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const [detailLoading, setDetailLoading] = useState(false);
  const listSeq = useRef(0);
  const detailSeq = useRef(0);
  const activeId = useRef(ticketId);
  activeId.current = ticketId;
  const load = useCallback(async () => {
    const sequence = ++listSeq.current;
    setLoading(true); setError('');
    const query = new URLSearchParams({ limit: '50', offset: String(offset) });
    if (status) query.set('status', status);
    if (search) query.set('search', search);
    if (department) query.set('department_id', department);
    try {
      const result = await api.get<{ items: FeedbackTicket[]; total: number }>(`/api/v1/quality/tickets?${query}`);
      if (sequence === listSeq.current) { setRows(result.items); setTotal(result.total); }
    } catch (failure) { if (sequence === listSeq.current) setError(failure instanceof Error ? failure.message : '读取工单失败'); }
    finally { if (sequence === listSeq.current) setLoading(false); }
  }, [offset, status, search, department]);
  const readDetail = useCallback(async () => {
    if (!ticketId) return;
    const sequence = ++detailSeq.current;
    setDetailLoading(true); setDetailError('');
    try {
      const result = await api.get<FeedbackTicketDetail>(`/api/v1/quality/tickets/${encodeURIComponent(ticketId)}`);
      if (sequence === detailSeq.current && activeId.current === ticketId) setDetail(result);
    } catch (failure) {
      if (sequence === detailSeq.current && activeId.current === ticketId) { setDetail(null); setDetailError(failure instanceof Error ? failure.message : '工单不存在或已删除'); }
    } finally { if (sequence === detailSeq.current) setDetailLoading(false); }
  }, [ticketId]);
  useEffect(() => {
    void load();
    const poll = () => { if (!document.hidden) void load(); };
    const timer = window.setInterval(poll, 30_000);
    document.addEventListener('visibilitychange', poll);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', poll);
      listSeq.current += 1;
    };
  }, [load]);
  useEffect(() => {
    setDetail(null); setDetailError('');
    void readDetail();
    const poll = () => { if (!document.hidden) void readDetail(); };
    const timer = window.setInterval(poll, 15_000);
    document.addEventListener('visibilitychange', poll);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', poll);
      detailSeq.current += 1;
    };
  }, [readDetail]);
  useEffect(() => {
    if (!admin) return;
    let cancelled = false;
    void api.get<{ id: number; username: string }[]>('/api/v1/quality/ticket-assignees').then(value => { if (!cancelled) setAssignees(value); }).catch(() => undefined);
    void api.get<{ id: number; name: string }[]>('/api/v1/departments').then(value => { if (!cancelled) setDepartments(value); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [admin]);
  if (legacyCase && !ticketId) return <Navigate replace to="/admin/quality" />;
  return <div className="flex min-h-0 flex-1 flex-col">
    <AdminPageShell>
      <AppHeader title={admin ? '反馈工单' : '我的反馈'} description={admin ? '查看员工反馈，联系处理并记录解决办法。' : '在回答下方点击“回答有问题”提交工单，在这里查看回复和处理进度。'} />
      <form className="flex flex-wrap items-center gap-3" onSubmit={event => { event.preventDefault(); setSearch(searchInput.trim()); setOffset(0); }}>
        <label className="flex items-center gap-2">状态<select className="rounded-md border p-2" aria-label="工单状态" value={status} onChange={event => { setStatus(event.target.value as TicketStatus | ''); setOffset(0); }}><option value="">全部状态</option>{Object.entries(TICKET_STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        {admin && <label className="flex items-center gap-2">部门<select className="rounded-md border p-2" aria-label="反馈部门" value={department} onChange={event => { setDepartment(event.target.value); setOffset(0); }}><option value="">全部部门</option>{departments.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>}
        <input className="min-w-0 rounded-md border p-2" aria-label="搜索工单" placeholder={admin ? '搜索员工、部门或工单编号' : '搜索工单编号'} value={searchInput} onChange={event => setSearchInput(event.target.value)} maxLength={200} />
        <Button variant="outline" type="submit">搜索</Button><Button variant="outline" type="button" disabled={loading} onClick={() => void load()}>刷新列表</Button>
      </form>
      {error && <ErrorState message={error} onRetry={() => void load()} />}
      {loading && !rows.length ? <LoadingState label="正在加载反馈工单…" /> : <div className="overflow-x-auto rounded-lg border"><table className="w-full min-w-[700px] text-left text-sm"><thead className="bg-muted"><tr>{['问题摘要', ...(admin ? ['反馈员工', '部门'] : []), '处理人', '状态', '更新时间', '操作'].map(title => <th className="p-3" key={title}>{title}</th>)}</tr></thead><tbody>
        {rows.map(row => <tr className="border-t" key={row.ticket_id}><td className="max-w-xs p-3"><p className="line-clamp-2 break-words">{row.summary || '未填写说明'}</p><p className="mt-1 text-xs text-muted-foreground">{row.ticket_id.slice(-12)}</p></td>{admin && <><td className="p-3">{row.requester_username}</td><td className="p-3">{row.department_name || (row.department_id == null ? '未设置部门' : `部门 ${row.department_id}`)}</td></>}<td className="p-3">{row.assignee_username || '尚未认领'}</td><td className="p-3 whitespace-nowrap"><StatusBadge tone={TICKET_TONE[row.status]}>{TICKET_STATUS[row.status]}</StatusBadge></td><td className="p-3 whitespace-nowrap">{formatAbsoluteTime(row.updated_at)}</td><td className="p-3"><Button variant="outline" size="sm" onClick={() => setParams({ ticket: row.ticket_id })}>查看工单</Button></td></tr>)}
        {!rows.length && !error && <tr><td colSpan={admin ? 7 : 5} className="p-8 text-center text-muted-foreground">暂无反馈工单</td></tr>}
      </tbody></table></div>}
      <div className="flex flex-wrap items-center gap-3"><span className="text-sm text-muted-foreground">共 {total} 条工单 · 第 {offset / 50 + 1} 页</span><Button variant="outline" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 50))}>上一页</Button><Button variant="outline" disabled={offset + 50 >= total || loading} onClick={() => setOffset(offset + 50)}>下一页</Button></div>
    </AdminPageShell>
    <Dialog open={Boolean(ticketId)} onOpenChange={open => { if (!open) setParams({}); }}><DialogContent className="flex max-h-[calc(100dvh-32px)] w-[min(850px,calc(100vw-32px))] max-w-none flex-col overflow-hidden sm:max-w-none"><DialogHeader className="shrink-0"><DialogTitle>工单详情</DialogTitle></DialogHeader><div className="min-h-0 overflow-y-auto pr-1">
      {detailError && <ErrorState message={detailError} onRetry={() => void readDetail()} />}
      {detailLoading && !detail && !detailError && <LoadingState label="正在读取工单…" />}
      {detail && <TicketDetail key={detail.ticket_id} detail={detail} admin={admin} assignees={assignees}
        onReload={readDetail} onChanged={value => { if (activeId.current === value.ticket_id) { detailSeq.current += 1; setDetailLoading(false); setDetail(value); } void load(); }}
        onDeleted={() => { setDetail(null); setParams({}); if (rows.length === 1 && offset > 0) setOffset(Math.max(0, offset - 50)); else void load(); }} />}
    </div></DialogContent></Dialog>
  </div>;
}
