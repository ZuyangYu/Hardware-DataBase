import { describe, expect, it } from 'vitest';

import { buildTurnRequest } from './useKbChat';

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
