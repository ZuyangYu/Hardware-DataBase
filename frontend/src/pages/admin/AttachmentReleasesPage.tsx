/**
 * AttachmentReleasesPage -- 部门知识库管理员的附件候选发布审核队列。
 *
 * 待审列表是跨会话的：同一知识库里其他用户提交的附件修订候选也会出现。
 * 审核动作只调用 REST：通过时服务端生成独立正式副本，驳回必须带原因；
 * 候选与正式发布严格分离，页面绝不把审核中当成已完成。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, apiDownload } from '../../api/client';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Textarea } from '@/components/ui/textarea';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';

export type PendingAttachmentRelease = {
  revision_id: string;
  session_id: number;
  kb_name: string;
  session_title: string;
  owner_user_id: number;
  candidate_filename: string;
  parent_filename: string;
  operation: string;
  change_summary: string;
  release_state: string;
  release_comment: string;
  release_requested_at: string | null;
  candidate_download_url: string;
};

type ViewProps = {
  items: PendingAttachmentRelease[];
  loading: boolean;
  busyRevisionId?: string | null;
  onApprove: (item: PendingAttachmentRelease) => void;
  onReject: (item: PendingAttachmentRelease) => void;
  onDownload: (item: PendingAttachmentRelease) => void;
  onRefresh: () => void;
};

function formatTime(value: string | null): string {
  if (!value) return '-';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

export function AttachmentReleasesView({
  items,
  loading,
  busyRevisionId = null,
  onApprove,
  onReject,
  onDownload,
  onRefresh,
}: ViewProps) {
  const rows = useMemo(() => items, [items]);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-[12px] px-[20px] pb-[20px]">
      <div className="flex items-center justify-between gap-[12px]">
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="text-[13px] text-[#858b9c]">
            候选与正式发布严格分离：通过后生成独立正式文件，驳回会保留候选并说明原因。
          </span>
        </div>
        <Button
          type="button"
          variant="outline"
          className={OUTLINE_ACTION_BUTTON_CLASS}
          onClick={onRefresh}
          disabled={loading}
        >
          刷新
        </Button>
      </div>
      {loading && rows.length === 0 ? (
        <div className="flex flex-col gap-[8px]">
          <Skeleton className="h-[52px] w-full" />
          <Skeleton className="h-[52px] w-full" />
        </div>
      ) : rows.length === 0 ? (
        <div className="rounded-[12px] border border-[#e3e7f1] bg-[#fafbfc] px-[16px] py-[24px] text-center text-[13px] text-[#858b9c]">
          当前没有待审核的附件发布申请。
        </div>
      ) : (
        <div className="overflow-hidden rounded-[12px] border border-[#e3e7f1]">
          <table className="w-full border-collapse text-left" data-attachment-release-queue>
            <thead className="bg-[#fafbfc] text-[12px] text-[#858b9c]">
              <tr>
                <th className="px-[12px] py-[8px] font-medium">知识库 / 会话</th>
                <th className="px-[12px] py-[8px] font-medium">候选文件</th>
                <th className="px-[12px] py-[8px] font-medium">变更摘要</th>
                <th className="px-[12px] py-[8px] font-medium">申请时间</th>
                <th className="px-[12px] py-[8px] font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <tr key={item.revision_id} className="border-t border-[#eef0f4] align-top" data-revision-id={item.revision_id}>
                  <td className="px-[12px] py-[10px] text-[13px] text-[#464c5e]">
                    <div className="flex flex-col gap-[2px]">
                      <span className="font-medium text-[#18181a]">{item.kb_name || '-'}</span>
                      <span className="truncate text-[11px] text-[#858b9c]">{item.session_title || `会话 ${item.session_id}`}</span>
                    </div>
                  </td>
                  <td className="px-[12px] py-[10px] text-[13px] text-[#464c5e]">
                    <div className="flex flex-col gap-[2px]">
                      <span className="font-mono text-[12px] text-[#18181a] [word-break:break-all]">{item.candidate_filename}</span>
                      <span className="text-[11px] text-[#858b9c]">基于 {item.parent_filename}</span>
                    </div>
                  </td>
                  <td className="max-w-[320px] px-[12px] py-[10px] text-[12px] text-[#464c5e] [word-break:break-word]">
                    {item.change_summary}
                    {item.release_comment && (
                      <div className="mt-[4px] text-[11px] text-[#858b9c]">申请说明：{item.release_comment}</div>
                    )}
                  </td>
                  <td className="px-[12px] py-[10px] text-[12px] text-[#68728a]">{formatTime(item.release_requested_at)}</td>
                  <td className="px-[12px] py-[10px]">
                    <div className="flex flex-wrap items-center gap-[6px]">
                      <button
                        type="button"
                        aria-label={`下载候选 ${item.candidate_filename}`}
                        onClick={() => onDownload(item)}
                        className="rounded-[8px] border border-[#e3e7f1] bg-white px-[8px] py-[2px] text-[11px] font-medium text-[#0b6cf5] transition-colors hover:border-[#c9d2e4] hover:bg-[#f4f8ff]"
                      >
                        下载候选
                      </button>
                      <button
                        type="button"
                        aria-label={`通过 ${item.candidate_filename}`}
                        disabled={busyRevisionId === item.revision_id}
                        onClick={() => onApprove(item)}
                        className="rounded-[8px] border border-[#0b6cf5] bg-[#0b6cf5] px-[8px] py-[2px] text-[11px] font-medium text-white transition-colors hover:bg-[#095ccc] disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        通过
                      </button>
                      <button
                        type="button"
                        aria-label={`驳回 ${item.candidate_filename}`}
                        disabled={busyRevisionId === item.revision_id}
                        onClick={() => onReject(item)}
                        className="rounded-[8px] border border-[#f2c7c7] bg-white px-[8px] py-[2px] text-[11px] font-medium text-[#b42318] transition-colors hover:bg-[#fff7f7] disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        驳回
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

export default function AttachmentReleasesPage({ auth, onLogout }: Props) {
  const [items, setItems] = useState<PendingAttachmentRelease[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyRevisionId, setBusyRevisionId] = useState<string | null>(null);
  const [rejectTarget, setRejectTarget] = useState<PendingAttachmentRelease | null>(null);
  const [rejectComment, setRejectComment] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const next = await api.get<PendingAttachmentRelease[]>('/api/v1/attachment-releases/pending');
      setItems(Array.isArray(next) ? next : []);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载待审列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const review = useCallback(
    async (item: PendingAttachmentRelease, decision: 'approve' | 'reject', comment: string) => {
      setBusyRevisionId(item.revision_id);
      try {
        await api.post(
          `/api/v1/conversations/${item.session_id}/attachments/revisions/${item.revision_id}/release-review`,
          { decision, comment },
        );
        setItems((prev) => prev.filter((row) => row.revision_id !== item.revision_id));
        notify.success(decision === 'approve' ? '已通过并生成正式文件' : '已驳回，候选保留');
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '审核提交失败');
      } finally {
        setBusyRevisionId(null);
      }
    },
    [],
  );

  const handleDownload = useCallback((item: PendingAttachmentRelease) => {
    void apiDownload.blob(item.candidate_download_url, item.candidate_filename).catch((error) => {
      notify.error(error instanceof Error ? error.message : '下载候选失败');
    });
  }, []);

  return (
    <>
      <AppHeader
        title="附件发布审核"
        description="部门知识库候选附件的正式发布审核"
        userName={auth.user.username}
        onLogout={onLogout}
      />
      <AttachmentReleasesView
        items={items}
        loading={loading}
        busyRevisionId={busyRevisionId}
        onApprove={(item) => void review(item, 'approve', '')}
        onReject={(item) => {
          setRejectTarget(item);
          setRejectComment('');
        }}
        onDownload={handleDownload}
        onRefresh={() => void load()}
      />
      <Dialog open={rejectTarget !== null} onOpenChange={(open) => !open && setRejectTarget(null)}>
        <DialogContent className="max-w-[440px]">
          <DialogHeader>
            <DialogTitle>驳回发布申请</DialogTitle>
          </DialogHeader>
          <p className="text-[13px] text-[#464c5e] [word-break:break-word]">
            {rejectTarget?.candidate_filename}
          </p>
          <Textarea
            value={rejectComment}
            onChange={(event) => setRejectComment(event.target.value)}
            placeholder="请说明驳回原因（会记录在修订审计中）"
            rows={3}
          />
          <DialogFooter>
            <Button type="button" variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => setRejectTarget(null)}>
              取消
            </Button>
            <Button
              type="button"
              disabled={busyRevisionId === rejectTarget?.revision_id || rejectComment.trim() === ''}
              onClick={() => {
                const target = rejectTarget;
                if (!target) return;
                setRejectTarget(null);
                void review(target, 'reject', rejectComment.trim());
              }}
            >
              确认驳回
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
