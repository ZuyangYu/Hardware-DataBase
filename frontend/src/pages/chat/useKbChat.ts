/**
 * useKbChat -- 单 KB 聊天会话的数据层 hook(lean 版,对齐 UseChatSession 的渲染字段)。
 *
 * 封装:会话列表 CRUD、消息加载、turns 执行模型(创建轮次 -> start -> SSE 订阅持久化
 * 事件,服务端自动落库 user/assistant 消息,刷新可凭 Last-Event-ID 重放)、证据累积、
 * 流式中断。不搬 agent/trace/定时任务/附件/点赞业务。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, isForbiddenError, sseGetStream } from '@/api/client';
import type {
  EvidenceItem,
  MemoryConsentListResponse,
  MemoryConsentView,
  MemoryOperationResponse,
  MessageView,
  OkResponse,
  QueryDonePayload,
  QueryTraceStatus,
  QueryTraceStep,
  SessionMemorySummary,
  SessionView,
  TurnStartResponse,
  TurnView,
} from '@/api/types';
import { notify } from '@/components/ui/app-toast';

const GENERAL_CHAT_KB_NAME = '__general__';
const QUERY_TRACE_HIDE_DELAY_MS = 5000;
const STREAMING_RENDER_INTERVAL_MS = 64;

/** crypto.randomUUID 仅在安全上下文(HTTPS/localhost)存在;HTTP+IP 访问时回退到手动拼 UUID。 */
function requestUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function normalizeTraceStatus(status: unknown): QueryTraceStatus {
  return status === 'pending' || status === 'running' || status === 'done' || status === 'error'
    ? status
    : 'running';
}

function createInitialTrace(_isGeneralChat: boolean): QueryTraceStep[] {
  // Route LLM is skipped for KB chat (deterministic, near-instant), so we must
  // NOT inject a misleading "正在判断是否需要检索知识库" step at the start of
  // every conversation — that reads as an auth/judgement preflight. The stepper
  // only appears once the agent emits tool_started / tool_result events.
  return [];
}

// Tool names emitted by the agent loop, mirrored from the backend ToolRuntime.
const TOOL_LABELS: Record<string, string> = {
  document_search: '文档检索',
  circuit_search: '电路检索',
  spreadsheet_row_search: '表格行检索',
  spreadsheet_cell_lookup: '单元格检索',
  list_kb_sources: '读取知识库目录',
  conversation_search: '外部对话检索',
};

