/**
 * documentCardModel -- document_card 事件的纯数据模型(对齐 documentGenerationModel 约定)。
 *
 * 线格式 {"type":"document_card","payload":{"card":{...}}};卡片只携带不可变引用与
 * 状态枚举:kind / status / next_actions / kb_name / task_id / work_order_id / generation_session_id /
 * clarification fields / target_format / artifacts(仅 artifact_id+stage)。深链指向文档生成工作台;
 * 刷新动作直调 REST 工单状态接口;产物下载 URL 由前端拼装(REST 直链)。
 */
import { api, apiDownload } from '@/api/client';
import type { DocumentChatTaskView, GenerationSession, WorkOrderStatus } from '@/api/types';
import { describeWorkOrderStatus, type DocumentStatusTone } from '../../documentGenerationModel';

export type DocumentCardArtifact = {
  artifact_id: string;
  stage: string;
  output_format?: string;
  preview_url?: string;
  download_url?: string;
};

/** Gate 1 提案摘要:只含 id/版本/哈希/计数与政策,不含来源名或证据正文。 */
export type DocumentPlanProposalSummary = {
  document_plan_id?: string;
  document_plan_version?: number;
  plan_hash?: string;
  output_spec_id?: string;
  output_spec_version?: number;
  output_spec_hash?: string;
  status?: string;
  executable?: boolean;
  deliverables?: Array<{ format: string; role: string; required?: boolean }>;
  layout_summary?: Record<string, unknown>;
  outline_count?: number;
  table_count?: number;
  source_summary?: Record<string, unknown>;
  policies?: Record<string, unknown>;
  warnings?: string[];
  blockers?: Array<Record<string, unknown> | string>;
};

export type DocumentCardGateException = {
  kind?: string;
  refdes?: string;
  pin_name?: string;
  recommended_action?: string;
  user_instruction?: string;
  suggested_refdes?: string[];
};

export type DocumentCardGateReview = {
  reviewKind: string;
  status?: string;
  scopePendingCount: number;
  scopeBlocking: boolean;
  scopeExceptions: DocumentCardGateException[];
};

export type DocumentCardData = {
  kind: string;
  status: string;
  next_actions: string[];
  kb_name: string;
  task_id?: string | null;
  work_order_id?: string | null;
  generation_session_id?: string | null;
  question_id?: string | null;
  options?: string[];
  reason?: string | null;
  content?: string | null;
  targetFormat?: string;
  artifacts?: DocumentCardArtifact[];
  proposal?: DocumentPlanProposalSummary;
  gateReview?: DocumentCardGateReview;
  errorCode?: string;
  errorMessage?: string;
  retryable?: boolean;
  progress?: {
    currentNode?: string;
    completedUnits: number;
    totalUnits: number;
    percent: number;
  };
};

/**
 * Normalize the mixed WorkOrder/DocumentTask status payload into one status
 * for the chat card.  The execution phase is not authoritative when a
 * release gate has failed (for example `phase=pending` with
 * `status=blocked`/`plan_release_blocked`).  Resolve terminal/error states
 * first so the user never sees contradictory "pending" and "complete"
 * labels for the same run.
 */
export function normalizeDocumentCardStatus(value: {
  status?: unknown;
  phase?: unknown;
  release_status?: unknown;
  error_code?: unknown;
  error_message?: unknown;
  harness_run?: { current_node?: unknown; error?: unknown } | null;
}): string {
  const rawStatus = normalizeStatusString(value.status);
  const rawPhase = normalizeStatusString(value.phase);
  const releaseStatus = normalizeStatusString(value.release_status);
  const errorCode = normalizeStatusString(value.error_code);
  const hasError = Boolean(errorCode || normalizeStatusString(value.error_message) || normalizeStatusString(value.harness_run?.error));
  const currentNode = normalizeStatusString(value.harness_run?.current_node);

  if (rawStatus === 'blocked' || rawPhase === 'blocked' || releaseStatus === 'blocked') return 'blocked';
  // A renderer/release error reported at the terminal node is a blocked
  // candidate, not a successfully completed document.
  if (hasError && (
    currentNode === 'complete'
    || errorCode === 'plan_release_blocked'
    || errorCode === 'release_blocked'
    || errorCode === 'renderer_safety_violation'
  )) return 'blocked';
  if (rawStatus === 'failed' || rawPhase === 'failed') return 'failed';
  if (hasError) return 'failed';

  const candidate = rawStatus || rawPhase;
  if (!candidate) return 'draft';
  // `pending` is the queue state in the durable task store.  The chat card
  // uses the user-facing queued label instead of exposing an implementation
  // detail that is easy to confuse with a release review pending state.
  if (candidate === 'pending') return 'queued';
  if (candidate === 'awaiting_plan_confirmation') return 'awaiting_confirmation';
  if (candidate === 'complete' || candidate === 'succeeded') return 'completed';
  if (candidate === 'waiting_human_input' || candidate === 'waiting_human_approval') return 'needs_review';
  return candidate;
}

