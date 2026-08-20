import type { HarnessRunView, WorkOrderStatus } from '../api/types';

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
