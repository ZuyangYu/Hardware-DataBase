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
import { createClientRequestId } from '@/api/clientRequestId';
import {
  deleteAttachment,
  listAttachments,
  retryAttachment,
  uploadAttachment,
} from '@/api/chatAttachments';
import type { AttachmentView, ExportArtifactView, ExportBatchResponse, ExportFormat, ExportFormatsResponse, ExportJobView, KbView, McpWriteProposal, MessageView } from '@/api/types';
import { cn } from '@/lib/utils';
import AppIcon from '@/components/AppIcon';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { CHAT_MAIN_CLASS, chatBubbleClass, chatRowClass } from './chatPageStyles';
import { useKbChat } from './useKbChat';
import McpWriteProposalCard from './components/McpWriteProposalCard';
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
};

const ATTACHMENT_STATUS_POLL_INTERVAL_MS = 2_000;

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
}: Props) {
  const navigate = useNavigate();
  const [mountedKbName, setMountedKbName] = useState(kbName);
  const [exportJobsByMessageId, setExportJobsByMessageId] = useState<Record<number, ExportJobView[]>>({});
  const [exportPreviewsByJobId, setExportPreviewsByJobId] = useState<Record<string, ExportArtifactView>>({});
  const [exportFormats, setExportFormats] = useState<ExportFormat[] | undefined>(undefined);
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

  useEffect(() => {
    setMountedKbName(kbName);
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
    livePlan,
    liveBudget,
    liveVerification,
    liveSubagents,
    degradedNotes,
    turnQueueNotice,
    send,
    abortStream,
    forbidden,
    mcpWriteCards,
    updateMcpWriteCard,
    refreshMcpWriteCards,
  } = chat;

  useEffect(() => {
    if (!mcpWriteCards.some((card) => card.status === 'confirmed' || card.status === 'running')) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshMcpWriteCards().catch(() => undefined);
    }, 3_000);
    const onVisibilityChange = () => {
      if (!document.hidden) void refreshMcpWriteCards().catch(() => undefined);
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
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

  const chatAttachmentsEnabled = isChatAttachmentsUiEnabled();

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
      if (disposed || document.hidden) return;
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
        if (!disposed && !document.hidden) {
          timer = setTimeout(() => {
            timer = null;
            void sync();
          }, ATTACHMENT_STATUS_POLL_INTERVAL_MS);
        }
      }
    };
    const onVisibilityChange = () => {
      if (!document.hidden && !disposed && timer == null) void sync();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    void sync();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
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
    const selectedAttachments = draftAttachments.filter((item) =>
      selectedAttachmentIds.includes(item.attachment_id),
    );
    const failed = selectedAttachments.find((item) => item.parse_status === 'failed');
    if (failed) {
      notify.error(`附件“${failed.filename}”解析失败，请先重试或移除`);
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

  // ---- 确认弹窗(单例 state) ----
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
        notify.success('已提交项目记忆提炼，结果进入候选状态待审核');
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
    if (streaming) return;
    setMountedKbName(nextKbName);
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
          livePlan={livePlan}
          liveBudget={liveBudget}
          liveVerification={liveVerification}
          liveSubagents={liveSubagents}
          onContinue={() => {
            void send({
              attachmentIds: [],
              textOverride:
                '请基于上一轮已经收集到的证据，继续完成未完成的部分；先说明哪些部分已完成、哪些仍待确认，并保留有效引用。',
            });
          }}
          onNarrowScope={() => {
            setInput('请缩小范围：只完成上一轮任务中最关键的一个小节，并明确列出其余待确认项。');
          }}
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
          onSend={handleSendWithAttachments}
          onStop={abortStream}
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
        description="将基于当前会话历史重新生成项目记忆，结果先进入候选状态，仍需审核。"
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