function normalizeStatusString(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

export type DocumentClarificationEventType =
  | 'document_clarification_question'
  | 'document_clarification_ready';

/** 这些动作可以直接刷新状态(REST),其余 next_action 视为人工门,深链到工作台。 */
export const DOCUMENT_CARD_REFRESH_ACTIONS: ReadonlySet<string> = new Set([
  'get_document_generation_status',
  'poll_status',
]);

const CARD_TITLES: Record<string, string> = {
  work_order_created: '工单已创建',
  work_order_status: '工单状态',
  generation_session: '需求会话',
  output_spec_confirmation: '计划确认',
  requirement_clarification: '需求澄清',
};

const CARD_STATUS_LABELS: Record<string, string> = {
  needs_input: '等待补充信息',
  needs_clarification: '等待对话澄清',
  awaiting_confirmation: '等待确认',
  queued: '排队中',
  retrieving: '正在检索资料',
  running: '进行中',
  generating: '正在生成内容',
  validating: '正在校验内容',
  rendering: '正在写入模板',
  needs_review: '候选稿待审核',
  blocked: '生成被阻止',
  cancelled: '已取消',
  completed: '已完成',
  succeeded: '已成功',
  failed: '已失败',
};

const CARD_STATUS_TONES: Record<string, DocumentStatusTone> = {
  needs_input: 'warning',
  needs_clarification: 'warning',
  awaiting_confirmation: 'warning',
  queued: 'info',
  retrieving: 'info',
  running: 'info',
  generating: 'info',
  validating: 'info',
  rendering: 'info',
  needs_review: 'warning',
  blocked: 'danger',
  cancelled: 'neutral',
  completed: 'success',
  succeeded: 'success',
  failed: 'danger',
};

const NEXT_ACTION_LABELS: Record<string, string> = {
  submit_icd_scope_resolution: '处理 ICD 范围待办',
  answer_clarification: '回答澄清问题',
  start_document_generation_session: '开始需求澄清',
  create_document_work_order: '创建生成工单',
  propose_document_plan: '生成计划提案',
  confirm_document_plan: '确认生成',
  await_generation: '等待后台生成',
  get_document_task_status: '查询任务状态',
  resume_document_task: '继续执行任务',
  review_revision: '查看文档修订',
  open_document_workbench: '打开文档工作台',
  provide_value: '补充缺失字段',
  replace_template: '更换模板',
  retry_generation: '重试生成',
  view_error: '查看错误',
  view_result: '查看结果',
};

/** artifacts 只保留不可变引用:非数组→undefined;无字符串 artifact_id 的条目丢弃;上限 8。 */
export function parseCardArtifacts(value: unknown): DocumentCardArtifact[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const parsed = value
    .filter((entry): entry is Record<string, unknown> =>
      Boolean(entry) && typeof entry === 'object' && !Array.isArray(entry))
    .filter((entry) => typeof entry.artifact_id === 'string' && entry.artifact_id.trim() !== '')
    .slice(0, 8)
    .map((entry) => ({
      artifact_id: entry.artifact_id as string,
      stage: typeof entry.stage === 'string' ? entry.stage : '',
      ...(typeof entry.output_format === 'string' && entry.output_format.trim()
        ? { output_format: entry.output_format }
        : {}),
      ...(typeof entry.preview_url === 'string' && entry.preview_url.trim()
        ? { preview_url: entry.preview_url }
        : {}),
      ...(typeof entry.download_url === 'string' && entry.download_url.trim()
        ? { download_url: entry.download_url }
        : {}),
    }));
  return parsed.length > 0 ? parsed : undefined;
}

/** Parse and bound live Harness unit progress for a status card. */
export function parseCardProgress(value: unknown): DocumentCardData['progress'] | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const completed = Number(record.completedUnits ?? record.completed_units);
  const total = Number(record.totalUnits ?? record.total_units);
  if (!Number.isFinite(completed) || !Number.isFinite(total) || total < 1) return undefined;
  const boundedCompleted = Math.max(0, Math.min(total, Math.round(completed)));
  const percent = Math.round((boundedCompleted / total) * 100);
  const currentNode = typeof record.currentNode === 'string'
    ? record.currentNode.trim()
    : typeof record.current_node === 'string'
      ? record.current_node.trim()
      : '';
  return {
    ...(currentNode ? { currentNode } : {}),
    completedUnits: boundedCompleted,
    totalUnits: Math.round(total),
    percent,
  };
}

