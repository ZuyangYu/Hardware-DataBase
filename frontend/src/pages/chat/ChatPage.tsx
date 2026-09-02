/**
 * 聊天页(薄壳,对齐 ChatPage 58 行结构)。
 * Shell 内聊天布局:会话侧栏 + 聊天主区(头部 + 消息列表 + 输入区)。
 * 数据层在 useKbChat;渲染在各 chat/components 子组件。
 */
import type { CSSProperties } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import type { AuthSession } from '../../auth';
import { api } from '@/api/client';
import { analyzeTemplate } from '@/api/documentAuthoring';
import type { KbView, MessageView } from '@/api/types';
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

type Props = {
  auth: AuthSession;
  kbName?: string;
  availableKbs?: KbView[];
  onLogout: () => void;
  /** Opt-out override for the Task 9 bridge; omitted means the env flag/default. */
  documentAuthoringEnabled?: boolean;
};

const DOCUMENT_TEMPLATE_EXTENSIONS = ['.xlsx', '.xlsm', '.docx'];

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
    refreshDocumentCardStatus,
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

  const canUploadDocumentTemplate = Boolean(
    documentAuthoringEnabled
      && mountedKbName
      && ['write', 'admin'].includes(
        availableKbs.find((kb) => kb.name === mountedKbName)?.permission ?? '',
      ),
  );

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

  function clearDocumentContext() {
    setDocumentContext(null);
    setDocumentContextLabel(null);
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

  // M17: 与 selectSession 同等门禁——回答生成中不允许切换知识库，
  // 避免旧会话在后端跑完后结果因会话比对被静默丢弃。
  function handleKbChange(nextKbName: string) {
    if (streaming || documentUploadPending) return;
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
        />
        {documentCards.length > 0 && (
          <div className="shrink-0 px-[24px] pb-[6px]">
            <div className="mx-auto flex w-full max-w-[820px] flex-col gap-[10px]">
              {documentCards.map((card, index) => (
                <div key={`${documentCardIdentity(card)}-${index}`} className={chatRowClass('assistant')}>
                  <div className={chatBubbleClass('assistant')}>
                    <DocumentStatusCard
                      card={card}
                      refreshing={Boolean(card.work_order_id) && documentCardRefreshingId === card.work_order_id}
                      onRefreshStatus={(target) => void refreshDocumentCardStatus(target)}
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
          onSend={() => void send()}
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
