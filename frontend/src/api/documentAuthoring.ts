/**
 * 文档创作共享契约:Chat 聊天页与文档生成工作台共用的模板上传/分析请求,
 * 以及从 useKbChat 迁入的 document context 纯函数。
 */
import { uploadFiles, uploadFilesWithProgress } from './client';
import type { DocumentAnalysis, DocumentContext } from './types';

export const DOCUMENT_CONTEXT_VERSION = 1;
// Keep the client-side affordance aligned with the server-owned 30 minute
// context lease.  The server remains authoritative and may shorten it.
export const DOCUMENT_CONTEXT_TTL_MS = 30 * 60 * 1000;

/** crypto.randomUUID 仅在安全上下文(HTTPS/localhost)存在;HTTP+IP 访问时回退到手动拼 UUID。 */
export function createClientRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

/**
 * Analysis fields the chat wire format needs. The runtime guards below still
 * tolerate missing/non-string values so older analyze responses fail closed.
 */
type DocumentContextAnalysis = Pick<
  DocumentAnalysis,
  'analysis_id' | 'template_version_id' | 'expiry' | 'expires_at'
>;

/**
 * Convert the upload response into the only document reference the chat wire
 * format understands. No file name, path, binary content, or user-written
 * instruction is copied into the query string.
 */
export function buildDocumentContext(
  analysis: DocumentContextAnalysis,
  knowledgeBaseName: string,
  clientRequestId = createClientRequestId(),
  now = Date.now(),
): DocumentContext | null {
  const analysisId = typeof analysis.analysis_id === 'string' ? analysis.analysis_id.trim() : '';
  const templateVersionId = typeof analysis.template_version_id === 'string'
    ? analysis.template_version_id.trim()
    : '';
  const kbName = knowledgeBaseName.trim();
  const requestId = clientRequestId.trim();
  if (!analysisId || !templateVersionId || !kbName || !requestId) return null;

  const fallbackExpiry = new Date(now + DOCUMENT_CONTEXT_TTL_MS).toISOString();
  const serverExpiry = (typeof analysis.expiry === 'string' ? analysis.expiry.trim() : '')
    || (typeof analysis.expires_at === 'string' ? analysis.expires_at.trim() : '');
  const expiry = serverExpiry && Number.isFinite(Date.parse(serverExpiry))
    ? serverExpiry
    : fallbackExpiry;

  return {
    analysis_id: analysisId,
    template_version_id: templateVersionId,
    knowledge_base_name: kbName,
    version: DOCUMENT_CONTEXT_VERSION,
    expiry,
    client_request_id: requestId,
  };
}

/** Invalid/missing expiry is treated as expired so a malformed reference is fail-closed. */
export function isDocumentContextExpired(
  context: DocumentContext | null | undefined,
  now = Date.now(),
): boolean {
  if (!context) return false;
  const expiry = Date.parse(typeof context.expiry === 'string' ? context.expiry : '');
  return !Number.isFinite(expiry) || expiry <= now;
}

/** The single analyze endpoint both the chat page and the workbench upload to. */
export function buildTemplateAnalyzeRequest(
  kb: string,
  file: File,
  name?: string,
  clientRequestId?: string,
): { path: string; form: FormData } {
  const form = new FormData();
  form.append('file', file);
  form.append('template_name', name || file.name);
  // Falsy ids (undefined or empty string) stay off the wire form entirely.
  if (clientRequestId) form.append('client_request_id', clientRequestId);
  return {
    path: `/api/v1/document-generation/templates/analyze?kb=${encodeURIComponent(kb)}`,
    form,
  };
}

export type TemplateAnalyzeOptions = {
  /** Chat page reuses a fingerprint-based id for dedupe; the workbench omits it. */
  clientRequestId?: string;
  /** Chat page renders an upload progress bar; the workbench does not. */
  onProgress?: (percent: number) => void;
};

/**
 * Shared template upload/analyze contract for ChatPage.handleTemplateUpload and
 * DocumentGenerationPage's TemplateSection. template_name falls back to the
 * file name when omitted or empty; per-page request/UX differences (dedupe id,
 * progress bar) are expressed purely through the optional options above.
 */
export async function analyzeTemplate(
  kb: string,
  file: File,
  name?: string,
  options: TemplateAnalyzeOptions = {},
): Promise<DocumentAnalysis> {
  const { path, form } = buildTemplateAnalyzeRequest(kb, file, name, options.clientRequestId);
  if (options.onProgress) {
    return uploadFilesWithProgress<DocumentAnalysis>(path, form, options.onProgress);
  }
  return uploadFiles<DocumentAnalysis>(path, form);
}
