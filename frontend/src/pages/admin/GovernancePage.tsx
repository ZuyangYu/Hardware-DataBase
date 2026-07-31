import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../../api/client';
import type { KbSummaryView } from '../../api/types';
import type { AuthSession } from '../../auth';
import { isSystemAdmin } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

export default function GovernancePage({ auth, onLogout }: Props) {
  const sysAdmin = isSystemAdmin(auth.user);
  const navigate = useNavigate();
  const [rows, setRows] = useState<KbSummaryView[]>([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    api
      .get<KbSummaryView[]>('/api/v1/governance/kb-summaries')
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载治理数据失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  const anomalyCount = rows.filter((r) => r.issue_flags.length > 0).length;
  const unassignedCount = rows.filter((r) => !r.department_id).length;
  const totalFiles = rows.reduce((s, r) => s + r.files, 0);
  const totalParsing = rows.reduce((s, r) => s + r.parsing, 0);
  const totalFailed = rows.reduce((s, r) => s + r.failed, 0);

  const columns: DataTableColumn<KbSummaryView>[] = useMemo(
    () => [
      {
        key: 'name',
        title: '知识库',
        render: (r) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{r.name}</span>
            {r.owner_username && (
              <span className="truncate text-[11px] text-[#858b9c]">owner: {r.owner_username}</span>
            )}
          </div>
        ),
      },
      {
        key: 'department',
        title: '部门',
        width: 130,
        render: (r) => (
          <span className="truncate text-[13px] text-[#464c5e]">{r.department_name ?? '-'}</span>
        ),
      },
      {
        key: 'files',
        title: '文件',
        width: 70,
        align: 'right',
        render: (r) => <span className="text-[13px] text-[#464c5e]">{r.files}</span>,
      },
      {
        key: 'parsing',
        title: '解析中',
        width: 80,
        align: 'right',
        render: (r) =>
          r.parsing > 0 ? (
            <span className="text-[13px] text-[#b45309]">{r.parsing}</span>
          ) : (
            <span className="text-[13px] text-[#b3b8c4]">0</span>
          ),
      },
      {
        key: 'failed',
        title: '失败',
        width: 70,
        align: 'right',
        render: (r) =>
          r.failed > 0 ? (
            <span className="text-[13px] text-[#d20b0b]">{r.failed}</span>
          ) : (
            <span className="text-[13px] text-[#b3b8c4]">0</span>
          ),
      },
      {
        key: 'permission_count',
        title: '授权',
        width: 70,
        align: 'right',
        render: (r) => <span className="text-[13px] text-[#464c5e]">{r.permission_count}</span>,
      },
      {
        key: 'registered',
        title: '状态',
        width: 90,
        render: (r) =>
          r.registered ? (
            <span className="text-[#2cb360]">已登记</span>
          ) : (
            <span className="text-[#b45309]">未登记</span>
          ),
      },
      {
        key: 'issues',
        title: '问题',
        width: 190,
        render: (r) =>
          r.issue_flags.length === 0 ? (
            <span className="text-[12px] text-[#b3b8c4]">-</span>
          ) : (
            <div className="flex flex-wrap gap-[4px]">
              {r.issue_flags.map((flag) => (
                <span
                  key={flag}
                  className="inline-flex rounded-full bg-[#fce7e7] px-[8px] py-[2px] text-[11px] text-[#d20b0b]"
                >
                  {flag}
                </span>
              ))}
            </div>
          ),
      },
      {
        key: 'actions',
        title: '处理',
        width: 100,
        align: 'right',
        render: (r) => (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              navigate(`/admin/kb-permissions?kb=${encodeURIComponent(r.name)}`);
            }}
            title="进入授权/重挂页"
            className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]"
          >
            <AppIcon name="lock" size={13} />
            授权
          </button>
        ),
      },
    ],
    [navigate],
  );

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="知识库治理"
        description={
          sysAdmin
            ? '全局知识库治理视图:文件统计、解析异常、授权与归属问题。'
            : '本部门知识库治理视图:文件统计与解析异常。'
        }
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap items-center justify-end gap-[12px]">
        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => load()}>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]">
          <StatCard label="知识库总数" value={rows.length} />
          <StatCard label="异常" value={anomalyCount} tone={anomalyCount > 0 ? 'red' : 'green'} />
          {sysAdmin && <StatCard label="未分配部门" value={unassignedCount} />}
          <StatCard label="文件总数" value={totalFiles} />
          <StatCard label="解析中" value={totalParsing} />
          <StatCard label="解析失败" value={totalFailed} tone={totalFailed > 0 ? 'red' : 'green'} />
        </div>

        {!loaded ? (
          <div className="grid gap-[10px]">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-[56px] rounded-[10px]" />
            ))}
          </div>
        ) : (
          <DataTable
            columns={columns}
            data={rows}
            rowKey={(r) => `${r.kb_id ?? 'none'}:${r.department_id ?? 'none'}:${r.name}`}
            size="compact"
            emptyText="暂无知识库"
          />
        )}
      </div>
    </div>
  );
}
