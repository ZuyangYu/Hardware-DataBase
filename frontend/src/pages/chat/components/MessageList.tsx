/**
 * 消息列表(对齐 MessageList:滚动容器 + 自动滚底 + 空态)。
 * 把会话侧栏选中状态、消息加载、流式气泡组合在这里。
 */
import { useEffect, useRef } from 'react';

import type { EvidenceItem, MessageView, QueryTraceStep } from '@/api/types';
import { Skeleton } from '@/components/ui/skeleton';
import { CHAT_MESSAGES_CLASS, CHAT_MESSAGE_STACK_CLASS } from '../chatPageStyles';
import MessageBubble from './MessageBubble';
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
  degradedNotes: Array<{ stage: string; reason: string }>;
  onCreateMemory?: (messageId: number) => void;
  onEditMessage?: (messageId: number) => void;
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
  degradedNotes,
  onCreateMemory,
  onEditMessage,
}: Props) {
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
          <MessageBubble
            key={msg.id}
            msg={msg}
            evidence={
              msg.role === 'assistant'
                ? (evidenceByMessageId[msg.id] ?? msg.citations ?? [])
                : undefined
            }
            onCreateMemory={onCreateMemory}
            onEditMessage={onEditMessage}
            showDiagnostics={showDiagnostics}
            traceSteps={msg.role === 'assistant' && showDiagnostics ? (traceByMessageId[msg.id] ?? []) : []}
          />
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
            degradedNotes={degradedNotes}
          />
        )}
      </div>
    </div>
  );
}