function progressFromHarness(run: unknown): DocumentCardData['progress'] | undefined {
  if (!run || typeof run !== 'object' || Array.isArray(run)) return undefined;
  return parseCardProgress(run);
}

/** 解析 Gate 1 提案摘要;任何形状不符的字段整体丢弃(fail-closed)。 */
export function parsePlanProposal(value: unknown): DocumentPlanProposalSummary | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const planHash = typeof record.plan_hash === 'string' ? record.plan_hash.trim() : '';
  const specHash = typeof record.output_spec_hash === 'string' ? record.output_spec_hash.trim() : '';
  if (!planHash && !specHash) return undefined;
  const warnings = Array.isArray(record.warnings)
    ? record.warnings.filter((item): item is string => typeof item === 'string' && item.trim() !== '').slice(0, 8)
    : [];
  // blockers 的存在性本身就是确认门:空数组也要保留。
  const blockers = Array.isArray(record.blockers)
    ? (record.blockers as unknown[]).slice(0, 8)
    : undefined;
  const deliverables = Array.isArray(record.deliverables)
    ? (record.deliverables as unknown[]).slice(0, 16)
    : [];
  return {
    ...(typeof record.document_plan_id === 'string' && record.document_plan_id.trim()
      ? { document_plan_id: record.document_plan_id } : {}),
    ...(typeof record.document_plan_version === 'number' ? { document_plan_version: record.document_plan_version } : {}),
    ...(planHash ? { plan_hash: planHash } : {}),
    ...(typeof record.output_spec_id === 'string' && record.output_spec_id.trim()
      ? { output_spec_id: record.output_spec_id } : {}),
    ...(typeof record.output_spec_version === 'number' ? { output_spec_version: record.output_spec_version } : {}),
    ...(specHash ? { output_spec_hash: specHash } : {}),
    ...(typeof record.status === 'string' ? { status: record.status } : {}),
    ...(typeof record.executable === 'boolean' ? { executable: record.executable } : {}),
    ...(deliverables.length ? { deliverables: deliverables as DocumentPlanProposalSummary['deliverables'] } : {}),
    ...(record.layout_summary && typeof record.layout_summary === 'object' && !Array.isArray(record.layout_summary)
      ? { layout_summary: record.layout_summary as Record<string, unknown> } : {}),
    ...(typeof record.outline_count === 'number' ? { outline_count: record.outline_count } : {}),
    ...(typeof record.table_count === 'number' ? { table_count: record.table_count } : {}),
    ...(warnings.length ? { warnings } : {}),
    ...(blockers !== undefined ? { blockers: blockers as DocumentPlanProposalSummary['blockers'] } : {}),
  };
}

