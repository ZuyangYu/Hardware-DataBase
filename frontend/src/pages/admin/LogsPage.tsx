import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../../api/client';
import type {
  AuditEventView,
  AuditStatsResponse,
  EvidenceView,
  QueryStatsResponse,
  QueryTraceView,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import { isSystemAdmin } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS, formatDateTime } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

type Tab = 'audit' | 'query';

function formatJsonText(value?: string | null): string {
  if (!value) return '-';
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

export default function LogsPage({ auth, onLogout }: Props) {
  const sysAdmin = isSystemAdmin(auth.user);
  const [tab, setTab] = useState<Tab>('audit');

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="日志中心"
        description={sysAdmin ? '全局审计日志与查询日志。' : '本部门审计日志与查询日志。'}
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] mb-[16px] flex items-center gap-[6px]">
        <TabButton active={tab === 'audit'} onClick={() => setTab('audit')}>
          审计日志
        </TabButton>
        <TabButton active={tab === 'query'} onClick={() => setTab('query')}>
          查询日志
        </TabButton>
      </div>

      {tab === 'audit' ? <AuditPanel /> : <QueryPanel />}
    </div>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'h-[34px] rounded-[10px] px-[14px] text-[13px] font-medium transition-colors',
        active ? 'bg-[#18181a] text-white' : 'bg-white text-[#464c5e] hover:bg-[#f6f6f6]',
      )}
    >
      {children}
    </button>
  );
}

// ---------------------------------------------------------------------------
// 审计日志面板
// ---------------------------------------------------------------------------

function AuditPanel() {
  const [events, setEvents] = useState<AuditEventView[]>([]);
  const [stats, setStats] = useState<AuditStatsResponse | null>(null);
  const [actions, setActions] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [action, setAction] = useState<string>('all');
  const [keyword, setKeyword] = useState('');
  const [detail, setDetail] = useState<AuditEventView | null>(null);

  const params = useMemo(() => {
    const p = new URLSearchParams({ limit: '300' });
    if (action !== 'all') p.set('action', action);
    if (keyword.trim()) p.set('keyword', keyword.trim());
    return p.toString();
  }, [action, keyword]);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    Promise.all([
      api.get<AuditEventView[]>(`/api/v1/logs/audit?${params}`),
      api.get<AuditStatsResponse>(`/api/v1/logs/audit/stats?${params}`),
    ])
      .then(([rows, s]) => {
        if (cancelled) return;
        setEvents(rows);
        setStats(s);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载审计日志失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [params]);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  useEffect(() => {
    api.get<string[]>('/api/v1/logs/audit/actions').then(setActions).catch(() => undefined);
  }, []);

  const columns: DataTableColumn<AuditEventView>[] = useMemo(
    () => [
      {
        key: 'created_at',
        title: '时间',
        width: 150,
        render: (e) => <span className="text-[12px] text-[#858b9c]">{formatDateTime(e.created_at)}</span>,
      },
      {
        key: 'actor_username',
        title: '操作者',
        width: 120,
        render: (e) => <span className="truncate text-[13px] text-[#18181a]">{e.actor_username || '-'}</span>,
      },
      {
        key: 'action',
        title: '动作',
        width: 150,
        render: (e) => (
          <span className="inline-flex rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">
            {e.action}
          </span>
        ),
      },
      {
        key: 'target',
        title: '目标',
        render: (e) => (
          <span className="truncate text-[13px] text-[#464c5e]">
            {e.target_type}
            {e.target_id ? `/${e.target_id}` : ''}
            {e.kb_name ? ` · ${e.kb_name}` : ''}
          </span>
        ),
      },
      {
        key: 'success',
        title: '结果',
        width: 80,
        render: (e) =>
          e.success ? (
            <span className="text-[#2cb360]">成功</span>
          ) : (
            <span className="text-[#d20b0b]">失败</span>
          ),
      },
    ],
    [],
  );

  return (
    <div className="flex flex-col gap-[16px]">
      <div className="flex flex-wrap items-end gap-[10px]">
        <div className="grid min-w-[180px] gap-[4px]">
          <span className="text-[11px] text-[#858b9c]">动作</span>
          <Select value={action} onValueChange={setAction}>
            <SelectTrigger className="h-[34px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              {actions.map((a) => (
                <SelectItem key={a} value={a}>
                  {a}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid min-w-[200px] gap-[4px]">
          <span className="text-[11px] text-[#858b9c]">关键词</span>
          <Input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索操作者/目标"
            className="h-[34px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]"
          />
        </div>
        <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={() => load()}>
          <AppIcon name="search" size={13} />
          查询
        </Button>
      </div>

      <div className="flex flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]">
          <StatCard label="总数" value={stats?.total ?? 0} />
          <StatCard label="成功" value={stats?.breakdown?.success ?? 0} tone="green" />
          <StatCard label="失败" value={stats?.breakdown?.failure ?? 0} tone="red" />
        </div>

        {!loaded ? (
          <div className="grid gap-[10px]">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-[48px] rounded-[10px]" />
            ))}
          </div>
        ) : (
          <DataTable
            columns={columns}
            data={events}
            rowKey={(e) => e.id}
            size="compact"
            emptyText="暂无审计日志"
            onRowClick={(e) => setDetail(e)}
          />
        )}
      </div>

      <AuditDetailDialog event={detail} onClose={() => setDetail(null)} />
    </div>
  );
}

