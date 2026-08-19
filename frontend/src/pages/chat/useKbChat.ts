/**
 * useKbChat -- 单 KB 聊天会话的数据层 hook(lean 版,对齐 UseChatSession 的渲染字段)。
 *
 * 封装:会话列表 CRUD、消息加载、SSE 流式查询(`/api/v1/query`)、done 后落库 assistant 消息
 * (API 有意不自动持久化会话,前端负责)、证据累积、流式中断。不搬 agent/trace/
 * 定时任务/附件/点赞业务。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, isForbiddenError, sseGetStream } from '@/api/client';
import type {
  EvidenceItem,
  MessageView,
  OkResponse,
  QueryDonePayload,
  QueryTraceStatus,
  QueryTraceStep,
  SessionView,
  TurnStartResponse,
  TurnView,
} from '@/api/types';

const HISTORY_PAIRS = 5; // 与后端保持一致:只带最近 5 轮对话
const GENERAL_CHAT_KB_NAME = '__general__';
const QUERY_TRACE_HIDE_DELAY_MS = 1400;
const STREAMING_RENDER_INTERVAL_MS = 64;

function buildHistory(messages: MessageView[]): string[][] {
  const pairs: string[][] = [];
  let pendingUser: string | null = null;
  for (const msg of messages) {
    if (msg.role === 'user') {
      pendingUser = msg.content;
    } else if (msg.role === 'assistant' && pendingUser != null) {
      pairs.push([pendingUser, msg.content]);
      pendingUser = null;
    }
  }
  return pairs.slice(-HISTORY_PAIRS);
}

function normalizeTraceStatus(status: unknown): QueryTraceStatus {
  return status === 'pending' || status === 'running' || status === 'done' || status === 'error'
    ? status
    : 'running';
}

function createInitialTrace(_isGeneralChat: boolean): QueryTraceStep[] {
  // Route LLM is skipped for KB chat (deterministic, near-instant), so we must
  // NOT inject a misleading "正在判断是否需要检索知识库" step at the start of
  // every conversation — that reads as an auth/judgement preflight. The 3-phase
  // stepper starts on "思考" as soon as the first analyze thought arrives.
  return [];
}

function mergeTraceDetail(current: string | undefined, next: string | undefined): string | undefined {
  const nextText = next?.trim();
  if (!nextText) return current;
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

/**
 * randomUUID() is only exposed by browsers in secure contexts. Direct HTTP
 * access to the development port (for example http://<server>:5175) may not
 * provide it, so keep the idempotency key compatible with that deployment.
 */