export function parseDocumentCardEvent(data: string): DocumentCardData | null {
  try {
    const parsed = JSON.parse(data) as { card?: Record<string, unknown> | null } | null;
    const card = parsed && typeof parsed === 'object' ? parsed.card : null;
    if (!card || typeof card !== 'object') return null;
    const kind = typeof card.kind === 'string' ? card.kind.trim() : '';
    if (!kind) return null;
    const targetFormat = typeof card.target_format === 'string' ? card.target_format : undefined;
    const artifacts = parseCardArtifacts(card.artifacts);
    const errorCode = nonEmptyString(card.error_code);
    const errorMessage = nonEmptyString(card.error_message);
    const harnessRun = card.harness_run && typeof card.harness_run === 'object' && !Array.isArray(card.harness_run)
      ? card.harness_run as { current_node?: unknown; error?: unknown }
      : null;
    const progress = parseCardProgress(card.progress) ?? progressFromHarness(harnessRun);
    const status = normalizeDocumentCardStatus({
      status: card.status,
      phase: card.phase,
      release_status: card.release_status,
      error_code: errorCode,
      error_message: errorMessage,
      harness_run: harnessRun,
    });
    return {
      kind,
      status,
      next_actions: Array.isArray(card.next_actions)
        ? card.next_actions.filter((action): action is string => typeof action === 'string')
        : [],
      kb_name: typeof card.kb_name === 'string' ? card.kb_name : '',
      ...(typeof card.task_id === 'string' && card.task_id.trim() ? { task_id: card.task_id } : {}),
      work_order_id: typeof card.work_order_id === 'string' && card.work_order_id.trim() ? card.work_order_id : null,
      generation_session_id: typeof card.generation_session_id === 'string' && card.generation_session_id.trim()
        ? card.generation_session_id
        : null,
      ...(nonEmptyString(card.question_id) ? { question_id: nonEmptyString(card.question_id) } : {}),
      ...(nonEmptyString(card.content) ? { content: nonEmptyString(card.content) } : {}),
      ...(Array.isArray(card.options) ? { options: parseStringOptions(card.options) } : {}),
      ...(nonEmptyString(card.reason) ? { reason: nonEmptyString(card.reason) } : {}),
      ...(targetFormat ? { targetFormat } : {}),
      ...(artifacts ? { artifacts } : {}),
      ...(errorCode ? { errorCode } : {}),
      ...(errorMessage ? { errorMessage } : {}),
      ...(progress ? { progress } : {}),
      ...(kind === 'output_spec_confirmation' ? { proposal: parsePlanProposal(card.proposal) } : {}),
    };
  } catch {
    return null;
  }
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function parseStringOptions(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((option): option is string => typeof option === 'string' && option.trim() !== '')
    .slice(0, 16);
}

function clarificationPayload(value: unknown): Record<string, unknown> | null {
  let parsed = value;
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed) as unknown;
    } catch {
      return null;
    }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const record = parsed as Record<string, unknown>;
  const wrapped = record.payload;
  if (wrapped && typeof wrapped === 'object' && !Array.isArray(wrapped)) {
    return wrapped as Record<string, unknown>;
  }
  return record;
}

/**
 * Project the durable clarification events into the same card contract used
 * by document_card/task polling.  The event carries no work-order metadata,
 * so task/session references are the stable identity and the current Chat KB
 * supplies the display scope.
 */
export function documentCardFromClarificationEvent(
  eventType: string,
  payload: unknown,
  kbName: string,
): DocumentCardData | null {
  const isQuestion = eventType === 'document_clarification_question';
  const isReady = eventType === 'document_clarification_ready';
  if (!isQuestion && !isReady) return null;

  const record = clarificationPayload(payload);
  if (!record) return null;
  const taskId = nonEmptyString(record.document_task_id) ?? nonEmptyString(record.task_id);
  const sessionId = nonEmptyString(record.generation_session_id);
  if (!taskId && !sessionId) return null;

  const questionId = nonEmptyString(record.question_id);
  const reason = nonEmptyString(record.reason);
  const content = nonEmptyString(record.content);
  const workOrderId = nonEmptyString(record.work_order_id);
  const eventKbName = nonEmptyString(record.kb_name) ?? kbName;

  return {
    kind: 'generation_session',
    status: isQuestion ? 'needs_clarification' : 'ready_to_generate',
    next_actions: isQuestion ? ['answer_clarification'] : ['create_document_work_order'],
    kb_name: eventKbName,
    ...(taskId ? { task_id: taskId } : {}),
    ...(workOrderId ? { work_order_id: workOrderId } : {}),
    ...(sessionId ? { generation_session_id: sessionId } : {}),
    question_id: isQuestion ? questionId : null,
    options: isQuestion ? parseStringOptions(record.options) : [],
    reason,
    content: isQuestion ? content : null,
  };
}

