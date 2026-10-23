/**
 * 聊天页(薄壳,对齐 ChatPage 58 行结构)。
 * Shell 内聊天布局:聊天主区(头部 + 消息列表 + 输入区) + 右侧会话侧栏。
 * 数据层在 useKbChat;渲染在各 chat/components 子组件。
 */
import type { CSSProperties } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import { api, downloadBlob } from '@/api/client';
import { analyzeTemplateFromAttachment } from '@/api/documentAuthoring';
import {
  deleteAttachment,
  listAttachments,
  retryAttachment,
  uploadAttachment,
} from '@/api/chatAttachments';
import type { AttachmentView, ExportArtifactView, ExportBatchResponse, ExportFormat, ExportFormatsResponse, ExportJobView, KbView, KnowledgePurpose, KnowledgeSelectionInput, McpWriteProposal, MessageView } from '@/api/types';
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
import McpWriteProposalCard from './components/McpWriteProposalCard';
import { planConfirmationStaleKey } from './useKbChat';
import ChatSessionSidebar from './components/ChatSessionSidebar';
import ChatHeader from './components/ChatHeader';
import MessageList from './components/MessageList';
import Composer from './components/Composer';
import ExportTaskCenter from '@/components/ExportTaskCenter';
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
const DOCUMENT_COMPOSE_PATTERN = /(?:生成|创建|撰写|起草|整理成|汇总成|导出|输出)[\s\S]{0,30}(?:文档|报告|说明书|word|docx|pdf|excel|xlsx|pptx)/i;

export function inferKnowledgePurpose(
  query: string,
  selected: KnowledgePurpose | 'auto',
): KnowledgePurpose {
  if (selected !== 'auto') return selected;
  const text = String(query || '').trim();
  if (!text) return 'answer';
  if (DOCUMENT_COMPARISON_PATTERN.test(text)) return 'compare';
  if (isTemplateGenerationChatIntent(text) || DOCUMENT_COMPOSE_PATTERN.test(text)) return 'compose';
  return 'answer';
}

/**
 * Attachments stay mounted for the whole session: opening a session selects
 * every parseable attachment by default (failed parses are excluded so they
 * cannot block sending), and the selection is intentionally NOT cleared after
 * a turn.  Users can still uncheck a chip for an attachment-free question.
 */
export function defaultSelectedAttachmentIds(attachments: AttachmentView[]): string[] {
  return attachments
    .filter((item) => item.parse_status !== 'failed')
    .map((item) => item.attachment_id);
}

/** Keep only ids that are still present in the session, preserving order. */
export function retainExistingAttachmentIds(
  previous: string[],
  attachments: AttachmentView[],
): string[] {
  return previous.filter((id) =>
    attachments.some((item) => item.attachment_id === id),
  );
}

/**
 * Detect the natural-language command that means "use the attached office
 * file as a template and produce a filled document".  The backend remains
 * authoritative; when exactly one unambiguous office attachment is selected,
 * this client hint lets the turn auto-convert it without a separate action.
 */
