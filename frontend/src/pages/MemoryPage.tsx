import { useCallback, useEffect, useState } from 'react';

import { api } from '../api/client';
import type { KbView, MemoryConsentListResponse, MemoryConsentView, MemoryListResponse, MemoryStatus, MemoryView, UserMemorySettingsView } from '../api/types';
import type { AuthSession } from '../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { notify } from '@/components/ui/app-toast';
import { formatDateTime } from '@/lib/enterprise-ui';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
  kbs: KbView[];
};

type ListStatus = MemoryStatus | 'all';

const STATUS_LABEL: Record<ListStatus, string> = {
  all: '全部状态',
  candidate: 'Candidate',
  verification_pending: '待审核',
  supersede_pending: '待替代',
  needs_rebuild: '待重建',
  verified: 'Verified',
  superseded: '已替代',
  rejected: '已驳回',
  deleted: '已删除',
  provenance_missing: '来源缺失',
};

function memoryText(memory: MemoryView): string {
  const value = memory.content.content;
  if (typeof value === 'string') return value;
  return JSON.stringify(memory.content);
}

function statusClass(status: MemoryStatus): string {
  if (status === 'verified') return 'bg-[#e8f7ee] text-[#28784f]';
  if (status === 'candidate') return 'bg-[#fff5df] text-[#99620b]';
  if (status === 'deleted' || status === 'rejected') return 'bg-[#fce7e7] text-[#b42323]';
  return 'bg-[#eef1f7] text-[#68728a]';
}

