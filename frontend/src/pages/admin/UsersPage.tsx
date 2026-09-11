import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, isForbiddenError } from '../../api/client';
import type {
  CreateUserPayload,
  DepartmentView,
  OkResponse,
  UserView,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import { ROLE_LABELS } from '../../auth';
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

export default function UsersPage({ auth, onLogout }: Props) {
  const [users, setUsers] = useState<UserView[]>([]);
  const [departments, setDepartments] = useState<DepartmentView[]>([]);
  const [loaded, setLoaded] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserView | null>(null);
  const [resetPassword, setResetPassword] = useState('');
  const [resetting, setResetting] = useState(false);

  const load = useCallback(() => {
    let cancelled = false;
    setLoaded(false);
    api
      .get<UserView[]>('/api/v1/users')
      .then((rows) => {
        if (!cancelled) setUsers(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载用户失败');
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    // 部门下拉(注册员工时选择归属部门)
    api
      .get<DepartmentView[]>('/api/v1/departments')
      .then((rows) => {
        if (!cancelled) setDepartments(rows);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const cancel = load();
    return cancel;
  }, [load]);

  async function handleToggleActive(user: UserView, next: boolean) {
    try {
      await api.put<OkResponse>(`/api/v1/users/${user.id}/active`, { is_active: next });
      setUsers((prev) => prev.map((u) => (u.id === user.id ? { ...u, is_active: next } : u)));
      notify.success(next ? '已启用' : '已停用');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '操作失败');
    }
  }

  async function handleResetConfirm() {
    if (!resetTarget) return;
    if (!resetPassword) {
      notify.error('请输入新密码');
      return;
    }
    setResetting(true);
    try {
      await api.put<OkResponse>(`/api/v1/users/${resetTarget.id}/password`, {
        new_password: resetPassword,
      });
      notify.success('密码已重置');
      setResetTarget(null);
      setResetPassword('');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重置失败');
    } finally {
      setResetting(false);
    }
  }

  const columns: DataTableColumn<UserView>[] = useMemo(
    () => [
      {
        key: 'username',
        title: '用户名',
        render: (u) => (
          <div className="flex min-w-0 items-center gap-[8px]">
            <span className="grid size-[24px] shrink-0 place-items-center rounded-full bg-[#eef1fb] text-[12px] font-medium text-[#7e96dc]">
              {u.username.slice(0, 1).toUpperCase()}
            </span>
            <span className="truncate font-medium text-[#18181a]">{u.username}</span>
            {u.username === auth.user.username && (
              <span className="shrink-0 text-[10px] text-[#b3b8c4]">(我)</span>
            )}
          </div>
        ),
      },
      {
        key: 'role',
        title: '角色',
        width: 110,
        render: (u) => (
          <span className="inline-flex rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">
            {ROLE_LABELS[u.role]}
          </span>
        ),
      },
      {
        key: 'department',
        title: '部门',
        width: 140,
        render: (u) => (
          <span className="truncate text-[13px] text-[#464c5e]">
            {u.department_name ?? '-'}
          </span>
        ),
      },
      {
        key: 'status',
        title: '状态',
        width: 110,
        className: 'whitespace-nowrap',
        render: (u) => {
          const isSelf = u.username === auth.user.username;
          return (
            <button
              type="button"
              disabled={isSelf}
              onClick={() => handleToggleActive(u, !u.is_active)}
              className={cn(
                'inline-flex h-[24px] items-center gap-[6px] whitespace-nowrap rounded-full px-[10px] text-[11px] transition-colors',
                u.is_active
                  ? 'bg-[#e6f6ec] text-[#138a55]'
                  : 'bg-[#f3f4f6] text-[#858b9c]',
                isSelf && 'cursor-not-allowed opacity-50',
              )}
              title={isSelf ? '不能操作自己' : undefined}
            >
              <span className={cn('size-[6px] rounded-full', u.is_active ? 'bg-[#138a55]' : 'bg-[#b3b8c4]')} />
              {u.is_active ? '启用' : '停用'}
            </button>
          );
        },
      },
      {
        key: 'actions',
        title: '操作',
        width: 140,
        align: 'right',
        render: (u) => {
          const isSelf = u.username === auth.user.username;
          return (
            <button
              type="button"
              disabled={isSelf}
              onClick={() => {
                setResetTarget(u);
                setResetPassword('');
              }}
              className={cn(
                'inline-flex h-[28px] min-w-[108px] items-center justify-center gap-[4px] whitespace-nowrap rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]',
                isSelf && 'cursor-not-allowed opacity-50',
              )}
              title={isSelf ? '不能操作自己' : undefined}
            >
              <AppIcon name="lock" size={13} />
              重置密码
            </button>
          );
        },
      },
    ],
    [auth.user.username],
  );

  const activeCount = users.filter((u) => u.is_active).length;

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="员工管理"
        description="注册员工到部门、创建系统管理员、停用/启用账号、重置密码。"
        userName={auth.user.username}
        onLogout={onLogout}
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
          注册员工
        </Button>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]">
          <StatCard label="用户总数" value={users.length} />
          <StatCard label="启用" value={activeCount} tone="green" />
          <StatCard label="停用" value={users.length - activeCount} />
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
            data={users}
            rowKey={(u) => u.id}
            size="compact"
            emptyText="暂无用户"
          />
        )}
      </div>

      <CreateUserDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        departments={departments}
        onCreated={() => load()}
      />

      <ResetPasswordDialog
        target={resetTarget}
        password={resetPassword}
        onPasswordChange={setResetPassword}
        loading={resetting}
        onOpenChange={(o: boolean) => {
          if (!o) {
            setResetTarget(null);
            setResetPassword('');
          }
        }}
        onConfirm={handleResetConfirm}
      />
    </div>
  );
}

/** 重置密码对话框(独立 Dialog,带新密码输入)。 */
function ResetPasswordDialog({
  target,
  password,
  onPasswordChange,
  loading,
  onOpenChange,
  onConfirm,
}: {
  target: UserView | null;
  password: string;
  onPasswordChange: (v: string) => void;
  loading: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={target !== null} onOpenChange={onOpenChange}>
      <DialogContent className="flex w-[calc(100%-32px)] max-w-[420px] flex-col rounded-[16px] p-0">
        <DialogHeader className="px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">
            重置密码
          </DialogTitle>
        </DialogHeader>
        <div className="grid gap-[10px] px-[24px] py-[16px]">
          <p className="text-[13px] text-[#858b9c]">
            为「{target?.username}」设置新密码,用户需用新密码登录。
          </p>
          <div className="grid gap-[6px]">
            <Label className="text-[12px] text-[#464c5e]">新密码</Label>
            <Input
              type="password"
              value={password}
              onChange={(e) => onPasswordChange(e.target.value)}
              placeholder="输入新密码"
              autoFocus
            />
          </div>
        </div>
        <div className="flex items-center justify-end gap-[8px] px-[24px] pb-[20px]">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={loading}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white text-[14px] text-[#464c5e] hover:bg-[#f6f6f6]"
          >
            取消
          </Button>
          <Button
            onClick={onConfirm}
            disabled={loading}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] text-[14px] text-white hover:bg-[#303030]"
          >
            {loading ? '重置中' : '重置'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** 注册员工对话框(仅系统管理员可用; 系统管理员账号由部署环境管理)。 */
function CreateUserDialog({
  open,
  onOpenChange,
  departments,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  departments: DepartmentView[];
  onCreated: () => void;
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [departmentId, setDepartmentId] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);

  const businessDepartments = useMemo(
    () => departments.filter((d) => d.name !== 'system'),
    [departments],
  );

  async function submit() {
    if (!username.trim() || !password) {
      notify.error('请输入用户名和密码');
      return;
    }
    if (!departmentId) {
      notify.error('请选择部门');
      return;
    }
    const payload: CreateUserPayload = {
      username: username.trim(),
      password,
      role: 'employee',
      department_id: Number(departmentId),
    };
    setSubmitting(true);
    try {
      await api.post<UserView>('/api/v1/users', payload);
      notify.success('员工已注册');
      onOpenChange(false);
      setUsername('');
      setPassword('');
      setDepartmentId('');
      onCreated();
    } catch (error) {
      if (isForbiddenError(error)) {
        notify.error('无权创建账号');
      } else {
        notify.error(error instanceof Error ? error.message : '创建失败');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex w-[calc(100%-32px)] max-w-[440px] flex-col rounded-[16px] p-0">
        <DialogHeader className="px-[24px] pt-[20px]">
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">注册员工</DialogTitle>
        </DialogHeader>
        <div className="grid gap-[14px] px-[24px] py-[16px]">
          <div className="grid gap-[6px]">
            <Label className="text-[12px] text-[#464c5e]">用户名</Label>
            <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="登录用户名" />
          </div>
          <div className="grid gap-[6px]">
            <Label className="text-[12px] text-[#464c5e]">密码</Label>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="初始密码"
            />
          </div>
          <div className="grid gap-[6px]">
            <Label className="text-[12px] text-[#464c5e]">部门</Label>
            <Select value={departmentId} onValueChange={setDepartmentId}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="选择业务部门" />
              </SelectTrigger>
              <SelectContent>
                {businessDepartments.map((d) => (
                  <SelectItem key={d.id} value={String(d.id)}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex items-center justify-end gap-[8px] px-[24px] pb-[20px]">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
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
