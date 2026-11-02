/**
 * Typed agent-turn event protocol.
 *
 * The backend persists and streams `event: <type>` + `data: <payload>`.
 * `payload.schema_version`, `payload.item_id` and `payload.call_id` are the
 * stable correlation envelope; tool events always carry `call_id` so parallel
 * calls and retries never need name-based fallbacks.
 */
import type {
  AttachmentRevisionCard,
  McpWriteProposalCard,
  QueryDonePayload,
  QueryTraceStatus,
  QueryTraceStep,
  TurnBudget,
  TurnPlanState,
  TurnSubagent,
  TurnVerification,
} from './types';

export interface AgentEventBase {
  schema_version: number;
  item_id: string;
}

export interface DeltaEvent extends AgentEventBase {
  type: 'delta';
  text: string;
}

export interface NarrationEvent extends AgentEventBase {
  type: 'narration';
  text: string;
}

export interface StageEvent extends AgentEventBase {
  type: 'stage';
  key: string;
  label: string;
  status: QueryTraceStatus;
  detail: string;
}

export interface DegradedEvent extends AgentEventBase {
  type: 'degraded';
  stage: string;
  reason: string;
}

export type ToolKind = 'retrieval' | 'mcp' | 'sandbox';

export interface ToolStartedEvent extends AgentEventBase {
  type: 'tool_started';
  call_id: string;
  tool_name: string;
  tool_kind: ToolKind;
  label: string;
  detail: string;
  query: string;
  server: string;
  tool: string;
  skill_ref: string;
  task_id: string;
  automatic: boolean;
}

export interface ToolCompletedEvent extends AgentEventBase {
  type: 'tool_result';
  call_id: string;
  tool_name: string;
  tool_kind: ToolKind;
  label: string;
  detail: string;
  status: string;
  hit_count: number;
  evidence_count: number;
  candidate_count: number;
  sources: string[];
  citation_numbers: number[];
  empty_reason: string;
  latency_ms?: number;
  result_bytes: number;
  truncated: boolean;
  error: string;
  server: string;
  tool: string;
  skill_ref: string;
  task_id: string;
  automatic: boolean;
}

export interface PlanEvent extends AgentEventBase {
  type: 'plan_created' | 'plan_updated';
  items: TurnPlanState['items'];
  revision: number;
  source?: string;
}

export type BudgetEvent = AgentEventBase & {
  type: 'budget' | 'budget_exhausted';
} & TurnBudget;

export type VerificationEvent = AgentEventBase & {
  type: 'verification';
} & TurnVerification;

export interface PartialResultEvent extends AgentEventBase {
  type: 'partial_result';
  reason: string;
  synthesized: boolean;
  fallback?: string;
  evidence_count: number;
  answer_chars: number;
}

export interface PartialSynthesisFailedEvent extends AgentEventBase {
  type: 'partial_synthesis_failed';
  reason: string;
  error: string;
  evidence_count: number;
}

export interface EvidenceCompressedEvent extends AgentEventBase {
  type: 'evidence_compressed';
  stage: string;
  evidence_count: number;
  compressed_bytes: number;
  max_bytes: number;
  note?: string;
}

export interface ContextPressureEvent extends AgentEventBase {
  type: 'context_pressure';
  max_prompt_tokens: number;
  window_tokens: number;
  threshold_tokens: number;
  note?: string;
}

export interface ContextCompactedEvent extends AgentEventBase {
  type: 'context_compacted';
  stage: string;
  trigger: string;
  cutoff_index: number;
  dropped_messages: number;
  retained_messages: number;
  before_tokens: number;
  after_tokens: number;
  window_tokens: number;
  summary_chars: number;
  summary_sha256: string;
  offload_path: string;
  window_id: string;
  note?: string;
}

export interface SubagentEvent extends AgentEventBase {
  type: 'subagent_started' | 'subagent_completed';
  name: string;
  description: string;
  status: TurnSubagent['status'];
  tool_call_id: string;
  result_chars: number;
  duration_ms: number;
}

