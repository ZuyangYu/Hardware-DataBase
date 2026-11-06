/**
 * useKbChat -- 单 KB 聊天会话的数据层 hook(lean 版,对齐 UseChatSession 的渲染字段)。
 *
 * 封装:会话列表 CRUD、消息加载、turns 执行模型(创建轮次 -> start -> SSE 订阅持久化
 * 事件,服务端自动落库 user/assistant 消息,刷新可凭 Last-Event-ID 重放)、证据累积、
 * 流式中断。不搬 agent/trace/定时任务/附件/点赞业务。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, ApiError, isForbiddenError, sseGetStream } from '@/api/client';
import {
  isHiddenChatTraceStep,
  mergeMcpWriteProposal,
  parseAgentEvent,
  traceStepFromToolEvent,
  type AgentEvent,
  type DoneEvent,
} from '@/api/agentEvents';
import type {
  AttachmentSnapshot,
  CreateTurnRequest,
  EvidenceItem,
  MemoryConsentListResponse,
  MemoryConsentView,
  MemoryOperationResponse,
  McpWriteProposal,
  McpWriteProposalCard,
  McpWriteProposalListResponse,
  MessageView,
  AttachmentRevisionCard,
  OkResponse,
  QueryTraceStep,
  SessionMemorySummary,
  SessionView,
  TurnBudget,
  TurnStartResponse,
  TurnSubagent,
  TurnVerification,
  TurnView,
  KnowledgeSelectionInput,
} from '@/api/types';
import { notify } from '@/components/ui/app-toast';
import { createClientRequestId } from '@/api/clientRequestId';
import { shouldCancelServerTurn, type ChatStreamDetachReason } from './chatTurnLifecycle';

const GENERAL_CHAT_KB_NAME = '__general__';
const QUERY_TRACE_HIDE_DELAY_MS = 5000;
const STREAMING_RENDER_INTERVAL_MS = 64;

/** P0-004 queue visibility derived from a pending/waiting turn. */
export interface TurnQueueNotice {
  reason: 'waiting_for_attachments' | 'queued_behind_previous_turn' | string;
  blockedByTurnId: string | null;
  waitDeadline: string | null;
}

/** 排队提示延迟显示：真实排队也可能很短（<1s），等一拍再提示避免闪烁。 */
export const TURN_QUEUE_NOTICE_DELAY_MS = 600;

/**
 * Pure mapping used by the hook and covered by unit tests.
 *
 * 只在**真有阻塞**时提示：
 * - `waiting_for_attachments`：本轮在等附件解析；
 * - pending 且存在前序未完成轮次（`blocked_by_turn_id`）：同会话串行排队。
 *
 * 普通 pending（worker 还没认领，通常几十~几百毫秒）**不提示**，避免误报
 * "等待前一轮"和每次发消息闪一下。
 */
export function turnQueueNoticeFromTurn(turn: TurnView): TurnQueueNotice | null {
  const blockedByTurnId = turn.blocked_by_turn_id ?? null;
  if (turn.status === 'waiting_for_attachments') {
    return {
      reason: turn.queue_reason || 'waiting_for_attachments',
      blockedByTurnId,
      waitDeadline: turn.wait_deadline ?? null,
    };
  }
  if (turn.status !== 'pending' || !blockedByTurnId) {
    return null;
  }
  return {
    reason: turn.queue_reason || 'queued_behind_previous_turn',
    blockedByTurnId,
    waitDeadline: turn.wait_deadline ?? null,
  };
}
function requestUuid(): string {
  return createClientRequestId();
}

/**
 * Build the persistent-turn request. Chat attachments and the knowledge scope
 * are separate optional fields; the user query is never decorated with them.
 */
export function buildTurnRequest(
  query: string,
  clientRequestId: string,
  queryMode: 'fast' | 'deep',
  attachments?: { attachmentIds: string[]; sourceScope?: CreateTurnRequest['source_scope'] } | null,
  knowledgeScope?: KnowledgeSelectionInput | null,
): CreateTurnRequest {
  const request: CreateTurnRequest = {
    query,
    client_request_id: clientRequestId,
    query_mode: queryMode,
  };
  if (attachments && attachments.attachmentIds.length > 0) {
    request.attachment_ids = [...attachments.attachmentIds];
    if (attachments.sourceScope && attachments.sourceScope !== 'auto') {
      request.source_scope = attachments.sourceScope;
    }
  }
  if (knowledgeScope) {
    request.knowledge_scope = {
      version: 1,
      purpose: knowledgeScope.purpose,
      sources: knowledgeScope.sources.map((source) => ({ ...source })),
    };
  }
  return request;
}

function createInitialTrace(_isGeneralChat: boolean): QueryTraceStep[] {
  // Route LLM is skipped for KB chat (deterministic, near-instant), so we must
  // NOT inject a misleading "正在判断是否需要检索知识库" step at the start of
  // every conversation — that reads as an auth/judgement preflight. The stepper
  // only appears once the agent emits tool_started / tool_result events.
  return [];
}

/**
 * 模型"过程话"(工具调用前的临时文本)会先被乐观下发为答案增量;后端随后把它归类为
 * 叙述并发 narration 事件携带原文。这里从累计答案中回收该文本(后缀优先,其次原位剔除),
 * 使答案面板只保留最终回答;done 事件仍携带权威答案兜底。
 */
function stripNarrationText(accumulated: string, text: string): string {
  if (!text) return accumulated;
  if (accumulated.endsWith(text)) {
    return accumulated.slice(0, accumulated.length - text.length);
  }
  const idx = accumulated.indexOf(text);
  if (idx >= 0) {
    return accumulated.slice(0, idx) + accumulated.slice(idx + text.length);
  }
  return accumulated;
}

function mergeTraceDetail(current: string | undefined, next: string | undefined): string | undefined {
  const nextText = next?.trim();
  if (!nextText) return current;
  const currentText = current?.trim();
  if (currentText && nextText.startsWith(currentText)) return nextText;
  const lines = (current || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines[lines.length - 1] === nextText || lines.includes(nextText)) {
    return current || nextText;
  }
  return [...lines, nextText].slice(-6).join('\n');
}