export function isTemplateGenerationChatIntent(query: string): boolean {
  const text = String(query || '').trim();
  if (!text) return false;
  // Comparison is a retrieval/reporting operation.  It must win over words
  // such as “模板” and “生成” so the UI never converts a comparison into a
  // template-fill request before the server sees the turn.
  if (DOCUMENT_COMPARISON_PATTERN.test(text)) return false;
  // Auto-conversion to a template is an expensive server analysis (structure
  // plus batched LLM suggestions, minutes on large documents).  It only runs
  // when the user explicitly talks about a template or a fill operation;
  // generic “生成/整理成文档” requests go straight to the server, which owns
  // the authoritative route decision.
  const explicitTemplate = /模板|模版|template/i.test(text);
  const fillVerb = /填充|填写|回填|\bfill\b/i.test(text);
  if (!explicitTemplate && !fillVerb) return false;
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
  pendingCandidates: AttachmentView[];
  waitingForTemplate: boolean;
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
  const pendingCandidates = intent && !hasDocumentContext
    ? attachments.filter((attachment) => {
      const extension = String(attachment.extension || '').toLowerCase()
        || `.${String(attachment.filename || '').split('.').pop() || ''}`.toLowerCase();
      return DOCUMENT_TEMPLATE_EXTENSIONS.includes(extension)
        && !['ready', 'degraded', 'failed'].includes(attachment.parse_status);
    })
    : [];
  return {
    intent,
    authority: 'backend',
    candidates,
    autoTemplate: candidates.length === 1 ? candidates[0] : null,
    pendingCandidates,
    waitingForTemplate: candidates.length === 0 && pendingCandidates.length > 0,
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
  const [selectedKnowledgeBaseIds, setSelectedKnowledgeBaseIds] = useState<number[]>([]);
  const [documentContextLabel, setDocumentContextLabel] = useState<string | null>(null);
  const [documentUploadPending, setDocumentUploadPending] = useState(false);
  const [exportJobsByMessageId, setExportJobsByMessageId] = useState<Record<number, ExportJobView[]>>({});
  const [exportPreviewsByJobId, setExportPreviewsByJobId] = useState<Record<string, ExportArtifactView>>({});
  const [exportFormats, setExportFormats] = useState<ExportFormat[] | undefined>(undefined);
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
        owner_user_id: null,
        is_shared: false,
        is_owner: false,
      },
      ...rows,
    ];
  }, [availableKbs, mountedKbName]);

  const anchorKnowledgeBaseId = useMemo(
    () => visibleKbs.find((item) => item.name === mountedKbName)?.kb_id ?? null,
    [visibleKbs, mountedKbName],
  );
  const previousAnchorKnowledgeBaseId = useRef<number | null>(null);
  useEffect(() => {
    const anchorChanged = previousAnchorKnowledgeBaseId.current !== anchorKnowledgeBaseId;
    previousAnchorKnowledgeBaseId.current = anchorKnowledgeBaseId;
    if (anchorKnowledgeBaseId == null) {
      setSelectedKnowledgeBaseIds([]);
      return;
    }
    if (anchorChanged) {
      setSelectedKnowledgeBaseIds([anchorKnowledgeBaseId]);
      return;
    }
    setSelectedKnowledgeBaseIds((previous) => {
      const allowed = new Set(
        visibleKbs.map((item) => item.kb_id).filter((value): value is number => value != null),
      );
      const retained = previous.filter((value) => allowed.has(value));
      return retained.includes(anchorKnowledgeBaseId) ? retained : [anchorKnowledgeBaseId, ...retained];
    });
  }, [anchorKnowledgeBaseId, visibleKbs]);

  const buildKnowledgeScope = useCallback(
    (purpose: KnowledgePurpose, kbIds: number[]): KnowledgeSelectionInput | null => {
      if (anchorKnowledgeBaseId == null || kbIds.length < 2) return null;
      const ids = [...new Set(kbIds)];
      if (purpose === 'compare') {
        return {
          version: 1,
          purpose: 'compare',
          sources: ids.map((kb_id) => ({ kb_id, role: 'peer' as const })),
        };
      }
      return {
        version: 1,
        purpose,
        sources: ids.map((kb_id) => ({
          kb_id,
          role: kb_id === anchorKnowledgeBaseId ? 'primary' as const : 'reference' as const,
        })),
      };
    },
    [anchorKnowledgeBaseId],
  );

  const knowledgeScope = useMemo<KnowledgeSelectionInput | null>(
    () => buildKnowledgeScope('answer', selectedKnowledgeBaseIds),
    [buildKnowledgeScope, selectedKnowledgeBaseIds],
  );

  const resolveKnowledgeScope = useCallback(
    (query: string): KnowledgeSelectionInput | null => {
      const purpose = inferKnowledgePurpose(query, 'auto');
      return buildKnowledgeScope(purpose, selectedKnowledgeBaseIds);
    },
    [buildKnowledgeScope, selectedKnowledgeBaseIds],
  );

  useEffect(() => {
    setMountedKbName(kbName);
    setDocumentContextLabel(null);
  }, [kbName]);

  const wikiAssistKey = `hdb.wikiAssist.${mountedKbName || '__general__'}`;
  const [wikiAvailable, setWikiAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    if (!mountedKbName) {
      setWikiAvailable(null);
      return;
    }
    let cancelled = false;
    api
      .get<{ total_pages: number }>(`/api/v1/kbs/${encodeURIComponent(mountedKbName)}/wiki/stats`)
      .then((stats) => {
        if (!cancelled) setWikiAvailable((stats.total_pages ?? 0) > 0);
      })
      .catch(() => {
        if (!cancelled) setWikiAvailable(null); // 查询失败不阻塞开关
      });
    return () => {
      cancelled = true;
    };
  }, [mountedKbName]);

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

  const [wikiAssist, setWikiAssist] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(wikiAssistKey) === '1';
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      setWikiAssist(window.localStorage.getItem(`hdb.wikiAssist.${mountedKbName || '__general__'}`) === '1');
    } catch {
      setWikiAssist(false);
    }
  }, [mountedKbName]);
  const updateWikiAssist = (value: boolean) => {
    setWikiAssist(value);
    try {
      window.localStorage.setItem(wikiAssistKey, value ? '1' : '0');
    } catch {
      // localStorage 不可用时仅当前会话生效
    }
  };

  const chat = useKbChat(mountedKbName, {
    username: auth.user.username,
    documentContextEnabled: documentAuthoringEnabled,
    knowledgeScope,
    resolveKnowledgeScope,
    knowledgeScopeAuto: true,
  });
  const {
    sessions,
    sessionsLoaded,
    activeSession,
    activeSessionId,
    selectSession,
    newConversation,
    ensureSession,
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
    turnQueueNotice,
    documentCardRefreshingId,
    documentCardReleaseRequestingId,
    requestDocumentCardRelease,
    refreshDocumentCardStatus,
    documentCardStaleKeys,
    send,
    abortStream,
    forbidden,
    documentContext,
  setDocumentContext,
    mcpWriteCards,
    updateMcpWriteCard,
    refreshMcpWriteCards,
  } = chat;

  // Each session keeps its own reference-KB set.  When the user opens a
  // different session, mirror that session's stored scope into the picker so
  // the next turn edits the same session instead of silently changing it.
  const syncedSessionScopeId = useRef<number | null>(null);
  useEffect(() => {
    if (activeSessionId == null) {
      syncedSessionScopeId.current = null;
      return;
    }
    if (syncedSessionScopeId.current === activeSessionId || !activeSession) return;
    syncedSessionScopeId.current = activeSessionId;
    const stored = activeSession.knowledge_scope;
    const storedIds = stored?.sources
      ?.map((source) => Number(source.kb_id))
      .filter((value) => Number.isSafeInteger(value) && value > 0) ?? [];
    if (stored && storedIds.length > 0) {
      setSelectedKnowledgeBaseIds([...new Set(storedIds)]);
    } else {
      setSelectedKnowledgeBaseIds(anchorKnowledgeBaseId != null ? [anchorKnowledgeBaseId] : []);
    }
  }, [activeSession, activeSessionId, anchorKnowledgeBaseId]);

  const handleKnowledgeScopeChange = (kbIds: number[]) => {
    if (streaming) return;
    const unique = [...new Set(kbIds)];
    if (unique.length === 0) {
      setSelectedKnowledgeBaseIds([]);
      if (mountedKbName) handleKbChange('');
      return;
    }
    const anchorId = anchorKnowledgeBaseId != null && unique.includes(anchorKnowledgeBaseId)
      ? anchorKnowledgeBaseId
      : unique[0];
    const anchor = visibleKbs.find((item) => Number(item.kb_id) === anchorId);
    setSelectedKnowledgeBaseIds(unique);
    if (anchor && anchor.name !== mountedKbName) {
      // Keep the scope popover mounted while the user checks several KBs.
      setMountedKbName(anchor.name);
      clearDocumentContext();
      window.history.replaceState(null, '', `/chat?kb=${encodeURIComponent(anchor.name)}`);
    }
  };

  useEffect(() => {
    if (!mcpWriteCards.some((card) => card.status === 'confirmed' || card.status === 'running')) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      void refreshMcpWriteCards().catch(() => undefined);
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [mcpWriteCards, refreshMcpWriteCards]);

  async function confirmMcpWriteCard(card: typeof mcpWriteCards[number]) {
    try {
      const response = await api.post<{ proposal: McpWriteProposal }>(
        `/api/v1/plugins/mcp/write-proposals/${encodeURIComponent(card.proposal_id)}/confirm`,
      );
      updateMcpWriteCard(response.proposal);
      notify.success('已确认，候选产物将由 worker 生成');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '确认写入提案失败');
    }
  }

  async function rejectMcpWriteCard(card: typeof mcpWriteCards[number]) {
    try {
      const response = await api.post<{ proposal: McpWriteProposal }>(
        `/api/v1/plugins/mcp/write-proposals/${encodeURIComponent(card.proposal_id)}/reject`,
      );
      updateMcpWriteCard(response.proposal);
      notify.success('已拒绝写入提案');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '拒绝写入提案失败');
    }
  }

  async function downloadMcpWriteCard(card: typeof mcpWriteCards[number]) {
    try {
      await downloadBlob(
        `/api/v1/plugins/mcp/write-proposals/${encodeURIComponent(card.proposal_id)}/artifact`,
        card.artifact_filename || `${card.proposal_id}.artifact`,
      );
      notify.success('下载已开始');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '下载候选产物失败');
    }
  }

  const documentQuickReplies = useMemo(() => {
    const card = documentCards[0];
    if (!card) return [];
    if (card.kind === 'output_spec_confirmation') return ['确认生成', '修改计划'];
    if (card.status === 'needs_input' || card.status === 'needs_clarification') {
      return card.options ?? [];
    }
    return [];
  }, [documentCards]);

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

  function clearDocumentContext() {
    setDocumentContext(null);
    setDocumentContextLabel(null);
  }

  // ---- 会话附件(design §15) ----
  const [draftAttachments, setDraftAttachments] = useState<AttachmentView[]>([]);
  const [selectedAttachmentIds, setSelectedAttachmentIds] = useState<string[]>([]);
  const [attachmentUploadPending, setAttachmentUploadPending] = useState(false);
  const [attachmentRetryingId, setAttachmentRetryingId] = useState<string | null>(null);
  const attachmentMutationVersionRef = useRef(0);
  const attachmentSessionRef = useRef<number | null>(null);

  // 会话切换/新建后重新加载服务端附件;轮询让解析状态和失败原因最终
  // 反映到当前会话,而不要求用户刷新页面。附件默认在会话内持续挂载：
  // 首次打开会话自动勾选可解析附件，发送后不再清空勾选。
  useEffect(() => {
    const sessionChanged = attachmentSessionRef.current !== activeSessionId;
    setDraftAttachments([]);
    if (sessionChanged) {
      setSelectedAttachmentIds([]);
      attachmentSessionRef.current = null;
    }
    setAttachmentRetryingId(null);
    if (!chatAttachmentsEnabled || activeSessionId == null) return undefined;

    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const sync = async () => {
      const mutationVersion = attachmentMutationVersionRef.current;
      try {
        const attachments = await listAttachments(activeSessionId);
        if (disposed || mutationVersion !== attachmentMutationVersionRef.current) return;
        setDraftAttachments(attachments);
        if (attachmentSessionRef.current !== activeSessionId) {
          attachmentSessionRef.current = activeSessionId;
          setSelectedAttachmentIds(defaultSelectedAttachmentIds(attachments));
        } else {
          setSelectedAttachmentIds((previous) =>
            retainExistingAttachmentIds(previous, attachments),
          );
        }
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
    attachmentMutationVersionRef.current += 1;
    setAttachmentUploadPending(true);
    try {
      // 空会话也允许挂附件:上传前按需建会话,不再要求先发一条消息。
      const sessionId = await ensureSession();
      const created = await uploadAttachment(sessionId, file, {
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
    let currentAttachments = draftAttachments;
    let selectedAttachments = currentAttachments.filter((item) =>
      selectedAttachmentIds.includes(item.attachment_id),
    );
    let failed = selectedAttachments.find((item) => item.parse_status === 'failed');
    if (failed) {
      notify.error(`附件“${failed.filename}”解析失败，请先重试或移除`);
      return;
    }

    // Natural-language template requests should create a template context
    // before the turn is persisted.  The context override is passed to
    // useKbChat.send in the same tick, avoiding a stale React state read.
    // If a context already exists, it is the explicit template choice and
    // unrelated Office attachments remain evidence.  Otherwise inspect all
    // draft attachments: one eligible candidate can be auto-selected; with
    // multiple candidates the backend remains authoritative and can clarify.
    let templateRoute = resolveTemplateAttachmentRoute(query, selectedAttachments, documentContext != null);
    if (templateRoute.waitingForTemplate) {
      if (activeSessionId == null) {
        notify.error('模板附件仍在解析，请稍后再试');
        return;
      }
      try {
        currentAttachments = await listAttachments(activeSessionId);
        setDraftAttachments(currentAttachments);
        selectedAttachments = currentAttachments.filter((item) =>
          selectedAttachmentIds.includes(item.attachment_id),
        );
        failed = selectedAttachments.find((item) => item.parse_status === 'failed');
        if (failed) {
          notify.error(`附件“${failed.filename}”解析失败，请先重试或移除`);
          return;
        }
        templateRoute = resolveTemplateAttachmentRoute(query, selectedAttachments, documentContext != null);
      } catch {
        notify.error('无法刷新模板解析状态，请稍后再试');
        return;
      }
      if (templateRoute.waitingForTemplate) {
        notify.error('模板附件仍在解析，请等待显示“已就绪”后再发送');
        return;
      }
    }
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
          notify.info('模板已识别，填充区域仍待校验；先整理知识库资料，暂不写入模板');
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
          snapshots,
          documentContextOverride: context,
          documentFlowOverride: true,
          wikiAssist,
        });
        // The template is now mounted as document context; keep the other
        // attachments selected so they stay available for later turns.
        setSelectedAttachmentIds((previous) =>
          previous.filter((id) => id !== autoTemplate.attachment_id),
        );
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '附件自动转换模板失败');
      } finally {
        setDocumentUploadPending(false);
      }
      return;
    }

    const ids = selectedAttachments.map((item) => item.attachment_id);
    if (ids.length === 0) {
      void send({ attachmentIds: [], wikiAssist });
      return;
    }
    const snapshots = selectedAttachments.map((item) => ({
      attachment_id: item.attachment_id,
      filename: item.filename,
      media_type: item.media_type,
      parse_status: item.parse_status,
    }));
    void send({ attachmentIds: ids, snapshots, wikiAssist });
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
  const [userMemoryTarget, setUserMemoryTarget] = useState<number[] | null>(null);
  // 个人记忆默认关闭：打开弹窗时读取 opt_in，未开启时在同一弹窗内先征得开启确认。
  const [userMemoryOptIn, setUserMemoryOptIn] = useState<boolean | null>(null);
  const [userMemorySubmitting, setUserMemorySubmitting] = useState(false);
  const [projectMemorySubmitting, setProjectMemorySubmitting] = useState(false);

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

  async function openUserMemoryDialog(messageIds: number[]) {
    setUserMemoryTarget(messageIds);
    setUserMemoryOptIn(null);
    try {
      const settings = await api.get<{ opt_in: boolean }>('/api/v1/memory-settings');
      setUserMemoryOptIn(Boolean(settings.opt_in));
    } catch {
      // 读取失败不阻塞流程：创建授权时后端仍会校验 opt-in。
    }
  }

  async function runUserMemoryConsent(messageIds: number[]) {
    if (activeSessionId == null) return;
    setUserMemorySubmitting(true);
    try {
      if (userMemoryOptIn === false) {
        await api.put('/api/v1/memory-settings', {
          opt_in: true,
          reason: '用户在对话页面创建个人记忆时明确开启',
          request_id: `chat-memory-optin-${Date.now()}`,
        });
        setUserMemoryOptIn(true);
        notify.success('已开启个人记忆提炼');
        void refreshMemorySummary();
      }
      await api.post(`/api/v1/conversations/${activeSessionId}/memory-consents`, {
        message_ids: messageIds,
        reason: '对话页面明确创建个人记忆',
        request_id: `chat-consent-${Date.now()}`,
      });
      notify.success('个人记忆授权已创建，提炼将在后台执行');
      void refreshSessionConsents();
    } catch (error) {
      const message = error instanceof Error ? error.message : '创建个人记忆授权失败';
      notify.error(
        message.includes('opt-in')
          ? '个人记忆尚未开启：请先在「长期记忆」页开启“允许个人记忆提炼”，再回到这里创建。'
          : message,
      );
    } finally {
      setUserMemorySubmitting(false);
      setUserMemoryTarget(null);
    }
  }

  async function runProjectMemoryRequest(messageIds: number[]) {
    if (activeSessionId == null || !mountedKbName || projectMemorySubmitting) return;
    setProjectMemorySubmitting(true);
    try {
      const result = await api.post<{ status: string }>(
        `/api/v1/conversations/${activeSessionId}/extract-memory`,
        {
          message_ids: messageIds,
          reason: '对话页面主动记入项目记忆',
          request_id: `chat-project-memory-${Date.now()}`,
        },
      );
      if (result?.status === 'already_covered') {
        notify.info('该轮已包含在最近一次项目提炼中，无需重复创建');
      } else {
        notify.success('已提交项目记忆提炼，结果进入 Candidate 待审核');
      }
      void refreshMemorySummary();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '提交项目记忆提炼失败');
    } finally {
      setProjectMemorySubmitting(false);
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
    if (streaming || documentUploadPending) return;
    setMountedKbName(nextKbName);
    clearDocumentContext();
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
      <main className={cn(CHAT_MAIN_CLASS, 'flex-1')}>
        <ChatHeader
          title={activeSession?.title || '新对话'}
          kbName={mountedKbName || '未选择知识库'}
          onExtractMemory={mountedKbName ? () => setExtractConfirmOpen(true) : undefined}
          extractDisabled={activeSessionId == null || streaming || extracting}
          memorySummary={memorySummary}
          onToggleAutoExtract={handleToggleAutoExtract}
          sessionConsents={sessionConsents}
          onRevokeConsent={revokeSessionConsent}
          wikiAssist={mountedKbName ? wikiAssist : undefined}
          onWikiAssistChange={mountedKbName ? updateWikiAssist : undefined}
          wikiAvailable={mountedKbName ? wikiAvailable : undefined}
          /* 导出任务中心嵌入页头,避免悬浮按钮遮挡右侧会话侧栏的新建按钮 */
          actions={<ExportTaskCenter variant="embedded" />}
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
          onCreateMemory={(messageIds) => void openUserMemoryDialog(messageIds)}
          onCreateProjectMemory={
            mountedKbName ? (messageIds) => void runProjectMemoryRequest(messageIds) : undefined
          }
          onEditMessage={openEditMessage}
          onExport={handleExportMessage}
          onDownloadExport={(job) => void handleDownloadExport(job)}
          onPreviewExport={(job) => void handlePreviewExport(job)}
          exportJobsByMessageId={exportJobsByMessageId}
          exportPreviewsByJobId={exportPreviewsByJobId}
          exportFormats={exportFormats}
        />
        {turnQueueNotice && (
          <div className="shrink-0 px-[24px] pb-[6px]" data-testid="turn-queue-notice">
            <div className="mx-auto flex w-full max-w-[820px] items-center gap-[8px] rounded-[8px] border border-[#f0e0c0] bg-[#fffaf0] px-[12px] py-[8px] text-[13px] text-[#8a6d3b]">
              <span>
                {turnQueueNotice.reason === 'waiting_for_attachments'
                  ? '等待附件解析中，完成后将自动开始回答'
                  : '已排队，等待同会话前一轮回答完成后自动开始'}
                {turnQueueNotice.waitDeadline
                  ? `（截止 ${new Date(turnQueueNotice.waitDeadline).toLocaleTimeString()}）`
                  : ''}
              </span>
            </div>
          </div>
        )}
        {documentCards.length > 0 && (
          <div className="shrink-0 px-[24px] pb-[6px]">
            <div className="mx-auto flex w-full max-w-[820px] flex-col gap-[10px]">
              {documentCards.map((card, index) => (
                <div key={`${documentCardIdentity(card)}-${index}`} className={chatRowClass('assistant')}>
                  <div className={chatBubbleClass('assistant')}>
                    <DocumentStatusCard
                      card={card}
                      refreshing={documentCardRefreshingId === documentCardIdentity(card)}
                      requestingRelease={documentCardReleaseRequestingId === documentCardIdentity(card)}
                      stale={documentCardStaleKeys.has(planConfirmationStaleKey(card))}
                      onRefreshStatus={(target) => void refreshDocumentCardStatus(target)}
                      onRequestRelease={(target, artifactId) => void requestDocumentCardRelease(target, artifactId)}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        {mcpWriteCards.length > 0 && (
          <div className="shrink-0 px-[24px] pb-[6px]">
            <div className="mx-auto flex w-full max-w-[820px] flex-col gap-[10px]">
              {mcpWriteCards.map((card) => (
                <div key={card.proposal_id} className={chatRowClass('assistant')}>
                  <div className={chatBubbleClass('assistant')}>
                    <McpWriteProposalCard
                      card={card}
                      onConfirm={(target) => void confirmMcpWriteCard(target)}
                      onReject={(target) => void rejectMcpWriteCard(target)}
                      onDownload={(target) => void downloadMcpWriteCard(target)}
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
          selectedKnowledgeBaseIds={selectedKnowledgeBaseIds}
          onKnowledgeScopeChange={handleKnowledgeScopeChange}
          onSend={handleSendWithAttachments}
          onStop={abortStream}
          quickReplies={documentQuickReplies}
          documentAuthoringEnabled={documentAuthoringEnabled}
          documentContext={documentContext}
          documentContextLabel={documentContextLabel ?? undefined}
          documentUploadPending={documentUploadPending}
          onClearDocumentContext={clearDocumentContext}
          attachmentsEnabled={chatAttachmentsEnabled}
          draftAttachments={draftAttachments}
          attachmentUploadPending={attachmentUploadPending}
          attachmentRetryingId={attachmentRetryingId}
          onUploadAttachment={handleAttachmentUpload}
          onRetryAttachment={handleRetryAttachment}
          onRemoveAttachment={handleRemoveAttachment}
        />
      </main>
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
        title={userMemoryOptIn === false ? '开启个人记忆提炼并创建？' : '创建个人记忆'}
        description={
          userMemoryOptIn === false
            ? '个人记忆默认关闭。确认后将先开启个人记忆提炼，并把本轮问答（提问与输出）记录为可撤销的授权来源；可随时在「长期记忆」页关闭。'
            : '记录本轮问答（提问与输出）为授权来源，提交后会生成可撤销的授权事件。'
        }
        confirmText={userMemoryOptIn === false ? '开启并创建' : '创建个人记忆'}
        destructive={false}
        loading={userMemorySubmitting}
        onConfirm={() => {
          if (userMemoryTarget == null) return;
          void runUserMemoryConsent(userMemoryTarget);
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
