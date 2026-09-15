import { useCallback, useEffect, useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';

import { api } from '../../api/client';
import type {
  AssignKbPayload,
  DepartmentView,
  KbView,
  OkResponse,
  UserView,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

function kbOptionValue(kb: KbView): string {
  if (kb.kb_id != null) return `kb:${kb.kb_id}`;
  return `name:${kb.department_id ?? 'none'}:${kb.name}`;
}

/** 知识库挂载: 系统管理员把 KB 归属到部门并指定部门员工为负责人。 */
export default function KbMountPage({ auth, onLogout }: Props) {
  const location = useLocation();
  const [kbs, setKbs] = useState<KbView[]>([]);
  const [kbsLoaded, setKbsLoaded] = useState(false);
  const [selectedKbKey, setSelectedKbKey] = useState<string>('');
  const [departments, setDepartments] = useState<DepartmentView[]>([]);
  const [users, setUsers] = useState<UserView[]>([]);

  const [assignDeptId, setAssignDeptId] = useState<string>('');
  const [assignOwnerId, setAssignOwnerId] = useState<string>('none');
  const [assigning, setAssigning] = useState(false);

  const loadKbs = useCallback(() => {
    const queryKb = new URLSearchParams(location.search).get('kb') || '';
    setKbsLoaded(false);
    api
      .get<KbView[]>('/api/v1/kbs')
      .then((rows) => {
        setKbs(rows);
        setSelectedKbKey((cur) => {
          if (queryKb) {
            const matched = rows.find((kb) => kb.name === queryKb);
            if (matched) return kbOptionValue(matched);
          }
          if (cur && rows.some((kb) => kbOptionValue(kb) === cur)) return cur;
          return rows[0] ? kbOptionValue(rows[0]) : '';
        });
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载知识库失败'))
      .finally(() => setKbsLoaded(true));
  }, [location.search]);

  useEffect(() => {
    loadKbs();
  }, [loadKbs]);

  useEffect(() => {
    api.get<DepartmentView[]>('/api/v1/departments').then(setDepartments).catch(() => undefined);
    api.get<UserView[]>('/api/v1/users').then(setUsers).catch(() => undefined);
  }, []);

  const selectedKbView = useMemo(
    () => kbs.find((k) => kbOptionValue(k) === selectedKbKey),
    [kbs, selectedKbKey],
  );
  const assignOwnerOptions = useMemo(
    () =>
      users.filter(
        (u) => u.role === 'employee' && assignDeptId && u.department_id === Number(assignDeptId),
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
      notify.success('知识库已挂载到新部门');
      setAssignDeptId('');
      setAssignOwnerId('none');
      loadKbs();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '挂载失败');
    } finally {
      setAssigning(false);
    }
  }

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="知识库挂载"
        description="把知识库归属到部门并指定部门员工为负责人; 该部门员工自动获得这个知识库的完整访问权限。"
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
        <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => loadKbs()}>
          <AppIcon name="refresh" size={14} />
          刷新
        </Button>
        {selectedKbView?.department_name && (
          <span className="text-[12px] text-[#858b9c]">
            当前归属:<span className="text-[#464c5e]">{selectedKbView.department_name}</span>
          </span>
        )}
      </div>

      {!kbsLoaded ? (
        <div className="py-[48px] text-center text-[13px] text-[#858b9c]">加载中…</div>
      ) : !selectedKbView ? (
        <div className="py-[48px] text-center text-[13px] text-[#858b9c]">请选择一个知识库。</div>
      ) : (
        <div className="flex flex-col gap-[20px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          <div className="flex flex-col gap-[12px]">
            <h3 className="text-[14px] font-semibold text-[#18181a]">挂载到部门</h3>
            <p className="text-[12px] text-[#858b9c]">
              把该知识库重新挂载到另一个部门,并可指定该部门的员工为负责人。
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
                <span className="text-[11px] text-[#858b9c]">负责人(该部门员工)</span>
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
                挂载
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
