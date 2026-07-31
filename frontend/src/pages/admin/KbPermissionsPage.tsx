import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { api } from '../../api/client';
import type {
  AssignKbPayload,
  DepartmentView,
  GrantKbPermissionPayload,
  KbPermissionView,
  KbView,
  OkResponse,
  UserView,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import { isSystemAdmin, ROLE_LABELS } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';

const PERMISSION_LABELS: Record<string, string> = {
  read: '可读',
  write: '可写',
  admin: '可管理',
};

function kbOptionValue(kb: KbView): string {
  if (kb.kb_id != null) return `kb:${kb.kb_id}`;
  return `name:${kb.department_id ?? 'none'}:${kb.name}`;
}

function kbScopeQuery(kb: KbView): string {
  const params = new URLSearchParams();
  if (kb.kb_id != null) params.set('kb_id', String(kb.kb_id));
  if (kb.department_id != null) params.set('department_id', String(kb.department_id));
  const query = params.toString();
  return query ? `?${query}` : '';
}

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

export default function KbPermissionsPage({ auth, onLogout }: Props) {
  const sysAdmin = isSystemAdmin(auth.user);
  const location = useLocation();
  const [kbs, setKbs] = useState<KbView[]>([]);
  const [kbsLoaded, setKbsLoaded] = useState(false);
  const [selectedKbKey, setSelectedKbKey] = useState<string>('');
  const [perms, setPerms] = useState<KbPermissionView[]>([]);
  const [permsLoaded, setPermsLoaded] = useState(false);
  const [departments, setDepartments] = useState<DepartmentView[]>([]);
  const [users, setUsers] = useState<UserView[]>([]);

  // 授予表单(dept_admin)
  const [grantUserId, setGrantUserId] = useState<string>('');
  const [grantPerm, setGrantPerm] = useState<'read' | 'write' | 'admin'>('read');
  const [granting, setGranting] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<KbPermissionView | null>(null);
  const [revoking, setRevoking] = useState(false);

  // 重挂(sysadmin)
  const [assignDeptId, setAssignDeptId] = useState<string>('');
  const [assignOwnerId, setAssignOwnerId] = useState<string>('none');
  const [assigning, setAssigning] = useState(false);

  // 加载 KB 列表
  useEffect(() => {
    let cancelled = false;
    const queryKb = new URLSearchParams(location.search).get('kb') || '';
    setKbsLoaded(false);
    api
      .get<KbView[]>('/api/v1/kbs')
      .then((rows) => {
        if (!cancelled) {
          setKbs(rows);
          setSelectedKbKey((cur) => {
            if (queryKb) {
              const matched = rows.find((kb) => kb.name === queryKb);
              if (matched) return kbOptionValue(matched);
            }
            if (cur && rows.some((kb) => kbOptionValue(kb) === cur)) return cur;
            return rows[0] ? kbOptionValue(rows[0]) : '';
          });
        }
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载知识库失败');
      })
      .finally(() => {
        if (!cancelled) setKbsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [location.search]);

  // 加载部门 + 用户(授予下拉用)
  useEffect(() => {
    api.get<DepartmentView[]>('/api/v1/departments').then(setDepartments).catch(() => undefined);
    // dept_admin 看本部门 user;sysadmin 看全部(用于 owner 选择)
    api
      .get<UserView[]>(`/api/v1/users?include_admins=${sysAdmin ? 'true' : 'false'}`)
      .then(setUsers)
      .catch(() => undefined);
  }, [sysAdmin]);

  // 加载选中 KB 的权限列表
  const loadPerms = useCallback(() => {
    const selected = kbs.find((kb) => kbOptionValue(kb) === selectedKbKey);
    if (!selected) {
      setPerms([]);
      setPermsLoaded(true);
      return;
    }
    let cancelled = false;
    setPermsLoaded(false);
    api
      .get<KbPermissionView[]>(
        `/api/v1/kbs/${encodeURIComponent(selected.name)}/permissions${kbScopeQuery(selected)}`,
      )
      .then((rows) => {
        if (!cancelled) setPerms(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载权限失败');
      })
      .finally(() => {
        if (!cancelled) setPermsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kbs, selectedKbKey]);

  useEffect(() => {
    const cancel = loadPerms();
    return cancel;
  }, [loadPerms]);

  const selectedKbView = useMemo(
    () => kbs.find((k) => kbOptionValue(k) === selectedKbKey),
    [kbs, selectedKbKey],
  );
  const selectedKbName = selectedKbView?.name ?? '';
  const assignOwnerOptions = useMemo(
    () =>
      users.filter(
        (u) =>
          u.role === 'dept_admin' &&
          assignDeptId &&
          u.department_id === Number(assignDeptId),
      ),
    [assignDeptId, users],
  );
  const businessDepartments = useMemo(
    () => departments.filter((department) => department.name !== 'system'),
    [departments],
  );

  useEffect(() => {
    setAssignOwnerId('none');
  }, [assignDeptId]);

  async function handleGrant() {
    if (!selectedKbName || !grantUserId) {
      notify.error('请选择用户和权限');
      return;
    }
    setGranting(true);
    try {
      const payload: GrantKbPermissionPayload = {
        user_id: Number(grantUserId),
        permission: grantPerm,
      };
      await api.post<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(selectedKbName)}/permissions`,
        payload,
      );
      notify.success('权限已授予');
      setGrantUserId('');
      loadPerms();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '授权失败');
    } finally {
      setGranting(false);
    }
  }

  async function handleRevoke() {
    if (!revokeTarget || !selectedKbName) return;
    setRevoking(true);
    try {
      const target = users.find((u) => u.username === revokeTarget.username);
      if (!target) {
        notify.error('找不到用户');
        return;
      }
      await api.delete<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(selectedKbName)}/permissions/${target.id}`,
      );
      notify.success('权限已撤销');
      setRevokeTarget(null);
      loadPerms();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '撤销失败');
    } finally {
      setRevoking(false);
    }
  }

  async function handleAssign() {
    if (!selectedKbView || !assignDeptId) {
      notify.error('请选择部门');
      return;
    }
    setAssigning(true);
    try {
      const payload: AssignKbPayload = {
        department_id: Number(assignDeptId),
        owner_user_id: assignOwnerId !== 'none' ? Number(assignOwnerId) : null,
        source_kb_id: selectedKbView.kb_id ?? null,
      };
      await api.put<OkResponse>(
        `/api/v1/kbs/${encodeURIComponent(selectedKbView.name)}/assign`,
        payload,
      );
      notify.success('知识库已重挂到新部门');
      setAssignDeptId('');
      setAssignOwnerId('none');
      const rows = await api.get<KbView[]>('/api/v1/kbs');
      setKbs(rows);
      loadPerms();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重挂失败');
    } finally {
      setAssigning(false);
    }
  }

  const permColumns: DataTableColumn<KbPermissionView>[] = useMemo(
    () => [
      {
        key: 'username',
        title: '用户名',
        render: (p) => <span className="truncate font-medium text-[#18181a]">{p.username}</span>,
      },
      {
        key: 'role',
        title: '角色',
        width: 110,
        render: (p) => (
          <span className="inline-flex rounded-full bg-[#f3f4f6] px-[8px] py-[2px] text-[11px] text-[#464c5e]">
            {ROLE_LABELS[p.role]}
          </span>
        ),
      },
      {
        key: 'department',
        title: '部门',
        width: 140,
        render: (p) => <span className="truncate text-[13px] text-[#464c5e]">{p.department_name ?? '-'}</span>,
      },
      {
        key: 'permission',
        title: '权限',
        width: 90,
        render: (p) => (
          <span className="inline-flex rounded-full bg-[#e6f6ec] px-[8px] py-[2px] text-[11px] text-[#138a55]">
            {PERMISSION_LABELS[p.permission] ?? p.permission}
          </span>
        ),
      },
      {
        key: 'actions',
        title: '操作',
        width: 110,
        align: 'right',
        render: (p) =>
          // sysadmin 不能撤销(铁律:不碰 KB 内容权限操作);dept_admin 可撤销
          sysAdmin ? (
            <span className="text-[12px] text-[#b3b8c4]">-</span>
          ) : (
            <button
              type="button"
              onClick={() => setRevokeTarget(p)}
              className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#d20b0b] transition-colors hover:border-[#f3b0b0] hover:bg-[#fce7e7]"
            >
              撤销
            </button>
          ),
      },
    ],
    [sysAdmin],
  );

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="知识库授权"
        description={
          sysAdmin
            ? '查看知识库的权限授予列表,或把知识库重挂到其它部门。'
            : '为本部门用户授予或撤销知识库的访问权限。'
        }
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap items-center gap-[12px]">
        <Select value={selectedKbKey} onValueChange={setSelectedKbKey}>
          <SelectTrigger className="h-[36px] w-[260px] rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
            <SelectValue placeholder="选择知识库" />
          </SelectTrigger>
          <SelectContent>
            {kbs.map((kb) => (
              <SelectItem key={kbOptionValue(kb)} value={kbOptionValue(kb)}>
                {kb.department_name ? `${kb.name} · ${kb.department_name}` : kb.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => loadPerms()}>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
        {selectedKbView?.department_name && (
          <span className="text-[12px] text-[#858b9c]">
            当前归属:<span className="text-[#464c5e]">{selectedKbView.department_name}</span>
          </span>
        )}
      </div>

      {!selectedKbView ? (
        <div className="py-[48px] text-center text-[13px] text-[#858b9c]">请选择一个知识库。</div>
      ) : (
        <div className="flex flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          {/* 权限授予列表 */}
          <div className="flex flex-col gap-[12px]">
            <h3 className="text-[14px] font-semibold text-[#18181a]">权限授予列表</h3>
            {!permsLoaded ? (
              <div className="grid gap-[10px]">
                {[0, 1].map((i) => (
                  <Skeleton key={i} className="h-[48px] rounded-[10px]" />
                ))}
              </div>
            ) : (
              <DataTable
                columns={permColumns}
                data={perms}
                rowKey={(p) => `${p.username}-${p.permission}`}
                size="compact"
                emptyText="暂无授权用户"
              />
            )}
          </div>

          {/* 授予新权限:仅 dept_admin(sysadmin 铁律不能碰) */}
          {!sysAdmin && (
            <div className="flex flex-col gap-[12px] border-t border-[#f0f1f4] pt-[16px]">
              <h3 className="text-[14px] font-semibold text-[#18181a]">授予新权限</h3>
              <div className="grid gap-[12px] md:grid-cols-[minmax(0,1fr)_minmax(0,180px)_auto] md:items-end">
                <div className="grid min-w-0 gap-[4px]">
                  <span className="text-[11px] text-[#858b9c]">用户(本部门)</span>
                  <Select value={grantUserId} onValueChange={setGrantUserId}>
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                      <SelectValue placeholder="选择用户" />
                    </SelectTrigger>
                    <SelectContent>
                      {users.map((u) => (
                        <SelectItem key={u.id} value={String(u.id)}>
                          {u.username}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid min-w-0 gap-[4px]">
                  <span className="text-[11px] text-[#858b9c]">权限级别</span>
                  <Select value={grantPerm} onValueChange={(v) => setGrantPerm(v as 'read' | 'write' | 'admin')}>
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="read">可读</SelectItem>
                      <SelectItem value="write">可写</SelectItem>
                      <SelectItem value="admin">可管理</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <Button
                  onClick={handleGrant}
                  disabled={granting}
                  className="h-[36px] shrink-0 self-end whitespace-nowrap gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
                >
                  <AppIcon name="plus" size={14} />
                  授予
                </Button>
              </div>
            </div>
          )}

          {/* 重挂部门:仅 sysadmin */}
          {sysAdmin && (
            <div className="flex flex-col gap-[12px] border-t border-[#f0f1f4] pt-[16px]">
              <h3 className="text-[14px] font-semibold text-[#18181a]">重挂部门</h3>
              <p className="text-[12px] text-[#858b9c]">
                把该知识库重新挂载到另一个部门,并可指定该部门的部门管理员为负责人。
              </p>
              <div className="grid gap-[12px] md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-end">
                <div className="grid min-w-0 gap-[4px]">
                  <span className="text-[11px] text-[#858b9c]">目标部门</span>
                  <Select value={assignDeptId} onValueChange={setAssignDeptId}>
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                      <SelectValue placeholder="选择部门" />
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
                <div className="grid min-w-0 gap-[4px]">
                  <span className="text-[11px] text-[#858b9c]">负责人</span>
                  <Select value={assignOwnerId} onValueChange={setAssignOwnerId} disabled={!assignDeptId}>
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] bg-white text-[13px]">
                      <SelectValue placeholder="选择负责人" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">不指定</SelectItem>
                      {assignOwnerOptions.map((u) => (
                        <SelectItem key={u.id} value={String(u.id)}>
                          {u.username}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <Button
                  onClick={handleAssign}
                  disabled={assigning}
                  className="h-[36px] shrink-0 self-end whitespace-nowrap gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
                >
                  重挂
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      <ConfirmDialog
        open={revokeTarget !== null}
        onOpenChange={(o: boolean) => {
          if (!o) setRevokeTarget(null);
        }}
        title={<>撤销「{revokeTarget?.username}」的权限</>}
        description="撤销后该用户将无法访问此知识库。"
        confirmText="撤销"
        loading={revoking}
        destructive
        onConfirm={handleRevoke}
      />
    </div>
  );
}
