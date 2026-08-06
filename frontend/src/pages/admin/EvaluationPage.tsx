import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, uploadFiles } from '../../api/client';
import type {
  CreateEvaluationRunPayload,
  EvaluationCompareResponse,
  EvaluationDatasetUploadResponse,
  EvaluationRunDetail,
  EvaluationRunListItem,
  EvaluationRunStatus,
  OkResponse,
} from '../../api/types';
import type { AuthSession } from '../../auth';
import AppHeader from '@/components/AppHeader';
import AppIcon from '@/components/AppIcon';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { StatCard } from '@/components/StatCard';
import { Button } from '@/components/ui/button';
import { Input, Label, Textarea } from '@/components/ui';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS, formatDateTime } from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

import EvaluationDashboard from './EvaluationDashboard';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

const DEFAULT_OUTPUT_ROOT = 'storage/evaluations';
const DEFAULT_DATASET = 'evaluation/datasets/hardware_qa_v1.jsonl';

const STATUS_LABELS: Record<string, string> = {
  queued: '待开始',
  running: '运行中',
  pause_requested: '等待暂停',
  paused: '已暂停',
  cancel_requested: '等待取消',
  cancelled: '已取消',
  completed: '已完成',
  failed: '失败',
  invalid: '状态损坏',
};

const STAGE_LABELS: Record<string, string> = {
  idle: '空闲',
  collecting: '采集回答',
  scoring: '评分',
  reporting: '生成报告',
};

const ACTIVE_STATUSES = new Set<EvaluationRunStatus>(['queued', 'running', 'pause_requested', 'cancel_requested']);

function splitList(value: string): string[] | null {
  const items = value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return items.length > 0 ? items : null;
}

function outputRootQuery(outputRoot: string): string {
  return `output_root=${encodeURIComponent(outputRoot.trim() || DEFAULT_OUTPUT_ROOT)}`;
}

function progressPercent(run: EvaluationRunDetail | null): number {
  if (!run || run.total_samples <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((run.completed_samples / run.total_samples) * 100)));
}

