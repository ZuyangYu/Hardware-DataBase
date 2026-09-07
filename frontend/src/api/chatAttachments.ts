/**
 * 会话附件 API(对齐 src/api/routes/attachments.py)。
 * 上传/列表/详情/删除/重试;所有请求跟随会话作用域。
 */
import { api, uploadFiles } from './client';
import type { AttachmentView } from './types';

export const CHAT_ATTACHMENT_ACCEPT = '.pdf,.docx,.txt,.md,.xlsx,.xlsm,.edf,.edif';

export function listAttachments(sessionId: number): Promise<AttachmentView[]> {
  return api.get<AttachmentView[]>(`/api/v1/conversations/${sessionId}/attachments`);
}

export function uploadAttachment(
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
  return uploadFiles<AttachmentView>(
    `/api/v1/conversations/${sessionId}/attachments${query ? `?${query}` : ''}`,
    form,
  );
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