/** Project a durable background task into the same card contract as SSE events. */
/** Normalize the safe human-gate projection into renderable card data. */
export function parseGateReview(value: unknown): DocumentCardGateReview | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const pending = value as Record<string, unknown>;
  const reviewKind = nonEmptyString(pending.review_kind);
  const scope = pending.scope_review && typeof pending.scope_review === 'object'
    && !Array.isArray(pending.scope_review)
    ? pending.scope_review as Record<string, unknown>
    : null;
  const exceptions = Array.isArray(scope?.exceptions)
    ? (scope.exceptions as unknown[])
      .filter((item): item is Record<string, unknown> => (
        Boolean(item) && typeof item === 'object' && !Array.isArray(item)
      ))
      .slice(0, 20)
      .map((item) => ({
        ...(typeof item.kind === 'string' && item.kind.trim() ? { kind: item.kind } : {}),
        ...(typeof item.refdes === 'string' && item.refdes.trim() ? { refdes: item.refdes } : {}),
        ...(typeof item.pin_name === 'string' && item.pin_name.trim() ? { pin_name: item.pin_name } : {}),
        ...(typeof item.recommended_action === 'string' && item.recommended_action.trim()
          ? { recommended_action: item.recommended_action }
          : {}),
        ...(typeof item.user_instruction === 'string' && item.user_instruction.trim()
          ? { user_instruction: item.user_instruction }
          : {}),
        ...(Array.isArray(item.suggested_refdes)
          ? {
            suggested_refdes: (item.suggested_refdes as unknown[])
              .filter((value): value is string => typeof value === 'string' && value.trim() !== '')
              .slice(0, 20),
          }
          : {}),
      }))
    : [];
  if (!reviewKind && exceptions.length === 0) return undefined;
  return {
    reviewKind: reviewKind || 'review',
    ...(typeof pending.status === 'string' && pending.status.trim() ? { status: pending.status } : {}),
    scopePendingCount: typeof scope?.pending_count === 'number' ? scope.pending_count : 0,
    scopeBlocking: scope?.blocking === true,
    scopeExceptions: exceptions,
  };
}

export function documentCardFromChatTask(task: DocumentChatTaskView): DocumentCardData {
  const status = task.status;
  const rawPhase = String(status.phase || status.status || task.job_status || '');
  const phase = normalizeDocumentCardStatus({
    status: status.status,
    phase: status.phase,
    release_status: status.release_status,
    error_code: status.error_code,
    error_message: status.error_message,
    harness_run: status.harness_run,
  });
  const planningState = status.planning_state && typeof status.planning_state === 'object'
    && !Array.isArray(status.planning_state)
    ? status.planning_state as Record<string, unknown>
    : null;
  const restoredProposal = (
    rawPhase === 'awaiting_confirmation' || rawPhase === 'awaiting_plan_confirmation'
  ) && planningState
    ? parsePlanProposal({
      ...planningState,
      status: planningState.proposal_status,
      executable: true,
      blockers: [],
    })
    : undefined;
  const clarificationState = status.clarification_state;
  let clarificationQuestion: Record<string, unknown> | null = null;
  if (clarificationState && typeof clarificationState === 'object' && !Array.isArray(clarificationState)) {
    const pending = (clarificationState as Record<string, unknown>).pending_question;
    if (pending && typeof pending === 'object' && !Array.isArray(pending)) {
      const question = pending as Record<string, unknown>;
      if (nonEmptyString(question.question_id) && nonEmptyString(question.content)) {
        clarificationQuestion = question;
      }
    }
  }
  // The card title must match the durable object it describes: a pending
  // intake question is a requirement clarification, and a session without a
  // work order is not a work order status.
  const cardKind = restoredProposal
    ? 'output_spec_confirmation'
    : clarificationQuestion
      ? 'requirement_clarification'
      : task.work_order_id
        ? 'work_order_status'
        : 'generation_session';
  const card: DocumentCardData = {
    kind: cardKind,
    status: phase,
    next_actions: Array.isArray(status.next_actions) ? status.next_actions : [],
    kb_name: task.kb_name,
    ...(task.task_id ? { task_id: task.task_id } : {}),
    work_order_id: task.work_order_id,
    generation_session_id: status.clarification_session_id ?? null,
  };
  if (restoredProposal) card.proposal = restoredProposal;
  if (status.target_format) card.targetFormat = status.target_format;
  const artifacts = parseCardArtifacts(status.artifacts);
  if (artifacts) card.artifacts = artifacts;
  if (typeof status.error_code === 'string' && status.error_code.trim()) {
    card.errorCode = status.error_code;
  }
  if (typeof status.error_message === 'string' && status.error_message.trim()) {
    card.errorMessage = status.error_message;
  }
  if (typeof status.retryable === 'boolean') card.retryable = status.retryable;
  const gateReview = parseGateReview(status.pending_review);
  if (gateReview) card.gateReview = gateReview;
  const progress = parseCardProgress(status.progress) ?? progressFromHarness(status.harness_run);
  if (progress) card.progress = progress;
  if (clarificationQuestion) {
    card.status = 'needs_clarification';
    card.question_id = nonEmptyString(clarificationQuestion.question_id);
    card.content = nonEmptyString(clarificationQuestion.content);
    card.options = parseStringOptions(clarificationQuestion.options);
    card.reason = nonEmptyString(clarificationQuestion.reason);
    card.next_actions = ['answer_clarification'];
  }
  return card;
}

