import type { DocumentCoverage, DocumentCoverageField, HarnessRunView, WorkOrderStatus } from '../api/types';

export type DocumentGenerationPhase =
  | 'draft'
  | 'analyzing_template'
  | 'needs_clarification'
  | 'ready_to_generate'
  | 'retrieving'
  | 'generating'
  | 'validating'
  | 'rendering'
  | 'paused'
  | 'completed'
  | 'needs_review'
  | 'blocked'
  | 'failed'
  | 'cancelled';

export type DocumentStatusTone = 'neutral' | 'info' | 'warning' | 'success' | 'danger';

export type DocumentStatusDescription = {
  label: string;
  tone: DocumentStatusTone;
  action: string;
};

export type DocumentNextAction =
  | 'refresh'
  | 'answer_questions'
  | 'start_generation'
  | 'view_result'
  | 'review_content'
  | 'retry'
  | 'view_error';

export const DOCUMENT_PHASES: ReadonlyArray<{
  key: DocumentGenerationPhase;
  label: string;
}> = [
  { key: 'analyzing_template', label: '模板解析' },
  { key: 'needs_clarification', label: '需求澄清' },
  { key: 'retrieving', label: '检索资料' },
  { key: 'generating', label: '生成内容' },
  { key: 'validating', label: '校验内容' },
  { key: 'rendering', label: '写入模板' },
  { key: 'paused', label: '任务已暂停' },
  { key: 'completed', label: '预览与下载' },
];

export const WORK_ORDER_STATUS_LABELS: Record<DocumentGenerationPhase, DocumentStatusDescription> = {
  draft: { label: '准备生成任务', tone: 'neutral', action: '选择模板和生成范围' },
  analyzing_template: { label: '正在分析模板', tone: 'info', action: '等待模板结构分析完成' },
  needs_clarification: { label: '需要补充需求', tone: 'warning', action: '回答 AI 的确认问题' },
  ready_to_generate: { label: '需求已确认', tone: 'success', action: '确认并开始生成' },
  retrieving: { label: '正在检索资料', tone: 'info', action: '查看当前字段和检索进度' },
  generating: { label: '正在生成内容', tone: 'info', action: '查看字段生成进度' },
  validating: { label: '正在校验内容', tone: 'info', action: '等待证据与一致性校验' },
  rendering: { label: '正在写入模板', tone: 'info', action: '等待文件渲染完成' },
  paused: { label: '任务已暂停', tone: 'warning', action: '可继续生成或取消任务' },
  completed: { label: '文档已生成', tone: 'success', action: '预览或下载文档' },
  needs_review: { label: '需要检查内容', tone: 'warning', action: '查看证据、冲突或缺失字段' },
  blocked: { label: '生成被阻止', tone: 'danger', action: '查看原因并重试' },
  failed: { label: '任务失败', tone: 'danger', action: '查看错误并重新运行' },
  cancelled: { label: '任务已取消', tone: 'neutral', action: '可新建生成任务' },
};

const STATUS_ALIASES: Record<string, DocumentGenerationPhase> = {
  planned: 'draft',
  ready_to_draft: 'generating',
  drafting: 'generating',
  waiting_human_input: 'needs_review',
  waiting_human_approval: 'needs_review',
  complete: 'completed',
  approved: 'completed',
};

export function describeWorkOrderStatus(status: string): DocumentStatusDescription {
  const phase = STATUS_ALIASES[status] ?? status;
  return WORK_ORDER_STATUS_LABELS[phase as DocumentGenerationPhase] ?? {
    label: status || '状态未知',
    tone: 'neutral',
    action: '刷新后查看最新状态',
  };
}

export function nextActionsForStatus(status: string): DocumentNextAction[] {
  const phase = STATUS_ALIASES[status] ?? status;
  if (['retrieving', 'generating', 'validating', 'rendering'].includes(phase)) return ['refresh'];
  if (phase === 'needs_clarification') return ['answer_questions'];
  if (phase === 'ready_to_generate') return ['start_generation'];
  if (phase === 'completed') return ['view_result'];
  if (phase === 'paused') return ['start_generation'];
  if (phase === 'needs_review') return ['review_content', 'retry'];
  if (phase === 'blocked' || phase === 'failed') return ['view_error', 'retry'];
  return [];
}

export function hasDocumentGenerationWritePermission(permission: string | null | undefined): boolean {
  return permission === 'write' || permission === 'admin';
}

