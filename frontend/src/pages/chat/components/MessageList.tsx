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
  McpWriteProposalCard,
  MessageView,
  QueryTraceStep,
  TurnSubagent,
} from '@/api/types';
import type { ConversationArtifactView } from '@/api/artifacts';
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
  liveSubagents?: TurnSubagent[];
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
  artifactsByTurnId?: Record<string, ConversationArtifactView[]>;
  /** 本会话实时/已恢复的 MCP 写入提案卡，按 turn_id 挂到 assistant 消息下。 */
  mcpWriteCards?: McpWriteProposalCard[];
  onConfirmMcpWrite?: (proposal: McpWriteProposalCard) => void;
  onRejectMcpWrite?: (proposal: McpWriteProposalCard) => void;
  onDownloadMcpWrite?: (proposal: McpWriteProposalCard) => void;
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
  liveSubagents,
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
  artifactsByTurnId = {},
  mcpWriteCards = [],
  onConfirmMcpWrite,
  onRejectMcpWrite,
  onDownloadMcpWrite,
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

  function mcpCardsFor(msg: MessageView): McpWriteProposalCard[] {
    const persisted = msg.mcp_write_cards ?? [];
    const live = msg.turn_id
      ? mcpWriteCards.filter((card) => card.turn_id === msg.turn_id)
      : [];
    if (persisted.length === 0 && live.length === 0) return [];
    const byId = new Map(persisted.map((card) => [card.proposal_id, card]));
    for (const card of live) {
      byId.set(card.proposal_id, { ...byId.get(card.proposal_id), ...card });
    }
    return [...byId.values()];
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
            artifacts={
              msg.role === 'assistant' && msg.turn_id
                ? (artifactsByTurnId[msg.turn_id] ?? [])
                : []
            }
            showDiagnostics={showDiagnostics}
            traceSteps={msg.role === 'assistant' && showDiagnostics ? (traceByMessageId[msg.id] ?? []) : []}
            mcpWriteCards={msg.role === 'assistant' ? mcpCardsFor(msg) : []}
            onConfirmMcpWrite={onConfirmMcpWrite}
            onRejectMcpWrite={onRejectMcpWrite}
            onDownloadMcpWrite={onDownloadMcpWrite}
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
            subagents={showDiagnostics ? liveSubagents : []}
            degradedNotes={degradedNotes}
          />
        )}
      </div>
    </div>
  );
}
