import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, uploadFiles } from '../../api/client';
import type {
  CreateEvaluationRunPayload,
  EvaluationCompareResponse,
  EvaluationDatasetSummary,
  EvaluationDatasetUploadResponse,
  CollectionQcReport,
  EvaluationKnowledgeBase,
  EvaluationPreflightResponse,
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
import { ConfirmDialog } from '@/components/ConfirmDialog';
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
import { canLoadSampleDiagnostics, shouldResetEvaluationLoading } from './evaluationDashboard';

type Props = {
  auth: AuthSession;
  onLogout: () => void;
};

const DEFAULT_DATASET = 'evaluation/datasets/hardware_qa_v1.jsonl';
const LAST_DATASET_KEY = 'hdb-eval-last-dataset';

const STATUS_LABELS: Record<string, string> = {
  queued: '待开始',
  running: '运行中',
  pause_requested: '等待暂停',
  paused: '已暂停',
  collected: '采集完成待评分',
  cancel_requested: '等待取消',
  cancelled: '已取消',
  completed: '已完成',
  failed: '失败',
  invalid: '状态损坏',
};

const STAGE_LABELS: Record<string, string> = {
  idle: '空闲',
  collecting: '采集回答',
  collected: '采集质检完成',
  scoring: '评分',
  reporting: '生成报告',
};

const ACTIVE_STATUSES = new Set<EvaluationRunStatus>(['queued', 'running', 'pause_requested', 'cancel_requested']);
const DELETABLE_STATUSES = new Set<EvaluationRunStatus>(['failed', 'cancelled', 'completed', 'collected']);

type LoadOptions = {
  silent?: boolean;
};

function splitList(value: string): string[] | null {
  const items = value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return items.length > 0 ? items : null;
}

function progressPercent(run: EvaluationRunDetail | null): number {
  if (!run) return 0;
  if (run.stage === 'scoring' && run.scoring_total_items > 0) {
    return Math.max(0, Math.min(100, Math.round((run.scoring_completed_items / run.scoring_total_items) * 100)));
  }
  if (run.total_samples <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((run.completed_samples / run.total_samples) * 100)));
}

function progressLabel(run: EvaluationRunDetail): string {
  if (run.status === 'collected') {
    return '采集完成，等待质检确认后评分';
  }
  if (run.stage === 'scoring' && run.scoring_total_items > 0) {
    return `评分 ${run.scoring_completed_items}/${run.scoring_total_items}`;
  }
  return run.current_sample_id || '当前无运行样本';
}

