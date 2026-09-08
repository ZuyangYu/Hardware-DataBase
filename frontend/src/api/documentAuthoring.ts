/**
 * 文档创作共享契约:Chat 聊天页与文档生成工作台共用的模板上传/分析请求,
 * 以及从 useKbChat 迁入的 document context 纯函数。
 */
import { api, uploadFiles, uploadFilesWithProgress } from './client';
import type {
  ArtifactRevision,
  DocumentAnalysis,
  DocumentContext,
  DocumentReview,
  DocumentTaskProjection,
  GenerationSession,
} from './types';

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

/**
 * Build the server-side attachment bridge request. The attachment bytes stay
 * in private storage; only the session-scoped reference crosses the wire.
 */
export function buildAttachmentTemplateAnalyzeRequest(
  _kb?: string,
  _sessionId?: number,
  _attachmentId?: string,
  _templateName?: string,
): string {
  return '/api/v1/document-generation/templates/analyze-from-attachment';
}

/** Build the idempotent clarification command shared by Workbench and Chat. */
export function buildClarificationAnswerRequest(
  sessionId: string | number,
  knowledgeBaseName: string,
  questionId: string,
  answer: string,
  clientRequestId = createClientRequestId(),
): {
  path: string;
  body: {
    question_id: string;
    answer: string;
    client_request_id: string;
  };
} {
  return {
    path: `/api/v1/document-generation/sessions/${encodeURIComponent(String(sessionId))}/messages?kb=${encodeURIComponent(knowledgeBaseName)}`,
    body: {
      question_id: questionId,
      answer,
      client_request_id: clientRequestId,
    },
  };
}

export function generationSessionPath(knowledgeBaseName: string, sessionId: string | number): string {
  return `/api/v1/document-generation/sessions/${encodeURIComponent(String(sessionId))}?kb=${encodeURIComponent(knowledgeBaseName)}`;
}

export async function fetchGenerationSession(
  knowledgeBaseName: string,
  sessionId: string | number,
): Promise<GenerationSession> {
  return api.get<GenerationSession>(generationSessionPath(knowledgeBaseName, sessionId));
}

export async function answerGenerationSession(
  knowledgeBaseName: string,
  sessionId: string | number,
  questionId: string,
  answer: string,
): Promise<GenerationSession> {
  const request = buildClarificationAnswerRequest(sessionId, knowledgeBaseName, questionId, answer);
  return api.post<GenerationSession>(request.path, request.body);
}

/** Convert an uploaded chat attachment into an independent TemplateVersion. */
export function analyzeTemplateFromAttachment(
  kb: string,
  sessionId: number,
  attachmentId: string,
  templateName: string,
): Promise<DocumentAnalysis> {
  return api.post<DocumentAnalysis>(
    buildAttachmentTemplateAnalyzeRequest(),
    {
      kb,
      session_id: sessionId,
      attachment_id: attachmentId,
      template_name: templateName,
    },
  );
}

export type DocumentArtifactConversionJob = {
  job_id: string;
  operation: 'convert_artifact';
  status: string;
  source_artifact_id: string;
  target_format: 'pdf' | 'pptx';
};

/** Queue semantic template-artifact conversion; the worker owns the bytes. */
export function requestDocumentArtifactConversion(
  kb: string,
  artifactId: string,
  targetFormat: 'pdf' | 'pptx',
): Promise<DocumentArtifactConversionJob> {
  return api.post<DocumentArtifactConversionJob>(
    `/api/v1/document-generation/artifacts/${encodeURIComponent(artifactId)}/convert?kb=${encodeURIComponent(kb)}`,
    { target_format: targetFormat },
  );
}

export function documentTaskProjectionPath(knowledgeBaseName: string, taskId: string): string {
  return `/api/v1/document-generation/tasks/${encodeURIComponent(taskId)}/projection?kb=${encodeURIComponent(knowledgeBaseName)}`;
}

export async function fetchDocumentTaskProjection(
  knowledgeBaseName: string,
  taskId: string,
): Promise<DocumentTaskProjection> {
  return api.get<DocumentTaskProjection>(documentTaskProjectionPath(knowledgeBaseName, taskId));
}

export function documentTaskReviewsPath(knowledgeBaseName: string, taskId: string): string {
  return `/api/v1/document-generation/tasks/${encodeURIComponent(taskId)}/reviews?kb=${encodeURIComponent(knowledgeBaseName)}`;
}

export async function fetchDocumentTaskReviews(
  knowledgeBaseName: string,
  taskId: string,
): Promise<DocumentReview[]> {
  return api.get<DocumentReview[]>(documentTaskReviewsPath(knowledgeBaseName, taskId));
}

export function documentTaskRevisionsPath(knowledgeBaseName: string, taskId: string): string {
  return `/api/v1/document-generation/tasks/${encodeURIComponent(taskId)}/revisions?kb=${encodeURIComponent(knowledgeBaseName)}`;
}

export async function fetchDocumentTaskRevisions(
  knowledgeBaseName: string,
  taskId: string,
): Promise<ArtifactRevision[]> {
  return api.get<ArtifactRevision[]>(documentTaskRevisionsPath(knowledgeBaseName, taskId));
}

export type CreateDocumentRevisionInput = {
  parent_artifact_id: string;
  request_type: ArtifactRevision['request_type'];
  request: string;
  changed_fields?: string[];
  changed_sections?: string[];
  client_request_id: string;
  metadata?: Record<string, unknown>;
};

export async function createDocumentRevision(
  knowledgeBaseName: string,
  taskId: string,
  body: CreateDocumentRevisionInput,
): Promise<ArtifactRevision> {
  return api.post<ArtifactRevision>(documentTaskRevisionsPath(knowledgeBaseName, taskId), body);
}

export type CompleteDocumentRevisionInput = {
  child_artifact_id: string;
  revalidation_status: 'passed' | 'failed' | 'requires_human';
  revalidation_result?: Record<string, unknown>;
};

/** Commit a controlled worker's already-persisted revision child artifact. */
export async function completeDocumentRevision(
  knowledgeBaseName: string,
  revisionId: string,
  body: CompleteDocumentRevisionInput,
): Promise<ArtifactRevision> {
  return api.post<ArtifactRevision>(
    `/api/v1/document-generation/revisions/${encodeURIComponent(revisionId)}/complete?kb=${encodeURIComponent(knowledgeBaseName)}`,
    body,
  );
}

export type DocumentReviewDecisionInput = {
  subject_hash: string;
  decision: unknown;
  client_request_id: string;
  status?: string;
  decision_metadata?: Record<string, unknown>;
};

export async function submitDocumentReviewDecision(
  knowledgeBaseName: string,
  reviewId: string,
  body: DocumentReviewDecisionInput,
): Promise<DocumentReview> {
  return api.post<DocumentReview>(
    `/api/v1/document-generation/reviews/${encodeURIComponent(reviewId)}/decision?kb=${encodeURIComponent(knowledgeBaseName)}`,
    body,
  );
}

export async function resumeDocumentTask(
  knowledgeBaseName: string,
  taskId: string,
): Promise<{ task_id: string; work_order_id?: string; run_id?: string; status: string }> {
  return api.post(
    `/api/v1/document-generation/tasks/${encodeURIComponent(taskId)}/resume?kb=${encodeURIComponent(knowledgeBaseName)}`,
    {},
  );
}
