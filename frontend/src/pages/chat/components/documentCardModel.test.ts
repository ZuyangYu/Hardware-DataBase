import { describe, expect, it } from 'vitest';

import {
  buildWorkbenchDeepLink,
  documentArtifactDownloadPath,
  documentArtifactFileName,
  documentCardIdentity,
  documentCardStatusLabel,
  documentCardStatusTone,
  documentCardWorkbenchActions,
  documentWorkOrderStatusPath,
  mergeDocumentCards,
  parseDocumentCardEvent,
  type DocumentCardData,
} from './documentCardModel';

function cardEvent(card: Record<string, unknown>): string {
  return JSON.stringify({ card });
}

const workOrderCard: DocumentCardData = {
  kind: 'work_order_created',
  status: 'queued',
  next_actions: ['get_document_generation_status'],
  kb_name: 'hardware',
  work_order_id: 'wo-123',
  generation_session_id: null,
};

describe('documentCardModel', () => {
  it('parses document_card event payloads defensively', () => {
    expect(parseDocumentCardEvent(cardEvent({
      kind: 'work_order_created',
      status: 'queued',
      next_actions: ['get_document_generation_status'],
      kb_name: 'hardware',
      work_order_id: 'wo-123',
    }))).toEqual(workOrderCard);

    expect(parseDocumentCardEvent('not-json')).toBeNull();
    expect(parseDocumentCardEvent(JSON.stringify({ payload: {} }))).toBeNull();
    expect(parseDocumentCardEvent(cardEvent({ status: 'queued' }))).toBeNull();
    const parsed = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 42,
      next_actions: ['get_document_generation_status', 7, null],
      kb_name: 'kb',
      work_order_id: ' ',
    }));
    expect(parsed).toEqual({
      kind: 'work_order_status',
      status: '',
      next_actions: ['get_document_generation_status'],
      kb_name: 'kb',
      work_order_id: null,
      generation_session_id: null,
    });
  });

  it('mergeDocumentCards replaces in place per work order and appends new identities', () => {
    const updated = mergeDocumentCards(
      [workOrderCard],
      { kind: 'work_order_status', status: 'retrieving', next_actions: [], kb_name: 'hardware', work_order_id: 'wo-123', generation_session_id: null },
    );
    expect(updated).toHaveLength(1);
    expect(updated[0].kind).toBe('work_order_status');
    expect(updated[0].status).toBe('retrieving');

    const appended = mergeDocumentCards(updated, {
      kind: 'generation_session',
      status: 'ready_to_generate',
      next_actions: [],
      kb_name: 'hardware',
      work_order_id: null,
      generation_session_id: 'gs-1',
    });
    expect(appended).toHaveLength(2);
    expect(appended[1].generation_session_id).toBe('gs-1');
  });

  it('mergeDocumentCards dedupes adhoc cards by kind, replacing in place', () => {
    const first = mergeDocumentCards([], {
      kind: 'generation_session', status: 'queued', next_actions: [], kb_name: 'hardware', work_order_id: null, generation_session_id: null,
    });
    const replaced = mergeDocumentCards(first, {
      kind: 'generation_session', status: 'running', next_actions: [], kb_name: 'hardware', work_order_id: null, generation_session_id: null,
    });
    expect(replaced).toHaveLength(1);
    expect(replaced[0].status).toBe('running');

    const appended = mergeDocumentCards(replaced, {
      kind: 'work_order_created', status: 'queued', next_actions: [], kb_name: 'hardware', work_order_id: null, generation_session_id: null,
    });
    expect(appended).toHaveLength(2);
    expect(appended.map((card) => card.kind)).toEqual(['generation_session', 'work_order_created']);
    expect(documentCardIdentity(appended[0])).toBe('adhoc:generation_session');
    expect(documentCardIdentity(appended[1])).toBe('adhoc:work_order_created');
  });

  it('keys identity by work order first, then generation session', () => {
    expect(documentCardIdentity({ ...workOrderCard, generation_session_id: 'gs-1' })).toBe('wo:wo-123');
    expect(documentCardIdentity({ ...workOrderCard, work_order_id: null, generation_session_id: 'gs-1' })).toBe('gs:gs-1');
    expect(documentCardIdentity({ ...workOrderCard, work_order_id: null, generation_session_id: null })).toBe('adhoc:work_order_created');
  });

  it('documentWorkOrderStatusPath targets the REST status endpoint, not the agent', () => {
    const path = documentWorkOrderStatusPath('hardware', 'wo-123');
    expect(path).toBe('/api/v1/document-generation/work-orders/wo-123/status?kb=hardware');
    expect(path.startsWith('/api/v1/document-generation/work-orders/')).toBe(true);
    expect(path).not.toContain('/api/v1/query');
    expect(path).not.toContain('/turns');
    expect(path).not.toContain('agent');
  });

  it('buildWorkbenchDeepLink encodes kb and work order', () => {
    expect(buildWorkbenchDeepLink('硬件知识库', 'wo 9')).toBe(
      `/document-generation?kb=${encodeURIComponent('硬件知识库')}&workOrder=${encodeURIComponent('wo 9')}`,
    );
  });

  it('maps human-gate actions to workbench deep links and keeps refresh actions out', () => {
    const gates = documentCardWorkbenchActions({
      kind: 'work_order_status',
      status: 'blocked',
      next_actions: ['get_document_generation_status', 'submit_icd_scope_resolution', 'view_error'],
      kb_name: '硬件知识库',
      work_order_id: 'wo-9',
      generation_session_id: null,
    });
    expect(gates.map((action) => action.action)).toEqual(['submit_icd_scope_resolution', 'view_error']);
    expect(gates[0].label).toBe('处理 ICD 范围待办');
    expect(gates[0].href).toBe(buildWorkbenchDeepLink('硬件知识库', 'wo-9'));

    const plain = documentCardWorkbenchActions({ ...workOrderCard, work_order_id: 'wo-9' });
    expect(plain).toEqual([
      { action: 'open_workbench', label: '前往工作台', href: buildWorkbenchDeepLink('hardware', 'wo-9') },
    ]);

    expect(documentCardWorkbenchActions({ ...workOrderCard, work_order_id: null })).toEqual([]);
  });

  it('caps gate actions at three entries', () => {
    const capped = documentCardWorkbenchActions({
      ...workOrderCard,
      work_order_id: 'wo-5',
      next_actions: [
        'get_document_generation_status',
        'submit_icd_scope_resolution',
        'answer_clarification',
        'view_error',
        'retry_generation',
        'replace_template',
      ],
    });
    expect(capped).toHaveLength(3);
    expect(capped.map((action) => action.action)).toEqual([
      'submit_icd_scope_resolution',
      'answer_clarification',
      'view_error',
    ]);
  });

  it('labels unknown gate actions with the workbench fallback', () => {
    const [fallback] = documentCardWorkbenchActions({
      ...workOrderCard,
      work_order_id: 'wo-6',
      next_actions: ['brand_new_gate_action'],
    });
    expect(fallback.action).toBe('brand_new_gate_action');
    expect(fallback.label).toBe('前往工作台处理');
    expect(fallback.href).toBe(buildWorkbenchDeepLink('hardware', 'wo-6'));
  });

  it('parses card artifacts and target_format defensively', () => {
    const parsed = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 'succeeded',
      next_actions: ['view_result'],
      kb_name: 'hardware',
      work_order_id: 'wo-11',
      target_format: 'xlsx',
      artifacts: [
        { artifact_id: 'a-1', stage: 'draft', validity_status: 'valid' },
        { artifact_id: ' ', stage: 'draft' },
        { stage: 'no-id' },
        'bogus',
        null,
        { artifact_id: 'a-2', stage: 'final' },
      ],
    }));
    expect(parsed?.targetFormat).toBe('xlsx');
    expect(parsed?.artifacts).toEqual([
      { artifact_id: 'a-1', stage: 'draft' },
      { artifact_id: 'a-2', stage: 'final' },
    ]);

    const capped = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 'succeeded',
      next_actions: [],
      kb_name: 'hardware',
      work_order_id: 'wo-12',
      artifacts: Array.from({ length: 11 }, (_, i) => ({ artifact_id: `a-${i}`, stage: `s-${i}` })),
    }));
    expect(capped?.artifacts).toHaveLength(8);
    expect(capped?.artifacts?.[7]).toEqual({ artifact_id: 'a-7', stage: 's-7' });

    const malformed = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 'succeeded',
      next_actions: [],
      kb_name: 'hardware',
      work_order_id: 'wo-13',
      artifacts: 'not-an-array',
      target_format: 42,
    }));
    expect(malformed?.artifacts).toBeUndefined();
    expect(malformed?.targetFormat).toBeUndefined();
    expect('artifacts' in malformed!).toBe(false);
    expect('targetFormat' in malformed!).toBe(false);
  });

  it('builds artifact download path and file name client-side', () => {
    expect(documentArtifactDownloadPath('a-1', 'hardware')).toBe(
      '/api/v1/document-generation/artifacts/a-1/download?kb=hardware',
    );
    expect(documentArtifactDownloadPath('a-1', '硬件知识库')).toBe(
      `/api/v1/document-generation/artifacts/a-1/download?kb=${encodeURIComponent('硬件知识库')}`,
    );
    expect(documentArtifactFileName('a-1', 'xlsx')).toBe('a-1.xlsx');
    expect(documentArtifactFileName('a-1', '')).toBe('a-1.bin');
    expect(documentArtifactFileName('a-1', undefined)).toBe('a-1.bin');
  });

  it('falls back to describeWorkOrderStatus for statuses outside the card map', () => {
    expect(documentCardStatusLabel('queued')).toBe('排队中');
    expect(documentCardStatusLabel('retrieving')).toBe('正在检索资料');
    expect(documentCardStatusLabel('mystery_status')).toBe('mystery_status');
    expect(documentCardStatusLabel('')).toBe('状态未知');
    expect(documentCardStatusTone('retrieving')).toBe('info');
    expect(documentCardStatusTone('mystery_status')).toBe('neutral');
  });
});
