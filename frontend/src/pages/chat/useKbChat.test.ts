import { describe, expect, it } from 'vitest';

import { mergeMcpWriteProposal, parseAgentEvent } from '@/api/agentEvents';
import {
  buildTurnRequest,
  skillInputRequestFromSseEvent,
} from './useKbChat';

const stored = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => { stored.set(key, value); },
    removeItem: (key: string) => { stored.delete(key); },
    clear: () => { stored.clear(); },
    key: (index: number) => [...stored.keys()][index] ?? null,
    get length() { return stored.size; },
  },
});

describe('typed Agent event projection', () => {
  it('keeps MCP write proposal cards outside the evidence stream', () => {
    const event = parseAgentEvent('mcp_write_proposal', JSON.stringify({
      schema_version: 1,
      item_id: 'mcp_write_proposal:proposal-1',
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
    expect(event?.type).toBe('mcp_write_proposal');
    if (!event || event.type !== 'mcp_write_proposal') return;
    const cards = mergeMcpWriteProposal([], event);
    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({ proposal_id: 'proposal-1', status: 'proposed' });
  });

  it('parses and deduplicates Skill input requests carried by stage events', () => {
    const request = JSON.stringify({
      skill_request: {
        task_id: 'task-1', skill_ref: 'skill/report',
        required_inputs: ['电源电压', '目标器件'], checkpoint_artifact_id: 'art-1',
      },
    });
    const first = skillInputRequestFromSseEvent([], 'stage', request);
    expect(first).toEqual([{ task_id: 'task-1', skill_ref: 'skill/report',
      required_inputs: ['电源电压', '目标器件'], checkpoint_artifact_id: 'art-1' }]);
    expect(skillInputRequestFromSseEvent(first, 'stage', request)).toEqual(first);
    expect(skillInputRequestFromSseEvent(first, 'stage', JSON.stringify({ label: '普通步骤' }))).toEqual(first);
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

  it('carries only the temporary Skill suppression into this turn', () => {
    localStorage.setItem('hdb.skill.disabled_refs', JSON.stringify(['skill/report', 'skill/report', ' ']));
    try {
      const request = buildTurnRequest('分析报告', 'turn-key-4', 'deep');
      expect(request.disabled_skill_refs).toEqual(['skill/report']);
    } finally {
      localStorage.removeItem('hdb.skill.disabled_refs');
    }
    expect(buildTurnRequest('分析报告', 'turn-key-5', 'deep')).not.toHaveProperty('disabled_skill_refs');
  });
});
