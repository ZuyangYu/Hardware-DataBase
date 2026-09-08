/**
 * 聊天页(薄壳,对齐 ChatPage 58 行结构)。
 * Shell 内聊天布局:会话侧栏 + 聊天主区(头部 + 消息列表 + 输入区)。
 * 数据层在 useKbChat;渲染在各 chat/components 子组件。
 */
import type { CSSProperties } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import { api, downloadBlob } from '@/api/client';
import { analyzeTemplate, analyzeTemplateFromAttachment } from '@/api/documentAuthoring';
import {
  deleteAttachment,
  listAttachments,
  retryAttachment,
  uploadAttachment,
} from '@/api/chatAttachments';
import type { AttachmentView, ExportArtifactView, ExportBatchResponse, ExportFormat, ExportFormatsResponse, ExportJobView, KbView, MessageView } from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { CHAT_MAIN_CLASS, chatBubbleClass, chatRowClass } from './chatPageStyles';
import {
  buildDocumentContext,
  createClientRequestId,
  useKbChat,
} from './useKbChat';
import DocumentStatusCard, { documentCardIdentity } from './components/DocumentStatusCard';
import ChatSessionSidebar from './components/ChatSessionSidebar';
import ChatHeader from './components/ChatHeader';
import MessageList from './components/MessageList';
import Composer from './components/Composer';
import EditMessageDialog from './EditMessageDialog';
import { notify } from '@/components/ui/app-toast';
import {
  buildExportRequest,
  exportFormatLabel,
  isExportTerminal,
  mergeExportJob,
} from './exportResultModel';

type Props = {
  auth: AuthSession;
  kbName?: string;
  availableKbs?: KbView[];
  onLogout: () => void;
  /** Opt-out override for the Task 9 bridge; omitted means the env flag/default. */
  documentAuthoringEnabled?: boolean;
};

const DOCUMENT_TEMPLATE_EXTENSIONS = ['.xlsx', '.xlsm', '.docx'];
const ATTACHMENT_STATUS_POLL_INTERVAL_MS = 2_000;
const DOCUMENT_COMPARISON_PATTERN = /对比|比较|区别|差异|差别|异同|对照|compare|comparison|difference|diff/i;

/**
 * Detect the natural-language command that means "use the attached office
 * file as a template and produce a filled document".  The backend remains
 * authoritative; this client hint only removes the extra manual "作为模板"
 * click when exactly one unambiguous office attachment is selected.
 */
export function isTemplateGenerationChatIntent(query: string): boolean {
  const text = String(query || '').trim();
  if (!text) return false;
  // Comparison is a retrieval/reporting operation.  It must win over words
  // such as “模板” and “生成” so the UI never converts a comparison into a
  // template-fill request before the server sees the turn.
  if (DOCUMENT_COMPARISON_PATTERN.test(text)) return false;
  const action = '(?:参考|按照|按|基于|根据|填充|填写|回填|生成|创建|制作|撰写|起草|导出|输出|整理成|generate|create|fill|draft|produce|write|export|output)';
  const target = '(?:模板|模版|文档|文件|报告|ICD|表格|表单|template|document|report|sheet|form)';
  // Users naturally place the object first ("将模板…回填") or the
  // action first ("参考模板生成文档").  Both forms represent the same
  // server-side document-generation intent.
  return new RegExp(`${action}[\\s\\S]{0,80}${target}|${target}[\\s\\S]{0,80}${action}`, 'i').test(text);
}

export function listTemplateCandidateAttachments(
  query: string,
  attachments: AttachmentView[],
): AttachmentView[] {
  if (!isTemplateGenerationChatIntent(query)) return [];
  const seen = new Set<string>();
  return attachments.filter((attachment) => {
    const extension = String(attachment.extension || '').toLowerCase()
      || `.${String(attachment.filename || '').split('.').pop() || ''}`.toLowerCase();
    const id = String(attachment.attachment_id || '');
    if (!DOCUMENT_TEMPLATE_EXTENSIONS.includes(extension)
      || (attachment.parse_status !== 'ready' && attachment.parse_status !== 'degraded')
      || (id && seen.has(id))) {
      return false;
    }
    if (id) seen.add(id);
    return true;
  });
}

