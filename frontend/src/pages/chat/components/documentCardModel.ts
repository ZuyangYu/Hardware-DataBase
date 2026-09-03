/**
 * documentCardModel -- document_card 事件的纯数据模型(对齐 documentGenerationModel 约定)。
 *
 * 线格式 {"type":"document_card","payload":{"card":{...}}};卡片只携带不可变引用与
 * 状态枚举:kind / status / next_actions / kb_name / work_order_id / generation_session_id /
 * target_format / artifacts(仅 artifact_id+stage)。深链指向文档生成工作台;
 * 刷新动作直调 REST 工单状态接口;产物下载 URL 由前端拼装(REST 直链)。
 */
import { api, apiDownload } from '@/api/client';
import type { DocumentChatTaskView, WorkOrderStatus } from '@/api/types';
import { describeWorkOrderStatus, type DocumentStatusTone } from '../../documentGenerationModel';

export type DocumentCardArtifact = {
  artifact_id: string;
  stage: string;
  preview_url?: string;
  download_url?: string;
};

export type DocumentCardData = {
  kind: string;
  status: string;
  next_actions: string[];
  kb_name: string;
  work_order_id?: string | null;
  generation_session_id?: string | null;
  targetFormat?: string;
  artifacts?: DocumentCardArtifact[];
};

/** 这些动作可以直接刷新状态(REST),其余 next_action 视为人工门,深链到工作台。 */
export const DOCUMENT_CARD_REFRESH_ACTIONS: ReadonlySet<string> = new Set([
  'get_document_generation_status',
  'poll_status',
]);

const CARD_TITLES: Record<string, string> = {
  work_order_created: '工单已创建',
  work_order_status: '工单状态',
  generation_session: '需求会话',
};

const CARD_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '进行中',
  succeeded: '已成功',
};

const CARD_STATUS_TONES: Record<string, DocumentStatusTone> = {
  queued: 'info',
  running: 'info',
  succeeded: 'success',
};

const NEXT_ACTION_LABELS: Record<string, string> = {
  submit_icd_scope_resolution: '处理 ICD 范围待办',
  answer_clarification: '回答澄清问题',
  start_document_generation_session: '开始需求澄清',
  create_document_work_order: '创建生成工单',
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
      ...(typeof entry.preview_url === 'string' && entry.preview_url.trim()
        ? { preview_url: entry.preview_url }
        : {}),
      ...(typeof entry.download_url === 'string' && entry.download_url.trim()
        ? { download_url: entry.download_url }
        : {}),
    }));
  return parsed.length > 0 ? parsed : undefined;
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
    return {
      kind,
      status: typeof card.status === 'string' ? card.status : '',
      next_actions: Array.isArray(card.next_actions)
        ? card.next_actions.filter((action): action is string => typeof action === 'string')
        : [],
      kb_name: typeof card.kb_name === 'string' ? card.kb_name : '',
      work_order_id: typeof card.work_order_id === 'string' && card.work_order_id.trim() ? card.work_order_id : null,
      generation_session_id: typeof card.generation_session_id === 'string' && card.generation_session_id.trim()
        ? card.generation_session_id
        : null,
      ...(targetFormat ? { targetFormat } : {}),
      ...(artifacts ? { artifacts } : {}),
    };
  } catch {
    return null;
  }
}

/** Project a durable background task into the same card contract as SSE events. */
export function documentCardFromChatTask(task: DocumentChatTaskView): DocumentCardData {
  const status = task.status;
  const card: DocumentCardData = {
    kind: 'work_order_status',
    status: String(status.phase || status.status || task.job_status || ''),
    next_actions: Array.isArray(status.next_actions) ? status.next_actions : [],
    kb_name: task.kb_name,
    work_order_id: task.work_order_id,
    generation_session_id: status.clarification_session_id ?? null,
  };
  if (status.target_format) card.targetFormat = status.target_format;
  const artifacts = parseCardArtifacts(status.artifacts);
  if (artifacts) card.artifacts = artifacts;
  return card;
}

export function documentCardIdentity(card: DocumentCardData): string {
  const workOrderId = card.work_order_id?.trim() ?? '';
  if (workOrderId) return `wo:${workOrderId}`;
  const sessionId = card.generation_session_id?.trim() ?? '';
  if (sessionId) return `gs:${sessionId}`;
  return `adhoc:${card.kind}`;
}

/** 同一工单/会话/同类 adhoc 卡片原地替换旧卡片(状态推进),其余追加,避免消息流重复刷屏。 */
export function mergeDocumentCards(prev: DocumentCardData[], next: DocumentCardData): DocumentCardData[] {
  const identity = documentCardIdentity(next);
  const index = prev.findIndex((card) => documentCardIdentity(card) === identity);
  if (index >= 0) {
    const updated = prev.slice();
    updated[index] = next;
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

export function documentCardStatusTone(status: string): DocumentStatusTone {
  return CARD_STATUS_TONES[status] ?? describeWorkOrderStatus(status).tone;
}

export function buildWorkbenchDeepLink(kbName: string, workOrderId: string): string {
  return `/document-generation?kb=${encodeURIComponent(kbName)}&workOrder=${encodeURIComponent(workOrderId)}`;
}

export function documentWorkOrderStatusPath(kbName: string, workOrderId: string): string {
  return `/api/v1/document-generation/work-orders/${encodeURIComponent(workOrderId)}/status?kb=${encodeURIComponent(kbName)}`;
}

export function documentChatTasksPath(sessionId?: number | null): string {
  const suffix = sessionId == null ? '' : `?session_id=${encodeURIComponent(String(sessionId))}`;
  return `/api/v1/document-generation/chat-tasks${suffix}`;
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
    documentArtifactFileName(artifact.artifact_id, card.targetFormat),
  );
}

export function documentCardWorkbenchActions(
  card: DocumentCardData,
): Array<{ action: string; label: string; href: string }> {
  const workOrderId = card.work_order_id?.trim() ?? '';
  if (!workOrderId) return [];
  const href = buildWorkbenchDeepLink(card.kb_name, workOrderId);
  const gates = card.next_actions.filter((action) => !DOCUMENT_CARD_REFRESH_ACTIONS.has(action));
  if (gates.length === 0) {
    return [{ action: 'open_workbench', label: '前往工作台', href }];
  }
  return gates
    .slice(0, 3)
    .map((action) => ({ action, label: NEXT_ACTION_LABELS[action] ?? '前往工作台处理', href }));
}