/** Apply the same authoritative status normalization to a manual REST refresh. */
export function documentCardFromWorkOrderStatus(
  status: WorkOrderStatus,
  fallback: DocumentCardData,
): DocumentCardData {
  const next: DocumentCardData = {
    ...fallback,
    status: normalizeDocumentCardStatus({
      status: status.status,
      phase: status.phase,
      release_status: status.release_status,
      error_code: status.error_code,
      error_message: status.error_message,
      harness_run: status.harness_run,
    }),
    task_id: status.task_id || fallback.task_id,
    work_order_id: status.work_order_id || fallback.work_order_id,
    generation_session_id: status.clarification_session_id || fallback.generation_session_id,
    next_actions: status.next_actions && status.next_actions.length > 0
      ? status.next_actions
      : fallback.next_actions,
  };
  if (status.target_format) next.targetFormat = status.target_format;
  const artifacts = parseCardArtifacts(status.artifacts);
  if (artifacts) next.artifacts = artifacts;
  if (typeof status.error_code === 'string' && status.error_code.trim()) next.errorCode = status.error_code;
  if (typeof status.error_message === 'string' && status.error_message.trim()) next.errorMessage = status.error_message;
  if (typeof status.retryable === 'boolean') next.retryable = status.retryable;
  const gateReview = parseGateReview(status.pending_review);
  if (gateReview) {
    next.gateReview = gateReview;
  } else {
    // The authoritative status no longer reports a pending gate: clear the
    // stale block instead of keeping it until the next stream event.
    delete next.gateReview;
  }
  const progress = parseCardProgress(status.progress) ?? progressFromHarness(status.harness_run);
  if (progress) next.progress = progress;
  const clarificationState = status.clarification_state;
  if (clarificationState && typeof clarificationState === 'object' && !Array.isArray(clarificationState)) {
    const pending = (clarificationState as Record<string, unknown>).pending_question;
    if (pending && typeof pending === 'object' && !Array.isArray(pending)) {
      const question = pending as Record<string, unknown>;
      const questionId = nonEmptyString(question.question_id);
      const content = nonEmptyString(question.content);
      if (questionId && content) {
        next.status = 'needs_clarification';
        next.question_id = questionId;
        next.content = content;
        next.options = parseStringOptions(question.options);
        next.reason = nonEmptyString(question.reason);
        next.next_actions = ['answer_clarification'];
      }
    }
  }
  return next;
}

/** Fill a restored task card with the durable pending question, when present. */
export function documentCardFromGenerationSession(
  session: GenerationSession,
  fallback: DocumentCardData,
): DocumentCardData {
  const answeredQuestionIds = new Set<string>();
  let pendingQuestion: GenerationSession['messages'][number] | undefined;
  for (let index = session.messages.length - 1; index >= 0; index -= 1) {
    const message = session.messages[index];
    const questionId = message.question_id?.trim() ?? '';
    if (message.role === 'user') {
      if (questionId && Boolean(message.answer?.trim() || message.content?.trim())) {
        answeredQuestionIds.add(questionId);
      }
      continue;
    }
    if (
      message.role === 'assistant'
      && questionId
      && Boolean(message.content?.trim())
      && !answeredQuestionIds.has(questionId)
    ) {
      pendingQuestion = message;
      break;
    }
  }
  const hasPendingClarification = Boolean(pendingQuestion)
    && (session.status === 'needs_clarification' || session.status === 'awaiting_plan');
  // Never resurrect the latest assistant question solely because the session
  // is in a clarification phase.  A durable session can contain several
  // answered questions; only the reverse-scanned unanswered payload is safe
  // to expose in the composer.  A last_question_id without its unanswered
  // payload is not sufficient to create an answer action.
  const question = pendingQuestion;
  const displayStatus = hasPendingClarification
    ? 'needs_clarification'
    : session.status === 'awaiting_plan'
      ? 'draft'
      : session.status;
  return {
    ...fallback,
    status: displayStatus,
    kb_name: session.knowledge_base_name || fallback.kb_name,
    task_id: session.document_task_id ?? fallback.task_id,
    work_order_id: session.work_order_id ?? fallback.work_order_id,
    generation_session_id: session.session_id,
    next_actions: hasPendingClarification
      ? ['answer_clarification']
      : fallback.next_actions,
    question_id: hasPendingClarification ? (question?.question_id ?? null) : null,
    content: hasPendingClarification ? (question?.content ?? null) : null,
    options: hasPendingClarification ? (question?.options ?? []) : [],
    reason: hasPendingClarification ? (question?.reason ?? null) : null,
  };
}