export type TemplateAttachmentRoute = {
  intent: boolean;
  /** The server owns executor selection; this is only a client-side hint. */
  authority: 'backend';
  candidates: AttachmentView[];
  autoTemplate: AttachmentView | null;
  requiresSelection: boolean;
  requiresTemplate: boolean;
};

export function resolveTemplateAttachmentRoute(
  query: string,
  attachments: AttachmentView[],
  hasDocumentContext = false,
): TemplateAttachmentRoute {
  const intent = isTemplateGenerationChatIntent(query);
  // An already attached context is an explicit template choice.  Do not let
  // additional Office evidence files make that choice ambiguous.
  const candidates = intent && !hasDocumentContext
    ? listTemplateCandidateAttachments(query, attachments)
    : [];
  return {
    intent,
    authority: 'backend',
    candidates,
    autoTemplate: candidates.length === 1 ? candidates[0] : null,
    requiresSelection: intent && !hasDocumentContext && candidates.length > 1,
    requiresTemplate: intent && !hasDocumentContext && candidates.length === 0,
  };
}

export function pickAutoTemplateAttachment(
  query: string,
  attachments: AttachmentView[],
): AttachmentView | null {
  const candidates = listTemplateCandidateAttachments(query, attachments);
  // More than one possible template is ambiguous; preserve the explicit UI
  // action so the user can choose the intended file.
  return candidates.length === 1 ? candidates[0] : null;
}

function documentUploadFingerprint(file: File): string {
  return `${file.name}\u0000${file.size}\u0000${file.lastModified}`;
}

/**
 * 对话侧文档工具桥默认开启;部署方将环境变量设为 falsy 字符串(如 "false")显式关闭。
 */
export function isDocumentAuthoringChatEnabled(value: unknown = undefined): boolean {
  const configured = value ?? import.meta.env.VITE_AGENT_DOCUMENT_TOOLS_ENABLED
    ?? import.meta.env.VITE_DOCUMENT_AUTHORING_CHAT_ENABLED;
  const normalized = String(configured ?? '').trim().toLowerCase();
  // 两个变量都未配置(或为空)时默认开启。
  if (normalized === '') {
    return true;
  }
  return ['1', 'true', 'yes', 'on'].includes(normalized);
}

export function resolveDocumentAuthoringEnabled(override?: boolean): boolean {
  return override ?? isDocumentAuthoringChatEnabled();
}

/**
 * 会话附件入口默认开启；部署方可将 VITE_CHAT_ATTACHMENTS_ENABLED
 * 设为 falsy 字符串显式关闭。
 */
export function isChatAttachmentsUiEnabled(value: unknown = undefined): boolean {
  const configured = value ?? import.meta.env.VITE_CHAT_ATTACHMENTS_ENABLED;
  const normalized = String(configured ?? '').trim().toLowerCase();
  if (normalized === '') return true;
  return ['1', 'true', 'yes', 'on'].includes(normalized);
}