function AuditDetailDialog({ event, onClose }: { event: AuditEventView | null; onClose: () => void }) {
  return (
    <Dialog open={event !== null} onOpenChange={(o: boolean) => { if (!o) onClose(); }}>
      <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] max-w-[840px] flex-col overflow-hidden rounded-[16px] p-0">
        <DialogHeader className="shrink-0 px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">审计事件详情</DialogTitle>
        </DialogHeader>
        {event && (
          <div className="max-h-[calc(100dvh-120px)] overflow-y-auto">
            <div className="grid min-w-0 gap-[10px] px-[24px] pb-[20px] text-[13px]">
              <Field label="时间" value={formatDateTime(event.created_at)} />
              <Field label="操作者" value={`${event.actor_username} (${event.actor_role})`} />
              <Field label="动作" value={event.action} />
              <Field label="目标" value={`${event.target_type}${event.target_id ? `/${event.target_id}` : ''}`} />
              <Field label="知识库" value={event.kb_name || '-'} />
              <Field label="结果" value={event.success ? '成功' : '失败'} />
              {event.error_message && <Field label="错误" value={event.error_message} />}
              <div className="grid min-w-0 gap-[4px]">
                <span className="text-[11px] text-[#858b9c]">元数据</span>
                <pre className="max-h-[380px] max-w-full overflow-auto whitespace-pre rounded-[8px] bg-[#f6f6f6] p-[10px] text-[11px] leading-[18px] text-[#464c5e]">
                  {formatJsonText(event.metadata_json)}
                </pre>
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// 查询日志面板
// ---------------------------------------------------------------------------

function QueryPanel() {
  const [traces, setTraces] = useState<QueryTraceView[]>([]);
  const [stats, setStats] = useState<QueryStatsResponse | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [status, setStatus] = useState<string>('all');
  const [keyword, setKeyword] = useState('');
  const [detail, setDetail] = useState<QueryTraceView | null>(null);
  const [evidence, setEvidence] = useState<EvidenceView[]>([]);
  const [evidenceLoading, setEvidenceLoading] = useState(false);

  const params = useMemo(() => {
    const p = new URLSearchParams({ limit: '300' });
    if (status !== 'all') p.set('status', status);
    if (keyword.trim()) p.set('keyword', keyword.trim());
    return p.toString();
  }, [status, keyword]);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    Promise.all([
      api.get<QueryTraceView[]>(`/api/v1/logs/query?${params}`),
      api.get<QueryStatsResponse>(`/api/v1/logs/query/stats?${params}`),
    ])
      .then(([rows, s]) => {
        if (cancelled) return;
        setTraces(rows);
        setStats(s);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载查询日志失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [params]);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  function openDetail(t: QueryTraceView) {
    setDetail(t);
    setEvidence([]);
    setEvidenceLoading(true);
    api
      .get<EvidenceView[]>(`/api/v1/logs/query/${t.id}/evidence`)
      .then(setEvidence)
      .catch(() => undefined)
      .finally(() => setEvidenceLoading(false));
  }

  const statusLabel = (s: string) =>
    ({ success: '成功', failed: '失败', partial: '部分', no_evidence: '无证据' } as Record<string, string>)[s] ?? s;

  const columns: DataTableColumn<QueryTraceView>[] = useMemo(
    () => [
      {
        key: 'created_at',
        title: '时间',
        width: 150,
        render: (t) => <span className="text-[12px] text-[#858b9c]">{formatDateTime(t.created_at)}</span>,
      },
      {
        key: 'username',
        title: '用户',
        width: 110,
        render: (t) => <span className="truncate text-[13px] text-[#18181a]">{t.username || '-'}</span>,
      },
      {
        key: 'kb_name',
        title: '知识库',
        width: 120,
        render: (t) => <span className="truncate text-[12px] text-[#464c5e]">{t.kb_name || '-'}</span>,
      },
      {
        key: 'original_query',
        title: '问题',
        render: (t) => <span className="line-clamp-1 text-[13px] text-[#464c5e]">{t.original_query || '-'}</span>,
      },
      {
        key: 'latency_ms',
        title: '耗时',
        width: 80,
        align: 'right',
        render: (t) => (
          <span className="text-[12px] text-[#858b9c]">{t.latency_ms != null ? `${t.latency_ms}ms` : '-'}</span>
        ),
      },
      {
        key: 'status',
        title: '状态',
        width: 80,
        render: (t) => (
          <span
            className={cn(
              'text-[12px]',
              t.status === 'success' ? 'text-[#2cb360]' : t.status === 'failed' ? 'text-[#d20b0b]' : 'text-[#b45309]',
            )}
          >
            {statusLabel(t.status)}
          </span>
        ),
      },
    ],
    [],
  );

  return (
    <div className="flex flex-col gap-[16px]">
      <div className="flex flex-wrap items-end gap-[10px]">
        <div className="grid min-w-[160px] gap-[4px]">
          <span className="text-[11px] text-[#858b9c]">状态</span>
          <Select value={status} onValueChange={setStatus}>
            <SelectTrigger className="h-[34px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部</SelectItem>
              <SelectItem value="success">成功</SelectItem>
              <SelectItem value="failed">失败</SelectItem>
              <SelectItem value="partial">部分</SelectItem>
              <SelectItem value="no_evidence">无证据</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="grid min-w-[220px] gap-[4px]">
          <span className="text-[11px] text-[#858b9c]">关键词</span>
          <Input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索问题/知识库"
            className="h-[34px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]"
          />
        </div>
        <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={() => load()}>
          <AppIcon name="search" size={13} />
          查询
        </Button>
      </div>

      <div className="flex flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]">
          <StatCard label="总数" value={stats?.total ?? 0} />
          <StatCard label="成功" value={stats?.breakdown?.success ?? 0} tone="green" />
          <StatCard label="失败" value={stats?.breakdown?.failed ?? 0} tone="red" />
        </div>

        {!loaded ? (
          <div className="grid gap-[10px]">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-[48px] rounded-[10px]" />
            ))}
          </div>
        ) : (
          <DataTable
            columns={columns}
            data={traces}
            rowKey={(t) => t.id}
            size="compact"
            emptyText="暂无查询日志"
            onRowClick={(t) => openDetail(t)}
          />
        )}
      </div>

      <QueryDetailDialog
        trace={detail}
        evidence={evidence}
        evidenceLoading={evidenceLoading}
        onClose={() => setDetail(null)}
      />
    </div>
  );
}

function QueryDetailDialog({
  trace,
  evidence,
  evidenceLoading,
  onClose,
}: {
  trace: QueryTraceView | null;
  evidence: EvidenceView[];
  evidenceLoading: boolean;
  onClose: () => void;
}) {
  return (
    <Dialog open={trace !== null} onOpenChange={(o: boolean) => { if (!o) onClose(); }}>
      <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] max-w-[720px] flex-col overflow-hidden rounded-[16px] p-0">
        <DialogHeader className="shrink-0 px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">查询详情</DialogTitle>
        </DialogHeader>
        {trace && (
          <div className="max-h-[calc(100dvh-120px)] overflow-y-auto">
            <div className="grid min-w-0 gap-[10px] px-[24px] pb-[20px] text-[13px]">
              <Field label="时间" value={formatDateTime(trace.created_at)} />
              <Field label="用户" value={trace.username || '-'} />
              <Field label="知识库" value={trace.kb_name || '-'} />
              <Field label="原始问题" value={trace.original_query || '-'} />
              <Field label="改写问题" value={trace.rewritten_query || '-'} />
              <Field label="后端" value={`${trace.backend || '-'} / ${trace.retriever_type || '-'}`} />
              <Field
                label="检索参数"
                value={`final_top_k=${trace.final_top_k ?? '-'}, latency=${trace.latency_ms ?? '-'}ms`}
              />
              <Field label="状态" value={trace.status} />
              {trace.error_message && <Field label="错误" value={trace.error_message} />}

              <div className="mt-[6px] border-t border-[#f0f1f4] pt-[10px]">
                <span className="text-[12px] font-semibold text-[#18181a]">
                  命中证据({evidence.length})
                </span>
                {evidenceLoading ? (
                  <Skeleton className="mt-[8px] h-[40px] rounded-[8px]" />
                ) : evidence.length === 0 ? (
                  <p className="mt-[6px] text-[12px] text-[#858b9c]">无证据记录</p>
                ) : (
                  <div className="mt-[8px] grid gap-[8px]">
                    {evidence.map((e) => (
                      <div key={e.id} className="rounded-[8px] border border-[#e3e7f1] bg-[#fafbfc] px-[10px] py-[8px]">
                        <div className="mb-[4px] flex items-center gap-[6px] text-[12px] font-semibold text-[#18181a]">
                          <span className="text-[#757f9c]">#{e.rank}</span>
                          <span className="min-w-0 truncate">{e.file_name || '未知文件'}</span>
                          {e.rerank_score != null && (
                            <span className="ml-auto shrink-0 text-[11px] font-normal text-[#858b9c]">
                              rerank {e.rerank_score.toFixed(3)}
                            </span>
                          )}
                        </div>
                        {e.text_preview && (
                          <p className="line-clamp-3 whitespace-pre-wrap text-[12px] leading-[18px] text-[#858b9c]">
                            {e.text_preview}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[80px_minmax(0,1fr)] gap-[8px]">
      <span className="text-[11px] text-[#858b9c]">{label}</span>
      <span className="text-[13px] text-[#18181a] [word-break:break-word]">{value}</span>
    </div>
  );
}