/** 澄清回答交互判定：卡片处于待澄清状态且具备问题、会话与提交回调时才可作答。 */
export function canAnswerClarification(card: DocumentCardData, hasAnswerCallback: boolean): boolean {
  return card.status === 'needs_clarification'
    && Boolean(card.question_id && card.generation_session_id && hasAnswerCallback);
}

/** 归一化澄清回答：空白或提交中返回 null（提交为 no-op），否则返回去空白的回答文本。 */
export function clarificationAnswerValue(rawAnswer: string, answering: boolean): string | null {
  const value = rawAnswer.trim();
  return !answering && value ? value : null;
}

export function documentCardIdentity(card: DocumentCardData): string {  const taskId = card.task_id?.trim() ?? '';
  if (taskId) return `task:${taskId}`;
  const workOrderId = card.work_order_id?.trim() ?? '';
  if (workOrderId) return `wo:${workOrderId}`;
  const sessionId = card.generation_session_id?.trim() ?? '';
  if (sessionId) return `gs:${sessionId}`;
  return `adhoc:${card.kind}`;
}

function documentCardReferences(card: DocumentCardData): string[] {
  const references: string[] = [];
  const taskId = card.task_id?.trim() ?? '';
  const workOrderId = card.work_order_id?.trim() ?? '';
  const sessionId = card.generation_session_id?.trim() ?? '';
  if (taskId) references.push(`task:${taskId}`);
  if (workOrderId) references.push(`wo:${workOrderId}`);
  if (sessionId) references.push(`gs:${sessionId}`);
  return references;
}

/** 同一工单/会话/同类 adhoc 卡片原地替换旧卡片(状态推进),其余追加,避免消息流重复刷屏。 */
export function mergeDocumentCards(prev: DocumentCardData[], next: DocumentCardData): DocumentCardData[] {
  const nextReferences = documentCardReferences(next);
  const index = prev.findIndex((card) => {
    const references = documentCardReferences(card);
    if (references.length > 0 && nextReferences.length > 0) {
      return references.some((reference) => nextReferences.includes(reference));
    }
    return documentCardIdentity(card) === documentCardIdentity(next);
  });
  if (index >= 0) {
    const updated = prev.slice();
    // Clarification events intentionally omit work-order/artifact fields;
    // spreading retains those already projected from document_card or REST.
    updated[index] = { ...prev[index], ...next };
    return updated;
  }
  return [...prev, next];
}

export function documentCardTitle(kind: string): string {
  return CARD_TITLES[kind] ?? '文档状态';
}

export function documentCardStatusLabel(status: string): string {
  return CARD_STATUS_LABELS[status] ?? describeWorkOrderStatus(status).label;
}

/** 暴露给卡片操作区使用的 next_action 文案。 */
export function nextActionLabel(action: string): string {
  return NEXT_ACTION_LABELS[action] ?? '前往工作台处理';
}

/**
 * Gate 1 确认输入:只允许提交用户看到过的 spec/plan 哈希。任一哈希缺失或
 * request id 为空时返回 null(提交为 no-op),前端绝不自行推导哈希。
 */
export function planConfirmationInput(
  card: DocumentCardData,
  clientRequestId: string,
): { expected_output_spec_hash: string; expected_plan_hash: string; client_request_id: string } | null {
  const specHash = card.proposal?.output_spec_hash?.trim() ?? '';
  const planHash = card.proposal?.plan_hash?.trim() ?? '';
  const requestId = clientRequestId.trim();
  if (!specHash || !planHash || !requestId) return null;
  return {
    expected_output_spec_hash: specHash,
    expected_plan_hash: planHash,
    client_request_id: requestId,
  };
}

export function documentCardStatusTone(status: string): DocumentStatusTone {
  return CARD_STATUS_TONES[status] ?? describeWorkOrderStatus(status).tone;
}

export function buildWorkbenchDeepLink(
  kbName: string,
  workOrderId = '',
  references: { sessionId?: string | null; taskId?: string | null } = {},
): string {
  const params = [`kb=${encodeURIComponent(kbName)}`];
  if (workOrderId.trim()) params.push(`workOrder=${encodeURIComponent(workOrderId)}`);
  else if (references.sessionId?.trim()) params.push(`session=${encodeURIComponent(references.sessionId)}`);
  else if (references.taskId?.trim()) params.push(`task=${encodeURIComponent(references.taskId)}`);
  return `/document-generation?${params.join('&')}`;
}

