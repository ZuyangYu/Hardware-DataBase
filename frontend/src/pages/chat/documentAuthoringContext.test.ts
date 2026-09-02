import { describe, expect, it } from 'vitest';

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

  it('sends document_flow true when generation mode is on and context attached', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'upload-key-1',
      now,
    );
    const request = buildTurnRequest('请生成评审表', 'turn-key-4', 'deep', context, true);
    expect(request.document_flow).toBe(true);
  });

  it('sends document_flow false when generation mode is off with context attached', () => {
    const context = buildDocumentContext(
      { analysis_id: 'analysis-1', template_version_id: 'template-v1' },
      'shared',
      'upload-key-1',
      now,
    );
    const request = buildTurnRequest('仅提问', 'turn-key-5', 'deep', context, false);
    expect(request.document_flow).toBe(false);
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
});
