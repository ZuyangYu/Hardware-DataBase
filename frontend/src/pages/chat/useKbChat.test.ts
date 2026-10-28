import { describe, expect, it } from 'vitest';

import {
  buildTurnRequest,
  mergeMcpWriteProposalFromSseEvent,
  parseTurnBudget,
  parseTurnPlan,
  parseTurnSubagent,
  parseTurnVerification,
} from './useKbChat';

describe('MCP write proposal card lifecycle', () => {
  it('parses and updates the inline proposal card without treating it as evidence', () => {
    const first = mergeMcpWriteProposalFromSseEvent([], 'mcp_write_proposal', JSON.stringify({
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
    }));
    expect(first).toHaveLength(1);
    const completed = mergeMcpWriteProposalFromSseEvent(first, 'mcp_write_proposal', JSON.stringify({
      card: {
        kind: 'mcp_write_proposal',
        proposal_id: 'proposal-1',
        status: 'completed',
        artifact_filename: 'board.kicad_sch',
      },
    }));
    expect(completed[0]).toMatchObject({ status: 'completed', artifact_filename: 'board.kicad_sch' });
    expect(mergeMcpWriteProposalFromSseEvent(completed, 'tool_result', '{}')).toEqual(completed);
  });
});

describe('turn request construction', () => {
  it('keeps attachments and the knowledge scope separate from the query', () => {
    const request = buildTurnRequest(
      '对比两个电源方案',
      'turn-key-1',
      'deep',
      { attachmentIds: ['attachment-1'], sourceScope: 'attachment_and_knowledge_base' },
      {
        version: 1,
        purpose: 'compare',
        sources: [
          { kb_id: 205, role: 'peer' },
          { kb_id: 101, role: 'peer' },
        ],
      },
    );

    expect(request).toEqual({
      query: '对比两个电源方案',
      client_request_id: 'turn-key-1',
      query_mode: 'deep',
      attachment_ids: ['attachment-1'],
      source_scope: 'attachment_and_knowledge_base',
      knowledge_scope: {
        version: 1,
        purpose: 'compare',
        sources: [
          { kb_id: 205, role: 'peer' },
          { kb_id: 101, role: 'peer' },
        ],
      },
    });
  });

  it('omits optional fields for a plain turn', () => {
    expect(buildTurnRequest('普通知识库问题', 'turn-key-2', 'fast')).toEqual({
      query: '普通知识库问题',
      client_request_id: 'turn-key-2',
      query_mode: 'fast',
    });
  });

  it('leaves the source scope at the server default when it is auto', () => {
    const request = buildTurnRequest(
      '读取附件',
      'turn-key-3',
      'deep',
      { attachmentIds: ['attachment-1'], sourceScope: 'auto' },
    );

    expect(request.attachment_ids).toEqual(['attachment-1']);
    expect(request).not.toHaveProperty('source_scope');
  });
});

describe('turn meta SSE projection', () => {
  it('normalizes plan items and drops empty entries', () => {
    const plan = parseTurnPlan(JSON.stringify({
      revision: 2,
      items: [
        { content: '读取 BOM', status: 'completed' },
        { content: '', status: 'pending' },
        { content: '输出表格', status: 'unexpected' },
      ],
    }));
    expect(plan?.revision).toBe(2);
    expect(plan?.items).toEqual([
      { content: '读取 BOM', status: 'completed' },
      { content: '输出表格', status: 'pending' },
    ]);
  });

  it('parses budget, verification and subagent payloads', () => {
    expect(parseTurnBudget(JSON.stringify({ status: 'exhausted', exhausted: true }))?.exhausted)
      .toBe(true);
    expect(parseTurnVerification(JSON.stringify({
      status: 'partial',
      checks: [{ key: 'answer_present', label: '回答非空', status: 'pass' }],
    }))?.status).toBe('partial');
    expect(parseTurnSubagent(JSON.stringify({
      name: 'research',
      status: 'completed',
    }))?.name).toBe('research');
    expect(parseTurnVerification('{"status":"weird"}')).toBeNull();
    expect(parseTurnPlan('not-json')).toBeNull();
  });
});
