import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, downloadBlob } from '@/api/client';
import type { ExportJobView } from '@/api/types';
import AppIcon from '@/components/AppIcon';
import { notify } from '@/components/ui/app-toast';
import { exportFormatLabel } from '@/pages/chat/exportResultModel';
import { exportJobStatusLabel } from '@/pages/chat/components/ExportMenu';

const ACTIVE_STATUSES = new Set(['queued', 'running']);
const RETRYABLE_STATUSES = new Set(['failed', 'dead_letter', 'cancelled']);

const FLOATING_TRIGGER_CLASS =
  'fixed top-[18px] right-[22px] z-40 flex size-[36px] items-center justify-center rounded-[10px] border border-[#dfe5f0] bg-white/95 text-[#464c5e] shadow-[0_5px_18px_rgba(31,41,55,0.12)] backdrop-blur transition-colors hover:border-[#b9c8e4] hover:text-[#0b6cf5]';
/** 与聊天头部其它文字按钮同款规格,嵌入页头操作区时使用。 */
const EMBEDDED_TRIGGER_CLASS =
  'inline-flex h-[30px] shrink-0 items-center gap-[5px] rounded-[8px] border border-[#e3e7f1] px-[9px] text-[11px] text-[#68728a] transition-colors hover:bg-[#f6f7fa] hover:text-[#18181a]';
const PANEL_CLASS =
  'w-[360px] max-w-[calc(100vw-32px)] rounded-[14px] border border-[#dfe5f0] bg-white p-[14px] shadow-[0_14px_40px_rgba(31,41,55,0.16)]';
const FLOATING_PANEL_CLASS = `${PANEL_CLASS} fixed top-[62px] right-[22px] z-40`;
const EMBEDDED_PANEL_CLASS = `${PANEL_CLASS} absolute right-0 top-[calc(100%+8px)] z-50`;

/** 面板定位契约;导出给测试锁定,避免再出现遮挡页面 chrome 的回归。 */
export const EXPORT_TASK_CENTER_PANEL_POSITION = {
  floating: FLOATING_PANEL_CLASS,
  embedded: EMBEDDED_PANEL_CLASS,
} as const;

export function isExportActive(status: string): boolean {
  return ACTIVE_STATUSES.has(status);
}

export function countActiveExportJobs(jobs: ExportJobView[]): number {
  return jobs.filter((job) => isExportActive(job.status)).length;
}

function mergeJob(jobs: ExportJobView[], next: ExportJobView): ExportJobView[] {
  const index = jobs.findIndex((job) => job.export_job_id === next.export_job_id);
  if (index < 0) return [next, ...jobs];
  return jobs.map((job, jobIndex) => (jobIndex === index ? next : job));
}

