import { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import { isDeptAdmin, isSystemAdmin, type AuthSession } from '../auth';
import type { KbView, OkResponse } from '../api/types';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Paginator } from '@/components/Paginator';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Dialog, DialogContent, DialogHeader, DialogTitle, Input, Label } from '@/components/ui';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

const PERMISSION_LABELS: Record<string, string> = {
  read: '可读',
  write: '可写',
  admin: '可管理',
};

const PAGE_SIZE = 10;

type Props = {
  auth: AuthSession;
  kbs: KbView[];
  kbsLoaded: boolean;
  onLogout: () => void;
  onRefresh: () => void;
};

export default function KbListPage({ auth, kbs, kbsLoaded, onLogout, onRefresh }: Props) {
  const navigate = useNavigate();
  const sysAdmin = isSystemAdmin(auth.user);
  const deptAdmin = isDeptAdmin(auth.user);
  const [page, setPage] = useState(1);
  const [createOpen, setCreateOpen] = useState(false);
  const [newKbName, setNewKbName] = useState('');
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<KbView | null>(null);
  const [deleting, setDeleting] = useState(false);
  const canManageContent = useCallback(
    (kb: KbView) =>
      kb.permission === 'write' ||
      kb.permission === 'admin' ||
      (deptAdmin && kb.department_id != null && kb.department_id === auth.user.department_id),
    [auth.user.department_id, deptAdmin],
  );

  function openKb(kb: KbView, target: 'chat' | 'content') {
    if (sysAdmin) {
      notify.error('system_admin 是治理角色,不能访问知识库内容');
      return;
    }
    if (target === 'content' && !canManageContent(kb)) {
      notify.error('没有文件管理权限');
      return;
    }
    if (target === 'chat') {
      navigate(`/chat?kb=${encodeURIComponent(kb.name)}`);
    } else {
      navigate(`/kbs/${encodeURIComponent(kb.name)}/files`);
    }
  }

  async function handleCreateKb() {
    const name = newKbName.trim();
    if (!name) {
      notify.error('请输入知识库名称');
      return;
    }
    setCreating(true);
    try {
      await api.post<OkResponse>('/api/v1/kbs', { name });
      notify.success('知识库已创建');
      setNewKbName('');
      setCreateOpen(false);
      setPage(1);
      onRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建失败');
    } finally {
      setCreating(false);
    }
  }

  async function handleDeleteKb() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.delete<OkResponse>(`/api/v1/kbs/${encodeURIComponent(deleteTarget.name)}`);
      notify.success('知识库已删除');
      setDeleteTarget(null);
      setPage(1);
      onRefresh();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  const columns: DataTableColumn<KbView>[] = useMemo(
    () => [
      {
        key: 'name',
        title: '名称',
        render: (kb) => (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate font-medium text-[#18181a]">{kb.name}</span>
            {(kb.permission || deptAdmin) && kb.department_name && (
              <span className="truncate text-[11px] text-[#858b9c]">{kb.department_name}</span>
            )}
          </div>
        ),
      },
      {
        key: 'permission',
        title: '权限',
        width: 100,
        render: (kb) =>
          kb.permission ? (
            <span className="inline-flex rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">
              {PERMISSION_LABELS[kb.permission] ?? kb.permission}
            </span>
          ) : (
            <span className="text-[#b3b8c4]">-</span>
          ),
      },
      {
        key: 'status',
        title: '状态',
        width: 100,
        render: (kb) =>
          kb.registered ? (
            <span className="text-[#2cb360]">已登记</span>
          ) : (
            <span className="text-[#b45309]">未登记</span>
          ),
      },
      {
        key: 'actions',
        title: '操作',
        width: 220,
        align: 'right',
        render: (kb) => (
          <div className="flex justify-end gap-[8px]">
            <button
              type="button"
              onClick={() => openKb(kb, 'chat')}
              className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] text-white transition-colors hover:bg-[#303030]"
            >
              对话
            </button>
            {canManageContent(kb) && (
              <button
                type="button"
                onClick={() => openKb(kb, 'content')}
                className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]"
              >
                内容管理
              </button>
            )}
            <button
              type="button"
              disabled={kb.permission !== 'admin' || sysAdmin}
              onClick={(event) => {
                event.stopPropagation();
                setDeleteTarget(kb);
              }}
              className={cn(
                'inline-flex h-[28px] items-center rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#d20b0b] transition-colors hover:border-[#f3b0b0] hover:bg-[#fce7e7]',
                (kb.permission !== 'admin' || sysAdmin) && 'cursor-not-allowed opacity-40',
              )}
              title={kb.permission === 'admin' && !sysAdmin ? '删除知识库' : '需要知识库 admin 权限'}
            >
              删除
            </button>
          </div>
        ),
      },
    ],
    [canManageContent, deptAdmin, sysAdmin],
  );

  const pageCount = Math.max(1, Math.ceil(kbs.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const paged = kbs.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  const queryableCount = sysAdmin ? 0 : kbs.length;
  const unregisteredCount = kbs.filter((kb) => !kb.registered).length;

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="全部知识库"
        description={
          sysAdmin
            ? '你是系统管理员(治理角色),可查看知识库登记信息,但不能访问库内容。'
            : '管理知识库名称、授权状态和删除操作;内容处理从具体知识库进入,对话可在侧边栏独立打开并选择挂载。'
        }
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap items-center justify-end gap-[12px]">
        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={onRefresh}>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
        {deptAdmin && (
          <Button
            onClick={() => setCreateOpen(true)}
            className="h-[36px] gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
          >
            <AppIcon name="plus" size={14} />
            新建
          </Button>
        )}
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]">
          <StatCard label="知识库总数" value={kbs.length} />
          <StatCard label="可访问" value={queryableCount} tone="green" />
          <StatCard label="未登记" value={unregisteredCount} />
        </div>

        {!kbsLoaded ? (
          <div className="grid gap-[10px]">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-[56px] rounded-[10px]" />
            ))}
          </div>
        ) : kbs.length === 0 ? (
          <div className="py-[48px] text-center text-[13px] text-[#858b9c]">
            暂无可访问的知识库,请联系部门管理员授权。
          </div>
        ) : (
          <>
            <DataTable
              columns={columns}
              data={paged}
              rowKey={(kb) => `${kb.kb_id ?? 'none'}:${kb.department_id ?? 'none'}:${kb.name}`}
              size="compact"
              emptyText="暂无知识库"
            />
            <Paginator page={currentPage} pageCount={pageCount} onChange={setPage} />
          </>
        )}
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent className="w-[calc(100%-32px)] max-w-[420px] rounded-[16px] p-0">
          <DialogHeader className="px-[24px] pt-[20px]">
            <DialogTitle className="text-[16px] font-semibold text-[#18181a]">新建知识库</DialogTitle>
          </DialogHeader>
          <div className="grid gap-[14px] px-[24px] pb-[20px]">
            <div className="grid gap-[4px]">
              <Label className="text-[12px] text-[#464c5e]">知识库名称</Label>
              <Input value={newKbName} onChange={(e) => setNewKbName(e.target.value)} placeholder="例如 project_alpha" autoFocus />
            </div>
            <div className="flex justify-end gap-[8px]">
              <Button variant="outline" className="h-[32px] rounded-[10px] px-[14px]" onClick={() => setCreateOpen(false)}>
                取消
              </Button>
              <Button
                onClick={handleCreateKb}
                disabled={creating}
                className="h-[32px] rounded-[10px] bg-[#18181a] px-[14px] text-white hover:bg-[#303030]"
              >
                {creating ? '创建中' : '创建'}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open: boolean) => {
          if (!open) setDeleteTarget(null);
        }}
        title={<>删除知识库「{deleteTarget?.name}」</>}
        description="删除后该知识库及其文档、归档和索引将不可恢复。"
        confirmText="删除"
        loading={deleting}
        destructive
        onConfirm={handleDeleteKb}
      />
    </div>
  );
}