function createClientRequestId(): string {
  const webCrypto = globalThis.crypto;
  if (typeof webCrypto?.randomUUID === 'function') {
    return webCrypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof webCrypto?.getRandomValues === 'function') {
    webCrypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export type UseKbChat = ReturnType<typeof useKbChat>;

export function useKbChat(kbName: string) {
  const scopeKbName = kbName || GENERAL_CHAT_KB_NAME;
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
  const [evidenceByMessageId, setEvidenceByMessageId] = useState<Record<number, EvidenceItem[]>>({});
  const [forbidden, setForbidden] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const currentTurnRef = useRef<string | null>(null);
  const sendingRef = useRef(false);
  const activeSessionRef = useRef<number | null>(null);
  const pendingStreamingTextRef = useRef('');
  const streamingFlushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastStreamingFlushAtRef = useRef(0);
  const traceClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  // ---- 会话列表 ----
  useEffect(() => {
    let cancelled = false;
    setSessionsLoaded(false);
    setForbidden(null);
    setActiveSessionId(null);
    setMessages([]);
    setEvidenceByMessageId({});
    api
      .get<SessionView[]>(`/api/v1/conversations?kb_name=${encodeURIComponent(scopeKbName)}`)
      .then((rows) => {
        if (cancelled) return;
        setSessions(rows);
        setActiveSessionId((current) => (rows.some((session) => session.id === current) ? current : rows[0]?.id ?? null));
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
  }, [scopeKbName]);

  // ---- 选中会话的消息 ----
  useEffect(() => {
    if (activeSessionId == null) {
      setMessages([]);
      setMessagesLoaded(true);
      return undefined;
    }
    let cancelled = false;
    setMessagesLoaded(false);
    api
      .get<MessageView[]>(`/api/v1/conversations/${activeSessionId}/messages`)
      .then((rows) => {
        if (!cancelled) setMessages(rows);
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

  // 离开页面时同时取消服务端 turn，不能只断开浏览器 SSE。
  useEffect(() => {
    return () => {
      const turnId = currentTurnRef.current;
      if (turnId) {
        void api.post(`/api/v1/turns/${turnId}/cancel`).catch(() => undefined);
      }
      abortRef.current?.abort();
      if (streamingFlushTimerRef.current) clearTimeout(streamingFlushTimerRef.current);
      if (traceClearTimerRef.current) clearTimeout(traceClearTimerRef.current);
    };
  }, []);

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
    if (next.key === 'permission' || next.key === 'generate') return;
    setTraceSteps((prev) => {
      if (prev.length === 0) return [next];
      const index = prev.findIndex((step) => step.key === next.key);
      if (index >= 0) {
        return prev.map((step, stepIndex) =>
          stepIndex === index
            ? { ...step, ...next, detail: mergeTraceDetail(step.detail, next.detail) }
            : step,
        );
      }
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (last?.status === 'running' && last.key !== next.key) {
        updated[updated.length - 1] = { ...last, status: 'done' };
      }
      updated.push(next);
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

  // 刷新或重新打开会话时，接回后端仍在执行的持久化 turn。
  useEffect(() => {
    if (activeSessionId == null || currentTurnRef.current || sendingRef.current) return undefined;
    let disposed = false;
    let controller: AbortController | null = null;
    void api.get<TurnView[]>(`/api/v1/conversations/${activeSessionId}/turns`).then(async (turns) => {
      const turn = turns[0];
      if (!turn || disposed) return;
      currentTurnRef.current = turn.id;
      controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);
      resetStreamingText();
      setTraceSteps(createInitialTrace(turn.kb_name === GENERAL_CHAT_KB_NAME));
      setDegradedNotes([]);
      let accumulated = '';
      try {
        await api.post(`/api/v1/turns/${turn.id}/start`);
        // Start from zero on a reload so the already-generated prefix is also restored.
        for await (const evt of sseGetStream(`/api/v1/turns/${turn.id}/events`, controller.signal)) {
          if (disposed) break;
          if (evt.event === 'delta') {
            const parsed = JSON.parse(evt.data) as { text?: string };
            accumulated += parsed.text ?? '';
            queueStreamingText(accumulated);
          } else if (evt.event === 'stage') {
            const parsed = JSON.parse(evt.data) as { key?: string; label?: string; status?: unknown; detail?: string };
            if (parsed.key) upsertTraceStep({
              key: parsed.key,
              label: parsed.label || parsed.key,
              status: normalizeTraceStatus(parsed.status),
              detail: parsed.detail || '',
            });
          } else if (evt.event === 'degraded') {
            const parsed = JSON.parse(evt.data) as { stage?: string; reason?: string };
            setDegradedNotes((prev) => [...prev, { stage: parsed.stage ?? '', reason: parsed.reason ?? '' }]);
          } else if (evt.event === 'done') {
            const payload = JSON.parse(evt.data) as QueryDonePayload;
            const answer = payload.answer ?? accumulated;
            if (activeSessionRef.current === turn.session_id) {
              setMessages((prev) => [
                ...prev.filter((message) => message.id !== turn.assistant_message_id),
                {
                  id: turn.assistant_message_id,
                  session_id: turn.session_id,
                  role: 'assistant',
                  content: answer,
                  footer: (payload.footer ?? '').trim(),
                  created_at: turn.created_at,
                },
              ]);
            }
            finishTrace('done', '已完成输出');
          } else if (evt.event === 'error') {
            const parsed = JSON.parse(evt.data) as { message?: string };
            finishTrace('error', parsed.message || '查询失败');
          }
        }
      } catch (error) {
        if (!disposed && !(error instanceof DOMException && error.name === 'AbortError')) {
          finishTrace('error', error instanceof Error ? error.message : '恢复生成失败');
        }
      } finally {
        if (!disposed) {
          currentTurnRef.current = null;
          abortRef.current = null;
          setStreaming(false);
          resetStreamingText();
        }
      }
    }).catch(() => undefined);
    return () => {
      disposed = true;
      controller?.abort();
    };
  }, [activeSessionId, finishTrace, queueStreamingText, resetStreamingText, upsertTraceStep]);

  const createSession = useCallback(
    async (title: string): Promise<SessionView> => {
      const session = await api.post<SessionView>('/api/v1/conversations', {
        kb_name: scopeKbName,
        title: title || '新对话',
      });
      setSessions((prev) => [session, ...prev]);
      setActiveSessionId(session.id);
      setMessages([]);
      return session;
    },
    [scopeKbName],
  );

  const newConversation = useCallback(async () => {
    if (streaming) return;
    try {
      await createSession('新对话');
    } catch (error) {
      setForbidden(error instanceof Error ? error.message : '创建会话失败');
    }
  }, [createSession, streaming]);

  const deleteSession = useCallback(
    async (sessionId: number) => {
      if (streaming) return;
      try {
        await api.delete<OkResponse>(`/api/v1/conversations/${sessionId}`);
        setSessions((prev) => prev.filter((s) => s.id !== sessionId));
        setActiveSessionId((current) => (current === sessionId ? null : current));
        if (activeSessionId === sessionId) setMessages([]);
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
    if (streaming) return;
    setActiveSessionId(sessionId);
  }, [streaming]);

  const abortStream = useCallback(async () => {
    const turnId = currentTurnRef.current;
    if (turnId) {
      try {
        await api.post(`/api/v1/turns/${turnId}/cancel`);
      } catch {
        // SSE abort below still detaches this browser from the stream.
      }
    }
    abortRef.current?.abort();
  }, []);

  const send = useCallback(async () => {
    const query = input.trim();
    if (!query || streaming) return;
    sendingRef.current = true;
    setInput('');
    setStreaming(true);
    resetStreamingText();
    clearTraceTimer();
    setTraceSteps(createInitialTrace(scopeKbName === GENERAL_CHAT_KB_NAME));
    setDegradedNotes([]);

    let sessionId = activeSessionId;
    try {
      // 1. 确保会话存在(首次提问自动建会话,标题取问题前 20 字)
      if (sessionId == null) {
        const session = await createSession(query.slice(0, 20));
        sessionId = session.id;
      }

      // 2. 后端原子写入 user/assistant 占位消息并创建幂等 turn。
      const created = await api.post<TurnStartResponse>(`/api/v1/conversations/${sessionId}/turns`, {
        query,
        client_request_id: createClientRequestId(),
        query_mode: scopeKbName === GENERAL_CHAT_KB_NAME ? 'fast' : 'deep',
      });
      setMessages((prev) => [...prev, created.user_message]);

      // 3. 后端任务独立执行; SSE 只订阅持久化事件，刷新可从事件序号重放。
      const controller = new AbortController();
      abortRef.current = controller;
      currentTurnRef.current = created.turn.id;
      await api.post(`/api/v1/turns/${created.turn.id}/start`);
      let finalPayload: QueryDonePayload | null = null;
      let errorMessage: string | null = null;
      let accumulated = '';

      for await (const evt of sseGetStream(`/api/v1/turns/${created.turn.id}/events`, controller.signal)) {
        if (evt.event === 'delta') {
          try {
            const parsed = JSON.parse(evt.data) as { text?: string };
            accumulated += parsed.text ?? '';
            queueStreamingText(accumulated);
          } catch {
            // 忽略坏帧
          }
        } else if (evt.event === 'stage') {
          try {
            const parsed = JSON.parse(evt.data) as { key?: string; label?: string; status?: unknown; detail?: string };
            if (parsed.key) {
              upsertTraceStep({
                key: parsed.key,
                label: parsed.label || parsed.key,
                status: normalizeTraceStatus(parsed.status),
                detail: parsed.detail || '',
              });
            }
          } catch {
            // 忽略坏帧
          }
        } else if (evt.event === 'degraded') {
          try {
            const parsed = JSON.parse(evt.data) as { stage?: string; reason?: string };
            setDegradedNotes((prev) => [...prev, { stage: parsed.stage ?? '', reason: parsed.reason ?? '' }]);
          } catch {
            // 忽略坏帧
          }
        } else if (evt.event === 'done') {
          finalPayload = JSON.parse(evt.data) as QueryDonePayload;
          finishTrace('done', '已完成输出');
        } else if (evt.event === 'error') {
          try {
            errorMessage = (JSON.parse(evt.data) as { message?: string }).message ?? '查询失败';
          } catch {
            errorMessage = '查询失败';
          }
          finishTrace('error', errorMessage);
        }
      }

      // 4. 收尾:done -> 落库 assistant 消息;error/中断 -> 提示
      if (finalPayload) {
        const payload: QueryDonePayload = finalPayload;
        const answer = payload.answer ?? accumulated;
        const assistantMessage: MessageView = {
          id: created.turn.assistant_message_id,
          session_id: sessionId,
          role: 'assistant',
          content: answer,
          footer: (payload.footer ?? '').trim(),
          created_at: new Date().toISOString(),
        };
        if (activeSessionRef.current === sessionId) {
          setMessages((prev) => [...prev.filter((message) => message.id !== assistantMessage.id), assistantMessage]);
        }
        const evidence = payload.summary?.evidence ?? [];
        if (evidence.length > 0) {
          setEvidenceByMessageId((prev) => ({ ...prev, [assistantMessage.id]: evidence }));
        }
        // 首轮对话后刷新会话列表(updated_at/标题排序变化)
        setSessions((prev) => {
          const target = prev.find((s) => s.id === sessionId);
          if (!target) return prev;
          const updated = {
            ...target,
            title: target.title === '新对话' ? query.slice(0, 20) : target.title,
          };
          return [updated, ...prev.filter((s) => s.id !== sessionId)];
        });
      } else if (errorMessage && activeSessionRef.current === sessionId) {
        setMessages((prev) => [...prev, localAssistantMessage(sessionId!, `⚠️ ${errorMessage}`)]);
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        finishTrace('error', '已停止生成');
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
    } finally {
      sendingRef.current = false;
      abortRef.current = null;
      currentTurnRef.current = null;
      setStreaming(false);
      resetStreamingText();
    }
  }, [input, streaming, activeSessionId, createSession, scopeKbName, clearTraceTimer, resetStreamingText, queueStreamingText, upsertTraceStep, finishTrace]);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeSessionId) ?? null,
    [sessions, activeSessionId],
  );

  return {
    kbName,
    // 会话
    sessions,
    sessionsLoaded,
    activeSession,
    activeSessionId,
    selectSession,
    newConversation,
    deleteSession,
    // 消息
    messages,
    messagesLoaded,
    evidenceByMessageId,
    // 输入
    input,
    setInput,
    // 流式
    streaming,
    streamingText,
    traceSteps,
    degradedNotes,
    currentSessionRunning: streaming,
    send,
    abortStream,
    // 权限
    forbidden,
  };
}