export function documentWorkOrderStatusPath(kbName: string, workOrderId: string): string {
  return `/api/v1/document-generation/work-orders/${encodeURIComponent(workOrderId)}/status?kb=${encodeURIComponent(kbName)}`;
}

export function documentChatTasksPath(sessionId?: number | null): string {
  const suffix = sessionId == null ? '' : `?session_id=${encodeURIComponent(String(sessionId))}`;
  return `/api/v1/document-generation/chat-tasks${suffix}`;
}

export function documentCurrentChatTaskPath(sessionId: number): string {
  return `/api/v1/document-generation/chat-tasks/current?session_id=${encodeURIComponent(String(sessionId))}`;
}

export function documentChatTaskEventsPath(sessionId: number): string {
  return `/api/v1/document-generation/chat-tasks/events?session_id=${encodeURIComponent(String(sessionId))}`;
}

export function parseDocumentTaskStreamEvent(data: string): DocumentChatTaskView | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data) as unknown;
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const current = (parsed as Record<string, unknown>).current;
  if (!current || typeof current !== 'object' || Array.isArray(current)) return null;
  const task = current as Record<string, unknown>;
  if (
    typeof task.session_id !== 'number'
    || !nonEmptyString(task.task_id)
    || !nonEmptyString(task.kb_name)
    || !task.status
    || typeof task.status !== 'object'
    || Array.isArray(task.status)
  ) return null;
  return current as DocumentChatTaskView;
}

export async function fetchDocumentCurrentChatTask(sessionId: number): Promise<DocumentChatTaskView | null> {
  return api.get<DocumentChatTaskView | null>(documentCurrentChatTaskPath(sessionId));
}

export async function fetchDocumentChatTasks(sessionId?: number | null): Promise<DocumentChatTaskView[]> {
  return api.get<DocumentChatTaskView[]>(documentChatTasksPath(sessionId));
}

export async function fetchDocumentWorkOrderStatus(kbName: string, workOrderId: string): Promise<WorkOrderStatus> {
  return api.get<WorkOrderStatus>(documentWorkOrderStatusPath(kbName, workOrderId));
}

/** 产物下载走 REST 直链(对齐工作台);URL 由前端拼装,卡片不携带任何路径/链接。 */
export function documentArtifactDownloadPath(artifactId: string, kb: string): string {
  return `/api/v1/document-generation/artifacts/${artifactId}/download?kb=${encodeURIComponent(kb)}`;
}

/** 客户端提供一个稳定回退文件名;服务端同时返回安全的 Content-Disposition。 */
export function documentArtifactFileName(artifactId: string, targetFormat?: string): string {
  return `${artifactId}.${targetFormat || 'bin'}`;
}

export async function downloadDocumentArtifact(
  card: DocumentCardData,
  artifact: DocumentCardArtifact,
): Promise<void> {
  await apiDownload.blob(
    artifact.download_url || documentArtifactDownloadPath(artifact.artifact_id, card.kb_name),
    documentArtifactFileName(artifact.artifact_id, artifact.output_format || card.targetFormat),
  );
}

export function documentCardWorkbenchActions(
  card: DocumentCardData,
): Array<{ action: string; label: string; href: string }> {
  const workOrderId = card.work_order_id?.trim() ?? '';
  const sessionId = card.generation_session_id?.trim() ?? '';
  const taskId = card.task_id?.trim() ?? '';
  if (!workOrderId && !sessionId && !taskId) return [];
  const gates = card.next_actions.filter((action) => !DOCUMENT_CARD_REFRESH_ACTIONS.has(action));
  // A session/task that only asks the client to poll has no workbench action;
  // keep the legacy card compact until a human action is available.
  if (!workOrderId && gates.length === 0) return [];
  if (!workOrderId && gates.includes('answer_clarification') && !card.question_id?.trim()) return [];
  const href = buildWorkbenchDeepLink(card.kb_name, workOrderId, { sessionId, taskId });
  if (gates.length === 0) {
    return [{ action: 'open_workbench', label: '前往工作台', href }];
  }
  return gates
    .slice(0, 3)
    .map((action) => ({ action, label: NEXT_ACTION_LABELS[action] ?? '前往工作台处理', href }));
}