function localAssistantMessage(sessionId: number, content: string): MessageView {
  return {
    id: -Date.now(),
    session_id: sessionId,
    role: 'assistant',
    content,
    created_at: new Date().toISOString(),
  };
}

export type UseKbChat = ReturnType<typeof useKbChat>;

export type UseKbChatOptions = {
  /** Keep tab-local conversation selection isolated across authenticated users. */
  username?: string;
  /** Called only for a newly completed turn, never for recovered history. */
  onTurnCompleted?: (message: MessageView, query: string) => void | Promise<void>;
  /**
   * Reserved for the future project-space mount.  The current ChatPage sends
   * no scope: the raw multi-select entry was removed and chat runs single-KB.
   */
  knowledgeScope?: KnowledgeSelectionInput | null;
  /** Reserved for project-space routing; not wired by the current ChatPage. */
  resolveKnowledgeScope?: (query: string) => KnowledgeSelectionInput | null;
  /** Reserved for project-space routing; not wired by the current ChatPage. */
  knowledgeScopeAuto?: boolean;
};

export function useKbChat(kbName: string, options: UseKbChatOptions = {}) {
  const scopeKbName = kbName || GENERAL_CHAT_KB_NAME;
  const knowledgeScope = options.knowledgeScope ?? null;
  const resolveKnowledgeScope = options.resolveKnowledgeScope;
  const knowledgeScopeAuto = options.knowledgeScopeAuto === true;
  const selectedSessionKey = `hdb_chat_session:${encodeURIComponent(options.username ?? 'anonymous')}:${encodeURIComponent(scopeKbName)}`;
  const loadedSessionKey = useRef<string | null>(null);
  const [sessions, setSessions] = useState<SessionView[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<MessageView[]>([]);
  const [messagesLoaded, setMessagesLoaded] = useState(false);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [traceSteps, setTraceSteps] = useState<QueryTraceStep[]>([]);
  const [degradedNotes, setDegradedNotes] = useState<Array<{ stage: string; reason: string }>>([]);
  const [mcpWriteCards, setMcpWriteCards] = useState<McpWriteProposalCard[]>([]);
  const [evidenceByMessageId, setEvidenceByMessageId] = useState<Record<number, EvidenceItem[]>>({});
  const [memorySummary, setMemorySummary] = useState<SessionMemorySummary | null>(null);
  const [sessionConsents, setSessionConsents] = useState<MemoryConsentView[] | null>(null);
  const [forbidden, setForbidden] = useState<string | null>(null);
  // P0-004: queue visibility for a turn that is pending behind an earlier turn
  // or waiting for attachment parsing (blocker, reason, wait deadline).
  const [turnQueueNotice, setTurnQueueNotice] = useState<TurnQueueNotice | null>(null);
  const queueNoticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const applyTurnQueueNotice = useCallback((turn: TurnView) => {
    const notice = turnQueueNoticeFromTurn(turn);
    if (queueNoticeTimerRef.current !== null) {
      clearTimeout(queueNoticeTimerRef.current);
      queueNoticeTimerRef.current = null;
    }
    if (notice === null) {
      setTurnQueueNotice(null);
      return;
    }
    queueNoticeTimerRef.current = setTimeout(() => {
      queueNoticeTimerRef.current = null;
      setTurnQueueNotice(notice);
    }, TURN_QUEUE_NOTICE_DELAY_MS);
  }, []);
  useEffect(() => () => {
    if (queueNoticeTimerRef.current !== null) clearTimeout(queueNoticeTimerRef.current);
  }, []);
  const abortRef = useRef<AbortController | null>(null);
  const currentTurnRef = useRef<string | null>(null);
  const streamTokenRef = useRef(0);
  const sendingRef = useRef(false);
  const sendingGenerationRef = useRef<number | null>(null);
  // Session created/selected by the in-flight first send.  The message-load
  // effect must not fetch and overwrite the optimistic user message before the
  // create-turn request has persisted it.
  const sendingSessionIdRef = useRef<number | null>(null);
  // Generations stopped by the user.  A stop can land while the create-turn
  // request is still in flight; the response must cancel that persisted turn
  // instead of starting an orphan that blocks the next question in the queue.
  const stoppedGenerationsRef = useRef<Set<number>>(new Set());
  const activeSessionRef = useRef<number | null>(null);
  // A login “新对话” click and the first message send can race two session
  // creations; share one in-flight request so only one durable session and
  // one selection exist.
  const pendingCreateRef = useRef<{ scope: string; promise: Promise<SessionView> } | null>(null);
  const liveAttachmentCardsRef = useRef<AttachmentRevisionCard[]>([]);
  const onTurnCompletedRef = useRef(options.onTurnCompleted);
  onTurnCompletedRef.current = options.onTurnCompleted;

  const applyTurnMetaEvent = useCallback((event: AgentEvent) => {
    if (event.type === 'document_card') {
      const card = event.card;
      const key = (item: AttachmentRevisionCard) =>
        (item.artifacts ?? []).map((artifact) => artifact.artifact_id).join(',') || item.content || '';
      if (!liveAttachmentCardsRef.current.some((item) => key(item) === key(card))) {
        liveAttachmentCardsRef.current = [...liveAttachmentCardsRef.current, card];
      }
      return;
    }
    if (event.type === 'subagent_started' || event.type === 'subagent_completed') {
      const subagent: TurnSubagent = {
        name: event.name,
        description: event.description,
        status: event.status,
        tool_call_id: event.tool_call_id,
        result_chars: event.result_chars,
        duration_ms: event.duration_ms,
      };
      setLiveSubagents((previous) => {
        const identity = subagent.tool_call_id || `${subagent.name ?? 'research'}:${subagent.description ?? ''}`;
        const index = previous.findIndex(
          (item) => (item.tool_call_id || `${item.name ?? 'research'}:${item.description ?? ''}`) === identity,
        );
        if (index < 0) return [...previous, subagent];
        return previous.map((item, itemIndex) => (itemIndex === index ? { ...item, ...subagent } : item));
      });
    }
  }, []);

  const pendingStreamingTextRef = useRef('');
  const streamingFlushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastStreamingFlushAtRef = useRef(0);
  const traceClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const traceRef = useRef<QueryTraceStep[]>([]);
  const [traceByMessageId, setTraceByMessageId] = useState<Record<number, QueryTraceStep[]>>({});
  // 复杂任务元数据：计划/预算/校验/子任务。实时值供流式气泡使用；
  // 完成后随 done.summary 落到 assistant message（刷新从服务端消息投影恢复）。
  const [liveSubagents, setLiveSubagents] = useState<TurnSubagent[]>([]);

  const flushStreamingText = useCallback((force = false) => {
    if (streamingFlushTimerRef.current && !force) return;
    if (streamingFlushTimerRef.current) {
      clearTimeout(streamingFlushTimerRef.current);
      streamingFlushTimerRef.current = null;
    }
    const now = performance.now();
    const elapsed = now - lastStreamingFlushAtRef.current;
    if (!force && elapsed < STREAMING_RENDER_INTERVAL_MS) {
      streamingFlushTimerRef.current = setTimeout(() => {
        streamingFlushTimerRef.current = null;
        lastStreamingFlushAtRef.current = performance.now();
        setStreamingText(pendingStreamingTextRef.current);
      }, STREAMING_RENDER_INTERVAL_MS - elapsed);
      return;
    }
    lastStreamingFlushAtRef.current = now;
    setStreamingText(pendingStreamingTextRef.current);
  }, []);

  const queueStreamingText = useCallback((text: string) => {
    pendingStreamingTextRef.current = text;
    flushStreamingText();
  }, [flushStreamingText]);

  const resetStreamingText = useCallback(() => {
    if (streamingFlushTimerRef.current) {
      clearTimeout(streamingFlushTimerRef.current);
      streamingFlushTimerRef.current = null;
    }
    pendingStreamingTextRef.current = '';
    lastStreamingFlushAtRef.current = 0;
    setStreamingText('');
  }, []);

  const detachClientStream = useCallback((reason: ChatStreamDetachReason) => {
    const turnId = currentTurnRef.current;
    if (turnId && shouldCancelServerTurn(reason)) {
      // Only the explicit Stop action is allowed to mutate durable turn state.
      void api.post(`/api/v1/turns/${turnId}/cancel`).catch(() => undefined);
    }
    streamTokenRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    currentTurnRef.current = null;
    sendingSessionIdRef.current = null;
    if (!shouldCancelServerTurn(reason)) {
      // Let the newly selected session reconnect immediately.  The stale
      // sender owns a separate generation and can no longer clear the new
      // sender's state in its finally block.
      sendingRef.current = false;
      sendingGenerationRef.current = null;
    }
    setStreaming(false);
    resetStreamingText();
  }, [resetStreamingText]);

  // ---- 会话列表 ----
  useEffect(() => {
    let cancelled = false;
    loadedSessionKey.current = null;
    let savedSessionId: number | null = null;
    try {
      const value = Number(window.sessionStorage.getItem(selectedSessionKey));
      savedSessionId = Number.isSafeInteger(value) && value > 0 ? value : null;
    } catch { /* Storage restrictions must not block loading server conversations. */ }
    setSessionsLoaded(false);
    setForbidden(null);
    setActiveSessionId(null);
    setMessages([]);
    setEvidenceByMessageId({});
    api
      .get<SessionView[]>(`/api/v1/conversations?kb_name=${encodeURIComponent(scopeKbName)}`)
      .then((rows) => {
        if (cancelled) return;
        // A session belongs to its primary knowledge base. Reference KB
        // choices are a per-session detail and must not hide other sessions.
        const sessionRows = rows;
        loadedSessionKey.current = selectedSessionKey;
        // A session created while this fetch was in flight is not in ``rows``.
        // Keep the new conversation in the list (and selected) instead of
        // dropping it and falling back to an unrelated session.
        setSessions((prev) => {
          const ids = new Set(sessionRows.map((row) => row.id));
          const localSelection = prev.filter(
            (session) => session.id === activeSessionRef.current && !ids.has(session.id),
          );
          return localSelection.length ? [...localSelection, ...sessionRows] : sessionRows;
        });
        setActiveSessionId((current) => {
          if (current != null) return current;
          const preferred = savedSessionId;
          return sessionRows.some((session) => session.id === preferred) ? preferred : sessionRows[0]?.id ?? null;
        });
      })
      .catch((error) => {
        if (cancelled) return;
        if (isForbiddenError(error)) {
          setForbidden(error instanceof Error ? error.message : '没有该知识库的访问权限');
        } else {
          setForbidden(error instanceof Error ? error.message : '加载会话失败');
        }
      })
      .finally(() => {
        if (!cancelled) setSessionsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [scopeKbName, selectedSessionKey]);

  useEffect(() => {
    if (loadedSessionKey.current !== selectedSessionKey || !sessionsLoaded || (activeSessionId !== null && !sessions.some(
      (session) => session.id === activeSessionId && session.kb_name === scopeKbName,
    ))) return;
    try {
      if (activeSessionId === null) window.sessionStorage.removeItem(selectedSessionKey);
      else window.sessionStorage.setItem(selectedSessionKey, String(activeSessionId));
    } catch { /* Refresh still falls back to the most recent authorized session. */ }
  }, [activeSessionId, selectedSessionKey, sessionsLoaded, sessions, scopeKbName]);

  // ---- 选中会话的消息 ----
  useEffect(() => {
    if (activeSessionId == null) {
      setMessages([]);
      setMessagesLoaded(true);
      return undefined;
    }
    // First send: the session is created by this send and the optimistic user
    // message is already on screen.  Fetching now can race the create-turn
    // request and replace the optimistic message with an empty history.
    if (sendingRef.current && sendingSessionIdRef.current === activeSessionId) {
      setMessagesLoaded(true);
      return undefined;
    }
    let cancelled = false;
    setMessagesLoaded(false);
    api
      .get<MessageView[]>(`/api/v1/conversations/${activeSessionId}/messages`)
      .then((rows) => {
        if (cancelled) return;
        setMessages(rows);
        // 执行轨迹回填：服务端把每个 turn 的 stage/tool 事件折叠成步骤投影到消息上，
        // 刷新/重开会话后轨迹面板不消失（与实时 SSE 使用同一形状）。
        // 合并而非覆盖：并发的活跃 turn 实时轨迹不会被历史投影清掉。
        const restoredTraces: Record<number, QueryTraceStep[]> = {};
        for (const row of rows) {
          const visibleSteps = (row.trace_steps ?? []).filter(
            (step) => !isHiddenChatTraceStep(step),
          );
          if (row.role === 'assistant' && visibleSteps.length) {
            restoredTraces[row.id] = visibleSteps;
          }
        }
        if (Object.keys(restoredTraces).length > 0) {
          setTraceByMessageId((previous) => ({ ...restoredTraces, ...previous }));
        }
      })
      .catch((error) => {
        if (!cancelled) {
          if (isForbiddenError(error)) {
            setForbidden(error instanceof Error ? error.message : '没有该知识库的访问权限');
          } else {
            setMessages((prev) => [
              ...prev,
              localAssistantMessage(activeSessionId, `⚠️ ${error instanceof Error ? error.message : '加载消息失败'}`),
            ]);
          }
        }
      })
      .finally(() => {
        if (!cancelled) setMessagesLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  // ---- 会话记忆摘要(无 session 不拉;切换会话时重新拉取) ----
  useEffect(() => {
    if (activeSessionId == null) {
      setMemorySummary(null);
      return undefined;
    }
    let cancelled = false;
    api
      .get<SessionMemorySummary>(`/api/v1/conversations/${activeSessionId}/memory-summary`)
      .then((summary) => {
        if (!cancelled) setMemorySummary(summary);
      })
      .catch(() => {
        // 摘要获取失败不打断聊天主流程
        if (!cancelled) setMemorySummary(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  const refreshMemorySummary = useCallback(async () => {
    const sessionId = activeSessionId;
    if (sessionId == null) {
      setMemorySummary(null);
      return;
    }
    try {
      const summary = await api.get<SessionMemorySummary>(`/api/v1/conversations/${sessionId}/memory-summary`);
      if (activeSessionRef.current !== sessionId) return;
      setMemorySummary(summary);
    } catch {
      // 摘要刷新失败保持旧值即可
    }
  }, [activeSessionId]);

  const updateAutoExtract = useCallback(
    async (autoExtract: boolean): Promise<SessionMemorySummary> => {
      const sessionId = activeSessionId;
      if (sessionId == null) throw new Error('当前没有选中会话');
      const summary = await api.put<SessionMemorySummary>(
        `/api/v1/conversations/${sessionId}/memory-settings`,
        { auto_extract: autoExtract },
      );
      if (activeSessionRef.current === sessionId) setMemorySummary(summary);
      return summary;
    },
    [activeSessionId],
  );

  // ---- 本会话授权台账(无 session 置 null;切换会话时重新拉取) ----
  useEffect(() => {
    if (activeSessionId == null) {
      setSessionConsents(null);
      return undefined;
    }
    let cancelled = false;
    setSessionConsents(null);
    api
      .get<MemoryConsentListResponse>(`/api/v1/memory-consents?session_id=${activeSessionId}`)
      .then((response) => {
        if (!cancelled) setSessionConsents(response.items ?? []);
      })
      .catch(() => {
        // 台账获取失败不打断聊天主流程
        if (!cancelled) setSessionConsents(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  const refreshSessionConsents = useCallback(async () => {
    const sessionId = activeSessionId;
    if (sessionId == null) {
      setSessionConsents(null);
      return;
    }
    try {
      const response = await api.get<MemoryConsentListResponse>(
        `/api/v1/memory-consents?session_id=${sessionId}`,
      );
      if (activeSessionRef.current !== sessionId) return;
      setSessionConsents(response.items ?? []);
    } catch {
      // 刷新失败保持旧值即可
    }
  }, [activeSessionId]);

  const revokeSessionConsent = useCallback(
    async (consentEventId: string, reason: string): Promise<boolean> => {
      const sessionId = activeSessionId;
      try {
        await api.delete<MemoryOperationResponse>(
          `/api/v1/memory-consents/${encodeURIComponent(consentEventId)}`,
          { reason, request_id: requestUuid() },
        );
        if (sessionId != null && activeSessionRef.current === sessionId) {
          setSessionConsents((prev) =>
            prev?.some((item) => item.consent_event_id === consentEventId)
              ? prev.map((item) =>
                  item.consent_event_id === consentEventId
                    ? { ...item, status: 'revoked' as const, revoked_at: new Date().toISOString() }
                    : item,
                )
              : prev,
          );
        }
        notify.success('个人记忆授权已撤销，相关记忆已下线');
        void refreshMemorySummary();
        return true;
      } catch (error) {
        notify.error(error instanceof Error ? error.message : '撤销个人记忆授权失败');
        return false;
      }
    },
    [activeSessionId, refreshMemorySummary],
  );

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
      currentTurnRef.current = null;
      streamTokenRef.current += 1;
      if (streamingFlushTimerRef.current) clearTimeout(streamingFlushTimerRef.current);
      if (traceClearTimerRef.current) clearTimeout(traceClearTimerRef.current);
    };
  }, []);

  // A KB switch is navigation, not cancellation. Detach the old browser
  // subscription so the next KB can accept a new request immediately.
  useEffect(() => {
    // A scope change invalidates even a request that is still creating its
    // session/turn and therefore has not populated currentTurnRef yet.
    if (currentTurnRef.current) {
      detachClientStream('knowledge_base_switch');
    } else {
      streamTokenRef.current += 1;
    }
  }, [scopeKbName, detachClientStream]);

  const clearTraceTimer = useCallback(() => {
    if (traceClearTimerRef.current) {
      clearTimeout(traceClearTimerRef.current);
      traceClearTimerRef.current = null;
    }
  }, []);

  const scheduleTraceClear = useCallback(() => {
    clearTraceTimer();
    traceClearTimerRef.current = setTimeout(() => {
      setTraceSteps([]);
      traceClearTimerRef.current = null;
    }, QUERY_TRACE_HIDE_DELAY_MS);
  }, [clearTraceTimer]);

  const upsertTraceStep = useCallback((next: QueryTraceStep) => {
    if (isHiddenChatTraceStep(next)) return;
    setTraceSteps((prev) => {
      let updated: QueryTraceStep[];
      if (prev.length === 0) {
        updated = [next];
      } else {
        const index = prev.findIndex((step) => step.key === next.key);
        if (index >= 0) {
          updated = prev.map((step, stepIndex) =>
            stepIndex === index
              ? { ...step, ...next, detail: mergeTraceDetail(step.detail, next.detail) }
              : step,
          );
        } else {
          updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.status === 'running' && last.key !== next.key) {
            updated[updated.length - 1] = { ...last, status: 'done' };
          }
          updated.push(next);
        }
      }
      traceRef.current = updated;
      return updated;
    });
  }, []);

  const finishTrace = useCallback((status: 'done' | 'error', detail?: string) => {
    setTraceSteps((prev) =>
      prev.map((step) => {
        if (status === 'done') return { ...step, status: 'done' };
        return step.status === 'running' ? { ...step, status: 'error', detail: detail || step.detail } : step;
      }),
    );
    scheduleTraceClear();
  }, [scheduleTraceClear]);

  // 切换会话时清空上一会话的 MCP 候选卡,避免跨会话串显。
  useEffect(() => {
    setMcpWriteCards([]);
  }, [activeSessionId]);

  // 刷新/重开会话后从消息投影恢复写入提案卡；实时卡优先覆盖同 id 的旧状态。
  useEffect(() => {
    const persisted = messages
      .filter((message) => message.session_id === activeSessionId)
      .flatMap((message) => message.mcp_write_cards ?? []);
    if (persisted.length === 0) return;
    setMcpWriteCards((previous) => {
      const byId = new Map(persisted.map((card) => [card.proposal_id, card]));
      for (const card of previous) {
        byId.set(card.proposal_id, { ...byId.get(card.proposal_id), ...card });
      }
      return [...byId.values()];
    });
  }, [messages, activeSessionId]);

  // 刷新或重新打开会话时，接回后端仍在执行的持久化 turn。
  useEffect(() => {
    if (activeSessionId == null || currentTurnRef.current || sendingRef.current) return undefined;
    const generation = streamTokenRef.current;
    let disposed = false;
    let controller: AbortController | null = null;
    const ownsStream = (turnId: string, sessionId: number) =>
      !disposed &&
      streamTokenRef.current === generation &&
      currentTurnRef.current === turnId &&
      activeSessionRef.current === sessionId;
    void api.get<TurnView[]>(`/api/v1/conversations/${activeSessionId}/turns?include_terminal=true`).then(async (turns) => {
      const turn = turns[0];
      if (disposed || streamTokenRef.current !== generation || activeSessionRef.current !== activeSessionId) return;
      if (!turn) return;
      applyTurnQueueNotice(turn);
      if (turn.status === 'completed' || turn.status === 'failed' || turn.status === 'cancelled') {
        // 已结束的 turn 不再接流:历史卡片与证据由消息投影恢复,避免重复重放。
        return;
      }
      currentTurnRef.current = turn.id;
      controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);
      resetStreamingText();
      setTraceSteps(createInitialTrace(turn.kb_name === GENERAL_CHAT_KB_NAME));
      setDegradedNotes([]);
      setLiveSubagents([]);
      let accumulated = '';
      try {
        await api.post(`/api/v1/turns/${turn.id}/start`);
        if (!ownsStream(turn.id, turn.session_id)) return;
        // Start from zero on a reload so the already-generated prefix is also restored.
        for await (const evt of sseGetStream(`/api/v1/turns/${turn.id}/events`, controller.signal)) {
          if (!ownsStream(turn.id, turn.session_id)) break;
          const event = parseAgentEvent(evt.event, evt.data);
          if (!event) continue;
          switch (event.type) {
            case 'delta':
              accumulated += event.text;
              queueStreamingText(accumulated);
              break;
            case 'narration':
              accumulated = stripNarrationText(accumulated, event.text);
              queueStreamingText(accumulated);
              break;
            case 'stage':
              upsertTraceStep({
                key: event.key,
                kind: 'stage',
                label: event.label,
                status: event.status,
                detail: event.detail,
              });
              break;
            case 'tool_started':
            case 'tool_result':
              upsertTraceStep(traceStepFromToolEvent(event));
              break;
            case 'degraded':
              setDegradedNotes((prev) => [...prev, { stage: event.stage, reason: event.reason }]);
              break;
            case 'subagent_started':
            case 'subagent_completed':
              applyTurnMetaEvent(event);
              break;
            case 'mcp_write_proposal':
              setMcpWriteCards((prev) => mergeMcpWriteProposal(prev, {
                ...event,
                card: { ...event.card, turn_id: currentTurnRef.current || '' },
              }));
              break;
            case 'done': {
              if (!ownsStream(turn.id, turn.session_id)) break;
              const answer = event.answer || accumulated;
              const summary = event.summary ?? {};
              setMessages((prev) => [
                ...prev.filter((message) => message.id !== turn.assistant_message_id),
                {
                  id: turn.assistant_message_id,
                  session_id: turn.session_id,
                  turn_id: turn.id,
                  role: 'assistant',
                  content: answer,
                  footer: (event.footer ?? '').trim(),
                  memory_context: summary.memory_context ?? [],
                  created_at: turn.created_at,
                  budget: (summary.budget as TurnBudget | undefined) ?? null,
                  verification: (summary.verification as TurnVerification | undefined) ?? null,
                  subagents: (summary.subagents as TurnSubagent[] | undefined) ?? [],
                },
              ]);
              // 刷新恢复时同样回填证据面板,否则引用标号 [n] 没有来源可看
              const recoveredEvidence = summary.evidence ?? [];
              if (recoveredEvidence.length > 0 && turn.assistant_message_id != null) {
                setEvidenceByMessageId((prev) => ({
                  ...prev,
                  [turn.assistant_message_id as number]: recoveredEvidence,
                }));
              }
              finishTrace('done', '已完成输出');
              void refreshMemorySummary();
              if (turn.assistant_message_id != null) {
                setTraceByMessageId((prev) => ({ ...prev, [turn.assistant_message_id as number]: traceRef.current }));
              }
              break;
            }
            case 'error':
              if (!ownsStream(turn.id, turn.session_id)) break;
              finishTrace('error', event.message || '查询失败');
              break;
            default:
              break;
          }
        }
      } catch (error) {
        if (ownsStream(turn.id, turn.session_id) && !(error instanceof DOMException && error.name === 'AbortError')) {
          finishTrace('error', error instanceof Error ? error.message : '恢复生成失败');
        }
      } finally {
        if (streamTokenRef.current === generation && currentTurnRef.current === turn.id) {
          currentTurnRef.current = null;
          abortRef.current = null;
          setStreaming(false);
          resetStreamingText();
          setTurnQueueNotice(null);
        }
      }
    }).catch(() => undefined);
    return () => {
      disposed = true;
      controller?.abort();
    };
  }, [activeSessionId, finishTrace, queueStreamingText, resetStreamingText, upsertTraceStep, refreshMemorySummary, applyTurnQueueNotice]);

  const createSession = useCallback(
    async (
      title: string,
      expectedStreamToken?: number,
      scopeOverride?: KnowledgeSelectionInput | null,
    ): Promise<SessionView> => {
      // Manual calls capture the current stream token too, so a session created
      // by the login “新对话” click cannot steal the selection from a message
      // send that started after it.
      const expected = expectedStreamToken ?? streamTokenRef.current;
      const scope = scopeOverride === undefined ? knowledgeScope : scopeOverride;
      let pending = pendingCreateRef.current;
      const createScopeKey = `${scopeKbName}:${scope ? JSON.stringify(scope) : 'none'}`;
      if (pending !== null && pending.scope !== createScopeKey) pending = null;
      if (pending === null) {
        const entry = {
          scope: createScopeKey,
          promise: api.post<SessionView>('/api/v1/conversations', {
            kb_name: scopeKbName,
            title: title || '新对话',
            ...(scope ? { knowledge_scope: scope } : {}),
          }),
        };
        pending = entry;
        pendingCreateRef.current = entry;
        void entry.promise
          .finally(() => {
            if (pendingCreateRef.current === entry) pendingCreateRef.current = null;
          })
          .catch(() => undefined);
      }
      const session = await pending.promise;
      // A navigation/KB switch (or a newer send) may happen while the POST is
      // in flight. Keep the durable session, but do not let a stale request
      // select it in the newly visible scope.
      if (streamTokenRef.current !== expected) {
        return session;
      }
      if (expectedStreamToken !== undefined) {
        sendingSessionIdRef.current = session.id;
      }
      setSessions((prev) => (prev.some((item) => item.id === session.id) ? prev : [session, ...prev]));
      activeSessionRef.current = session.id;
      setActiveSessionId(session.id);
      if (expectedStreamToken === undefined) {
        // 只有显式“新对话”才清空上一条会话的消息。发送路径的首轮
        // createSession 必须保留刚上屏的乐观消息，供后续对账替换。
        setMessages([]);
      }
      return session;
    },
    [knowledgeScope, scopeKbName],
  );

  const newConversation = useCallback(async () => {
    detachClientStream('route_navigation');
    try {
      await createSession('新对话', undefined, knowledgeScopeAuto ? null : undefined);
    } catch (error) {
      setForbidden(error instanceof Error ? error.message : '创建会话失败');
    }
  }, [createSession, detachClientStream, knowledgeScopeAuto]);

  /** 无活动会话时先建一个空会话;附件上传与发送共用。 */
  const ensureSession = useCallback(async (): Promise<number> => {
    const current = activeSessionRef.current;
    if (current != null) return current;
    const session = await createSession('新对话', undefined, knowledgeScopeAuto ? null : undefined);
    return session.id;
  }, [createSession, knowledgeScopeAuto]);

  const deleteSession = useCallback(
    async (sessionId: number) => {
      if (streaming) return;
      try {
        await api.delete<OkResponse>(`/api/v1/conversations/${sessionId}`);
        setSessions((prev) => prev.filter((s) => s.id !== sessionId));
        setActiveSessionId((current) => (current === sessionId ? null : current));
        if (activeSessionId === sessionId) {
          setMessages([]);
        }
      } catch (error) {
        if (activeSessionId === sessionId) {
          setMessages((prev) => [
            ...prev,
            localAssistantMessage(sessionId, `⚠️ ${error instanceof Error ? error.message : '删除会话失败'}`),
          ]);
        }
      }
    },
    [activeSessionId, streaming],
  );

  const selectSession = useCallback((sessionId: number) => {
    if (sessionId !== activeSessionRef.current) detachClientStream('route_navigation');
    setActiveSessionId(sessionId);
  }, [detachClientStream]);

  /** 用服务端返回的 MessageView 替换本地消息(编辑/脱敏回写)。 */
  const updateMessage = useCallback((updated: MessageView) => {
    setMessages((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
  }, []);

  const abortStream = useCallback(async () => {
    const generation = sendingGenerationRef.current;
    if (generation !== null) stoppedGenerationsRef.current.add(generation);
    detachClientStream('user_stop');
  }, [detachClientStream]);

  const send = useCallback(async (
    attachments?: {
      attachmentIds: string[];
      sourceScope?: CreateTurnRequest['source_scope'];
      snapshots?: AttachmentSnapshot[];
      /** Wiki 辅助开关(本轮): 默认关闭。 */
      wikiAssist?: boolean;
      /** 由“继续完成”等操作直接提供文本，避免依赖同一 tick 的 input 状态。 */
      textOverride?: string;
    } | null,
  ) => {
    const query = (attachments?.textOverride ?? input).trim();
    if (!query || streaming) return;
    const activeSession = sessions.find((session) => session.id === activeSessionId);
    const hasActiveSession = activeSessionId != null && activeSession != null;
    const activeHasScope = Boolean(activeSession?.knowledge_scope || activeSession?.knowledge_selection_hash);
    const requestedScope = resolveKnowledgeScope?.(query) ?? knowledgeScope;
    // Reference KBs can change between turns in the same primary-KB session.
    // Only a session that currently has a scope and is being reduced to a
    // single KB needs a fresh session, because "no scope" is represented by an
    // omitted field on the turn request.
    const mustStartNewSession = !hasActiveSession || (activeHasScope && requestedScope == null);
    const turnKnowledgeScope = mustStartNewSession
      ? requestedScope
      : (requestedScope ?? undefined);
    const generation = streamTokenRef.current + 1;
    streamTokenRef.current = generation;
    sendingGenerationRef.current = generation;
    sendingRef.current = true;
    const ownsGeneration = () => streamTokenRef.current === generation;
    setInput('');
    setStreaming(true);
    resetStreamingText();
    clearTraceTimer();
    setTraceSteps(createInitialTrace(scopeKbName === GENERAL_CHAT_KB_NAME));
    setDegradedNotes([]);
    setLiveSubagents([]);

    let sessionId = mustStartNewSession ? null : activeSessionId;
    // 乐观更新:点发送立即上屏,不等建会话/建 turn 的两个往返(否则"思考中"
    // 会先于自己的消息出现,体感像页面卡了一下)。turn 建好后用服务端记录对账替换。
    const optimisticId = -Date.now();
    const optimisticAttachments = (attachments?.attachmentIds ?? []).map(
      (attachmentId, ordinal) => {
        const known = attachments?.snapshots?.find((item) => item.attachment_id === attachmentId);
        return {
          attachment_id: attachmentId,
          filename: known?.filename ?? '附件',
          media_type: known?.media_type,
          ordinal,
          parse_status: known?.parse_status ?? 'queued',
        };
      },
    );
    setMessages((prev) => [
      ...prev,
      {
        id: optimisticId,
        session_id: sessionId ?? -1,
        role: 'user',
        content: query,
        created_at: new Date().toISOString(),
        attachments: optimisticAttachments,
      },
    ]);
    try {
      // 1. 确保会话存在(首次提问自动建会话,标题取问题前 20 字)
      if (sessionId == null) {
        const session = await createSession(query.slice(0, 20), generation, turnKnowledgeScope);
        sessionId = session.id;
      }
      if (!ownsGeneration()) return;

      // The optimistic message is the only live message in a freshly created
      // session.  Drop the previous session's history now that the new session
      // exists, and attach the real session id before the turn is persisted.
      if (mustStartNewSession) {
        setMessages((prev) => prev
          .filter((message) => message.id === optimisticId)
          .map((message) => ({ ...message, session_id: sessionId as number })));
      }

      // 2. 后端原子写入 user/assistant 占位消息并创建幂等 turn。
      liveAttachmentCardsRef.current = [];
      const turnRequest = buildTurnRequest(
        query,
        requestUuid(),
        scopeKbName === GENERAL_CHAT_KB_NAME ? 'fast' : 'deep',
        attachments ?? null,
        turnKnowledgeScope,
      );
      turnRequest.wiki_assist = attachments?.wikiAssist ?? false;
      const created = await api.post<TurnStartResponse>(
        `/api/v1/conversations/${sessionId}/turns`,
        turnRequest,
      );
      if (!ownsGeneration()) {
        // Stop was clicked while the create-turn request was in flight.  The
        // server has already persisted the turn, so cancel it explicitly;
        // otherwise it starts as an orphan and the next question queues
        // behind it even though the user already pressed Stop.
        if (stoppedGenerationsRef.current.delete(generation)) {
          await api.post(`/api/v1/turns/${created.turn.id}/cancel`).catch(() => undefined);
        }
        return;
      }
      if (ownsGeneration()) {
        setMessages((prev) => prev.map((m) => (m.id === optimisticId ? created.user_message : m)));
      }
      applyTurnQueueNotice(created.turn);

      // 3. 后端任务独立执行; SSE 只订阅持久化事件，刷新可从事件序号重放。
      const controller = new AbortController();
      if (ownsGeneration()) {
        abortRef.current = controller;
        currentTurnRef.current = created.turn.id;
      }
      await api.post(`/api/v1/turns/${created.turn.id}/start`);
      if (!ownsGeneration()) return;
      const ownsStream = () =>
        ownsGeneration() && currentTurnRef.current === created.turn.id && activeSessionRef.current === sessionId;
      let finalPayload: DoneEvent | null = null;
      let errorMessage: string | null = null;
      let accumulated = '';

      for await (const evt of sseGetStream(`/api/v1/turns/${created.turn.id}/events`, controller.signal)) {
        if (!ownsStream()) break;
        const event = parseAgentEvent(evt.event, evt.data);
        if (!event) continue;
        switch (event.type) {
          case 'delta':
            accumulated += event.text;
            queueStreamingText(accumulated);
            break;
          case 'narration':
            accumulated = stripNarrationText(accumulated, event.text);
            queueStreamingText(accumulated);
            break;
          case 'stage':
            upsertTraceStep({
              key: event.key,
              kind: 'stage',
              label: event.label,
              status: event.status,
              detail: event.detail,
            });
            break;
          case 'tool_started':
          case 'tool_result':
            upsertTraceStep(traceStepFromToolEvent(event));
            break;
          case 'degraded':
            setDegradedNotes((prev) => [...prev, { stage: event.stage, reason: event.reason }]);
            break;
          case 'subagent_started':
          case 'subagent_completed':
            applyTurnMetaEvent(event);
            break;
          case 'mcp_write_proposal':
            setMcpWriteCards((prev) => mergeMcpWriteProposal(prev, {
              ...event,
              card: { ...event.card, turn_id: currentTurnRef.current || '' },
            }));
            break;
          case 'done':
            finalPayload = event;
            finishTrace('done', '已完成输出');
            setTraceByMessageId((prev) => ({ ...prev, [created.turn.assistant_message_id]: traceRef.current }));
            break;
          case 'error':
            errorMessage = event.message || '查询失败';
            finishTrace('error', errorMessage);
            break;
          default:
            break;
        }
      }

      // 4. 收尾:done -> 落库 assistant 消息;error/中断 -> 提示
      if (!ownsStream()) return;
      if (finalPayload) {
        const payload: DoneEvent = finalPayload;
        const answer = payload.answer || accumulated;
        const summary = payload.summary ?? {};
        const assistantMessage: MessageView = {
          id: created.turn.assistant_message_id,
          session_id: sessionId,
          turn_id: created.turn.id,
          role: 'assistant',
          content: answer,
          footer: (payload.footer ?? '').trim(),
          memory_context: summary.memory_context ?? [],
          created_at: new Date().toISOString(),
          budget: (summary.budget as TurnBudget | undefined) ?? null,
          verification: (summary.verification as TurnVerification | undefined) ?? null,
          subagents: (summary.subagents as TurnSubagent[] | undefined) ?? [],
          cards: liveAttachmentCardsRef.current.length ? liveAttachmentCardsRef.current : undefined,
        };
        setMessages((prev) => [...prev.filter((message) => message.id !== assistantMessage.id), assistantMessage]);
        const evidence = payload.summary?.evidence ?? [];
        if (evidence.length > 0) {
          setEvidenceByMessageId((prev) => ({ ...prev, [assistantMessage.id]: evidence }));
        }
        const onTurnCompleted = onTurnCompletedRef.current;
        if (onTurnCompleted) {
          // The callback is intentionally detached from the SSE lifecycle:
          // submitting an optional export must not delay or fail the chat turn.
          void Promise.resolve()
            .then(() => onTurnCompleted(assistantMessage, query))
            .catch(() => undefined);
        }
        // 首轮对话后刷新会话列表(updated_at/标题排序变化)
        setSessions((prev) => {
          const target = prev.find((s) => s.id === sessionId);
          if (!target) return prev;
          const updated = {
            ...target,
            title: target.title === '新对话' ? query.slice(0, 20) : target.title,
            ...(created.turn.knowledge_scope
              ? {
                knowledge_scope: created.turn.knowledge_scope,
                knowledge_selection_hash: created.turn.knowledge_selection_hash ?? target.knowledge_selection_hash,
              }
              : {}),
          };
          return [updated, ...prev.filter((s) => s.id !== sessionId)];
        });
        void refreshMemorySummary();
      } else if (errorMessage) {
        setMessages((prev) => [...prev, localAssistantMessage(sessionId!, `⚠️ ${errorMessage}`)]);
      } else if (!finalPayload) {
        // 流在 done/error 之前中断(网络断开、服务重启):明确提示,不能静默吞掉
        setMessages((prev) => [
          ...prev,
          localAssistantMessage(sessionId!, '⚠️ 连接中断，回答可能未完成；刷新页面可尝试恢复。'),
        ]);
        finishTrace('error', '连接中断');
      }
    } catch (error) {
      // 乐观消息还没被服务端记录对账过:撤回,避免发送失败后残留一条"幽灵消息"
      if (ownsGeneration()) {
        setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
        if (error instanceof DOMException && error.name === 'AbortError') {
          finishTrace('error', '已停止生成');
        } else if (error instanceof ApiError && error.status === 409 && error.body.includes('knowledge_scope_changed')) {
          setInput((current) => current || query);
          activeSessionRef.current = null;
          setActiveSessionId(null);
          notify.error('来源范围已变化，草稿已保留；重新发送将新建会话，请重新选择附件。');
        } else {
          finishTrace('error', error instanceof Error ? error.message : '发送失败');
          const failedSessionId = sessionId;
          if (failedSessionId != null) {
            setMessages((prev) => [
              ...prev,
              localAssistantMessage(failedSessionId, `⚠️ ${error instanceof Error ? error.message : '发送失败'}`),
            ]);
          }
        }
      }
    } finally {
      stoppedGenerationsRef.current.delete(generation);
      if (sendingGenerationRef.current === generation) {
        sendingRef.current = false;
        sendingGenerationRef.current = null;
        sendingSessionIdRef.current = null;
      }
      if (ownsGeneration()) {
        abortRef.current = null;
        currentTurnRef.current = null;
        setStreaming(false);
        resetStreamingText();
        setTurnQueueNotice(null);
      }
    }
  }, [input, streaming, sessions, activeSessionId, createSession, scopeKbName, clearTraceTimer, resetStreamingText, queueStreamingText, upsertTraceStep, finishTrace, refreshMemorySummary, applyTurnQueueNotice, knowledgeScope, resolveKnowledgeScope]);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeSessionId) ?? null,
    [sessions, activeSessionId],
  );

  const updateMcpWriteCard = useCallback((proposal: McpWriteProposal) => {
    setMcpWriteCards((previous) => previous.map((card) => (
      card.proposal_id === proposal.proposal_id
        ? {
            ...card,
            status: proposal.status,
            artifact_ref: proposal.artifact_ref,
            artifact_filename: proposal.artifact_filename,
            artifact_sha256: proposal.artifact_sha256,
            error_code: proposal.error_code,
          }
        : card
    )));
  }, []);

  const refreshMcpWriteCards = useCallback(async () => {
    if (mcpWriteCards.length === 0) return;
    const payload = await api.get<McpWriteProposalListResponse>('/api/v1/plugins/mcp/write-proposals');
    const byId = new Map(payload.items.map((proposal) => [proposal.proposal_id, proposal]));
    setMcpWriteCards((previous) => previous.map((card) => {
      const proposal = byId.get(card.proposal_id);
      return proposal
        ? {
            ...card,
            status: proposal.status,
            artifact_ref: proposal.artifact_ref,
            artifact_filename: proposal.artifact_filename,
            artifact_sha256: proposal.artifact_sha256,
            error_code: proposal.error_code,
          }
        : card;
    }));
  }, [mcpWriteCards.length]);

  return {
    kbName,
    // 会话
    sessions,
    sessionsLoaded,
    activeSession,
    activeSessionId,
    selectSession,
    newConversation,
    ensureSession,
    deleteSession,
    updateMessage,
    // 消息
    messages,
    messagesLoaded,
    evidenceByMessageId,
    // 记忆
    memorySummary,
    refreshMemorySummary,
    updateAutoExtract,
    sessionConsents,
    refreshSessionConsents,
    revokeSessionConsent,
    // 输入
    input,
    setInput,
    // 流式
    streaming,
    streamingText,
    traceSteps,
    traceByMessageId,
    degradedNotes,
    liveSubagents,
    mcpWriteCards,
    updateMcpWriteCard,
    refreshMcpWriteCards,
    turnQueueNotice,
    currentSessionRunning: streaming,
    send,
    abortStream,
    // 权限
    forbidden,
  };
}
