import { api } from '../api/client';
import type { KbView, OkResponse } from '../api/types';

export function kbVisibilityLabel(kb: Pick<KbView, 'is_shared'>): string {
  return kb.is_shared ? '部门共享' : '私有';
}

export function kbDeletionWarning(kb: Pick<KbView, 'is_shared' | 'is_owner'> | null): string {
  if (!kb?.is_shared) return '';
  return `${kb.is_owner ? '' : '你不是该知识库的所有者。'}这是部门共享知识库，此操作会影响全部门成员，而不只是你个人。`;
}

export function kbSharingDescription(isShared: boolean): string {
  return isShared
    ? '改为私有后，仅所有者可访问，部门其他成员将失去访问和管理权限。已有内容不会删除。'
    : '共享后，本部门其他成员将获得完整 admin 权限，可查看、上传、删除文件和删除整个知识库。只有所有者可更改共享状态。';
}

export async function updateKbSharing(kb: KbView, isShared: boolean): Promise<void> {
  if (!kb.is_owner) throw new Error('只有所有者可以更改共享状态');
  const result = await api.patch<OkResponse>(`/api/v1/kbs/${encodeURIComponent(kb.name)}/sharing`, {
    is_shared: isShared,
  });
  if (!result.ok) throw new Error(result.message || '更新共享状态失败');
}