export default function MemoryPage({ auth, onLogout, kbs }: Props) {
  const [scope, setScope] = useState<'user' | 'project'>('user');
  const [status, setStatus] = useState<ListStatus>('all');
  const [selectedKb, setSelectedKb] = useState(kbs[0]?.name ?? '');
  const [rows, setRows] = useState<MemoryView[]>([]);
  const [settings, setSettings] = useState<UserMemorySettingsView | null>(null);
  const [consents, setConsents] = useState<MemoryConsentView[]>([]);
  const [detail, setDetail] = useState<MemoryView | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!selectedKb || !kbs.some((kb) => kb.name === selectedKb)) {
      setSelectedKb(kbs[0]?.name ?? '');
    }
  }, [kbs, selectedKb]);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    const params = new URLSearchParams({ scope });
    if (status !== 'all') params.set('status', status);
    if (scope === 'project' && selectedKb) params.set('kb_name', selectedKb);
    const listRequest = scope === 'project' && !selectedKb
      ? Promise.resolve<MemoryListResponse>({ items: [], next_cursor: null, total: 0 })
      : api.get<MemoryListResponse>(`/api/v1/memories?${params.toString()}`);
    Promise.all([
      api.get<UserMemorySettingsView>('/api/v1/memory-settings'),
      api.get<MemoryConsentListResponse>('/api/v1/memory-consents'),
      listRequest,
    ])
      .then(([userSettings, consentResponse, response]) => {
        if (cancelled) return;
        setSettings(userSettings);
        setConsents(consentResponse.items ?? []);
        setRows(response.items ?? []);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载长期记忆失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [scope, selectedKb, status]);

  useEffect(() => load(), [load]);

  async function changeOptIn(optIn: boolean) {
    setSaving(true);
    try {
      const next = await api.put<UserMemorySettingsView>('/api/v1/memory-settings', {
        opt_in: optIn,
        reason: optIn ? '用户在长期记忆页面明确开启' : '用户在长期记忆页面明确关闭',
        request_id: `memory-ui-${Date.now()}`,
      });
      setSettings(next);
      notify.success(optIn ? '已开启个人记忆提炼' : '已关闭个人记忆提炼，相关记忆已撤下');
      if (!optIn && scope === 'user') setRows([]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新个人记忆设置失败');
    } finally {
      setSaving(false);
    }
  }

  async function govern(memory: MemoryView, action: 'verify' | 'reject' | 'supersede' | 'delete') {
    const actionLabel = action === 'verify' ? '审核通过' : action === 'reject' ? '驳回' : action === 'supersede' ? '标记为已替代' : '删除';
    if (!window.confirm(`确定${actionLabel}这条记忆吗？`)) return;
    const successor = action === 'supersede'
      ? window.prompt('请输入同一范围内用于替代的新 Memory ID')?.trim()
      : undefined;
    if (action === 'supersede' && !successor) return;
    const body = {
      expected_revision: memory.revision,
      reason: `长期记忆页面执行${actionLabel}`,
      request_id: `memory-ui-${action}-${Date.now()}`,
      ...(action === 'verify' ? { evidence_refs: ['memory-ui-manual-review'] } : {}),
      ...(successor ? { successor_memory_id: successor } : {}),
    };
    try {
      if (action === 'delete') {
        await api.delete(`/api/v1/memories/${encodeURIComponent(memory.memory_id)}`, body);
      } else {
        await api.post(`/api/v1/memories/${encodeURIComponent(memory.memory_id)}/${action}`, body);
      }
      notify.success(`${actionLabel}请求已提交`);
      setDetail(null);
      void load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : `${actionLabel}失败`);
    }
  }

  async function toggleDetail(memory: MemoryView) {
    if (detail?.memory_id === memory.memory_id) {
      setDetail(null);
      return;
    }
    try {
      setDetail(await api.get<MemoryView>(`/api/v1/memories/${encodeURIComponent(memory.memory_id)}`));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载记忆详情失败');
    }
  }

  async function editDraft(memory: MemoryView) {
    const value = window.prompt('修改 Candidate 正文', memoryText(memory));
    if (value == null || !value.trim() || value.trim() === memoryText(memory)) return;
    try {
      await api.patch(`/api/v1/memories/${encodeURIComponent(memory.memory_id)}/draft`, {
        content: { ...memory.content, content: value.trim() },
        expected_revision: memory.revision,
        reason: '长期记忆页面编辑 Candidate',
        request_id: `memory-ui-draft-${Date.now()}`,
      });
      notify.success('Draft 更新请求已提交');
      void load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '编辑 Candidate 失败');
    }
  }

  async function revokeConsent(consent: MemoryConsentView) {
    if (!window.confirm(`撤销会话 #${consent.session_id} 的个人记忆授权吗？相关记忆会立即撤下。`)) return;
    try {
      await api.delete(`/api/v1/memory-consents/${encodeURIComponent(consent.consent_event_id)}`, {
        reason: '长期记忆页面撤销授权',
        request_id: `memory-ui-revoke-${Date.now()}`,
      });
      notify.success('个人记忆授权已撤销');
      void load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '撤销个人记忆授权失败');
    }
  }

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="长期记忆"
        description="记忆只作为不可信上下文线索；正式规格、Datasheet 与知识库证据始终优先。"
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[28px] flex flex-wrap items-center gap-[10px] rounded-[14px] border border-[#e3e7f1] bg-white px-[16px] py-[12px]">
        <div className="flex rounded-[9px] bg-[#f4f6fa] p-[3px]">
          <button
            type="button"
            onClick={() => setScope('user')}
            className={`rounded-[7px] px-[12px] py-[6px] text-[12px] ${scope === 'user' ? 'bg-white font-medium text-[#18181a] shadow-sm' : 'text-[#757f9c]'}`}
          >
            我的记忆
          </button>
          <button
            type="button"
            onClick={() => setScope('project')}
            className={`rounded-[7px] px-[12px] py-[6px] text-[12px] ${scope === 'project' ? 'bg-white font-medium text-[#18181a] shadow-sm' : 'text-[#757f9c]'}`}
          >
            项目记忆
          </button>
        </div>
        {scope === 'project' && (
          <select
            value={selectedKb}
            onChange={(event) => setSelectedKb(event.target.value)}
            className="h-[32px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#464c5e] outline-none"
            aria-label="选择知识库"
          >
            {kbs.length === 0 && <option value="">暂无可访问知识库</option>}
            {kbs.map((kb) => <option key={kb.name} value={kb.name}>{kb.name}</option>)}
          </select>
        )}
        <select
          value={status}
          onChange={(event) => setStatus(event.target.value as ListStatus)}
          className="h-[32px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#464c5e] outline-none"
          aria-label="记忆状态"
        >
          {(Object.keys(STATUS_LABEL) as ListStatus[]).map((value) => <option key={value} value={value}>{STATUS_LABEL[value]}</option>)}
        </select>
        <Button type="button" variant="outline" size="sm" className="ml-auto" onClick={() => load() }>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
        {settings && (
          <label className="flex items-center gap-[8px] text-[12px] text-[#59627a]">
            <input
              type="checkbox"
              checked={settings.opt_in}
              disabled={saving}
              onChange={(event) => void changeOptIn(event.target.checked)}
            />
            允许个人记忆提炼
          </label>
        )}
      </div>

      {!loaded ? (
        <div className="mt-[18px] grid gap-[12px]">
          {[1, 2, 3].map((item) => <Skeleton key={item} className="h-[150px] rounded-[14px]" />)}
        </div>
      ) : rows.length === 0 ? (
        <div className="mt-[18px] rounded-[14px] border border-dashed border-[#d8deeb] bg-white px-[20px] py-[42px] text-center text-[13px] text-[#858b9c]">
          {scope === 'user' && !settings?.opt_in ? '个人记忆默认关闭；开启后仍需对具体对话明确授权。' : '当前授权范围内暂无记忆。'}
        </div>
      ) : (
        <div className="mt-[18px] grid gap-[12px]">
          {rows.map((memory) => (
            <article key={memory.memory_id} className="rounded-[14px] border border-[#e3e7f1] bg-white px-[20px] py-[16px] shadow-[0_2px_8px_rgba(43,55,87,0.03)]">
              <div className="flex items-start justify-between gap-[12px]">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-[8px]">
                    <h2 className="truncate text-[15px] font-medium text-[#18181a]">{String(memory.content.title ?? '未命名记忆')}</h2>
                    <span className={`rounded-full px-[8px] py-[2px] text-[11px] ${statusClass(memory.status)}`}>{STATUS_LABEL[memory.status]}</span>
                    <span className="text-[11px] text-[#858b9c]">Rev.{memory.revision}</span>
                  </div>
                  <p className="mt-[8px] whitespace-pre-wrap text-[13px] leading-[1.7] text-[#464c5e]">{memoryText(memory)}</p>
                </div>
                {memory.status === 'candidate' && (memory.scope === 'user' || auth.user.role !== 'user') && (
                  <div className="flex shrink-0 flex-wrap justify-end gap-[6px]">
                    <Button type="button" size="sm" variant="outline" onClick={() => void editDraft(memory)}>编辑</Button>
                    <Button type="button" size="sm" onClick={() => void govern(memory, 'verify')}>审核通过</Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => void govern(memory, 'reject')}>驳回</Button>
                    <Button type="button" size="sm" variant="outline" onClick={() => void govern(memory, 'supersede')}>替代</Button>
                    <Button type="button" size="sm" variant="destructive" onClick={() => void govern(memory, 'delete')}>删除</Button>
                  </div>
                )}
              </div>
              <div className="mt-[14px] flex flex-wrap items-center gap-[14px] border-t border-[#eef1f6] pt-[10px] text-[11px] text-[#858b9c]">
                <span>{memory.scope === 'user' ? '个人范围' : '项目范围'}</span>
                <span>来源 {memory.source_count} 条</span>
                <span>{memory.projection_status || 'projection pending'}</span>
                <span className="ml-auto">更新于 {formatDateTime(memory.updated_at)}</span>
                <Button type="button" size="sm" variant="ghost" onClick={() => void toggleDetail(memory)}>
                  {detail?.memory_id === memory.memory_id ? '收起详情' : '查看来源与审计'}
                </Button>
              </div>
              {detail?.memory_id === memory.memory_id && (
                <div className="mt-[12px] grid gap-[10px] border-t border-[#eef1f6] pt-[12px] text-[11px] text-[#68728a]">
                  <div>
                    <div className="mb-[4px] font-medium text-[#464c5e]">来源</div>
                    {detail.sources.length === 0 ? '暂无有效来源' : detail.sources.map((source) => (
                      <div key={source.source_id} className="rounded-[6px] bg-[#fafbfc] px-[8px] py-[5px]">
                        {source.source_kind || 'extracted'} · 会话 {source.session_id ?? '-'} · 消息 {source.message_id ?? '-'} · {source.valid ? '有效' : '已失效'}
                      </div>
                    ))}
                  </div>
                  <div>
                    <div className="mb-[4px] font-medium text-[#464c5e]">审计事件</div>
                    <div>{Array.isArray(detail.audit.events) ? detail.audit.events.length : 0} 条（详情接口仅返回当前授权范围内的 Catalog 审计摘要）</div>
                  </div>
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      {scope === 'user' && consents.length > 0 && (
        <section className="mt-[18px] rounded-[14px] border border-[#e3e7f1] bg-white px-[20px] py-[16px]">
          <div className="flex items-center justify-between gap-[12px]">
            <div>
              <h2 className="text-[14px] font-medium text-[#18181a]">个人记忆授权记录</h2>
              <p className="mt-[4px] text-[11px] text-[#858b9c]">授权范围由服务端按消息快照固化；撤销后不会再被检索或重放。</p>
            </div>
          </div>
          <div className="mt-[10px] grid gap-[6px]">
            {consents.map((consent) => (
              <div key={consent.consent_event_id} className="flex flex-wrap items-center gap-[8px] rounded-[8px] bg-[#fafbfc] px-[10px] py-[8px] text-[11px] text-[#68728a]">
                <span>会话 #{consent.session_id}</span>
                <span>来源 {consent.source_count} 条</span>
                <span className={consent.status === 'active' ? 'text-[#28784f]' : 'text-[#858b9c]'}>{consent.status === 'active' ? '有效' : '已撤销'}</span>
                <span className="ml-auto">{formatDateTime(consent.granted_at)}</span>
                {consent.status === 'active' && (
                  <Button type="button" size="sm" variant="outline" onClick={() => void revokeConsent(consent)}>撤销授权</Button>
                )}
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