function toolLabel(name: string): string {
  return TOOL_LABELS[name] || name;
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

/** Translate a backend tool/trace event into a UI step, or null if irrelevant. */
function traceStepFromToolEvent(etype: string, raw: string): QueryTraceStep | null {
  let parsed: Record<string, unknown>;
  try {
    parsed = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
  if (etype === 'tool_started') {
    const name = String(parsed.tool_name ?? '');
    return { key: name, label: toolLabel(name), status: 'running', detail: String(parsed.query ?? '') };
  }
  if (etype === 'tool_result') {
    const name = String(parsed.tool_name ?? '');
    const status: QueryTraceStatus = parsed.status === 'failed' ? 'error' : 'done';
    const hits = Number(parsed.hit_count ?? 0);
    const latency = Number(parsed.latency_ms ?? NaN);
    const latencyText = Number.isFinite(latency) ? ` · ${latency}ms` : '';
    return { key: name, label: toolLabel(name), status, detail: `${hits} 条命中${latencyText}` };
  }
  if (etype === 'stage') {
    const key = parsed.key ? String(parsed.key) : '';
    if (!key) return null;
    return {
      key,
      label: parsed.label ? String(parsed.label) : key,
      status: normalizeTraceStatus(parsed.status),
      detail: parsed.detail ? String(parsed.detail) : '',
    };
  }
  return null;
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
  const [memorySummary, setMemorySummary] = useState<SessionMemorySummary | null>(null);
  const [sessionConsents, setSessionConsents] = useState<MemoryConsentView[] | null>(null);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const currentTurnRef = useRef<string | null>(null);
  const sendingRef = useRef(false);
  const activeSessionRef = useRef<number | null>(null);
  // opencode 风格轨迹:同一工具的多次调用各自占一行。
  // toolCallSeqRef 生成单调序号;toolActiveKeyRef 记录"工具名 -> 最近一次 running 的 step key",
  // 使 tool_result 能挂回正确的调用条目(流式恢复场景下退化为按工具名合并)。
  const toolCallSeqRef = useRef(0);
  const toolActiveKeyRef = useRef<Map<string, string>>(new Map());

  const stepFromToolEvent = useCallback((etype: string, raw: string): QueryTraceStep | null => {
    const step = traceStepFromToolEvent(etype, raw);
    if (!step) return null;
    if (etype === 'tool_started') {
      toolCallSeqRef.current += 1;
      const key = `${step.key}#${toolCallSeqRef.current}`;
      toolActiveKeyRef.current.set(step.key, key);
      return { ...step, key };
    }
    if (etype === 'tool_result') {
      const activeKey = toolActiveKeyRef.current.get(step.key);
      return { ...step, key: activeKey ?? step.key };
    }
    return step;
  }, []);
  const pendingStreamingTextRef = useRef('');
  const streamingFlushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastStreamingFlushAtRef = useRef(0);
  const traceClearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const traceRef = useRef<QueryTraceStep[]>([]);
  const [traceByMessageId, setTraceByMessageId] = useState<Record<number, QueryTraceStep[]>>({});

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
      toolCallSeqRef.current = 0;
      toolActiveKeyRef.current.clear();
      traceClearTimerRef.current = null;
    }, QUERY_TRACE_HIDE_DELAY_MS);
  }, [clearTraceTimer]);

  const upsertTraceStep = useCallback((next: QueryTraceStep) => {
    if (next.key === 'permission' || next.key === 'generate') return;
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
      toolCallSeqRef.current = 0;
      toolActiveKeyRef.current.clear();
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
          } else if (evt.event === 'narration') {
            const parsed = JSON.parse(evt.data) as { text?: string };
            accumulated = stripNarrationText(accumulated, parsed.text ?? '');
            queueStreamingText(accumulated);
          } else if (evt.event === 'stage' || evt.event === 'tool_started' || evt.event === 'tool_result') {
            const step = stepFromToolEvent(evt.event, evt.data);
            if (step) upsertTraceStep(step);
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
                  memory_context: payload.summary?.memory_context ?? [],
                  created_at: turn.created_at,
                },
              ]);
              // 刷新恢复时同样回填证据面板,否则引用标号 [n] 没有来源可看
              const recoveredEvidence = payload.summary?.evidence ?? [];
              if (recoveredEvidence.length > 0 && turn.assistant_message_id != null) {
                setEvidenceByMessageId((prev) => ({
                  ...prev,
                  [turn.assistant_message_id as number]: recoveredEvidence,
                }));
              }
            }
            finishTrace('done', '已完成输出');
            void refreshMemorySummary();
            if (turn.assistant_message_id != null) {
              setTraceByMessageId((prev) => ({ ...prev, [turn.assistant_message_id as number]: traceRef.current }));
            }
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
  }, [activeSessionId, finishTrace, queueStreamingText, resetStreamingText, upsertTraceStep, refreshMemorySummary]);

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

  /** 用服务端返回的 MessageView 替换本地消息(编辑/脱敏回写)。 */
  const updateMessage = useCallback((updated: MessageView) => {
    setMessages((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
  }, []);

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
    toolCallSeqRef.current = 0;
    toolActiveKeyRef.current.clear();
    setTraceSteps(createInitialTrace(scopeKbName === GENERAL_CHAT_KB_NAME));
    setDegradedNotes([]);

    let sessionId = activeSessionId;
    // 乐观更新:点发送立即上屏,不等建会话/建 turn 的两个往返(否则"思考中"
    // 会先于自己的消息出现,体感像页面卡了一下)。turn 建好后用服务端记录对账替换。
    const optimisticId = -Date.now();
    setMessages((prev) => [
      ...prev,
      {
        id: optimisticId,
        session_id: sessionId ?? -1,
        role: 'user',
        content: query,
        created_at: new Date().toISOString(),
      },
    ]);
    try {
      // 1. 确保会话存在(首次提问自动建会话,标题取问题前 20 字)
      if (sessionId == null) {
        const session = await createSession(query.slice(0, 20));
        sessionId = session.id;
      }

      // 2. 后端原子写入 user/assistant 占位消息并创建幂等 turn。
      const created = await api.post<TurnStartResponse>(`/api/v1/conversations/${sessionId}/turns`, {
        query,
        client_request_id: requestUuid(),
        query_mode: scopeKbName === GENERAL_CHAT_KB_NAME ? 'fast' : 'deep',
      });
      setMessages((prev) => prev.map((m) => (m.id === optimisticId ? created.user_message : m)));

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
        } else if (evt.event === 'narration') {
          try {
            const parsed = JSON.parse(evt.data) as { text?: string };
            accumulated = stripNarrationText(accumulated, parsed.text ?? '');
            queueStreamingText(accumulated);
          } catch {
            // 忽略坏帧
          }
        } else if (evt.event === 'stage' || evt.event === 'tool_started' || evt.event === 'tool_result') {
          try {
            const step = stepFromToolEvent(evt.event, evt.data);
            if (step) upsertTraceStep(step);
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
          setTraceByMessageId((prev) => ({ ...prev, [created.turn.assistant_message_id]: traceRef.current }));
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
          memory_context: payload.summary?.memory_context ?? [],
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
        void refreshMemorySummary();
      } else if (errorMessage && activeSessionRef.current === sessionId) {
        setMessages((prev) => [...prev, localAssistantMessage(sessionId!, `⚠️ ${errorMessage}`)]);
      } else if (!finalPayload) {
        // 流在 done/error 之前中断(网络断开、服务重启):明确提示,不能静默吞掉
        if (activeSessionRef.current === sessionId) {
          setMessages((prev) => [
            ...prev,
            localAssistantMessage(sessionId!, '⚠️ 连接中断，回答可能未完成；刷新页面可尝试恢复。'),
          ]);
        }
        finishTrace('error', '连接中断');
      }
    } catch (error) {
      // 乐观消息还没被服务端记录对账过:撤回,避免发送失败后残留一条"幽灵消息"
      setMessages((prev) => prev.filter((m) => m.id !== optimisticId));
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
  }, [input, streaming, activeSessionId, createSession, scopeKbName, clearTraceTimer, resetStreamingText, queueStreamingText, upsertTraceStep, finishTrace, refreshMemorySummary]);

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
    currentSessionRunning: streaming,
    send,
    abortStream,
    // 权限
    forbidden,
  };
}
