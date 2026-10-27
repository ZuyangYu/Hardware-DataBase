import { describe, expect, it } from 'vitest';
import type { DocumentContextV2 } from '../../api/types';

import {
  buildDocumentContext,
  buildTurnRequest,
  isDocumentContextExpired,
} from './useKbChat';

describe('document authoring chat context', () => {
  const now = Date.UTC(2026, 7, 31, 0, 0, 0);

  it('creates a stable, structured reference from an analysis response', () => {
    expect(
      buildDocumentContext(
        { analysis_id: ' analysis-1 ', template_version_id: ' template-v1 ' },
        ' shared ',
        'upload-key-1',
        now,
      ),
    ).toEqual({
      analysis_id: 'analysis-1',
      template_version_id: 'template-v1',
      knowledge_base_name: 'shared',
      version: 1,
      expiry: '2026-08-31T00:30:00.000Z',
      client_request_id: 'upload-key-1',
    });
  });

  it('keeps a server-provided expiry and fails closed for an expired reference', () => {
    const context = buildDocumentContext(
      {
        analysis_id: 'analysis-1',
        template_version_id: 'template-v1',
      expiry: '2026-09-01T00:00:00.000Z',
      },
      'shared',
      'upload-key-1',
      now,
    );
    expect(context?.expiry).toBe('2026-09-01T00:00:00.000Z');
    expect(isDocumentContextExpired(context, Date.parse('2026-08-31T23:59:59.000Z'))).toBe(false);
    expect(isDocumentContextExpired(context, Date.parse('2026-09-01T00:00:00.000Z'))).toBe(true);
    expect(
      isDocumentContextExpired(
        { ...context!, expiry: 'not-a-date' },
        now,
      ),
    ).toBe(true);
  });

  it('does not create a reference when the upload response is incomplete', () => {
    expect(
      buildDocumentContext(
        { analysis_id: '', template_version_id: 'template-v1' },
        'shared',
        'upload-key-1',
        now,
      ),
    ).toBeNull();
  });

  it('sends context as a separate optional turn field and preserves legacy payloads', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'upload-key-1',
      now,
    );
    const withContext = buildTurnRequest('请生成评审表', 'turn-key-1', 'deep', context);
    expect(withContext).toMatchObject({
      query: '请生成评审表',
      client_request_id: 'turn-key-1',
      query_mode: 'deep',
      document_context: context,
    });
    expect(withContext).not.toHaveProperty('document_flow');
    expect(withContext.query).not.toContain('analysis-1');
    expect(withContext.query).not.toContain('template-v1');

    const legacy = buildTurnRequest('普通知识库问题', 'turn-key-2', 'deep');
    expect(legacy).toEqual({
      query: '普通知识库问题',
      client_request_id: 'turn-key-2',
      query_mode: 'deep',
    });
    expect(legacy).not.toHaveProperty('document_context');
  });

  it('sends the selected multi-KB purpose and stable source roles separately from the query', () => {
    const request = buildTurnRequest(
      '对比两个电源方案',
      'turn-kb-1',
      'deep',
      null,
      undefined,
      null,
      {
        version: 1,
        purpose: 'compare',
        sources: [
          { kb_id: 205, role: 'peer' },
          { kb_id: 101, role: 'peer' },
        ],
      },
    );
    expect(request.knowledge_scope).toEqual({
      version: 1,
      purpose: 'compare',
      sources: [
        { kb_id: 205, role: 'peer' },
        { kb_id: 101, role: 'peer' },
      ],
    });
    expect(request.query).toBe('对比两个电源方案');
  });

  it('does not send legacy document_flow when a new client has document context', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'upload-key-1',
      now,
    );
    const request = buildTurnRequest('请生成评审表', 'turn-key-4', 'deep', context, true);
    expect(request).not.toHaveProperty('document_flow');
  });

  it('does not turn the legacy mode toggle into a new-client wire contract', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'upload-key-1',
      now,
    );
    const request = buildTurnRequest('仅提问', 'turn-key-5', 'deep', context, false);
    expect(request).not.toHaveProperty('document_flow');
  });

  it('omits document_flow when no document context attached', () => {
    const withStaleToggle = buildTurnRequest('普通知识库问题', 'turn-key-6', 'deep', null, true);
    expect(withStaleToggle).not.toHaveProperty('document_flow');
    expect(withStaleToggle).not.toHaveProperty('document_context');

    const legacy = buildTurnRequest('普通知识库问题', 'turn-key-7', 'deep');
    expect(legacy).not.toHaveProperty('document_flow');
  });

  it('strips server-owned fields when a refreshed context is sent again', () => {
    const serverContext = {
      ...buildDocumentContext(
        { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
        'shared',
        'upload-key-1',
        now,
      )!,
      tenant_id: 'tenant-a',
      owner_user_id: 'user-a',
      created_at: '2026-08-31T00:00:00.000Z',
      permission_use: 'read',
    };
    const request = buildTurnRequest('继续', 'turn-key-3', 'deep', serverContext);
    expect(request.document_context).toEqual({
      analysis_id: 'analysis-1',
      template_version_id: 'template-v1',
      knowledge_base_name: 'shared',
      version: 1,
      expiry: '2026-08-31T00:30:00.000Z',
      client_request_id: 'upload-key-1',
    });
    expect(request.document_context).not.toHaveProperty('tenant_id');
    expect(request.document_context).not.toHaveProperty('owner_user_id');
  });

  it('round-trips all client-owned v2 references for a follow-up turn', () => {
    const v2Context = {
      version: 'v2',
      knowledge_base_name: 'shared',
      attachment_ids: ['attachment-1'],
      source_scope: 'attachment_and_knowledge_base',
      task_id: 'task-1',
      generation_session_id: 'generation-1',
      output_spec_id: 'output-spec-1',
      output_spec_version: 2,
      template_version_id: 'template-v2',
      analysis_id: 'analysis-2',
      expiry: '2026-09-01T00:30:00.000Z',
      client_request_id: 'context-key-2',
      tenant_id: 'tenant-a',
      owner_user_id: 'user-a',
      created_at: '2026-08-31T00:00:00.000Z',
      permission_use: 'read',
    } satisfies DocumentContextV2 & {
      tenant_id: string;
      owner_user_id: string;
      created_at: string;
      permission_use: string;
    };

    const request = buildTurnRequest('确认', 'turn-key-8', 'deep', v2Context, true);
    expect(request.document_context).toEqual({
      version: 'v2',
      knowledge_base_name: 'shared',
      attachment_ids: ['attachment-1'],
      source_scope: 'attachment_and_knowledge_base',
      task_id: 'task-1',
      generation_session_id: 'generation-1',
      output_spec_id: 'output-spec-1',
      output_spec_version: 2,
      template_version_id: 'template-v2',
      analysis_id: 'analysis-2',
      expiry: '2026-09-01T00:30:00.000Z',
      client_request_id: 'context-key-2',
    });
    expect(request.document_context).not.toHaveProperty('tenant_id');
    expect(request.document_context).not.toHaveProperty('owner_user_id');
    expect(request.document_context).not.toHaveProperty('created_at');
    expect(request.document_context).not.toHaveProperty('permission_use');
  });
});
