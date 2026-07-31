import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../../api/client';
import type { DepartmentView, OkResponse } from '../../api/types';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
} from '@/components/ui';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

export default function DepartmentsPage({ auth, onLogout }: Props) {
  const [departments, setDepartments] = useState<DepartmentView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DepartmentView | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    api
      .get<DepartmentView[]>('/api/v1/departments')
      .then((rows) => {
        if (!cancelled) setDepartments(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载部门失败');
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

  async function handleDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api.delete<OkResponse>(`/api/v1/departments/${deleteTarget.id}`);
      notify.success('部门已删除');
      setDepartments((prev) => prev.filter((d) => d.id !== deleteTarget.id));
      setDeleteTarget(null);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  const columns: DataTableColumn<DepartmentView>[] = useMemo(
    () => [
      {
        key: 'name',
        title: '部门名称',
        render: (d) => (
          <span className="truncate font-medium text-[#18181a]">{d.name}</span>
        ),
      },
      {
        key: 'id',
        title: 'ID',
        width: 80,
        render: (d) => <span className="text-[12px] text-[#858b9c]">{d.id}</span>,
      },
      {
        key: 'actions',
        title: '操作',
        width: 120,
        align: 'right',
        render: (d) => {
          return (
            <button
              type="button"
              onClick={() => setDeleteTarget(d)}
              className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#d20b0b] transition-colors hover:border-[#f3b0b0] hover:bg-[#fce7e7]"
              title="删除部门"
            >
              <AppIcon name="trash" size={13} />
              删除
            </button>
          );
        },
      },
    ],
    [],
  );

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="部门管理"
        description="创建、删除部门。部门下仍有用户或知识库时无法删除。"
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="page-toolbar page-toolbar-end mt-[20px] mb-[16px]">
        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => load()}>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
        <Button
          onClick={() => setCreateOpen(true)}
          className="h-[36px] gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
        >
          <AppIcon name="plus" size={14} />
          创建部门
        </Button>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]">
          <StatCard label="部门总数" value={departments.length} />
        </div>

        {!loaded ? (
          <div className="grid gap-[10px]">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-[56px] rounded-[10px]" />
            ))}
          </div>
        ) : (
          <DataTable
            columns={columns}
            data={departments}
            rowKey={(d) => d.id}
            size="compact"
            emptyText="暂无部门"
          />
        )}
      </div>

      <CreateDepartmentDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => load()}
      />

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(o: boolean) => {
          if (!o) setDeleteTarget(null);
        }}
        title={<>删除部门「{deleteTarget?.name}」</>}
        description="删除后不可恢复。若部门下仍有用户或知识库,后端将拒绝删除。"
        confirmText="删除"
        loading={deleting}
        destructive
        onConfirm={handleDelete}
      />
    </div>
  );
}

function CreateDepartmentDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    if (!name.trim()) {
      notify.error('请输入部门名称');
      return;
    }
    setSubmitting(true);
    try {
      await api.post<DepartmentView>('/api/v1/departments', { name: name.trim() });
      notify.success('部门已创建');
      onOpenChange(false);
      setName('');
      onCreated();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex w-[calc(100%-32px)] max-w-[420px] flex-col rounded-[16px] p-0">
        <DialogHeader className="px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">创建部门</DialogTitle>
        </DialogHeader>
        <div className="grid gap-[6px] px-[24px] py-[16px]">
          <Label className="text-[12px] text-[#464c5e]">部门名称</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 硬件设计部" autoFocus />
        </div>
        <div className="flex items-center justify-end gap-[8px] px-[24px] pb-[20px]">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
          >
            取消
          </Button>
          <Button
            onClick={submit}
            disabled={submitting}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] text-[14px] text-white hover:bg-[#303030]"
          >
            {submitting ? '创建中' : '创建'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
