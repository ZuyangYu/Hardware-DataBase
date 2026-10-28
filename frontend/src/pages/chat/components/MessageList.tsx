/**
 * 消息列表(对齐 MessageList:滚动容器 + 自动滚底 + 空态)。
 * 把会话侧栏选中状态、消息加载、流式气泡组合在这里。
 */
import { Fragment, useEffect, useMemo, useRef } from 'react';

import type {
  EvidenceItem,
  ExportArtifactView,
  ExportFormat,
  ExportJobView,
  MessageView,
  QueryTraceStep,
  TurnBudget,
  TurnPlanState,
  TurnSubagent,
  TurnVerification,
} from '@/api/types';
import { Skeleton } from '@/components/ui/skeleton';
import { CHAT_MESSAGES_CLASS, CHAT_MESSAGE_STACK_CLASS } from '../chatPageStyles';
import MessageBubble from './MessageBubble';
import AttachmentRevisionCardView from './AttachmentRevisionCard';
import ChatEmptyState from './ChatEmptyState';

type Props = {
  messages: MessageView[];
  messagesLoaded: boolean;
  evidenceByMessageId: Record<number, EvidenceItem[]>;
  /** 是否处于空态(无选中会话且非流式)。 */
  showEmpty: boolean;
  userName: string;
  kbName: string;
  onPickSuggestion: (text: string) => void;
  streaming: boolean;
  streamingText: string;
  traceSteps: QueryTraceStep[];
  traceByMessageId: Record<number, QueryTraceStep[]>;
  livePlan?: TurnPlanState | null;
  liveBudget?: TurnBudget | null;
  liveVerification?: TurnVerification | null;
  liveSubagents?: TurnSubagent[];
  onContinue?: () => void;
  onNarrowScope?: () => void;
  degradedNotes: Array<{ stage: string; reason: string }>;
  onCreateMemory?: (messageIds: number[]) => void;
  onCreateProjectMemory?: (messageIds: number[]) => void;
  onEditMessage?: (messageId: number) => void;
  onExport?: (message: MessageView, format: ExportFormat) => void;
  onDownloadExport?: (job: ExportJobView) => void;
  onPreviewExport?: (job: ExportJobView) => void;
  exportJobsByMessageId?: Record<number, ExportJobView[]>;
  exportPreviewsByJobId?: Record<string, ExportArtifactView>;
  exportFormats?: ExportFormat[];
};

export default function MessageList({
  messages,
  messagesLoaded,
  evidenceByMessageId,
  showEmpty,
  userName,
  kbName,
  onPickSuggestion,
  streaming,
  streamingText,
  traceSteps,
  traceByMessageId,
  livePlan,
  liveBudget,
  liveVerification,
  liveSubagents,
  onContinue,
  onNarrowScope,
  degradedNotes,
  onCreateMemory,
  onCreateProjectMemory,
  onEditMessage,
  onExport,
  onDownloadExport,
  onPreviewExport,
  exportJobsByMessageId = {},
  exportPreviewsByJobId = {},
  exportFormats,
}: Props) {
  // 个人记忆的授权来源是本轮问答(提问 + 输出),缺失输出时不提供入口。
  const turnMessageIds = useMemo(() => {
    const map = new Map<string, { user?: number; assistant?: number }>();
    for (const msg of messages) {
      if (!msg.turn_id || msg.id <= 0) continue;
      const entry = map.get(msg.turn_id) ?? {};
      if (msg.role === 'user' && !msg.redacted) entry.user = msg.id;
      if (msg.role === 'assistant' && msg.content.trim()) entry.assistant = msg.id;
      map.set(msg.turn_id, entry);
    }
    return map;
  }, [messages]);

  function memorySourceFor(msg: MessageView): number[] | undefined {
    if (!msg.turn_id) return undefined;
    const entry = turnMessageIds.get(msg.turn_id);
    if (!entry?.assistant) return undefined;
    return [entry.user, entry.assistant].filter((value): value is number => typeof value === 'number');
  }

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const showDiagnostics = Boolean(kbName && kbName !== '__general__');

  // 只在用户仍停留在底部附近时跟随输出，避免每个 token 触发平滑动画。
  useEffect(() => {
    const el = scrollRef.current;
    if (el && shouldAutoScrollRef.current) el.scrollTop = el.scrollHeight;
    }, [messages.length, streamingText, streaming, traceSteps, degradedNotes]);

  if (showEmpty) {
    return (
      <div ref={scrollRef} className={CHAT_MESSAGES_CLASS}>
        <ChatEmptyState userName={userName} kbName={kbName} onPickSuggestion={onPickSuggestion} />
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      className={CHAT_MESSAGES_CLASS}
      onScroll={(event) => {
        const el = event.currentTarget;
        shouldAutoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
    >
      <div className={CHAT_MESSAGE_STACK_CLASS}>
        {!messagesLoaded && (
          <div className="grid gap-[10px]">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-[64px] rounded-[12px]" />
            ))}
          </div>
        )}
        {messages.filter((msg) => msg.role !== 'assistant' || msg.content.trim()).map((msg) => (
          <Fragment key={msg.id}>
          <MessageBubble
            msg={msg}
            evidence={
              msg.role === 'assistant'
                ? (evidenceByMessageId[msg.id] ?? msg.citations ?? [])
                : undefined
            }
            onCreateMemory={onCreateMemory}
            onCreateProjectMemory={onCreateProjectMemory}
            memorySourceMessageIds={memorySourceFor(msg)}
            onEditMessage={onEditMessage}
            onExport={onExport}
            onDownload={onDownloadExport}
            onPreview={onPreviewExport}
            exportJobs={msg.role === 'assistant' ? (exportJobsByMessageId[msg.id] ?? []) : []}
            exportPreviews={exportPreviewsByJobId}
            exportFormats={exportFormats}
            showDiagnostics={showDiagnostics}
            traceSteps={msg.role === 'assistant' && showDiagnostics ? (traceByMessageId[msg.id] ?? []) : []}
            onContinue={msg.role === 'assistant' ? onContinue : undefined}
            onNarrowScope={msg.role === 'assistant' ? onNarrowScope : undefined}
          />
          {(msg.cards ?? [])
            .filter((card) => card.kind === 'attachment_revision')
            .map((card, index) => (
              <AttachmentRevisionCardView key={`${msg.id}-card-${index}`} card={card} />
            ))}
          </Fragment>
        ))}
        {streaming && (
          <MessageBubble
            msg={
              {
                id: -1,
                session_id: -1,
                role: 'assistant',
                content: '',
                created_at: '',
            } as MessageView
            }
            streaming
            streamingText={streamingText}
            showDiagnostics={showDiagnostics}
            traceSteps={showDiagnostics ? traceSteps : []}
            plan={showDiagnostics ? livePlan : null}
            budget={showDiagnostics ? liveBudget : null}
            verification={showDiagnostics ? liveVerification : null}
            subagents={showDiagnostics ? liveSubagents : []}
            onContinue={onContinue}
            onNarrowScope={onNarrowScope}
            degradedNotes={degradedNotes}
          />
        )}
      </div>
    </div>
  );
}
