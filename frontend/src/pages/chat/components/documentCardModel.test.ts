import { describe, expect, it } from 'vitest';

import {
  buildWorkbenchDeepLink,
  canAnswerClarification,
  clarificationAnswerValue,
  documentArtifactDownloadPath,
  documentArtifactFileName,
  documentCardFromChatTask,
  documentCardFromClarificationEvent,
  documentCardFromGenerationSession,
  documentCardIdentity,
  documentCardStatusLabel,
  documentCardStatusTone,
  documentCardWorkbenchActions,
  documentWorkOrderStatusPath,
  mergeDocumentCards,
  parseDocumentCardEvent,
  type DocumentCardData,
} from './documentCardModel';
import type { DocumentChatTaskView, GenerationSession } from '@/api/types';

function cardEvent(card: Record<string, unknown>): string {
  return JSON.stringify({ card });
}

const workOrderCard: DocumentCardData = {
  kind: 'work_order_created',
  status: 'queued',
  next_actions: ['get_document_generation_status'],
  kb_name: 'hardware',
  task_id: 'task-123',
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
      task_id: 'task-123',
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

  it('projects clarification question and ready events into one task card', () => {
    const question = documentCardFromClarificationEvent(
      'document_clarification_question',
      {
        generation_session_id: 'gs-clarify-1',
        document_task_id: 'task-clarify-1',
        question_id: 'scope',
        options: ['全量', '增量', 7, null],
        reason: 'scope_required',
        content: '请选择生成范围',
      },
      'hardware',
    );
    expect(question).toEqual({
      kind: 'generation_session',
      status: 'needs_clarification',
      next_actions: ['answer_clarification'],
      kb_name: 'hardware',
      task_id: 'task-clarify-1',
      generation_session_id: 'gs-clarify-1',
      question_id: 'scope',
      options: ['全量', '增量'],
      reason: 'scope_required',
      content: '请选择生成范围',
    });

    const ready = documentCardFromClarificationEvent(
      'document_clarification_ready',
      {
        generation_session_id: 'gs-clarify-1',
        document_task_id: 'task-clarify-1',
        reason: 'user_confirmed',
      },
      'hardware',
    );
    expect(ready).toMatchObject({
      kind: 'generation_session',
      status: 'ready_to_generate',
      next_actions: ['create_document_work_order'],
      task_id: 'task-clarify-1',
      generation_session_id: 'gs-clarify-1',
      question_id: null,
      options: [],
      reason: 'user_confirmed',
      content: null,
    });

    const cards = mergeDocumentCards(mergeDocumentCards([], question!), ready!);
    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({
      status: 'ready_to_generate',
      task_id: 'task-clarify-1',
      question_id: null,
      options: [],
    });
  });

  it('accepts wrapped or raw clarification payloads and fails closed without identity', () => {
    expect(documentCardFromClarificationEvent(
      'document_clarification_question',
      JSON.stringify({ payload: { generation_session_id: 'gs-2', question_id: 'q-2' } }),
      'hardware',
    )).toMatchObject({ generation_session_id: 'gs-2', question_id: 'q-2' });
    expect(documentCardFromClarificationEvent(
      'document_clarification_ready',
      { reason: 'user_confirmed' },
      'hardware',
    )).toBeNull();
    expect(documentCardFromClarificationEvent('unknown', {}, 'hardware')).toBeNull();
    expect(documentCardFromClarificationEvent('document_clarification_question', 'not-json', 'hardware')).toBeNull();
  });

  it('matches a clarification task to an older session-only card', () => {
    const oldCard: DocumentCardData = {
      kind: 'generation_session',
      status: 'needs_clarification',
      next_actions: ['answer_clarification'],
      kb_name: 'hardware',
      generation_session_id: 'gs-3',
    };
    const nextCard = documentCardFromClarificationEvent(
      'document_clarification_question',
      {
        generation_session_id: 'gs-3',
        document_task_id: 'task-3',
        question_id: 'q-3',
        options: [],
        reason: 'missing_field',
        content: '请补充字段',
      },
      'hardware',
    );
    const cards = mergeDocumentCards([oldCard], nextCard!);
    expect(cards).toHaveLength(1);
    expect(cards[0].task_id).toBe('task-3');
  });

  it('mergeDocumentCards replaces in place per work order and appends new identities', () => {
    const updated = mergeDocumentCards(
      [workOrderCard],
      { kind: 'work_order_status', status: 'retrieving', next_actions: [], kb_name: 'hardware', task_id: 'task-123', work_order_id: 'wo-123', generation_session_id: null },
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
    expect(documentCardIdentity({ ...workOrderCard, generation_session_id: 'gs-1' })).toBe('task:task-123');
    expect(documentCardIdentity({ ...workOrderCard, task_id: null, generation_session_id: 'gs-1' })).toBe('wo:wo-123');
    expect(documentCardIdentity({ ...workOrderCard, task_id: null, work_order_id: null, generation_session_id: 'gs-1' })).toBe('gs:gs-1');
    expect(documentCardIdentity({ ...workOrderCard, task_id: null, work_order_id: null, generation_session_id: null })).toBe('adhoc:work_order_created');
  });

  it('keeps one card when a task receives a replacement work order', () => {
    const first: DocumentCardData = {
      ...workOrderCard,
      work_order_id: 'wo-old',
    };
    const replaced = mergeDocumentCards([first], {
      ...first,
      kind: 'work_order_status',
      work_order_id: 'wo-new',
      status: 'running',
    });

    expect(replaced).toHaveLength(1);
    expect(replaced[0].work_order_id).toBe('wo-new');
    expect(documentCardIdentity(replaced[0])).toBe('task:task-123');
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

  it('parses clarification fields from document cards without exposing identifiers', () => {
    const parsed = parseDocumentCardEvent(cardEvent({
      kind: 'generation_session',
      status: 'needs_clarification',
      next_actions: ['answer_clarification'],
      kb_name: 'hardware',
      task_id: 'task-private',
      generation_session_id: 'session-private',
      question_id: 'scope',
      content: '请选择生成范围',
      options: ['当前版本', '全部版本'],
    }));

    expect(parsed).toMatchObject({
      task_id: 'task-private',
      question_id: 'scope',
      content: '请选择生成范围',
      options: ['当前版本', '全部版本'],
    });
  });

  it('deep-links clarification cards with a session or task when no work order exists', () => {
    expect(documentCardWorkbenchActions({
      kind: 'generation_session',
      status: 'needs_clarification',
      next_actions: ['answer_clarification'],
      kb_name: 'hardware',
      generation_session_id: 'session/1',
      question_id: 'scope',
      work_order_id: null,
    })).toEqual([{
      action: 'answer_clarification',
      label: '回答澄清问题',
      href: '/document-generation?kb=hardware&session=session%2F1',
    }]);
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

  it('projects a durable chat document task into a downloadable status card', () => {
    const task: DocumentChatTaskView = {
      session_id: 17,
      task_id: 'task-chat-1',
      work_order_id: 'wo-chat-1',
      kb_name: 'hardware',
      job_status: 'succeeded',
      created_at: '2026-09-02T12:00:00Z',
      updated_at: '2026-09-02T12:05:00Z',
      status: {
        work_order_id: 'wo-chat-1',
        status: 'complete',
        phase: 'completed',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'hardware',
        target_format: 'docx',
        unit_statuses: {},
        next_actions: ['view_result'],
        artifacts: [{ artifact_id: 'artifact-chat-1', stage: 'approved_release' }],
      },
    };

    expect(documentCardFromChatTask(task)).toEqual({
      kind: 'work_order_status',
      status: 'completed',
      next_actions: ['view_result'],
      kb_name: 'hardware',
      task_id: 'task-chat-1',
      work_order_id: 'wo-chat-1',
      generation_session_id: null,
      targetFormat: 'docx',
      artifacts: [{ artifact_id: 'artifact-chat-1', stage: 'approved_release' }],
    });
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

describe('documentCardFromGenerationSession restores durable clarification state', () => {
  const fallback: DocumentCardData = {
    kind: 'work_order_status',
    status: 'queued',
    next_actions: ['get_document_generation_status'],
    kb_name: 'hardware',
    work_order_id: 'wo-stale',
  };

  function session(overrides: Partial<GenerationSession>): GenerationSession {
    return {
      session_id: 'gs-refresh-1',
      knowledge_base_name: 'hardware',
      status: 'needs_clarification',
      brief: {
        purpose: 'icd',
        confirmed: false,
        confidence: 0.9,
        scope: {},
        source_policy: {},
        output_policy: {},
        missing_data_policy: null,
        inference_policy: null,
      },
      messages: [],
      work_order_id: 'wo-refresh-1',
      document_task_id: 'task-refresh-1',
      last_question_id: null,
      ...overrides,
    };
  }

  it('restores the pending question, options and answer affordance after a page refresh', () => {
    const restored = documentCardFromGenerationSession(session({
      status: 'needs_clarification',
      last_question_id: 'output_format',
      messages: [
        { message_id: 'm1', role: 'assistant', content: '请选择输出格式', question_id: 'output_format', options: ['docx', 'xlsx'], reason: 'format_required' },
        { message_id: 'm2', role: 'user', content: 'docx', question_id: 'output_format', answer: 'docx' },
      ],
    }), fallback);

    expect(restored).toMatchObject({
      kind: 'work_order_status',
      status: 'needs_clarification',
      kb_name: 'hardware',
      task_id: 'task-refresh-1',
      work_order_id: 'wo-refresh-1',
      generation_session_id: 'gs-refresh-1',
      next_actions: ['answer_clarification'],
      question_id: 'output_format',
      options: ['docx', 'xlsx'],
      reason: 'format_required',
    });
    expect(restored.content).toBe('请选择输出格式');
  });

  it('uses the latest unanswered assistant question over earlier answered ones', () => {
    const restored = documentCardFromGenerationSession(session({
      status: 'needs_clarification',
      last_question_id: 'scope',
      messages: [
        { message_id: 'm1', role: 'assistant', content: '已回答的问题', question_id: 'format', options: ['docx'] },
        { message_id: 'm2', role: 'user', content: 'docx', question_id: 'format', answer: 'docx' },
        { message_id: 'm3', role: 'assistant', content: '请选择范围', question_id: 'scope', options: ['全部', '章节'], reason: 'scope_required' },
      ],
    }), fallback);

    expect(restored.question_id).toBe('scope');
    expect(restored.options).toEqual(['全部', '章节']);
    expect(restored.content).toBe('请选择范围');
  });

  it('falls back to last_question_id when the message history has no question payload', () => {
    const restored = documentCardFromGenerationSession(session({
      status: 'needs_clarification',
      last_question_id: 'pin_range',
      messages: [],
    }), fallback);

    expect(restored.question_id).toBe('pin_range');
    expect(restored.options).toEqual([]);
    expect(restored.content).toBeNull();
  });

  it('clears the question state once the session leaves clarification', () => {
    const restored = documentCardFromGenerationSession(session({
      status: 'ready_to_generate',
      last_question_id: 'output_format',
      messages: [
        { message_id: 'm1', role: 'assistant', content: '请选择输出格式', question_id: 'output_format', options: ['docx'] },
      ],
    }), fallback);

    expect(restored.status).toBe('ready_to_generate');
    expect(restored.question_id).toBeNull();
    expect(restored.options).toEqual([]);
    expect(restored.content).toBeNull();
    expect(restored.next_actions).toEqual(fallback.next_actions);
  });
});

describe('clarification answer interaction guards', () => {
  const answerable: DocumentCardData = {
    kind: 'generation_session',
    status: 'needs_clarification',
    next_actions: ['answer_clarification'],
    kb_name: 'hardware',
    task_id: 'task-clarify-2',
    work_order_id: 'wo-clarify-2',
    generation_session_id: 'gs-clarify-2',
    question_id: 'scope',
    options: ['全部'],
    reason: 'scope_required',
    content: '请选择范围',
  };

  it('allows answering only with question, session and callback present', () => {
    expect(canAnswerClarification(answerable, true)).toBe(true);
    expect(canAnswerClarification(answerable, false)).toBe(false);
    expect(canAnswerClarification({ ...answerable, question_id: null }, true)).toBe(false);
    expect(canAnswerClarification({ ...answerable, generation_session_id: null }, true)).toBe(false);
    expect(canAnswerClarification({ ...answerable, status: 'running' }, true)).toBe(false);
  });

  it('normalizes answers and blocks submission while answering or blank', () => {
    expect(clarificationAnswerValue('  docx  ', false)).toBe('docx');
    expect(clarificationAnswerValue('   ', false)).toBeNull();
    expect(clarificationAnswerValue('docx', true)).toBeNull();
  });
});
