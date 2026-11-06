/**
 * 会话附件 API(对齐 src/api/routes/attachments.py)。
 * 上传/列表/详情/删除/重试;所有请求跟随会话作用域。
 */
import { ApiError, api, uploadFiles } from './client';
import type { AttachmentView } from './types';

export const CHAT_ATTACHMENT_ACCEPT = '.pdf,.docx,.txt,.md,.xlsx,.xlsm,.edf,.edif,.drawio';

const RETRYABLE_UPLOAD_STATUSES = new Set([502, 503, 504]);
const UPLOAD_RETRY_DELAYS_MS = [700, 1400];

function isTransientUploadError(error: unknown): error is ApiError {
  return error instanceof ApiError
    && (RETRYABLE_UPLOAD_STATUSES.has(error.status)
      || (error.status === 500 && !error.body.trim()));
}

export function listAttachments(sessionId: number): Promise<AttachmentView[]> {
  return api.get<AttachmentView[]>(`/api/v1/conversations/${sessionId}/attachments`);
}

export async function uploadAttachment(
  sessionId: number,
  file: File,
  options: { clientRequestId?: string; usageHint?: 'reference' | 'data' } = {},
): Promise<AttachmentView> {
  const form = new FormData();
  form.append('file', file);
  const params = new URLSearchParams();
  if (options.clientRequestId) params.set('client_request_id', options.clientRequestId);
  if (options.usageHint) params.set('usage_hint', options.usageHint);
  const query = params.toString();
  const path = `/api/v1/conversations/${sessionId}/attachments${query ? `?${query}` : ''}`;
  const send = () => uploadFiles<AttachmentView>(path, form);
  // The API may restart while the user is uploading.  Retrying is safe only
  // when the caller supplied an idempotency key; the backend then returns the
  // existing attachment instead of creating a duplicate.
  if (!options.clientRequestId) return send();
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await send();
    } catch (error) {
      if (attempt >= UPLOAD_RETRY_DELAYS_MS.length || !isTransientUploadError(error)) throw error;
      await new Promise((resolve) => setTimeout(resolve, UPLOAD_RETRY_DELAYS_MS[attempt]));
    }
  }
}

export function deleteAttachment(sessionId: number, attachmentId: string): Promise<void> {
  return api.delete(`/api/v1/conversations/${sessionId}/attachments/${attachmentId}`);
}

export function retryAttachment(sessionId: number, attachmentId: string): Promise<AttachmentView> {
  return api.post<AttachmentView>(
    `/api/v1/conversations/${sessionId}/attachments/${attachmentId}/retry`,
  );
}

export function isAttachmentTerminal(attachment: AttachmentView): boolean {
  return attachment.parse_status === 'ready' || attachment.parse_status === 'degraded' || attachment.parse_status === 'failed';
}