export default function EvaluationPage({ auth, onLogout }: Props) {
  const [outputRoot, setOutputRoot] = useState(DEFAULT_OUTPUT_ROOT);
  const [runs, setRuns] = useState<EvaluationRunListItem[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [detail, setDetail] = useState<EvaluationRunDetail | null>(null);
  const [detailLoaded, setDetailLoaded] = useState(false);

  const [datasetPath, setDatasetPath] = useState(DEFAULT_DATASET);
  const [uploadingDataset, setUploadingDataset] = useState(false);
  const [mode, setMode] = useState<'online' | 'offline'>('online');
  const [scoreEnabled, setScoreEnabled] = useState(true);
  const [sampleIds, setSampleIds] = useState('');
  const [tags, setTags] = useState('');
  const [snapshotPath, setSnapshotPath] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const [baselineRunId, setBaselineRunId] = useState('');
  const [compare, setCompare] = useState<EvaluationCompareResponse | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);

  const loadRuns = useCallback(() => {
    let cancelled = false;
    setRunsLoaded(false);
    api
      .get<EvaluationRunListItem[]>(`/api/v1/evaluation/runs?${outputRootQuery(outputRoot)}`)
      .then((rows) => {
        if (cancelled) return;
        setRuns(rows);
        setSelectedRunId((current) => (rows.some((run) => run.run_id === current) ? current : rows[0]?.run_id || ''));
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载评估运行失败');
      })
      .finally(() => {
        if (!cancelled) setRunsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [outputRoot]);

  const loadDetail = useCallback((runId = selectedRunId) => {
    if (!runId) {
      setDetail(null);
      setDetailLoaded(true);
      return undefined;
    }
    let cancelled = false;
    setDetailLoaded(false);
    api
      .get<EvaluationRunDetail>(`/api/v1/evaluation/runs/${encodeURIComponent(runId)}?${outputRootQuery(outputRoot)}`)
      .then((run) => {
        if (!cancelled) setDetail(run);
      })
      .catch((error) => {
        if (!cancelled) {
          setDetail(null);
          notify.error(error instanceof Error ? error.message : '加载评估详情失败');
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [outputRoot, selectedRunId]);

  useEffect(() => {
    const cancel = loadRuns();
    return cancel;
  }, [loadRuns]);

  useEffect(() => {
    const cancel = loadDetail();
    return cancel;
  }, [loadDetail]);

  useEffect(() => {
    if (!detail || !ACTIVE_STATUSES.has(detail.status)) return undefined;
    const timer = window.setInterval(() => {
      loadDetail(detail.run_id);
      loadRuns();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [detail, loadDetail, loadRuns]);

  useEffect(() => {
    setCompare(null);
    setBaselineRunId('');
  }, [selectedRunId, outputRoot]);

  async function handleDatasetUpload(fileList: FileList | null) {
    const file = fileList?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.jsonl')) {
      notify.error('请上传 JSONL 文件');
      return;
    }
    setUploadingDataset(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const result = await uploadFiles<EvaluationDatasetUploadResponse>(
        `/api/v1/evaluation/datasets?${outputRootQuery(outputRoot)}`,
        form,
      );
      setDatasetPath(result.dataset_path);
      notify.success(`数据集已上传:${result.sample_count} 条样本`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传数据集失败');
    } finally {
      setUploadingDataset(false);
    }
  }

  async function handleCreateAndStart() {
    if (!datasetPath.trim()) {
      notify.error('请输入数据集路径');
      return;
    }
    if (mode === 'offline' && !snapshotPath.trim()) {
      notify.error('离线重评需要快照 JSONL 路径');
      return;
    }
    setSubmitting(true);
    try {
      const payload: CreateEvaluationRunPayload = {
        dataset_path: datasetPath.trim(),
        mode,
        score_enabled: mode === 'online' ? scoreEnabled : true,
        sample_ids: splitList(sampleIds),
        tags: splitList(tags),
        snapshot_path: mode === 'offline' ? snapshotPath.trim() : null,
      };
      const run = await api.post<EvaluationRunDetail>(
        `/api/v1/evaluation/runs?${outputRootQuery(outputRoot)}`,
        payload,
      );
      await api.post<OkResponse>(
        `/api/v1/evaluation/runs/${encodeURIComponent(run.run_id)}/start?${outputRootQuery(outputRoot)}`,
      );
      notify.success('评估已创建并开始运行');
      setSelectedRunId(run.run_id);
      await Promise.all([loadRuns(), loadDetail(run.run_id)]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建评估失败');
    } finally {
      setSubmitting(false);
    }
  }

  async function controlRun(action: 'start' | 'pause' | 'resume' | 'cancel') {
    if (!selectedRunId) return;
    try {
      await api.post<OkResponse | EvaluationRunDetail>(
        `/api/v1/evaluation/runs/${encodeURIComponent(selectedRunId)}/${action}?${outputRootQuery(outputRoot)}`,
      );
      notify.success(
        action === 'start'
          ? '评估已开始'
          : action === 'pause'
            ? '已请求暂停'
            : action === 'resume'
              ? '评估已继续'
              : '已请求取消',
      );
      await Promise.all([loadRuns(), loadDetail(selectedRunId)]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '操作失败');
    }
  }

  async function handleCompare() {
    if (!selectedRunId || !baselineRunId) {
      notify.error('请选择当前运行和基线运行');
      return;
    }
    setCompareLoading(true);
    setCompare(null);
    try {
      const result = await api.get<EvaluationCompareResponse>(
        `/api/v1/evaluation/runs/${encodeURIComponent(selectedRunId)}/compare?baseline=${encodeURIComponent(baselineRunId)}&${outputRootQuery(outputRoot)}`,
      );
      setCompare(result);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载对比失败');
    } finally {
      setCompareLoading(false);
    }
  }

  const runColumns: DataTableColumn<EvaluationRunListItem>[] = useMemo(
    () => [
      {
        key: 'run_id',
        title: '运行',
        render: (run) => (
          <div className="flex min-w-0 items-center gap-[8px]">
            <span className="truncate font-medium text-[#18181a]">{run.run_id}</span>
            {run.run_id === selectedRunId && (
              <span className="shrink-0 rounded-full bg-[#18181a] px-[7px] py-[2px] text-[10px] text-white">
                当前
              </span>
            )}
          </div>
        ),
      },
      {
        key: 'status',
        title: '状态',
        width: 100,
        render: (run) => <StatusPill status={run.status || 'queued'} />,
      },
      {
        key: 'summary',
        title: '报告',
        width: 80,
        render: (run) => (
          <span className={run.has_summary ? 'text-[#2cb360]' : 'text-[#b3b8c4]'}>
            {run.has_summary ? '已生成' : '-'}
          </span>
        ),
      },
      {
        key: 'actions',
        title: '操作',
        width: 90,
        align: 'right',
        render: (run) => (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              setSelectedRunId(run.run_id);
              setCompare(null);
            }}
            className="inline-flex h-[28px] items-center rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]"
          >
            查看
          </button>
        ),
      },
    ],
    [selectedRunId],
  );

  const completedRuns = runs.filter((run) => run.has_summary && run.run_id !== selectedRunId);
  const selectedStatus = detail?.status;
  const canStart = selectedStatus === 'queued';
  const canPause = selectedStatus === 'queued' || selectedStatus === 'running';
  const canResume = selectedStatus === 'paused' || selectedStatus === 'cancelled';
  const canCancel = selectedStatus != null && ['queued', 'running', 'pause_requested', 'paused'].includes(selectedStatus);

  return (
    <div className="min-h-full px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        title="RAGAS 评估"
        description="创建、控制和对比检索回答评估运行。仅系统管理员可访问。"
        userName={auth.user.username}
        onLogout={onLogout}
      />

      <section className="mt-[20px] rounded-[14px] bg-white p-[16px] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
        <div className="mb-[14px] flex flex-wrap items-center justify-between gap-[12px]">
          <h3 className="text-[14px] font-semibold text-[#18181a]">新建评估</h3>
          <Button
            onClick={handleCreateAndStart}
            disabled={submitting}
            className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
          >
            <AppIcon name="plus" size={14} />
            {submitting ? '创建中' : '创建并开始'}
          </Button>
        </div>
        <div className="grid grid-cols-4 gap-[12px] max-[1200px]:grid-cols-2 max-[720px]:grid-cols-1">
              <Field label="输出目录">
                <Input value={outputRoot} onChange={(e) => setOutputRoot(e.target.value)} className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
              </Field>
              <Field label="数据集 JSONL">
                <Input value={datasetPath} onChange={(e) => setDatasetPath(e.target.value)} className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
              </Field>
              <Field label="上传数据集">
                <input
                  type="file"
                  accept=".jsonl"
                  disabled={uploadingDataset}
                  onChange={(event) => void handleDatasetUpload(event.target.files)}
                  className="h-[36px] rounded-[10px] border border-[#e3e7f1] bg-white px-[10px] py-[6px] text-[12px] text-[#464c5e] file:mr-[10px] file:rounded-[8px] file:border-0 file:bg-[#f3f4f6] file:px-[10px] file:py-[4px] file:text-[12px] file:text-[#464c5e] disabled:cursor-not-allowed disabled:opacity-60"
                />
              </Field>
              <Field label="模式">
                <Select value={mode} onValueChange={(value) => setMode(value as 'online' | 'offline')}>
                  <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="online">在线采集</SelectItem>
                    <SelectItem value="offline">离线重评快照</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              {mode === 'offline' ? (
                <Field label="快照 JSONL">
                  <Input value={snapshotPath} onChange={(e) => setSnapshotPath(e.target.value)} className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
                </Field>
              ) : (
                <label className="flex items-center gap-[8px] text-[13px] text-[#464c5e]">
                  <input
                    type="checkbox"
                    checked={scoreEnabled}
                    onChange={(e) => setScoreEnabled(e.target.checked)}
                    className="size-[14px] accent-[#18181a]"
                  />
                  启用 RAGAS 评分
                </label>
              )}
        </div>
        <div className="mt-[12px] grid grid-cols-2 gap-[12px] max-[720px]:grid-cols-1">
              <Field label="样本 ID">
                <Textarea value={sampleIds} onChange={(e) => setSampleIds(e.target.value)} placeholder="可选,逗号或换行分隔" className="min-h-[68px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
              </Field>
              <Field label="标签">
                <Textarea value={tags} onChange={(e) => setTags(e.target.value)} placeholder="可选,逗号或换行分隔" className="min-h-[68px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
              </Field>
        </div>
      </section>

      <section className="mt-[16px] flex flex-col gap-[16px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-center justify-between gap-[12px]">
          <h3 className="text-[14px] font-semibold text-[#18181a]">评估运行</h3>
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')} onClick={() => loadRuns()}>
            <AppIcon name="refresh" size={13} />
            刷新
          </Button>
        </div>
        {!runsLoaded ? (
          <div className="grid gap-[10px]">
            {[0, 1, 2].map((i) => <Skeleton key={i} className="h-[48px] rounded-[10px]" />)}
          </div>
        ) : (
          <DataTable
            columns={runColumns}
            data={runs}
            rowKey={(run) => run.run_id}
            size="compact"
            emptyText="尚无评估运行"
            onRowClick={(run) => {
              setSelectedRunId(run.run_id);
              setCompare(null);
            }}
          />
        )}
      </section>

      <section className="mt-[16px] rounded-[14px] bg-white p-[16px] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
        <div className="mb-[12px] flex flex-wrap items-end gap-[10px]">
          <div className="grid min-w-[240px] gap-[4px]">
            <Label className="text-[11px] text-[#858b9c]">历史对比基线</Label>
            <Select value={baselineRunId} onValueChange={setBaselineRunId}>
              <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                <SelectValue placeholder="选择已生成报告的运行" />
              </SelectTrigger>
              <SelectContent>
                {completedRuns.map((run) => (
                  <SelectItem key={run.run_id} value={run.run_id}>
                    {run.run_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={handleCompare} disabled={compareLoading || !baselineRunId || !detail?.summary}>
            对比
          </Button>
        </div>
        {compare ? (
          <p className="text-[12px] text-[#858b9c]">已选择基线；分组柱状图和数值对比显示在下方评估总览中。</p>
        ) : (
          <p className="text-[12px] text-[#858b9c]">选择一个已完成的运行作为基线,可比较当前摘要与历史摘要。</p>
        )}
      </section>

      <section className="mt-[16px]">
        <RunDetailPanel
          run={detail}
          loaded={detailLoaded}
          compare={compare}
          canStart={canStart}
          canPause={canPause}
          canResume={canResume}
          canCancel={canCancel}
          onStart={() => void controlRun('start')}
          onPause={() => void controlRun('pause')}
          onResume={() => void controlRun('resume')}
          onCancel={() => void controlRun('cancel')}
        />
      </section>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-[4px]">
      <Label className="text-[11px] text-[#858b9c]">{label}</Label>
      {children}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const tone =
    status === 'completed'
      ? 'bg-[#e6f6ec] text-[#138a55]'
      : status === 'failed' || status === 'cancelled' || status === 'invalid'
        ? 'bg-[#fce7e7] text-[#d20b0b]'
        : status === 'running'
          ? 'bg-[#eef1fb] text-[#4b63b7]'
          : 'bg-[#f3f4f6] text-[#464c5e]';
  return (
    <span className={cn('inline-flex rounded-full px-[8px] py-[2px] text-[11px]', tone)}>
      {STATUS_LABELS[status] ?? status}
    </span>
  );
}

function RunDetailPanel({
  run,
  loaded,
  compare,
  canStart,
  canPause,
  canResume,
  canCancel,
  onStart,
  onPause,
  onResume,
  onCancel,
}: {
  run: EvaluationRunDetail | null;
  loaded: boolean;
  compare: EvaluationCompareResponse | null;
  canStart: boolean;
  canPause: boolean;
  canResume: boolean;
  canCancel: boolean;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
}) {
  if (!loaded) {
    return (
      <div className="grid gap-[10px] rounded-[14px] bg-white p-[16px] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
        {[0, 1, 2].map((i) => <Skeleton key={i} className="h-[56px] rounded-[10px]" />)}
      </div>
    );
  }
  if (!run) {
    return (
      <div className="rounded-[14px] bg-white p-[48px] text-center text-[13px] text-[#858b9c] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
        请选择或创建一个评估运行。
      </div>
    );
  }

  const percent = progressPercent(run);
  return (
    <div className="flex flex-col gap-[16px] rounded-[14px] bg-white p-[16px] shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
      <div className="flex flex-wrap items-start justify-between gap-[12px]">
        <div className="min-w-0">
          <div className="flex items-center gap-[8px]">
            <h3 className="truncate text-[16px] font-semibold text-[#18181a]">{run.run_id}</h3>
            <StatusPill status={run.status} />
          </div>
          <p className="mt-[4px] text-[12px] text-[#858b9c]">
            {run.mode === 'online' ? '在线采集' : '离线重评'} · {STAGE_LABELS[run.stage] ?? run.stage}
            {run.score_enabled ? ' · 启用评分' : ' · 仅采集'}
          </p>
        </div>
        <div className="flex flex-wrap gap-[8px]">
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[32px] px-[12px]')} onClick={onStart} disabled={!canStart}>
            开始
          </Button>
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[32px] px-[12px]')} onClick={onPause} disabled={!canPause}>
            暂停
          </Button>
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[32px] px-[12px]')} onClick={onResume} disabled={!canResume}>
            继续
          </Button>
          <Button variant="outline" className="h-[32px] gap-[4px] rounded-[10px] border-[#f3b0b0] bg-white px-[12px] text-[12px] text-[#d20b0b] hover:bg-[#fce7e7]" onClick={onCancel} disabled={!canCancel}>
            <AppIcon name="stop" size={13} />
            取消
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-[12px] max-[900px]:grid-cols-2">
        <StatCard label="样本总数" value={run.total_samples} />
        <StatCard label="已完成" value={run.completed_samples} />
        <StatCard label="成功" value={run.successful_samples} tone="green" />
        <StatCard label="失败" value={run.failed_samples} tone={run.failed_samples > 0 ? 'red' : 'green'} />
      </div>

      <div>
        <div className="mb-[6px] flex justify-between text-[11px] text-[#858b9c]">
          <span>{run.current_sample_id || '当前无运行样本'}</span>
          <span>{percent}%</span>
        </div>
        <div className="h-[8px] overflow-hidden rounded-full bg-[#f2f3f7]">
          <div className="h-full rounded-full bg-[#18181a] transition-[width]" style={{ width: `${percent}%` }} />
        </div>
        {run.current_question && (
          <p className="mt-[8px] line-clamp-2 text-[12px] leading-[18px] text-[#464c5e]">{run.current_question}</p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-[10px] text-[12px] max-[900px]:grid-cols-1">
        <Meta label="数据集" value={run.dataset_path} />
        <Meta label="快照" value={run.snapshot_path || '-'} />
        <Meta label="开始时间" value={run.started_at ? formatDateTime(run.started_at) : '-'} />
        <Meta label="更新时间" value={run.updated_at ? formatDateTime(run.updated_at) : '-'} />
        {run.error_message && <Meta label="错误" value={run.error_message} />}
        {run.report_path && <Meta label="报告" value={run.report_path} />}
      </div>

      {run.summary && (
        <EvaluationDashboard
          summary={run.summary}
          sampleResults={run.sample_results ?? []}
          sampleResultsError={run.sample_results_error ?? ''}
          compare={compare}
        />
      )}
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid gap-[3px] rounded-[8px] bg-[#fafbfc] px-[10px] py-[8px]">
      <span className="text-[11px] text-[#858b9c]">{label}</span>
      <span className="break-words text-[12px] text-[#18181a]">{value}</span>
    </div>
  );
}
