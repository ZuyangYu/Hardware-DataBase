import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../../api/client';
import type { DepartmentView, OkResponse, UserView } from '../../api/types';
import { ROLE_LABELS } from '../../auth';
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
import { AdminPageFrame } from '@/components/admin/AdminPageShell';

/** system 部门受保护,不可删(后端也会拒)。 */
const PROTECTED_DEPARTMENT_NAMES = new Set(['system', '系统']);

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

export default function DepartmentsPage({ auth, onLogout }: Props) {
  const [departments, setDepartments] = useState<DepartmentView[]>([]);
  const [users, setUsers] = useState<UserView[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [usersLoaded, setUsersLoaded] = useState(false);
  const [expandedDepartments, setExpandedDepartments] = useState<Set<number>>(
    () => new Set(),
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DepartmentView | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    setUsersLoaded(false);
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
    api
      .get<UserView[]>('/api/v1/users')
      .then((rows) => {
        if (!cancelled) setUsers(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载员工失败');
      })
      .finally(() => {
        if (!cancelled) setUsersLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  function toggleDepartment(departmentId: number) {
    setExpandedDepartments((current) => {
      const next = new Set(current);
      if (next.has(departmentId)) next.delete(departmentId);
      else next.add(departmentId);
      return next;
    });
  }

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
        render: (d) => {
          const expanded = expandedDepartments.has(d.id);
          const members = users.filter((user) => user.department_id === d.id);
          const membersId = `department-members-${d.id}`;
          return (
            <div className="min-w-0">
              <button
                type="button"
                aria-expanded={expanded}
                aria-controls={membersId}
                onClick={() => toggleDepartment(d.id)}
                className="inline-flex max-w-full items-center gap-[6px] rounded-[6px] text-left font-medium text-[#18181a] transition-colors hover:text-[#516bc4] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#91a5ef] focus-visible:ring-offset-1"
              >
                <AppIcon
                  name="arrow"
                  size={13}
                  className={expanded ? 'rotate-90 transition-transform' : 'transition-transform'}
                />
                <span className="truncate">{d.name}</span>
              </button>
              {expanded && (
                <div
                  id={membersId}
                  className="mt-[8px] grid gap-[5px] rounded-[8px] bg-[#f8f9fc] px-[10px] py-[8px] text-[11px]"
                >
                  {!usersLoaded ? (
                    <span className="text-[#858b9c]">加载员工中…</span>
                  ) : members.length === 0 ? (
                    <span className="text-[#858b9c]">暂无员工</span>
                  ) : (
                    members.map((member) => (
                      <div
                        key={member.id}
                        className="flex min-w-0 flex-wrap items-center gap-x-[8px] gap-y-[2px]"
                      >
                        <span className="min-w-0 truncate font-medium text-[#464c5e]">
                          {member.username}
                        </span>
                        <span className="text-[#858b9c]">{ROLE_LABELS[member.role]}</span>
                        <span className={member.is_active ? 'text-[#138a55]' : 'text-[#858b9c]'}>
                          {member.is_active ? '启用' : '停用'}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          );
        },
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
        className: 'whitespace-nowrap',
        render: (d) => {
          const protectedDept = PROTECTED_DEPARTMENT_NAMES.has(d.name);
          return (
            <button
              type="button"
              disabled={protectedDept}
              onClick={() => setDeleteTarget(d)}
              className="inline-flex h-[28px] items-center gap-[4px] whitespace-nowrap rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#d20b0b] transition-colors hover:border-[#f3b0b0] hover:bg-[#fce7e7] disabled:cursor-not-allowed disabled:text-[#b3b8c4] disabled:hover:border-[#e3e7f1] disabled:hover:bg-white"
              title={protectedDept ? '系统部门不可删除' : '删除部门'}
            >
              <AppIcon name="trash" size={13} />
              删除
            </button>
          );
        },
      },
    ],
    [expandedDepartments, users, usersLoaded],
  );

  return (
    <AdminPageFrame>
      <AppHeader
        title="部门管理"
        description="创建、删除部门。部门下仍有用户或知识库时无法删除。"
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap items-center justify-end gap-[12px]">
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
    </AdminPageFrame>
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
            className="h-[32px] min-w-[80px] rounded-[10px] border-[#e3e7f1] bg-white text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
          >
            取消
          </Button>
          <Button
            onClick={submit}
            disabled={submitting}
            className="h-[32px] min-w-[80px] rounded-[10px] bg-[#18181a] text-[14px] text-white hover:bg-[#303030]"
          >
            {submitting ? '创建中' : '创建'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