function jobTime(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return '';
  return new Date(timestamp).toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export type ExportTaskCenterVariant = 'floating' | 'embedded';

type Props = {
  /**
   * floating:全局悬浮入口(内容区右上角没有页面级 chrome 的页面使用)。
   * embedded:嵌入聊天页头部操作区,避免遮挡右上角会话侧栏。
   */
  variant?: ExportTaskCenterVariant;
};

export default function ExportTaskCenter({ variant = 'floating' }: Props) {
  const [jobs, setJobs] = useState<ExportJobView[]>([]);
  const [open, setOpen] = useState(false);
  const [busyIds, setBusyIds] = useState<Set<string>>(() => new Set());
  const embedded = variant === 'embedded';

  const loadJobs = useCallback(async () => {
    try {
      const next = await api.get<ExportJobView[]>('/api/v1/exports?limit=50');
      setJobs(next);
    } catch {
      // A transient poll failure must not interrupt the chat or spam toasts.
    }
  }, []);

  useEffect(() => {
    void loadJobs();
    const timer = window.setInterval(() => void loadJobs(), 5000);
    return () => window.clearInterval(timer);
  }, [loadJobs]);

  const activeCount = useMemo(() => countActiveExportJobs(jobs), [jobs]);

  async function runJobAction(job: ExportJobView, action: 'retry' | 'cancel') {
    if (busyIds.has(job.export_job_id)) return;
    setBusyIds((current) => new Set(current).add(job.export_job_id));
    try {
      const updated = await api.post<ExportJobView>(
        `/api/v1/exports/${encodeURIComponent(job.export_job_id)}/${action}`,
      );
      setJobs((current) => mergeJob(current, updated));
      notify.success(action === 'retry' ? '导出任务已重新排队' : '导出任务已取消');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新导出任务失败');
    } finally {
      setBusyIds((current) => {
        const next = new Set(current);
        next.delete(job.export_job_id);
        return next;
      });
    }
  }

  async function downloadJob(job: ExportJobView) {
    if (!job.artifact || busyIds.has(job.export_job_id)) return;
    setBusyIds((current) => new Set(current).add(job.export_job_id));
    try {
      await downloadBlob(
        job.artifact.download_url || `/api/v1/artifacts/${encodeURIComponent(job.artifact.artifact_id)}/download`,
        job.artifact.filename,
      );
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '下载导出文件失败');
    } finally {
      setBusyIds((current) => {
        const next = new Set(current);
        next.delete(job.export_job_id);
        return next;
      });
    }
  }

  const activeBadge = activeCount > 0 ? (
    embedded ? (
      <span className="inline-flex min-w-[16px] justify-center rounded-full bg-[#0b6cf5] px-[4px] text-[10px] leading-[16px] font-semibold text-white">
        {activeCount > 99 ? '99+' : activeCount}
      </span>
    ) : (
      <span className="absolute -top-[6px] -right-[6px] min-w-[17px] rounded-full bg-[#0b6cf5] px-[4px] text-center text-[10px] leading-[17px] font-semibold text-white">
        {activeCount > 99 ? '99+' : activeCount}
      </span>
    )
  ) : null;

  const trigger = (
    <button
      type="button"
      aria-label="导出任务中心"
      aria-expanded={open}
      title="查看后台导出任务"
      onClick={() => setOpen((value) => !value)}
      className={embedded ? EMBEDDED_TRIGGER_CLASS : FLOATING_TRIGGER_CLASS}
    >
      <AppIcon name="file" size={embedded ? 13 : 17} />
      {embedded && <span className="select-none whitespace-nowrap">导出任务</span>}
      {activeBadge}
    </button>
  );

  const panelBody = (
    <>
      <div className="mb-[10px] flex items-center justify-between gap-[8px]">
        <div>
          <h2 className="text-[14px] font-semibold text-[#18181a]">导出任务中心</h2>
          <p className="mt-[2px] text-[11px] text-[#858b9c]">切换页面后任务仍会在后台继续</p>
        </div>
        <button
          type="button"
          aria-label="关闭导出任务中心"
          onClick={() => setOpen(false)}
          className="flex size-[26px] items-center justify-center rounded-[7px] text-[#858b9c] hover:bg-[#f4f6fa] hover:text-[#464c5e]"
        >
          <AppIcon name="close" size={14} />
        </button>
      </div>
      {jobs.length === 0 ? (
        <div className="rounded-[9px] bg-[#f8fafc] px-[10px] py-[18px] text-center text-[12px] text-[#858b9c]">
          暂无后台导出任务
        </div>
      ) : (
        <div className="max-h-[390px] space-y-[7px] overflow-y-auto pr-[2px]">
          {jobs.slice(0, 30).map((job) => {
            const busy = busyIds.has(job.export_job_id);
            return (
              <div key={job.export_job_id} className="rounded-[9px] border border-[#edf0f5] px-[10px] py-[8px]">
                <div className="flex items-center gap-[6px] text-[12px]">
                  <span className="font-medium text-[#2d3140]">{exportFormatLabel(job.format)}</span>
                  <span className={job.status === 'succeeded' ? 'text-[#16803c]' : job.status === 'failed' || job.status === 'dead_letter' ? 'text-[#b42318]' : 'text-[#0b6cf5]'}>
                    {exportJobStatusLabel(job.status)}
                  </span>
                  <span className="ml-auto text-[10px] text-[#9aa1b1]">{jobTime(job.created_at)}</span>
                </div>
                <div className="mt-[3px] truncate text-[10px] text-[#9aa1b1]">会话 #{job.session_id}</div>
                {job.error_message && (
                  <div className="mt-[4px] break-words text-[11px] leading-[16px] text-[#b42318]">{job.error_message}</div>
                )}
                <div className="mt-[6px] flex items-center gap-[10px] text-[11px]">
                  {job.status === 'succeeded' && job.artifact && (
                    <button type="button" disabled={busy} onClick={() => void downloadJob(job)} className="font-medium text-[#0b6cf5] hover:underline disabled:opacity-50">
                      下载文件
                    </button>
                  )}
                  {RETRYABLE_STATUSES.has(job.status) && (
                    <button type="button" disabled={busy} onClick={() => void runJobAction(job, 'retry')} className="font-medium text-[#0b6cf5] hover:underline disabled:opacity-50">
                      重试
                    </button>
                  )}
                  {isExportActive(job.status) && (
                    <button type="button" disabled={busy} onClick={() => void runJobAction(job, 'cancel')} className="text-[#858b9c] hover:text-[#b42318] disabled:opacity-50">
                      取消
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </>
  );

  if (embedded) {
    return (
      <div className="relative">
        {trigger}
        {open && (
          <section aria-label="后台导出任务" className={EMBEDDED_PANEL_CLASS}>
            {panelBody}
          </section>
        )}
      </div>
    );
  }

  return (
    <>
      {trigger}
      {open && (
        <section aria-label="后台导出任务" className={FLOATING_PANEL_CLASS}>
          {panelBody}
        </section>
      )}
    </>
  );
}