/**
 * 深链 kb 预选校验:仅当预选值出现在已加载的可访问知识库列表中时才保留
 * (与 KbSelect 的选项 value 一致,按 kb.name 精确比较),否则返回空串。
 */
export function resolveDeepLinkKb(deepLinkKb: string, kbs: ReadonlyArray<{ name: string } | string>): string {
  if (!deepLinkKb) return '';
  return kbs.some((kb) => (typeof kb === 'string' ? kb : kb.name) === deepLinkKb) ? deepLinkKb : '';
}

export function describeHarnessProgress(run: HarnessRunView | undefined): string | null {
  const completed = Number(run?.completed_units ?? 0);
  const total = Number(run?.total_units ?? 0);
  if (!Number.isFinite(completed) || !Number.isFinite(total) || total < 1) return null;
  return `已完成单元：${Math.max(0, completed)} / ${total}`;
}

export function resolveDocumentPhase(status: WorkOrderStatus): DocumentGenerationPhase {
  if (status.harness_run?.current_node === 'complete' && status.harness_run.error) return 'blocked';
  const raw = String(status.phase ?? status.status ?? 'draft');
  return STATUS_ALIASES[raw] ?? (raw in WORK_ORDER_STATUS_LABELS ? raw as DocumentGenerationPhase : 'draft');
}

/** 覆盖面板：执行单元状态 -> 用户可读文案。 */
export const DOCUMENT_UNIT_STATUS_LABELS: Record<string, string> = {
  planned: '待处理',
  ready_to_render: '已完成',
  passed: '通过',
  failed: '未通过',
  tbd: '未提供',
  insufficient_evidence: '缺证据',
  conflicting: '冲突',
  retrieval_failed: '检索失败',
  blocked: '阻断',
  requires_human: '需人工确认',
};

export function describeDocumentUnitStatus(status: string): string {
  return DOCUMENT_UNIT_STATUS_LABELS[status] ?? (status || '状态未知');
}

export function documentUnitStatusTone(status: string): DocumentStatusTone {
  const bucket = (
    status === 'ready_to_render' || status === 'passed'
      ? 'success'
      : status === 'tbd' || status === 'insufficient_evidence'
        ? 'warning'
        : status === 'conflicting' || status === 'retrieval_failed' || status === 'failed' || status === 'blocked'
          ? 'danger'
          : 'neutral'
  );
  return bucket as DocumentStatusTone;
}

export type DocumentCoverageBucket = {
  key: 'covered' | 'missing' | 'conflicting' | 'failed' | 'pending';
  label: string;
  tone: DocumentStatusTone;
  count: number;
};

/** 覆盖汇总徽章：只展示非零桶，全零/无数据返回空数组。 */
export function documentCoverageBuckets(coverage: DocumentCoverage | undefined): DocumentCoverageBucket[] {
  if (!coverage || coverage.total < 1) return [];
  const summary = coverage.summary;
  return ([
    { key: 'covered', label: '已完成', tone: 'success' as const },
    { key: 'missing', label: '缺证据', tone: 'warning' as const },
    { key: 'conflicting', label: '冲突', tone: 'danger' as const },
    { key: 'failed', label: '失败', tone: 'danger' as const },
    { key: 'pending', label: '待处理', tone: 'neutral' as const },
  ] as const)
    .map((bucket) => ({ ...bucket, count: summary?.[bucket.key] ?? 0 }))
    .filter((bucket) => bucket.count > 0);
}

/** 供面板渲染的覆盖条目：必填标记融入 label 之外单独返回。 */
export function documentCoverageRows(coverage: DocumentCoverage | undefined): DocumentCoverageField[] {
  return coverage?.fields ?? [];
}

/** 修订差异报告摘要行；无报告/读取失败时返回可读文案。 */
export function describeRevisionDiffSummary(
  result: Record<string, unknown> | null | undefined,
): string | null {
  const report = result?.diff_report as
    | {
        summary?: { changed?: number; added?: number; removed?: number; unchanged?: number };
        error?: string;
        truncated?: boolean;
      }
    | undefined;
  if (!report) return null;
  if (report.error) return `差异报告不可用：${report.error}`;
  const summary = report.summary ?? {};
  const parts = [
    `变更 ${summary.changed ?? 0}`,
    `新增 ${summary.added ?? 0}`,
    `删除 ${summary.removed ?? 0}`,
    `保持不变 ${summary.unchanged ?? 0}`,
  ];
  return `差异报告：${parts.join('；')}${report.truncated ? '（明细已截断，详情见产物）' : ''}`;
}
