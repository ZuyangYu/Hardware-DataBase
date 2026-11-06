import { describe, expect, it } from 'vitest';

import {
  isHiddenChatTraceStep,
  mergeMcpWriteProposal,
  parseAgentEvent,
  traceStepFromToolEvent,
  type McpWriteProposalEvent,
  type ToolCompletedEvent,
  type ToolStartedEvent,
} from './agentEvents';

const envelope = {
  schema_version: 1,
  item_id: 'tool:inv-1',
};

describe('typed agent events', () => {
  it('hides runtime orchestration stages from the chat process panel', () => {
    for (const key of ['permission', 'generate', 'skill_loaded']) {
      expect(isHiddenChatTraceStep({ key })).toBe(true);
    }
    expect(isHiddenChatTraceStep({ key: 'inv-1' })).toBe(false);
    expect(isHiddenChatTraceStep({ key: 'balanced_route_retry' })).toBe(false);
  });

  it('parses tool started/results with a stable call_id', () => {
    const started = parseAgentEvent('tool_started', JSON.stringify({
      ...envelope,
      call_id: 'inv-1',
      tool_name: 'document_search',
      tool_kind: 'retrieval',
      label: '文档检索',
      detail: '电源结构',
      query: '电源结构',
    })) as ToolStartedEvent | null;
    expect(started?.call_id).toBe('inv-1');
    expect(started?.label).toBe('文档检索');
    expect(traceStepFromToolEvent(started!)).toMatchObject({
      key: 'inv-1',
      label: '文档检索',
      status: 'running',
      detail: '电源结构',
    });

    const completed = parseAgentEvent('tool_result', JSON.stringify({
      ...envelope,
      call_id: 'inv-1',
      tool_name: 'document_search',
      tool_kind: 'retrieval',
      label: '文档检索',
      detail: '3 条命中 · 9ms',
      status: 'done',
      hit_count: 3,
      evidence_count: 3,
      candidate_count: 2,
      sources: ['设计说明.docx'],
      citation_numbers: [1, 2, 3],
      empty_reason: '',
      latency_ms: 9,
    })) as ToolCompletedEvent | null;
    expect(traceStepFromToolEvent(completed!)).toMatchObject({
      key: 'inv-1',
      kind: 'tool',
      status: 'done',
      detail: '3 条命中 · 9ms',
      sources: ['设计说明.docx'],
      evidence_count: 3,
      candidate_count: 2,
    });
  });

  it('treats proposed MCP writes as running and rejected calls as errors', () => {
    const proposed = parseAgentEvent('tool_result', JSON.stringify({
      call_id: 'inv-2',
      tool_name: 'mcp__kicad__create',
      tool_kind: 'mcp',
      label: '外部插件 · kicad · create',
      detail: 'kicad/create · proposed',
      status: 'proposed',
    })) as ToolCompletedEvent | null;
    expect(traceStepFromToolEvent(proposed!).status).toBe('running');

    const rejected = parseAgentEvent('tool_result', JSON.stringify({
      call_id: 'inv-3',
      tool_name: 'mcp__kicad__create',
      tool_kind: 'mcp',
      status: 'rejected',
    })) as ToolCompletedEvent | null;
    expect(traceStepFromToolEvent(rejected!).status).toBe('error');
  });

  it('parses budget, verification, subagent and compaction payloads', () => {
    const budget = parseAgentEvent('budget_exhausted', JSON.stringify({
      status: 'exhausted',
      exhausted: true,
    }));
    expect(budget?.type).toBe('budget_exhausted');
    expect(budget && 'exhausted' in budget ? budget.exhausted : false).toBe(true);

    const verification = parseAgentEvent('verification', JSON.stringify({
      status: 'partial',
      checks: [{ key: 'answer_present', label: '回答非空', status: 'pass' }],
    }));
    expect(verification?.type).toBe('verification');

    const subagent = parseAgentEvent('subagent_completed', JSON.stringify({
      name: 'research',
      status: 'completed',
      tool_call_id: 'call-1',
    }));
    expect(subagent && 'tool_call_id' in subagent ? subagent.tool_call_id : '').toBe('call-1');

    const compaction = parseAgentEvent('context_compacted', JSON.stringify({
      stage: 'context_window',
      max_prompt_tokens: 100,
      window_tokens: 200,
      threshold_tokens: 170,
    }));
    expect(compaction?.type).toBe('context_compacted');
  });

  it('distinguishes context pressure from a real compaction event', () => {
    const pressure = parseAgentEvent('context_pressure', JSON.stringify({
      schema_version: 1,
      item_id: 'context_pressure',
      max_prompt_tokens: 30000,
      window_tokens: 32768,
      threshold_tokens: 27852,
      note: '下一轮可能触发框架摘要',
    }));
    expect(pressure?.type).toBe('context_pressure');
    expect(pressure && 'window_tokens' in pressure ? pressure.window_tokens : 0).toBe(32768);

    const compaction = parseAgentEvent('context_compacted', JSON.stringify({
      schema_version: 1,
      item_id: 'context_compaction:turn-1:abcdef',
      stage: 'summarization',
      trigger: 'auto',
      cutoff_index: 8,
      dropped_messages: 8,
      retained_messages: 4,
      before_tokens: 30000,
      after_tokens: 9000,
      window_tokens: 32768,
      summary_chars: 1200,
      summary_sha256: 'a'.repeat(64),
      offload_path: '/conversation_history/t1.md',
      window_id: 'turn-1:abcdef',
    }));
    expect(compaction?.type).toBe('context_compacted');
    expect(compaction && 'dropped_messages' in compaction ? compaction.dropped_messages : 0).toBe(8);
    expect(compaction && 'after_tokens' in compaction ? compaction.after_tokens : 0).toBe(9000);
  });

  it('keeps evidence compression separate from conversation compaction', () => {
    const event = parseAgentEvent('evidence_compressed', JSON.stringify({
      schema_version: 1,
      item_id: 'evidence_compressed',
      stage: 'partial_synthesis',
      evidence_count: 3,
      compressed_bytes: 2048,
      max_bytes: 4096,
    }));
    expect(event?.type).toBe('evidence_compressed');
    expect(event && 'compressed_bytes' in event ? event.compressed_bytes : 0).toBe(2048);
  });

  it('rejects malformed events instead of leaking untyped payloads', () => {
    expect(parseAgentEvent('verification', '{"status":"weird"}')).toBeNull();
    expect(parseAgentEvent('tool_started', '{"tool_name":"document_search"}')).toBeNull();
    expect(parseAgentEvent('made_up_event', '{}')).toBeNull();
  });

  it('merges partial MCP proposal updates by proposal_id', () => {
    const first = parseAgentEvent('mcp_write_proposal', JSON.stringify({
      schema_version: 1,
      card: {
        kind: 'mcp_write_proposal',
        proposal_id: 'proposal-1',
        server: 'kicad',
        server_name: 'KiCad',
        tool: 'create_schematic',
        arguments_digest: 'abc123',
        artifact_hint: 'kicad_sch',
        status: 'proposed',
        expires_at: '2030-01-01T00:00:00Z',
      },
    })) as McpWriteProposalEvent | null;
    expect(first).not.toBeNull();
    const cards = mergeMcpWriteProposal([], first!);

    const completed = parseAgentEvent('mcp_write_proposal', JSON.stringify({
      schema_version: 1,
      card: {
        kind: 'mcp_write_proposal',
        proposal_id: 'proposal-1',
        server: 'kicad',
        server_name: 'KiCad',
        tool: 'create_schematic',
        arguments_digest: 'abc123',
        artifact_hint: 'kicad_sch',
        status: 'completed',
        expires_at: '2030-01-01T00:00:00Z',
        artifact_filename: 'board.kicad_sch',
      },
    })) as McpWriteProposalEvent;
    expect(mergeMcpWriteProposal(cards, completed)[0]).toMatchObject({
      status: 'completed',
      artifact_filename: 'board.kicad_sch',
    });
  });
});