export interface DocumentCardEvent extends AgentEventBase {
  type: 'document_card';
  card: AttachmentRevisionCard;
}

export interface AttachmentEvent extends AgentEventBase {
  type: 'attachment_revision' | 'attachment_release';
  [key: string]: unknown;
}

export interface McpWriteProposalEvent extends AgentEventBase {
  type: 'mcp_write_proposal';
  card: McpWriteProposalCard;
}

export interface TurnStartEvent extends AgentEventBase {
  type: 'turn_start';
  query: string;
  query_mode: string;
}

export interface TurnEndEvent extends AgentEventBase {
  type: 'turn_end';
  status: string;
}

export interface ErrorEvent extends AgentEventBase {
  type: 'error';
  message: string;
  cancelled: boolean;
}

export type DoneEvent = AgentEventBase &
  {
    type: 'done';
    turn?: Record<string, unknown>;
  } & QueryDonePayload;

export type AgentEvent =
  | DeltaEvent
  | NarrationEvent
  | StageEvent
  | DegradedEvent
  | ToolStartedEvent
  | ToolCompletedEvent
  | PlanEvent
  | BudgetEvent
  | VerificationEvent
  | PartialResultEvent
  | PartialSynthesisFailedEvent
  | EvidenceCompressedEvent
  | ContextPressureEvent
  | ContextCompactedEvent
  | SubagentEvent
  | DocumentCardEvent
  | AttachmentEvent
  | McpWriteProposalEvent
  | TurnStartEvent
  | TurnEndEvent
  | ErrorEvent
  | DoneEvent;

type JsonRecord = Record<string, unknown>;

export function parseJsonRecord(raw: string): JsonRecord | null {
  try {
    const parsed = JSON.parse(raw) as unknown;
    return asRecord(parsed);
  } catch {
    return null;
  }
}

function asRecord(value: unknown): JsonRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function integer(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : fallback;
}