export default function ChatPage({
  auth,
  kbName = '',
  availableKbs = [],
  onLogout,
  documentAuthoringEnabled: documentAuthoringEnabledOverride,
}: Props) {
  const navigate = useNavigate();
  const [mountedKbName, setMountedKbName] = useState(kbName);
  const [documentContextLabel, setDocumentContextLabel] = useState<string | null>(null);
  const [documentUploadPending, setDocumentUploadPending] = useState(false);
  const [documentUploadProgress, setDocumentUploadProgress] = useState(0);
  const [exportJobsByMessageId, setExportJobsByMessageId] = useState<Record<number, ExportJobView[]>>({});
  const [exportPreviewsByJobId, setExportPreviewsByJobId] = useState<Record<string, ExportArtifactView>>({});
  const [exportFormats, setExportFormats] = useState<ExportFormat[] | undefined>(undefined);
  const documentUploadRequestRef = useRef<{ fingerprint: string; clientRequestId: string } | null>(null);
  const documentAuthoringEnabled = resolveDocumentAuthoringEnabled(documentAuthoringEnabledOverride);
  const visibleKbs = useMemo(() => {
    const rows = availableKbs.filter((kb) => kb.permission);
    if (!mountedKbName || rows.some((kb) => kb.name === mountedKbName)) {
      return rows;
    }
    return [
      {
        name: mountedKbName,
        kb_id: null,
        department_id: null,
        department_name: null,
        permission: 'read',
        registered: true,
      },
      ...rows,
    ];
  }, [availableKbs, mountedKbName]);

  useEffect(() => {
    setMountedKbName(kbName);
    setDocumentContextLabel(null);
    documentUploadRequestRef.current = null;
  }, [kbName]);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<ExportFormatsResponse>('/api/v1/exports/formats')
      .then((formats) => {
        if (!cancelled) setExportFormats(formats);
      })
      .catch(() => {
        // Keep the legacy menu during a transient capability request failure;
        // the API remains authoritative and rejects disabled formats.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const chat = useKbChat(mountedKbName, { documentContextEnabled: documentAuthoringEnabled });
  const {
    sessions,
    sessionsLoaded,
    activeSession,
    activeSessionId,
    selectSession,
    newConversation,
    deleteSession,
    messages,
    messagesLoaded,
    evidenceByMessageId,
    memorySummary,
    refreshMemorySummary,
    updateAutoExtract,
    sessionConsents,
    refreshSessionConsents,
    revokeSessionConsent,
    updateMessage,
    input,
    setInput,
    streaming,
    streamingText,
    traceSteps,
    traceByMessageId,
    degradedNotes,
    documentCards,
    documentCardRefreshingId,
    documentCardAnsweringId,
    refreshDocumentCardStatus,
    answerDocumentCard,
    send,
    abortStream,
    forbidden,
    documentContext,
    setDocumentContext,
    documentFlowEnabled,
    setDocumentFlowEnabled,
  } = chat;

  // 标签跟随上下文生命周期:hook 侧清空模板上下文(新建/切换/删除会话)时同步清掉 chip 文案。
  useEffect(() => {
    if (documentContext == null) setDocumentContextLabel(null);
  }, [documentContext]);

  // 导出任务只保存 Artifact/任务引用，不把二进制塞进消息；刷新或切换会话后从服务端重新对账。
  useEffect(() => {
    if (activeSessionId == null) return undefined;
    let cancelled = false;
    void api
      .get<ExportJobView[]>(`/api/v1/exports?session_id=${activeSessionId}`)
      .then((jobs) => {
        if (cancelled) return;
        const byTurn = new Map(
          messages
            .filter((message) => message.role === 'assistant' && message.turn_id)
            .map((message) => [message.turn_id as string, message.id]),
        );
        setExportJobsByMessageId((previous) => {
          const next = { ...previous };
          for (const job of jobs) {
            const messageId = job.turn_id ? byTurn.get(job.turn_id) : undefined;
            if (messageId == null) continue;
            next[messageId] = mergeExportJob(next[messageId] ?? [], job);
          }
          return next;
        });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, messages]);

  async function waitForExportJob(messageId: number, jobId: string) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, attempt < 4 ? 500 : 1000));
      try {
        const job = await api.get<ExportJobView>(`/api/v1/exports/${encodeURIComponent(jobId)}`);
        setExportJobsByMessageId((previous) => ({
          ...previous,
          [messageId]: mergeExportJob(previous[messageId] ?? [], job),
        }));
        if (isExportTerminal(job.status)) return;
      } catch {
        // Worker/API 暂时不可用时继续轮询；任务本身仍由服务端持久化。
      }
    }
  }

  async function handleExportMessage(message: MessageView, format: ExportFormat) {
    if (!message.turn_id) {
      notify.error('该消息缺少可导出的持久化轮次，请刷新页面后重试');
      return;
    }
    try {
      const response = await api.post<ExportBatchResponse>(
        '/api/v1/exports',
        buildExportRequest(message.turn_id, format, `chat-export-${message.id}-${format}-${Date.now()}`),
      );
      const job = response.jobs[0];
      if (!job) throw new Error('导出任务未创建');
      setExportJobsByMessageId((previous) => ({
        ...previous,
        [message.id]: mergeExportJob(previous[message.id] ?? [], job),
      }));
      notify.success(`${exportFormatLabel(format)} 导出任务已提交`);
      if (!isExportTerminal(job.status)) void waitForExportJob(message.id, job.export_job_id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '提交导出任务失败');
    }
  }

  async function handleDownloadExport(job: ExportJobView) {
    if (!job.artifact) return;
    try {
      await downloadBlob(
        job.artifact.download_url || `/api/v1/artifacts/${encodeURIComponent(job.artifact.artifact_id)}/download`,
        job.artifact.filename,
      );
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '下载导出文件失败');
    }
  }

  async function handlePreviewExport(job: ExportJobView) {
    if (!job.artifact) return;
    try {
      const preview = await api.get<ExportArtifactView>(
        job.artifact.preview_url || `/api/v1/artifacts/${encodeURIComponent(job.artifact.artifact_id)}/preview`,
      );
      setExportPreviewsByJobId((previous) => ({ ...previous, [job.export_job_id]: preview }));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载 Artifact 预览失败');
    }
  }

  const canUploadDocumentTemplate = Boolean(
    documentAuthoringEnabled
      && mountedKbName
      && ['write', 'admin'].includes(
        availableKbs.find((kb) => kb.name === mountedKbName)?.permission ?? '',
      ),
  );
  const chatAttachmentsEnabled = isChatAttachmentsUiEnabled();

  useEffect(() => {
    if (!documentAuthoringEnabled) {
      setDocumentContext(null);
      setDocumentContextLabel(null);
    }
  }, [documentAuthoringEnabled, setDocumentContext]);

  async function handleTemplateUpload(file: File) {
    if (!canUploadDocumentTemplate || documentUploadPending) return;
    const extension = `.${file.name.split('.').pop() || ''}`.toLowerCase();
    if (!DOCUMENT_TEMPLATE_EXTENSIONS.includes(extension)) {
      notify.error('模板仅支持 .xlsx、.xlsm 或 .docx 文件');
      return;
    }

    const kbForUpload = mountedKbName;
    const fingerprint = documentUploadFingerprint(file);
    const previous = documentUploadRequestRef.current;
    const clientRequestId = previous?.fingerprint === fingerprint
      ? previous.clientRequestId
      : createClientRequestId();
    documentUploadRequestRef.current = { fingerprint, clientRequestId };

    setDocumentUploadPending(true);
    setDocumentUploadProgress(0);
    try {
      const analysis = await analyzeTemplate(kbForUpload, file, file.name, {
        // Current analyze endpoint ignores this optional field; Task 8 can use it
        // for idempotent upload handling without requiring a second client path.
        clientRequestId,
        onProgress: (percent) => setDocumentUploadProgress(percent),
      });
      const context = buildDocumentContext(analysis, kbForUpload, clientRequestId);
      if (!context) throw new Error('分析响应缺少可用的模板引用');
      setDocumentContext(context);
      setDocumentContextLabel(file.name);
      notify.success(`模板已上传并分析：${analysis.analysis_id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传并分析模板失败');
    } finally {
      setDocumentUploadPending(false);
      setDocumentUploadProgress(0);
    }
  }

  async function handleUseAttachmentAsTemplate(attachment: AttachmentView) {
    if (!canUploadDocumentTemplate || documentUploadPending || activeSessionId == null) return;
    const extension = attachment.extension.toLowerCase();
    if (!DOCUMENT_TEMPLATE_EXTENSIONS.includes(extension)) {
      notify.error('仅支持将 .xlsx、.xlsm 或 .docx 附件转换为模板');
      return;
    }
    if (attachment.parse_status !== 'ready' && attachment.parse_status !== 'degraded') {
      notify.error('附件尚未完成解析，暂时不能转换为模板');
      return;
    }

    setDocumentUploadPending(true);
    try {
      const analysis = await analyzeTemplateFromAttachment(
        mountedKbName,
        activeSessionId,
        attachment.attachment_id,
        attachment.filename,
      );
      const context = buildDocumentContext(analysis, mountedKbName, createClientRequestId());
      if (!context) throw new Error('分析响应缺少可用的模板引用');
      setDocumentContext(context);
      setDocumentContextLabel(attachment.filename);
      notify.success(`附件已转换为模板：${analysis.analysis_id}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '附件转换模板失败');
    } finally {
      setDocumentUploadPending(false);
    }
  }

  function clearDocumentContext() {
    setDocumentContext(null);
    setDocumentContextLabel(null);
  }

  // ---- 会话附件(design §15) ----
  const [draftAttachments, setDraftAttachments] = useState<AttachmentView[]>([]);
  const [selectedAttachmentIds, setSelectedAttachmentIds] = useState<string[]>([]);
  const [attachmentUploadPending, setAttachmentUploadPending] = useState(false);
  const [attachmentRetryingId, setAttachmentRetryingId] = useState<string | null>(null);
  const [sourceScope, setSourceScope] = useState<
    'auto' | 'attachment_only' | 'knowledge_base_only' | 'attachment_and_knowledge_base'
  >('auto');
  const attachmentMutationVersionRef = useRef(0);

  // 会话切换/新建后重新加载服务端附件;轮询让解析状态和失败原因最终
  // 反映到当前会话,而不要求用户刷新页面。
  useEffect(() => {
    setDraftAttachments([]);
    setSelectedAttachmentIds([]);
    setAttachmentRetryingId(null);
    setSourceScope('auto');
    if (!chatAttachmentsEnabled || activeSessionId == null) return undefined;

    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const sync = async () => {
      const mutationVersion = attachmentMutationVersionRef.current;
      try {
        const attachments = await listAttachments(activeSessionId);
        if (disposed || mutationVersion !== attachmentMutationVersionRef.current) return;
        setDraftAttachments(attachments);
        setSelectedAttachmentIds((previous) =>
          previous.filter((id) => attachments.some((item) => item.attachment_id === id)),
        );
      } catch {
        // The backend flag is independently configurable; a transient list
        // failure must not interrupt normal chat input.
      } finally {
        if (!disposed) timer = setTimeout(() => void sync(), ATTACHMENT_STATUS_POLL_INTERVAL_MS);
      }
    };
    void sync();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeSessionId, chatAttachmentsEnabled]);

  async function handleAttachmentUpload(file: File) {
    if (attachmentUploadPending) return;
    if (activeSessionId == null) {
      notify.error('请先发送一条消息以创建会话，再添加附件');
      return;
    }
    attachmentMutationVersionRef.current += 1;
    setAttachmentUploadPending(true);
    try {
      const created = await uploadAttachment(activeSessionId, file, {
        clientRequestId: createClientRequestId(),
      });
      setDraftAttachments((previous) =>
        previous.some((item) => item.attachment_id === created.attachment_id)
          ? previous
          : [...previous, created],
      );
      setSelectedAttachmentIds((previous) =>
        previous.includes(created.attachment_id) ? previous : [...previous, created.attachment_id],
      );
      notify.success(`附件已上传：${created.filename}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传附件失败');
    } finally {
      setAttachmentUploadPending(false);
    }
  }

  async function handleRemoveAttachment(attachmentId: string) {
    attachmentMutationVersionRef.current += 1;
    setDraftAttachments((previous) =>
      previous.filter((item) => item.attachment_id !== attachmentId),
    );
    setSelectedAttachmentIds((previous) => previous.filter((id) => id !== attachmentId));
    if (activeSessionId == null) return;
    try {
      await deleteAttachment(activeSessionId, attachmentId);
    } catch {
      // 草稿已移除;服务端删除失败时不阻塞输入,真实状态以下次列表为准。
    }
  }

  async function handleRetryAttachment(attachmentId: string) {
    if (attachmentRetryingId !== null || activeSessionId == null) return;
    attachmentMutationVersionRef.current += 1;
    setAttachmentRetryingId(attachmentId);
    try {
      const updated = await retryAttachment(activeSessionId, attachmentId);
      setDraftAttachments((previous) =>
        previous.map((item) => (item.attachment_id === updated.attachment_id ? updated : item)),
      );
      notify.success(`已重新排队解析：${updated.filename}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重试附件解析失败');
    } finally {
      setAttachmentRetryingId(null);
    }
  }

  async function handleSendWithAttachments() {
    const query = input.trim();
    const selectedAttachments = draftAttachments.filter((item) =>
      selectedAttachmentIds.includes(item.attachment_id),
    );
    const failed = selectedAttachments.find((item) => item.parse_status === 'failed');
    if (failed) {
      notify.error(`附件“${failed.filename}”解析失败，请先重试或移除`);
      return;
    }

    // Natural-language template requests should create a template context
    // before the turn is persisted.  The context override is passed to
    // useKbChat.send in the same tick, avoiding a stale React state read.
    // If a context already exists, it is the explicit template choice and
    // unrelated Office attachments remain evidence.  Otherwise inspect all
    // draft attachments: one eligible candidate can be auto-selected, while
    // multiple candidates must be chosen with the explicit “作为模板” action.
    const templateRoute = resolveTemplateAttachmentRoute(query, draftAttachments, documentContext != null);
    // These checks remain UI hints for affordances and copy.  They must not
    // block the turn: the backend ConversationPlan is authoritative and can
    // route an ambiguous request to clarification, retrieval, or chat.
    const autoTemplate = templateRoute.autoTemplate;
    if (autoTemplate && (!canUploadDocumentTemplate || activeSessionId == null)) {
      notify.error(
        !canUploadDocumentTemplate
          ? '当前知识库没有模板写权限，无法发起文档生成'
          : '请先建立会话后再发起模板生成',
      );
      return;
    }
    if (autoTemplate && canUploadDocumentTemplate && activeSessionId != null) {
      setDocumentUploadPending(true);
      try {
        const analysis = await analyzeTemplateFromAttachment(
          mountedKbName,
          activeSessionId,
          autoTemplate.attachment_id,
          autoTemplate.filename,
        );
        const context = buildDocumentContext(analysis, mountedKbName, createClientRequestId());
        if (!context) throw new Error('分析响应缺少可用的模板引用');
        setDocumentContext(context);
        setDocumentContextLabel(autoTemplate.filename);
        if (analysis.auto_activated === false) {
          notify.error('附件已识别为模板，但映射需要人工确认；请先在文档工作台完成确认');
          return;
        }
        const sourceAttachments = selectedAttachments.filter(
          (item) => item.attachment_id !== autoTemplate.attachment_id,
        );
        const sourceIds = sourceAttachments.map((item) => item.attachment_id);
        const snapshots = sourceAttachments.map((item) => ({
          attachment_id: item.attachment_id,
          filename: item.filename,
          media_type: item.media_type,
          parse_status: item.parse_status,
        }));
        void send({
          attachmentIds: sourceIds,
          sourceScope: sourceIds.length > 0 ? sourceScope : 'auto',
          snapshots,
          documentContextOverride: context,
          documentFlowOverride: true,
        });
        setSelectedAttachmentIds([]);
        setSourceScope('auto');
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '附件自动转换模板失败');
      } finally {
        setDocumentUploadPending(false);
      }
      return;
    }

    const ids = selectedAttachments.map((item) => item.attachment_id);
    if (ids.length === 0) {
      void send();
      setSelectedAttachmentIds([]);
      setSourceScope('auto');
      return;
    }
    const snapshots = selectedAttachments.map((item) => ({
      attachment_id: item.attachment_id,
      filename: item.filename,
      media_type: item.media_type,
      parse_status: item.parse_status,
    }));
    void send({ attachmentIds: ids, sourceScope, snapshots });
    setSelectedAttachmentIds([]);
    setSourceScope('auto');
  }

  // ---- 消息编辑 ----
  const [editTarget, setEditTarget] = useState<MessageView | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);

  function openEditMessage(messageId: number) {
    const target = messages.find((m) => m.id === messageId);
    if (target && target.role === 'user' && !target.redacted) setEditTarget(target);
  }

  async function submitMessageEdit(payload: { content?: string | null; redact?: boolean }) {
    if (!editTarget) return;
    setEditSubmitting(true);
    try {
      const updated = await api.patch<MessageView>(
        `/api/v1/conversations/${editTarget.session_id}/messages/${editTarget.id}`,
        { ...payload, request_id: `chat-message-${Date.now()}` },
      );
      updateMessage(updated);
      setEditTarget(null);
      notify.success('消息已更新');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '消息更新失败');
    } finally {
      setEditSubmitting(false);
    }
  }

  // ---- 确认弹窗(单例 state,替代 window.confirm) ----
  const [extractConfirmOpen, setExtractConfirmOpen] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [userMemoryTarget, setUserMemoryTarget] = useState<number | null>(null);

  async function runProjectExtraction() {
    if (activeSessionId == null || !mountedKbName) return;
    setExtracting(true);
    try {
      await api.post(`/api/v1/conversations/${activeSessionId}/extract-memory`, {
        reason: '对话页面请求重新提炼项目记忆',
        request_id: `chat-memory-${Date.now()}`,
      });
      notify.success('重新提炼请求已提交');
      void refreshMemorySummary();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重新提炼失败');
    } finally {
      setExtracting(false);
      setExtractConfirmOpen(false);
    }
  }

  async function runUserMemoryConsent(messageId: number) {
    if (activeSessionId == null) return;
    try {
      await api.post(`/api/v1/conversations/${activeSessionId}/memory-consents`, {
        message_ids: [messageId],
        reason: '对话页面明确创建个人记忆',
        request_id: `chat-consent-${Date.now()}`,
      });
      notify.success('个人记忆授权已创建，提炼将在后台执行');
      void refreshSessionConsents();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建个人记忆授权失败');
    }
  }

  async function handleToggleAutoExtract(enabled: boolean): Promise<boolean> {
    try {
      await updateAutoExtract(enabled);
      notify.success(enabled ? '已开启自动提炼' : '已关闭自动提炼');
      return true;
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新记忆设置失败');
      return false;
    }
  }

  function handleKbChange(nextKbName: string) {
    if (documentUploadPending) return;
    setMountedKbName(nextKbName);
    clearDocumentContext();
    documentUploadRequestRef.current = null;
    navigate(nextKbName ? `/chat?kb=${encodeURIComponent(nextKbName)}` : '/chat', { replace: true });
  }

  // 403 整页提示(system_admin 或无权限)
  if (forbidden) {
    return (
      <div className="flex h-full min-h-[360px] items-center justify-center bg-[#fcfcfc]">
        <div className="flex max-w-[420px] flex-col items-center gap-[12px] rounded-[16px] border border-[#e3e7f1] bg-white p-[36px] text-center shadow-[0_8px_24px_rgba(17,17,17,0.045)]">
          <AppIcon name="warning" size={36} className="text-[#b45309]" />
          <div className="text-[15px] font-semibold text-[#18181a]">无法访问该知识库</div>
          <div className="text-[13px] leading-[20px] text-[#757f9c]">{forbidden}</div>
        </div>
      </div>
    );
  }

  const sidebarProviderStyle = { '--sidebar-width': '220px', '--sidebar-width-icon': '72px' } as CSSProperties;
  const showEmpty = activeSession == null && sessionsLoaded && !streaming;

  return (
    <div className="flex h-full min-h-0 bg-[#fcfcfc] text-[#18181a]" style={sidebarProviderStyle}>
      <ChatSessionSidebar
        kbName={mountedKbName}
        sessions={sessions}
        sessionsLoaded={sessionsLoaded}
        activeSessionId={activeSessionId}
        streaming={streaming}
        onSelect={selectSession}
        onNew={() => void newConversation()}
        onDelete={(id) => void deleteSession(id)}
      />
      <main className={cn(CHAT_MAIN_CLASS, 'flex-1')}>
        <ChatHeader
          title={activeSession?.title || '新对话'}
          kbName={mountedKbName || '未挂载'}
          userName={auth.user.username}
          onLogout={onLogout}
          onExtractMemory={mountedKbName ? () => setExtractConfirmOpen(true) : undefined}
          extractDisabled={activeSessionId == null || streaming || extracting}
          memorySummary={memorySummary}
          onToggleAutoExtract={handleToggleAutoExtract}
          sessionConsents={sessionConsents}
          onRevokeConsent={revokeSessionConsent}
        />
        <MessageList
          messages={messages}
          messagesLoaded={messagesLoaded}
          evidenceByMessageId={evidenceByMessageId}
          showEmpty={showEmpty}
          userName={auth.user.username}
          kbName={mountedKbName}
          onPickSuggestion={(text) => setInput(text)}
          streaming={streaming}
          streamingText={streamingText}
          traceSteps={traceSteps}
          traceByMessageId={traceByMessageId}
          degradedNotes={degradedNotes}
          onCreateMemory={(messageId) => setUserMemoryTarget(messageId)}
          onEditMessage={openEditMessage}
          onExport={handleExportMessage}
          onDownloadExport={(job) => void handleDownloadExport(job)}
          onPreviewExport={(job) => void handlePreviewExport(job)}
          exportJobsByMessageId={exportJobsByMessageId}
          exportPreviewsByJobId={exportPreviewsByJobId}
          exportFormats={exportFormats}
        />
        {documentCards.length > 0 && (
          <div className="shrink-0 px-[24px] pb-[6px]">
            <div className="mx-auto flex w-full max-w-[820px] flex-col gap-[10px]">
              {documentCards.map((card, index) => (
                <div key={`${documentCardIdentity(card)}-${index}`} className={chatRowClass('assistant')}>
                  <div className={chatBubbleClass('assistant')}>
                    <DocumentStatusCard
                      card={card}
                      refreshing={documentCardRefreshingId === documentCardIdentity(card)}
                      answering={documentCardAnsweringId === documentCardIdentity(card)}
                      onRefreshStatus={(target) => void refreshDocumentCardStatus(target)}
                      onAnswerClarification={(target, answer) => void answerDocumentCard(target, answer)}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        <Composer
          kbName={mountedKbName}
          availableKbs={visibleKbs}
          input={input}
          setInput={setInput}
          streaming={streaming}
          onKbChange={handleKbChange}
          onSend={handleSendWithAttachments}
          onStop={abortStream}
          documentAuthoringEnabled={documentAuthoringEnabled}
          canUploadDocumentTemplate={canUploadDocumentTemplate}
          documentContext={documentContext}
          documentContextLabel={documentContextLabel ?? undefined}
          documentFlowEnabled={documentFlowEnabled}
          onToggleDocumentFlow={setDocumentFlowEnabled}
          documentUploadPending={documentUploadPending}
          documentUploadProgress={documentUploadProgress}
          onUploadTemplate={handleTemplateUpload}
          onClearDocumentContext={clearDocumentContext}
          attachmentsEnabled={chatAttachmentsEnabled}
          draftAttachments={draftAttachments}
          selectedAttachmentIds={selectedAttachmentIds}
          attachmentUploadPending={attachmentUploadPending}
          attachmentRetryingId={attachmentRetryingId}
          sourceScope={sourceScope}
          onSourceScopeChange={setSourceScope}
          onUploadAttachment={handleAttachmentUpload}
          onToggleAttachment={(attachmentId) =>
            setSelectedAttachmentIds((previous) =>
              previous.includes(attachmentId)
                ? previous.filter((id) => id !== attachmentId)
                : [...previous, attachmentId],
            )
          }
          onRetryAttachment={handleRetryAttachment}
          onUseAsTemplate={handleUseAttachmentAsTemplate}
          onRemoveAttachment={handleRemoveAttachment}
        />
      </main>
      <ConfirmDialog
        open={extractConfirmOpen}
        onOpenChange={(next) => {
          if (!next) setExtractConfirmOpen(false);
        }}
        title="重新提炼本项目记忆？"
        description="将基于当前会话历史重新生成项目记忆，结果先进入 Candidate，仍需审核。"
        confirmText="重新提炼"
        destructive={false}
        loading={extracting}
        onConfirm={() => void runProjectExtraction()}
      />
      <ConfirmDialog
        open={userMemoryTarget !== null}
        onOpenChange={(next) => {
          if (!next) setUserMemoryTarget(null);
        }}
        title="创建个人记忆"
        description="记录所选消息为授权来源，提交后会生成可撤销的授权事件。"
        confirmText="创建个人记忆"
        destructive={false}
        onConfirm={() => {
          if (userMemoryTarget == null) return;
          const target = userMemoryTarget;
          setUserMemoryTarget(null);
          void runUserMemoryConsent(target);
        }}
      />
      <EditMessageDialog
        open={editTarget !== null}
        message={editTarget}
        submitting={editSubmitting}
        onClose={() => setEditTarget(null)}
        onSaveEdit={(content, reason) =>
          void submitMessageEdit({ content, ...(reason ? { reason } : {}) })
        }
        onRedact={(reason) => void submitMessageEdit({ redact: true, ...(reason ? { reason } : {}) })}
      />
    </div>
  );
}