export default function EvaluationPage({ auth, onLogout }: Props) {
  const [runs, setRuns] = useState<EvaluationRunListItem[]>([]);
  const [runsLoaded, setRunsLoaded] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [detail, setDetail] = useState<EvaluationRunDetail | null>(null);
  const [detailLoaded, setDetailLoaded] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<EvaluationRunListItem | null>(null);
  const [deleting, setDeleting] = useState(false);

  const [datasetPath, setDatasetPath] = useState(DEFAULT_DATASET);
  const [uploadingDataset, setUploadingDataset] = useState(false);
  const [mode, setMode] = useState<'online' | 'offline'>('online');
  const [sampleIds, setSampleIds] = useState('');
  const [tags, setTags] = useState('');
  const [advancedFiltersOpen, setAdvancedFiltersOpen] = useState(false);
  const [snapshotPath, setSnapshotPath] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [knowledgeBases, setKnowledgeBases] = useState<EvaluationKnowledgeBase[]>([]);
  const [knowledgeBasesLoaded, setKnowledgeBasesLoaded] = useState(false);
  const [selectedKbId, setSelectedKbId] = useState<number | null>(null);
  const [preflight, setPreflight] = useState<EvaluationPreflightResponse | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const preflightRequestRef = useRef(0);
  const [datasets, setDatasets] = useState<EvaluationDatasetSummary[]>([]);
  const [datasetsLoaded, setDatasetsLoaded] = useState(false);
  const [datasetSelect, setDatasetSelect] = useState<string>('');

  const [baselineRunId, setBaselineRunId] = useState('');
  const [compare, setCompare] = useState<EvaluationCompareResponse | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [scoringLoading, setScoringLoading] = useState(false);

  const selectedKnowledgeBase = knowledgeBases.find((item) => item.kb_id === selectedKbId) ?? null;

  const loadKnowledgeBases = useCallback(() => {
    let cancelled = false;
    api
      .get<EvaluationKnowledgeBase[]>(`/api/v1/evaluation/knowledge-bases`)
      .then((rows) => {
        if (cancelled) return;
        setKnowledgeBases(rows);
        setSelectedKbId((current) => (
          current != null && rows.some((item) => item.kb_id === current) ? current : rows[0]?.kb_id ?? null
        ));
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载评估知识库失败');
      })
      .finally(() => {
        if (!cancelled) setKnowledgeBasesLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadDatasets = useCallback(() => {
    let cancelled = false;
    api
      .get<EvaluationDatasetSummary[]>('/api/v1/evaluation/datasets')
      .then((rows) => {
        if (cancelled) return;
        setDatasets(rows);
        setDatasetsLoaded(true);
        setDatasetSelect((current) => {
          if (current && rows.some((row) => row.path === current)) {
            setDatasetPath(current);
            return current;
          }
          const last = window.localStorage.getItem(LAST_DATASET_KEY) ?? '';
          const next = last && rows.some((row) => row.path === last)
            ? last
            : rows.some((row) => row.path === DEFAULT_DATASET)
              ? DEFAULT_DATASET
              : rows[0]?.path ?? '';
          setDatasetPath(next);
          return next;
        });
      })
      .catch(() => {
        if (!cancelled) setDatasetsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedDataset = useMemo(
    () => datasets.find((item) => item.path === datasetPath) ?? null,
    [datasets, datasetPath],
  );

  function handleDatasetSelectChange(value: string) {
    setDatasetSelect(value);
    setDatasetPath(value);
    window.localStorage.setItem(LAST_DATASET_KEY, value);
    const summary = datasets.find((item) => item.path === value);
    if (summary && summary.kb_bindings.length === 1) {
      const bound = knowledgeBases.find((kb) => kb.kb_name === summary.kb_bindings[0].kb_name);
      if (bound) setSelectedKbId(bound.kb_id);
    }
  }

  function toggleTag(tag: string) {
    const current = splitList(tags) ?? [];
    const next = current.includes(tag)
      ? current.filter((item) => item !== tag)
      : [...current, tag];
    setTags(next.join(','));
  }

  const loadRuns = useCallback((options: LoadOptions = {}) => {
    const silent = options.silent ?? false;
    let cancelled = false;
    if (shouldResetEvaluationLoading(silent)) setRunsLoaded(false);
    api
      .get<EvaluationRunListItem[]>('/api/v1/evaluation/runs')
      .then((rows) => {
        if (cancelled) return;
        setRuns(rows);
        setSelectedRunId((current) => (rows.some((run) => run.run_id === current) ? current : rows[0]?.run_id || ''));
      })
      .catch((error) => {
        if (!cancelled && !silent) {
          notify.error(error instanceof Error ? error.message : '加载评估运行失败');
        }
      })
      .finally(() => {
        if (!cancelled) setRunsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const loadDetail = useCallback((runId = selectedRunId, options: LoadOptions = {}) => {
    const silent = options.silent ?? false;
    if (!runId) {
      setDetail(null);
      setDetailLoaded(true);
      return undefined;
    }
    let cancelled = false;
    if (shouldResetEvaluationLoading(silent)) setDetailLoaded(false);
    api
      .get<EvaluationRunDetail>(`/api/v1/evaluation/runs/${encodeURIComponent(runId)}`)
      .then((run) => {
        if (!cancelled) setDetail(run);
      })
      .catch((error) => {
        if (!cancelled) {
          // Keep the last known detail during a background poll failure. A
          // transient request error should not replace the whole panel with
          // an empty state (or produce a toast every two seconds).
          if (!silent) {
            setDetail(null);
            notify.error(error instanceof Error ? error.message : '加载评估详情失败');
          }
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  useEffect(() => {
    // Any configuration change invalidates the previous preflight result.
    // The automatic preflight below will repopulate it after the debounce.
    setPreflight(null);
    preflightRequestRef.current += 1;
  }, [datasetPath, selectedKbId, mode, sampleIds, tags, snapshotPath]);

  useEffect(() => {
    const cancel = loadKnowledgeBases();
    return cancel;
  }, [loadKnowledgeBases]);

  useEffect(() => {
    const cancel = loadDatasets();
    return cancel;
  }, [loadDatasets]);

  useEffect(() => {
    const cancel = loadRuns();
    return cancel;
  }, [loadRuns]);

  useEffect(() => {
    const cancel = loadDetail();
    return cancel;
  }, [loadDetail]);

  useEffect(() => {
    const activeRunId = detail?.run_id;
    const isActive = detail != null && ACTIVE_STATUSES.has(detail.status);
    if (!activeRunId || !isActive) return undefined;
    const timer = window.setInterval(() => {
      void loadDetail(activeRunId, { silent: true });
      void loadRuns({ silent: true });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [detail?.run_id, detail?.status, loadDetail, loadRuns]);

  useEffect(() => {
    setCompare(null);
    setBaselineRunId('');
  }, [selectedRunId]);

  useEffect(() => {
    // 数据集/知识库/过滤条件变化后自动预检（防抖），替代手动清空旧预检结果。
    if (!knowledgeBasesLoaded) return undefined;
    if (selectedKbId == null || !datasetPath.trim()) return undefined;
    if (mode === 'offline' && !snapshotPath.trim()) return undefined;
    const timer = window.setTimeout(() => {
      void runPreflight({ silent: true });
    }, 600);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetPath, selectedKbId, mode, sampleIds, tags, snapshotPath, knowledgeBasesLoaded]);

  useEffect(() => {
    if (sampleIds.trim() || tags.trim()) setAdvancedFiltersOpen(true);
  }, [sampleIds, tags]);

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
        '/api/v1/evaluation/datasets',
        form,
      );
      setDatasetPath(result.dataset_path);
      setDatasetSelect(result.dataset_path);
      window.localStorage.setItem(LAST_DATASET_KEY, result.dataset_path);
      loadDatasets();
      notify.success(`数据集已上传:${result.sample_count} 条样本`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传数据集失败');
    } finally {
      setUploadingDataset(false);
    }
  }

  async function runPreflight(options: { silent: boolean } = { silent: false }) {
    const silent = options.silent;
    if (selectedKnowledgeBase == null) {
      if (!silent) notify.error('请选择评估知识库');
      return;
    }
    if (!datasetPath.trim()) {
      if (!silent) notify.error('请选择或输入数据集路径');
      return;
    }
    if (mode === 'offline' && !snapshotPath.trim()) {
      if (!silent) notify.error('离线重评需要快照 JSONL 路径');
      return;
    }
    const requestId = ++preflightRequestRef.current;
    setPreflightLoading(true);
    try {
      const payload: CreateEvaluationRunPayload = {
        dataset_path: datasetPath.trim(),
        kb_id: selectedKnowledgeBase.kb_id,
        kb_name: selectedKnowledgeBase.kb_name,
        mode,
        sample_ids: splitList(sampleIds),
        tags: splitList(tags),
        snapshot_path: mode === 'offline' ? snapshotPath.trim() : null,
      };
      const result = await api.post<EvaluationPreflightResponse>(
        '/api/v1/evaluation/preflight',
        payload,
      );
      // Ignore late responses from an older configuration/request.
      if (requestId !== preflightRequestRef.current) return;
      setPreflight(result);
      if (!silent) {
        if (result.can_create) {
          notify.success(`预检通过：${result.dataset_sample_count} 条样本`);
        } else {
          notify.error(`预检未通过：${result.errors.join('；') || '请检查返回详情'}`);
        }
      }
    } catch (error) {
      if (!silent) notify.error(error instanceof Error ? error.message : '评估预检失败');
    } finally {
      if (requestId === preflightRequestRef.current) setPreflightLoading(false);
    }
  }

  function handlePreflight() {
    void runPreflight({ silent: false });
  }

  async function handleCreateAndStart() {
    if (!preflight?.can_create) {
      notify.error('请先执行并通过预检；修改数据集或知识库后需要重新预检');
      return;
    }
    if (selectedKnowledgeBase == null) {
      notify.error('评估知识库不可用，请重新加载后再试');
      return;
    }
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
        kb_id: selectedKnowledgeBase.kb_id,
        kb_name: selectedKnowledgeBase.kb_name,
        mode,
        sample_ids: splitList(sampleIds),
        tags: splitList(tags),
        snapshot_path: mode === 'offline' ? snapshotPath.trim() : null,
      };
      const run = await api.post<EvaluationRunDetail>(
        '/api/v1/evaluation/runs',
        payload,
      );
      await api.post<OkResponse>(
        `/api/v1/evaluation/runs/${encodeURIComponent(run.run_id)}/start`,
      );
      window.localStorage.setItem(LAST_DATASET_KEY, datasetPath.trim());
      notify.success(
        mode === 'online'
          ? '采集已开始；完成后质检确认，再点击「开始评分」'
          : '评估已创建并开始运行',
      );
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
        `/api/v1/evaluation/runs/${encodeURIComponent(selectedRunId)}/${action}`,
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

  async function handleDeleteRun() {
    if (!deleteTarget) return;
    const runId = deleteTarget.run_id;
    setDeleting(true);
    try {
      await api.delete<OkResponse>(
        `/api/v1/evaluation/runs/${encodeURIComponent(runId)}`,
      );
      notify.success('评估运行已删除');
      setDeleteTarget(null);
      setRuns((current) => current.filter((run) => run.run_id !== runId));
      setCompare(null);
      setBaselineRunId('');
      if (selectedRunId === runId) {
        setSelectedRunId('');
        setDetail(null);
      }
      loadRuns();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除评估运行失败');
    } finally {
      setDeleting(false);
    }
  }

  async function handleCompare(strict: boolean) {
    if (!selectedRunId || !baselineRunId) {
      notify.error('请选择当前运行和基线运行');
      return;
    }
    setCompareLoading(true);
    setCompare(null);
    try {
      const result = await api.get<EvaluationCompareResponse>(
        `/api/v1/evaluation/runs/${encodeURIComponent(selectedRunId)}/compare?baseline=${encodeURIComponent(baselineRunId)}&strict=${strict}`,
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
        key: 'knowledge_base',
        title: '知识库',
        width: 180,
        render: (run) => (
          <div className="min-w-0">
            <div className="truncate text-[12px] text-[#18181a]">{run.kb_name || '旧版任务'}</div>
            {run.kb_name && <div className="truncate text-[10px] text-[#858b9c]">{run.department_id == null ? '未分配部门' : `部门 ${run.department_id}`} · ID {run.kb_id ?? '-'}</div>}
          </div>
        ),
      },
      {
        key: 'samples',
        title: '样本',
        width: 90,
        render: (run) => run.dataset_sample_count > 0 ? `${run.dataset_sample_count}（拒绝 ${run.expected_denied_sample_count}）` : '-',
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
        width: 160,
        align: 'right',
        render: (run) => (
          <div className="flex justify-end gap-[6px]">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                setSelectedRunId(run.run_id);
                setCompare(null);
                loadDetail(run.run_id);
              }}
              className="inline-flex h-[28px] items-center rounded-[8px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]"
            >
              查看
            </button>
            {run.status && DELETABLE_STATUSES.has(run.status) && (
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  setDeleteTarget(run);
                }}
                className="inline-flex h-[28px] items-center gap-[4px] rounded-[8px] border border-[#f3b0b0] bg-white px-[10px] text-[12px] text-[#d20b0b] transition-colors hover:bg-[#fce7e7]"
                title="删除已完成、失败或已取消的运行"
              >
                <AppIcon name="trash" size={13} />
                删除
              </button>
            )}
          </div>
        ),
      },
    ],
    [selectedRunId, loadDetail],
  );

  const completedRuns = runs.filter((run) => run.has_summary && run.run_id !== selectedRunId);
  const selectedStatus = detail?.status;
  const canStart = selectedStatus === 'queued';
  const canPause = selectedStatus === 'queued' || selectedStatus === 'running';
  const canResume = selectedStatus === 'paused' || selectedStatus === 'cancelled';
  const canCancel = selectedStatus != null && ['queued', 'running', 'pause_requested', 'paused', 'collected'].includes(selectedStatus);
  const canScore = selectedStatus === 'collected';

  async function startScoring(force: boolean) {
    if (!selectedRunId) return;
    setScoringLoading(true);
    try {
      await api.post<EvaluationRunDetail>(
        `/api/v1/evaluation/runs/${encodeURIComponent(selectedRunId)}/score${force ? '?force=true' : ''}`,
      );
      notify.success('评分已开始');
      await Promise.all([loadRuns(), loadDetail(selectedRunId)]);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '开始评分失败');
    } finally {
      setScoringLoading(false);
    }
  }

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
          <div className="flex flex-wrap gap-[8px]">
            <Button
              variant="outline"
              onClick={() => void handlePreflight()}
              disabled={preflightLoading || submitting || !knowledgeBasesLoaded}
              className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[34px]')}
            >
              {preflightLoading ? '预检中' : '执行预检'}
            </Button>
            <Button
              onClick={() => void handleCreateAndStart()}
              disabled={submitting || preflightLoading || !preflight?.can_create}
              className="h-[34px] gap-[6px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#303030]"
            >
              <AppIcon name="plus" size={14} />
              {submitting ? '创建中' : '创建并开始'}
            </Button>
          </div>
        </div>
        <div className="grid grid-cols-4 gap-[12px] max-[1200px]:grid-cols-2 max-[720px]:grid-cols-1">
              <Field label="评估知识库">
                {!knowledgeBasesLoaded ? (
                  <Skeleton className="h-[36px] rounded-[10px]" />
                ) : (
                  <Select
                    value={selectedKbId == null ? '' : String(selectedKbId)}
                    onValueChange={(value) => setSelectedKbId(Number(value))}
                  >
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                      <SelectValue placeholder="选择知识库" />
                    </SelectTrigger>
                    <SelectContent>
                      {knowledgeBases.map((item) => (
                        <SelectItem key={item.kb_id} value={String(item.kb_id)}>
                          {item.kb_name} · {item.department_name || `部门 ${item.department_id ?? '-'}`}（ID {item.kb_id}）
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </Field>
              <Field label="评估数据集">
                {!datasetsLoaded ? (
                  <Skeleton className="h-[36px] rounded-[10px]" />
                ) : !datasetSelect ? (
                  <Select disabled>
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                      <SelectValue placeholder="暂无可用数据集，请上传" />
                    </SelectTrigger>
                  </Select>
                ) : (
                  <Select value={datasetSelect} onValueChange={handleDatasetSelectChange}>
                    <SelectTrigger className="h-[36px] w-full rounded-[10px] border-[#e3e7f1] text-[13px]">
                      <SelectValue placeholder="选择数据集" />
                    </SelectTrigger>
                    <SelectContent>
                      {datasets.map((item) => (
                        <SelectItem key={item.path} value={item.path}>
                          {item.name}（{item.sample_count} 条 · {item.kb_bindings.map((b) => b.kb_name).join('/') || '未绑定 KB'}）
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
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
              {mode === 'offline' && (
                <Field label="快照 JSONL">
                  <Input value={snapshotPath} onChange={(e) => setSnapshotPath(e.target.value)} className="h-[36px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
                </Field>
              )}
        </div>
        {selectedDataset && (
          <p className="mt-[6px] text-[11px] text-[#858b9c]">
            数据集绑定 {selectedDataset.kb_bindings.map((b) => `${b.kb_name}×${b.count}`).join('、') || '未绑定'} · 拒绝样本 {selectedDataset.expected_denied_count} · 关键 {selectedDataset.critical_count}
            {selectedDataset.malformed_lines > 0 && ` · 异常行 ${selectedDataset.malformed_lines}`}
          </p>
        )}
        <button
          type="button"
          aria-expanded={advancedFiltersOpen}
          onClick={() => setAdvancedFiltersOpen((open) => !open)}
          className="mt-[12px] inline-flex h-[30px] items-center gap-[6px] self-start rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a]"
        >
          <AppIcon
            name="arrow"
            size={13}
            className="shrink-0 transition-transform"
            style={{ transform: `rotate(${advancedFiltersOpen ? -90 : 90}deg)` }}
          />
          高级筛选
          {(sampleIds.trim() || tags.trim()) && (
            <span className="text-[11px] text-[#858b9c]">已设置</span>
          )}
        </button>
        {advancedFiltersOpen && (
          <>
            <div className="mt-[10px] grid grid-cols-2 gap-[12px] max-[720px]:grid-cols-1">
              <Field label="样本 ID">
                <Textarea value={sampleIds} onChange={(e) => setSampleIds(e.target.value)} placeholder="可选，逗号或换行分隔" className="min-h-[68px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
              </Field>
              <Field label="标签">
                <Textarea value={tags} onChange={(e) => setTags(e.target.value)} placeholder="可选，逗号或换行分隔" className="min-h-[68px] rounded-[10px] border-[#e3e7f1] text-[13px]" />
              </Field>
            </div>
            {selectedDataset && selectedDataset.tags.length > 0 && (
              <div className="mt-[6px] flex flex-wrap gap-[6px]">
                {selectedDataset.tags.map((tag) => {
                  const active = (splitList(tags) ?? []).includes(tag);
                  return (
                    <button
                      key={tag}
                      type="button"
                      onClick={() => toggleTag(tag)}
                      className={cn(
                        'rounded-full px-[9px] py-[2px] text-[11px] transition-colors',
                        active
                          ? 'bg-[#18181a] text-white'
                          : 'bg-[#f3f4f6] text-[#464c5e] hover:bg-[#e8eaf0]',
                      )}
                    >
                      #{tag}
                    </button>
                  );
                })}
              </div>
            )}
          </>
        )}
        {preflight && (
          <div className={cn(
            'mt-[12px] rounded-[10px] border px-[12px] py-[10px] text-[12px]',
            preflight.can_create ? 'border-[#b7e5c8] bg-[#f2fbf5] text-[#176b3c]' : 'border-[#f3b0b0] bg-[#fff7f7] text-[#a10b0b]',
          )}>
            <div className="flex flex-wrap gap-x-[14px] gap-y-[4px] font-medium">
              <span>匹配样本 {preflight.matched_sample_count}</span>
              <span>本次样本 {preflight.dataset_sample_count}</span>
              <span>被过滤 {preflight.filtered_sample_count}</span>
              <span>正常检索 {preflight.normal_sample_count}</span>
              <span>拒绝样本 {preflight.expected_denied_sample_count}</span>
            </div>
            {preflight.errors.length > 0 && <p className="mt-[6px] whitespace-pre-wrap">错误：{preflight.errors.join('；')}</p>}
            {preflight.warnings.length > 0 && <p className="mt-[6px] whitespace-pre-wrap">提示：{preflight.warnings.join('；')}</p>}
          </div>
        )}
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
          <div className="flex flex-wrap gap-[8px]">
            <Button
              variant="outline"
              className={OUTLINE_ACTION_BUTTON_CLASS}
              onClick={() => void handleCompare(true)}
              disabled={compareLoading || !baselineRunId || !detail?.summary}
            >
              严格对比
            </Button>
            <Button
              variant="outline"
              className={OUTLINE_ACTION_BUTTON_CLASS}
              onClick={() => void handleCompare(false)}
              disabled={compareLoading || !baselineRunId || !detail?.summary}
            >
              仅查看对比
            </Button>
          </div>
        </div>
        {compare ? (
          <div className="text-[12px] text-[#858b9c]">
            <p>已选择基线；分组柱状图和数值对比显示在下方评估总览中。当前模式：{compare.strict ? '严格对比' : '仅查看对比'}。</p>
            {compare.warnings.length > 0 && (
              <div className="mt-[8px] rounded-[8px] border border-[#f1d59a] bg-[#fffaf0] px-[10px] py-[8px] text-[#8a5a00]">
                {compare.warnings.join('；')}
              </div>
            )}
          </div>
        ) : (
          <p className="text-[12px] text-[#858b9c]">选择一个已完成的运行作为基线。严格对比校验知识库、样本集、指标和模型配置；仅查看对比会保留差异警告。</p>
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
          canScore={canScore}
          scoringLoading={scoringLoading}
          onStart={() => void controlRun('start')}
          onPause={() => void controlRun('pause')}
          onResume={() => void controlRun('resume')}
          onCancel={() => void controlRun('cancel')}
          onScore={(force) => void startScoring(force)}
        />
      </section>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        title={<>删除评估运行「{deleteTarget?.run_id}」</>}
        description="仅已完成、失败、已取消或采集完成的运行可以删除。删除后该运行的状态、快照和评估报告将不可恢复；运行目录外的共享快照不会被删除。"
        confirmText="删除"
        loading={deleting}
        destructive
        onConfirm={() => void handleDeleteRun()}
      />
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
          : status === 'collected'
            ? 'bg-[#e6f4f8] text-[#1b6d92]'
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
  canScore,
  scoringLoading,
  onStart,
  onPause,
  onResume,
  onCancel,
  onScore,
}: {
  run: EvaluationRunDetail | null;
  loaded: boolean;
  compare: EvaluationCompareResponse | null;
  canStart: boolean;
  canPause: boolean;
  canResume: boolean;
  canCancel: boolean;
  canScore: boolean;
  scoringLoading: boolean;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onScore: (force: boolean) => void;
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
  const scoringPercent = run.scoring_total_items > 0
    ? Math.max(0, Math.min(100, Math.round((run.scoring_completed_items / run.scoring_total_items) * 100)))
    : 0;
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
          </p>
        </div>
        <div className="flex flex-wrap gap-[8px]">
          <Button variant="outline" className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[32px] px-[12px]')} onClick={onStart} disabled={!canStart}>
            开始
          </Button>
          {canScore && (
            <Button
              className="h-[32px] rounded-[10px] bg-[#18181a] px-[12px] text-[12px] text-white hover:bg-[#333]"
              onClick={() => onScore(false)}
              disabled={scoringLoading || run.collection_qc?.verdict === 'fail'}
            >
              开始评分
            </Button>
          )}
          {canScore && run.collection_qc?.verdict === 'fail' && (
            <Button
              variant="outline"
              className={cn(OUTLINE_ACTION_BUTTON_CLASS, 'h-[32px] border-[#f1d59a] text-[#8a5a00]')}
              onClick={() => onScore(true)}
              disabled={scoringLoading}
            >
              忽略质检并强制评分
            </Button>
          )}
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

      {run.status === 'collected' && run.mode === 'online' && (
        <CollectionQcPanel qc={run.collection_qc ?? null} />
      )}

      <div className="grid grid-cols-4 gap-[12px] max-[900px]:grid-cols-2">
        <StatCard label="样本总数" value={run.total_samples} />
        <StatCard label="采集完成" value={`${run.completed_samples}/${run.total_samples}`} />
        <StatCard label="评分任务" value={run.scoring_total_items > 0 ? `${run.scoring_completed_items}/${run.scoring_total_items}` : '待开始'} />
        <StatCard label="待评分" value={run.scoring_total_items <= 0 ? '—' : Math.max(0, run.scoring_total_items - run.scoring_completed_items)} />
      </div>

      <div>
        <div className="mb-[6px] flex justify-between text-[11px] text-[#858b9c]">
          <span>{progressLabel(run)}</span>
          <span>{percent}%</span>
        </div>
        <div className="h-[8px] overflow-hidden rounded-full bg-[#f2f3f7]">
          <div className="h-full rounded-full bg-[#18181a] transition-[width]" style={{ width: `${percent}%` }} />
        </div>
        {run.current_question && (
          <p className="mt-[8px] line-clamp-2 text-[12px] leading-[18px] text-[#464c5e]">{run.current_question}</p>
        )}
      </div>

      {run.scoring_total_items > 0 && (
        <div>
          <div className="mb-[6px] flex justify-between text-[11px] text-[#858b9c]">
            <span>评分项进度</span>
            <span>{run.scoring_completed_items} / {run.scoring_total_items} · {scoringPercent}%</span>
          </div>
          <div className="h-[8px] overflow-hidden rounded-full bg-[#f2f3f7]">
            <div className="h-full rounded-full bg-[#4b63b7] transition-[width]" style={{ width: `${scoringPercent}%` }} />
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-[10px] text-[12px] max-[900px]:grid-cols-1">
        <Meta label="知识库" value={run.kb_name ? `${run.kb_name}（ID ${run.kb_id ?? '-'} · 部门 ${run.department_id ?? '-'}）` : '旧版任务，未记录知识库'} />
        <Meta label="样本范围" value={`${run.dataset_sample_count || run.total_samples} 条 · 正常 ${run.normal_sample_count || Math.max(0, run.total_samples - run.expected_denied_sample_count)} · 拒绝 ${run.expected_denied_sample_count}`} />
        <Meta label="数据集" value={run.dataset_path} />
        {run.source_dataset_path && <Meta label="原始数据集" value={run.source_dataset_path} />}
        {run.created_by && <Meta label="创建人" value={run.created_by} />}
        <Meta label="快照" value={run.snapshot_path || '-'} />
        <Meta label="开始时间" value={run.started_at ? formatDateTime(run.started_at) : '-'} />
        <Meta label="更新时间" value={run.updated_at ? formatDateTime(run.updated_at) : '-'} />
        {run.error_message && <Meta label="错误" value={run.error_message} />}
        {run.report_path && <Meta label="报告" value={run.report_path} />}
      </div>

      {run.summary && (
        <EvaluationDashboard
          summary={run.summary}
          sampleResults={canLoadSampleDiagnostics(run.status, Boolean(run.summary)) ? run.sample_results ?? [] : []}
          sampleResultsError={canLoadSampleDiagnostics(run.status, Boolean(run.summary)) ? run.sample_results_error ?? '' : ''}
          compare={compare}
          isInProgress={ACTIVE_STATUSES.has(run.status)}
          isFinal={run.status === 'completed'}
          scoringCompletedItems={run.scoring_completed_items}
          scoringTotalItems={run.scoring_total_items}
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

const QC_VERDICT_LABELS: Record<CollectionQcReport['verdict'], string> = {
  pass: '质检通过',
  pass_with_warnings: '通过（有提醒）',
  fail: '质检未通过',
};

function CollectionQcPanel({ qc }: { qc: CollectionQcReport | null }) {
  if (!qc) {
    return (
      <div className="rounded-[10px] border border-[#edf0f4] px-[12px] py-[10px] text-[12px] text-[#858b9c]">
        采集质检报告尚未生成；可刷新重试，评分前请先确认采集结果。
      </div>
    );
  }
  const verdictTone =
    qc.verdict === 'pass'
      ? 'bg-[#e6f6ec] text-[#138a55]'
      : qc.verdict === 'pass_with_warnings'
        ? 'bg-[#fff7e7] text-[#9a610d]'
        : 'bg-[#fce7e7] text-[#d20b0b]';
  return (
    <section className="rounded-[10px] border border-[#edf0f4] p-[12px]">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <div className="flex items-center gap-[8px]">
          <h4 className="text-[13px] font-semibold text-[#18181a]">采集质检</h4>
          <span className={cn('rounded-full px-[8px] py-[2px] text-[11px]', verdictTone)}>
            {QC_VERDICT_LABELS[qc.verdict]}
          </span>
        </div>
        <span className="text-[11px] text-[#858b9c]">
          样本 {qc.totals.snapshots}/{qc.totals.samples} · 失败 {qc.totals.failed} · 降级 {qc.totals.degraded} · 无证据 {qc.totals.zero_evidence} · 拒绝样本 {qc.totals.expected_denied}
        </span>
      </div>
      {qc.issues.length > 0 && (
        <div className="mt-[8px] rounded-[8px] bg-[#fff7f7] px-[10px] py-[8px]">
          <p className="text-[12px] font-medium text-[#d20b0b]">必须处理的问题</p>
          <ul className="mt-[3px] grid list-disc gap-[2px] pl-[18px] text-[12px] text-[#8a3030]">
            {qc.issues.map((issue) => <li key={issue}>{issue}</li>)}
          </ul>
        </div>
      )}
      {qc.warnings.length > 0 && (
        <div className="mt-[8px] rounded-[8px] bg-[#fffaf0] px-[10px] py-[8px]">
          <p className="text-[12px] font-medium text-[#8a5a10]">提醒</p>
          <ul className="mt-[3px] grid list-disc gap-[2px] pl-[18px] text-[12px] text-[#8a5a10]">
            {qc.warnings.map((warning) => <li key={warning}>{warning}</li>)}
          </ul>
        </div>
      )}
      {qc.issues.length === 0 && qc.warnings.length === 0 && (
        <p className="mt-[8px] text-[12px] text-[#464c5e]">采集结果未发现异常，可以进入评分。</p>
      )}
    </section>
  );
}