function bool(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function base(record: JsonRecord): AgentEventBase {
  return {
    schema_version: integer(record.schema_version, 0),
    item_id: text(record.item_id),
  };
}

function normalizeTraceStatus(value: unknown): QueryTraceStatus {
  return value === 'pending' || value === 'running' || value === 'done' || value === 'error'
    ? value
    : 'running';
}

function normalizeToolKind(value: unknown): ToolKind {
  return value === 'mcp' || value === 'sandbox' ? value : 'retrieval';
}

function parsePlanItems(value: unknown): TurnPlanState['items'] {
  const rawItems = Array.isArray(value) ? value : [];
  return rawItems
    .filter((item): item is JsonRecord => Boolean(item) && typeof item === 'object')
    .map((item) => {
      const status = text(item.status, 'pending');
      return {
        content: text(item.content).trim(),
        status: status === 'completed' || status === 'in_progress' ? status : 'pending',
      } as const;
    })
    .filter((item) => item.content);
}

function parsePlan(record: JsonRecord): PlanEvent | null {
  const items = parsePlanItems(record.items);
  if (items.length === 0) return null;
  return {
    ...base(record),
    type: record.type === 'plan_updated' ? 'plan_updated' : 'plan_created',
    items,
    revision: integer(record.revision, 1),
    source: typeof record.source === 'string' ? record.source : undefined,
  };
}

function parseVerification(record: JsonRecord): VerificationEvent | null {
  const status = text(record.status, 'unknown');
  if (!['pass', 'warn', 'fail', 'partial', 'unknown'].includes(status)) return null;
  return {
    ...base(record),
    ...(record as unknown as TurnVerification),
    type: 'verification',
    status: status as TurnVerification['status'],
  };
}

function parseSubagent(record: JsonRecord, type: SubagentEvent['type']): SubagentEvent {
  const status = text(record.status, 'running');
  const normalized = ['running', 'completed', 'failed', 'abandoned'].includes(status)
    ? (status as NonNullable<TurnSubagent['status']>)
    : 'running';
  return {
    ...base(record),
    type,
    name: text(record.name, 'research'),
    description: text(record.description),
    status: normalized,
    tool_call_id: text(record.tool_call_id),
    result_chars: integer(record.result_chars),
    duration_ms: integer(record.duration_ms),
  };
}

export function parseAgentEvent(eventType: string, raw: string): AgentEvent | null {
  const record = parseJsonRecord(raw);
  if (!record) return null;
  const common = base(record);

  switch (eventType) {
    case 'delta':
      return { ...common, type: 'delta', text: text(record.text) };
    case 'narration':
      return { ...common, type: 'narration', text: text(record.text) };
    case 'stage': {
      const key = text(record.key).trim();
      if (!key) return null;
      return {
        ...common,
        type: 'stage',
        key,
        label: text(record.label, key),
        status: normalizeTraceStatus(record.status),
        detail: text(record.detail),
      };
    }
    case 'degraded':
      return {
        ...common,
        type: 'degraded',
        stage: text(record.stage),
        reason: text(record.reason),
      };
    case 'tool_started': {
      const callId = text(record.call_id).trim();
      const toolName = text(record.tool_name).trim();
      if (!callId || !toolName) return null;
      return {
        ...common,
        type: 'tool_started',
        call_id: callId,
        tool_name: toolName,
        tool_kind: normalizeToolKind(record.tool_kind),
        label: text(record.label, toolName),
        detail: text(record.detail) || text(record.query),
        query: text(record.query),
        server: text(record.server),
        tool: text(record.tool),
        skill_ref: text(record.skill_ref),
        task_id: text(record.task_id),
        automatic: bool(record.automatic),
      };
    }
    case 'tool_result': {
      const callId = text(record.call_id).trim();
      const toolName = text(record.tool_name).trim();
      if (!callId || !toolName) return null;
      return {
        ...common,
        type: 'tool_result',
        call_id: callId,
        tool_name: toolName,
        tool_kind: normalizeToolKind(record.tool_kind),
        label: text(record.label, toolName),
        detail: text(record.detail),
        status: text(record.status, 'done'),
        hit_count: integer(record.hit_count),
        evidence_count: integer(record.evidence_count),
        candidate_count: integer(record.candidate_count),
        sources: Array.isArray(record.sources)
          ? record.sources.filter((item): item is string => typeof item === 'string').slice(0, 5)
          : [],
        citation_numbers: Array.isArray(record.citation_numbers)
          ? record.citation_numbers.map((item) => Number(item)).filter((item) => Number.isFinite(item))
          : [],
        empty_reason: text(record.empty_reason),
        latency_ms: record.latency_ms === undefined ? undefined : integer(record.latency_ms),
        result_bytes: integer(record.result_bytes),
        truncated: bool(record.truncated),
        error: text(record.error),
        server: text(record.server),
        tool: text(record.tool),
        skill_ref: text(record.skill_ref),
        task_id: text(record.task_id),
        automatic: bool(record.automatic),
      };
    }
    case 'plan_created':
    case 'plan_updated':
      return parsePlan({ ...record, type: eventType });
    case 'budget':
    case 'budget_exhausted':
      return {
        ...common,
        ...(record as unknown as TurnBudget),
        type: eventType,
      };
    case 'verification':
      return parseVerification(record);
    case 'partial_result':
      return {
        ...common,
        type: 'partial_result',
        reason: text(record.reason),
        synthesized: bool(record.synthesized),
        fallback: text(record.fallback) || undefined,
        evidence_count: integer(record.evidence_count),
        answer_chars: integer(record.answer_chars),
      };
    case 'partial_synthesis_failed':
      return {
        ...common,
        type: 'partial_synthesis_failed',
        reason: text(record.reason),
        error: text(record.error),
        evidence_count: integer(record.evidence_count),
      };
    case 'evidence_compressed':
      return {
        ...common,
        type: 'evidence_compressed',
        stage: text(record.stage, 'partial_synthesis'),
        evidence_count: integer(record.evidence_count),
        compressed_bytes: integer(record.compressed_bytes),
        max_bytes: integer(record.max_bytes),
        note: text(record.note) || undefined,
      };
    case 'context_pressure':
      return {
        ...common,
        type: 'context_pressure',
        max_prompt_tokens: integer(record.max_prompt_tokens),
        window_tokens: integer(record.window_tokens),
        threshold_tokens: integer(record.threshold_tokens),
        note: text(record.note) || undefined,
      };
    case 'context_compacted':
      return {
        ...common,
        type: 'context_compacted',
        stage: text(record.stage, 'summarization'),
        trigger: text(record.trigger, 'auto'),
        cutoff_index: integer(record.cutoff_index),
        dropped_messages: integer(record.dropped_messages),
        retained_messages: integer(record.retained_messages),
        before_tokens: integer(record.before_tokens),
        after_tokens: integer(record.after_tokens),
        window_tokens: integer(record.window_tokens),
        summary_chars: integer(record.summary_chars),
        summary_sha256: text(record.summary_sha256),
        offload_path: text(record.offload_path),
        window_id: text(record.window_id),
        note: text(record.note) || undefined,
      };
    case 'subagent_started':
      return parseSubagent(record, 'subagent_started');
    case 'subagent_completed':
      return parseSubagent(record, 'subagent_completed');
    case 'document_card': {
      const card = asRecord(record.card);
      if (!card || card.kind !== 'attachment_revision') return null;
      return { ...common, type: 'document_card', card: card as unknown as AttachmentRevisionCard };
    }
    case 'mcp_write_proposal': {
      const card = asRecord(record.card);
      if (!card || card.kind !== 'mcp_write_proposal' || !text(card.proposal_id)) return null;
      return {
        ...common,
        type: 'mcp_write_proposal',
        card: card as unknown as McpWriteProposalCard,
      };
    }
    case 'attachment_revision':
    case 'attachment_release':
      return { ...common, type: eventType, ...record };
    case 'turn_start':
      return {
        ...common,
        type: 'turn_start',
        query: text(record.query),
        query_mode: text(record.query_mode),
      };
    case 'turn_end':
      return { ...common, type: 'turn_end', status: text(record.status) };
    case 'error':
      return {
        ...common,
        type: 'error',
        message: text(record.message),
        cancelled: bool(record.cancelled),
      };
    case 'done':
      return {
        ...common,
        ...(record as unknown as QueryDonePayload),
        type: 'done',
        answer: text(record.answer),
        summary: asRecord(record.summary) ?? null,
        footer: text(record.footer),
        turn: asRecord(record.turn) ?? undefined,
      };
    default:
      return null;
  }
}

function toolStatus(value: string): QueryTraceStatus {
  if (value === 'done' || value === 'succeeded') return 'done';
  if (value === 'proposed' || value === 'queued' || value === 'running') return 'running';
  return 'error';
}

export function traceStepFromToolEvent(
  event: ToolStartedEvent | ToolCompletedEvent,
): QueryTraceStep {
  if (event.type === 'tool_started') {
    return {
      key: event.call_id,
      kind: 'tool',
      call_id: event.call_id,
      label: event.label || event.tool_name,
      status: 'running',
      detail: event.detail || event.query,
      input: event.query,
    };
  }
  return {
    key: event.call_id,
    kind: 'tool',
    call_id: event.call_id,
    label: event.label || event.tool_name,
    status: toolStatus(event.status),
    detail: event.detail,
    sources: event.sources,
    evidence_count: event.evidence_count,
    candidate_count: event.candidate_count,
    citation_numbers: event.citation_numbers,
    empty_reason: event.empty_reason,
  };
}

export function mergeMcpWriteProposal(
  previous: McpWriteProposalCard[],
  event: McpWriteProposalEvent,
): McpWriteProposalCard[] {
  const card = event.card;
  const index = previous.findIndex((item) => item.proposal_id === card.proposal_id);
  if (index < 0) return [...previous, card];
  return previous.map((item, itemIndex) => (
    itemIndex === index ? { ...item, ...card } : item
  ));
}
