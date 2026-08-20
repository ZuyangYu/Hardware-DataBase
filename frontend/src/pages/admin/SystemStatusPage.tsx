import { useCallback, useEffect, useState } from 'react';

import { api } from '../../api/client';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { StatCard } from '@/components/StatCard';

type Props = { auth: AuthSession; onLogout: () => void };
type StatusPayload = {
  ready?: { status?: string; dependencies?: Record<string, { status?: string; error?: string }> };
  dependencies?: { status?: string; dependencies?: Record<string, { status?: string; count?: number; error?: string }> };
  tasks?: {
    total?: number;
    completed?: number;
    failed?: number;
    cancelled?: number;
    failure_rate?: number;
    avg_queue_ms?: number | null;
    avg_first_token_ms?: number | null;
    avg_total_ms?: number | null;
  };
};

const statusText: Record<string, string> = { up: '正常', ready: '就绪', degraded: '降级', down: '故障', not_ready: '未就绪' };

export default function SystemStatusPage({ auth, onLogout }: Props) {
  const [payload, setPayload] = useState<StatusPayload | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    api
      .get<StatusPayload>('/api/v1/system/status')
      .then(setPayload)
      .catch((error) => notify.error(error instanceof Error ? error.message : '系统状态读取失败'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const dependencies = payload?.dependencies?.dependencies ?? {};
  const taskStatuses = {
    completed: payload?.tasks?.completed ?? 0,
    failed: payload?.tasks?.failed ?? 0,
    cancelled: payload?.tasks?.cancelled ?? 0,
  };

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="系统状态"
        description="查看 API、worker、RAGFlow、LLM 与持久化任务状态。"
        userName={auth.user.username}
        onLogout={onLogout}
      />
      <div className="mt-[20px] flex items-center gap-[10px]">
        <Button variant="outline" onClick={load} disabled={loading}>刷新</Button>
        <span className="text-[13px] text-[#858b9c]">
          总体状态：{statusText[payload?.dependencies?.status ?? ''] ?? (loading ? '读取中' : '-')}
        </span>
      </div>
      <div className="mt-[16px] grid gap-[12px] md:grid-cols-3">
        {Object.entries(dependencies).map(([name, item]) => (
          <div key={name} className="rounded-[14px] bg-white p-[16px] shadow-[0_2px_10px_rgba(0,0,0,0.05)]">
            <div className="text-[13px] text-[#858b9c]">{name}</div>
            <div className="mt-[8px] text-[20px] font-semibold text-[#18181a]">{statusText[item.status ?? ''] ?? item.status ?? '-'}</div>
            {item.count != null && <div className="mt-[4px] text-[12px] text-[#858b9c]">实例数：{item.count}</div>}
            {item.error && <div className="mt-[4px] text-[12px] text-[#d20b0b]">{item.error}</div>}
          </div>
        ))}
      </div>
      <div className="mt-[16px] rounded-[16px] bg-white p-[18px] shadow-[0_2px_10px_rgba(0,0,0,0.05)]">
        <div className="mb-[12px] text-[14px] font-semibold text-[#18181a]">最近 24 小时任务</div>
        <div className="flex flex-wrap gap-[12px]">
          <StatCard label="任务总数" value={payload?.tasks?.total ?? 0} />
          {Object.entries(taskStatuses).map(([name, value]) => <StatCard key={name} label={name} value={value} />)}
        </div>
      </div>
    </div>
  );
}
