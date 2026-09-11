import { describe, expect, it } from 'vitest';

import {
  buildWorkbenchDeepLink,
  canAnswerClarification,
  clarificationAnswerValue,
  documentArtifactDownloadPath,
  documentArtifactFileName,
  documentCardFromChatTask,
  documentCardFromWorkOrderStatus,
  documentCardFromClarificationEvent,
  documentCardFromGenerationSession,
  documentCardIdentity,
  documentCardStatusLabel,
  documentCardStatusTone,
  documentCardTitle,
  documentCardWorkbenchActions,
  documentCurrentChatTaskPath,
  documentChatTaskEventsPath,
  nextActionLabel,
  documentWorkOrderStatusPath,
  mergeDocumentCards,
  parseDocumentCardEvent,
  parseDocumentTaskStreamEvent,
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
  it('builds the durable current-task snapshot path for one conversation', () => {
    expect(documentCurrentChatTaskPath(85)).toBe(
      '/api/v1/document-generation/chat-tasks/current?session_id=85',
    );
  });

  it('builds and parses the replayable current-task event stream', () => {
    expect(documentChatTaskEventsPath(85)).toBe(
      '/api/v1/document-generation/chat-tasks/events?session_id=85',
    );
    const task = {
      session_id: 85,
      task_id: 'task-current',
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: 'running',
      created_at: '',
      updated_at: '',
      status: { status: 'running', phase: 'running', artifacts: [] },
    };
    expect(parseDocumentTaskStreamEvent(JSON.stringify({
      event_type: 'projection',
      current: task,
    }))).toEqual(task);
    expect(parseDocumentTaskStreamEvent('{bad json')).toBeNull();
    expect(parseDocumentTaskStreamEvent(JSON.stringify({ current: { task_id: 'task' } }))).toBeNull();
  });

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
      status: 'draft',
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

  it('titles a session-only task as a requirement clarification, not a work order', () => {
    const task: DocumentChatTaskView = {
      session_id: 97,
      task_id: 'task-clarify-1',
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: 'needs_clarification',
      created_at: '2026-09-10T11:21:34Z',
      updated_at: '2026-09-10T11:21:34Z',
      status: {
        work_order_id: null,
        status: 'needs_clarification',
        phase: 'needs_clarification',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'ADAS',
        unit_statuses: {},
        next_actions: ['answer_clarification'],
        clarification_session_id: 'generation-session-clarify',
        clarification_state: {
          session_id: 'generation-session-clarify',
          status: 'needs_clarification',
          pending_question: {
            question_id: 'recommendations',
            content: '是否采用推荐的生成与审核策略？',
            options: ['采用推荐方案', '逐项设置'],
            reason: '可一次确认安全默认值。',
          },
        },
        artifacts: [],
      },
    };

    const card = documentCardFromChatTask(task);
    expect(card.kind).toBe('requirement_clarification');
    expect(card.status).toBe('needs_clarification');
    expect(card.question_id).toBe('recommendations');
    expect(documentCardTitle(card.kind)).toBe('需求澄清');
  });

  it('titles a session without a work order as a requirement session', () => {
    const task: DocumentChatTaskView = {
      session_id: 98,
      task_id: 'task-session-1',
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: 'pending',
      created_at: '2026-09-10T11:22:00Z',
      updated_at: '2026-09-10T11:22:00Z',
      status: {
        work_order_id: null,
        status: 'draft',
        phase: 'draft',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'ADAS',
        unit_statuses: {},
        next_actions: ['answer_clarification', 'propose_document_plan'],
        artifacts: [],
      },
    };

    const card = documentCardFromChatTask(task);
    expect(card.kind).toBe('generation_session');
    expect(documentCardTitle(card.kind)).toBe('需求会话');
  });

  it('maps a pending ICD scope review into an actionable gate', () => {
    const task: DocumentChatTaskView = {
      session_id: 99,
      task_id: 'task-scope',
      work_order_id: 'wo-89b3',
      kb_name: 'ADAS',
      job_status: 'needs_review',
      created_at: '2026-09-10T16:12:57Z',
      updated_at: '2026-09-10T16:12:58Z',
      status: {
        work_order_id: 'wo-89b3',
        status: 'needs_review',
        phase: 'needs_review',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'ADAS',
        unit_statuses: {},
        next_actions: ['submit_icd_scope_resolution', 'open_document_workbench'],
        artifacts: [],
        pending_review: {
          review_id: 'review-scope',
          review_kind: 'icd_scope',
          status: 'pending',
          scope_review: {
            status: 'pending',
            pending_count: 1,
            blocking: true,
            exceptions: [{
              kind: 'connector_mapping_missing',
              refdes: 'X302',
              recommended_action: 'check_edf_mapping',
              user_instruction: '已确定接插件 X302，但当前冻结来源中未找到其 EDF 管脚映射。',
              suggested_refdes: ['X1900', 'X1902'],
            }],
          },
        },
      },
    };

    const card = documentCardFromChatTask(task);

    expect(card.status).toBe('needs_review');
    expect(card.gateReview).toMatchObject({
      reviewKind: 'icd_scope',
      status: 'pending',
      scopePendingCount: 1,
      scopeBlocking: true,
      scopeExceptions: [expect.objectContaining({
        refdes: 'X302',
        suggested_refdes: ['X1900', 'X1902'],
      })],
    });
  });

  it('clears the gate on refresh when the work order no longer reports it', () => {
    const fallback: DocumentCardData = {
      kind: 'work_order_status',
      status: 'needs_review',
      next_actions: ['submit_icd_scope_resolution', 'open_document_workbench'],
      kb_name: 'ADAS',
      work_order_id: 'wo-89b3',
      generation_session_id: null,
      gateReview: {
        reviewKind: 'icd_scope',
        status: 'pending',
        scopePendingCount: 1,
        scopeBlocking: false,
        scopeExceptions: [{ kind: 'connector_scope_ambiguous', refdes: 'X301' }],
      },
    };

    const refreshed = documentCardFromWorkOrderStatus({
      work_order_id: 'wo-89b3',
      status: 'running',
      scope_type: 'knowledge_base',
      knowledge_base_name: 'ADAS',
      unit_statuses: {},
      artifacts: [],
    }, fallback);

    expect(refreshed.gateReview).toBeUndefined();
  });

  it('keeps the gate on refresh when the work order still reports it', () => {
    const fallback: DocumentCardData = {
      kind: 'work_order_status',
      status: 'needs_review',
      next_actions: ['submit_icd_scope_resolution', 'open_document_workbench'],
      kb_name: 'ADAS',
      work_order_id: 'wo-89b3',
      generation_session_id: null,
    };

    const refreshed = documentCardFromWorkOrderStatus({
      work_order_id: 'wo-89b3',
      status: 'needs_review',
      scope_type: 'knowledge_base',
      knowledge_base_name: 'ADAS',
      unit_statuses: {},
      artifacts: [],
      pending_review: {
        review_kind: 'icd_scope',
        status: 'pending',
        scope_review: {
          status: 'pending',
          pending_count: 1,
          blocking: true,
          exceptions: [{ kind: 'connector_mapping_missing', refdes: 'X302' }],
        },
      },
    }, fallback);

    expect(refreshed.gateReview?.scopeBlocking).toBe(true);
    expect(refreshed.gateReview?.scopeExceptions[0]?.refdes).toBe('X302');
  });

  it('does not invent a gate review when the task has no pending review', () => {
    const task: DocumentChatTaskView = {
      session_id: 98,
      task_id: 'task-session-1',
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: 'pending',
      created_at: '2026-09-10T11:22:00Z',
      updated_at: '2026-09-10T11:22:00Z',
      status: {
        work_order_id: null,
        status: 'draft',
        phase: 'draft',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'ADAS',
        unit_statuses: {},
        next_actions: ['answer_clarification'],
        artifacts: [],
        pending_review: null,
      },
    };

    expect(documentCardFromChatTask(task).gateReview).toBeUndefined();
  });

  it('restores a hash-bound confirmation card after refresh', () => {
    const task: DocumentChatTaskView = {
      session_id: 85,
      task_id: 'task-confirm-refresh',
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: 'awaiting_plan_confirmation',
      created_at: '2026-09-09T09:38:16Z',
      updated_at: '2026-09-09T09:38:16Z',
      status: {
        work_order_id: null,
        status: 'awaiting_plan_confirmation',
        phase: 'awaiting_confirmation',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'ADAS',
        unit_statuses: {},
        next_actions: ['confirm_document_plan'],
        clarification_session_id: 'generation-session-confirm',
        planning_state: {
          output_spec_id: 'spec-1',
          output_spec_version: 2,
          output_spec_hash: 'sha256:spec',
          document_plan_id: 'plan-1',
          document_plan_version: 2,
          plan_hash: 'sha256:plan',
          proposal_status: 'proposed',
        },
        artifacts: [],
      },
    };

    expect(documentCardFromChatTask(task)).toMatchObject({
      kind: 'output_spec_confirmation',
      status: 'awaiting_confirmation',
      task_id: 'task-confirm-refresh',
      generation_session_id: 'generation-session-confirm',
      proposal: {
        output_spec_hash: 'sha256:spec',
        plan_hash: 'sha256:plan',
        status: 'proposed',
        executable: true,
      },
    });
  });

  it('projects authoritative execution failure and real unit progress', () => {
    const task: DocumentChatTaskView = {
      session_id: 17,
      task_id: 'task-failed',
      work_order_id: 'wo-failed',
      kb_name: 'hardware',
      job_status: 'failed',
      created_at: '2026-09-09T09:00:00Z',
      updated_at: '2026-09-09T09:06:22Z',
      status: {
        work_order_id: 'wo-failed',
        status: 'failed',
        phase: 'failed',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'hardware',
        target_format: 'xlsx',
        unit_statuses: {},
        next_actions: [],
        error_code: 'document_job_failed',
        error_message: 'plan execution scope is not present in the configured allowlist',
        retryable: false,
        harness_run: {
          status: 'running',
          current_node: 'fill_fields',
          completed_units: 7,
          total_units: 21,
        },
        artifacts: [],
      },
    };

    expect(documentCardFromChatTask(task)).toMatchObject({
      status: 'failed',
      errorCode: 'document_job_failed',
      errorMessage: 'plan execution scope is not present in the configured allowlist',
      retryable: false,
      progress: {
        currentNode: 'fill_fields',
        completedUnits: 7,
        totalUnits: 21,
        percent: 33,
      },
    });
  });

  it('normalizes a blocked release even when the execution node says complete', () => {
    const task: DocumentChatTaskView = {
      session_id: 17,
      task_id: 'task-blocked',
      work_order_id: 'wo-blocked',
      kb_name: 'ADAS',
      job_status: 'pending',
      created_at: '',
      updated_at: '',
      status: {
        work_order_id: 'wo-blocked',
        status: 'pending',
        phase: 'pending',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'ADAS',
        unit_statuses: {},
        next_actions: ['view_error', 'provide_value'],
        error_code: 'plan_release_blocked',
        error_message: '21 个字段尚未完成，无法发布',
        harness_run: {
          status: 'completed',
          current_node: 'complete',
          completed_units: 0,
          total_units: 21,
        },
        artifacts: [{ artifact_id: 'candidate-1', stage: 'review_candidate' }],
      },
    };

    expect(documentCardFromChatTask(task)).toMatchObject({
      status: 'blocked',
      errorCode: 'plan_release_blocked',
      progress: { completedUnits: 0, totalUnits: 21, percent: 0 },
      artifacts: [{ artifact_id: 'candidate-1', stage: 'review_candidate' }],
    });
  });

  it('keeps blocked/error/progress semantics when a work-order refresh replaces the card', () => {
    const refreshed = documentCardFromWorkOrderStatus({
      work_order_id: 'wo-blocked',
      task_id: 'task-blocked',
      status: 'pending',
      phase: 'pending',
      scope_type: 'knowledge_base',
      knowledge_base_name: 'ADAS',
      unit_statuses: {},
      next_actions: ['view_error', 'provide_value'],
      error_code: 'plan_release_blocked',
      error_message: '发布被阻止',
      harness_run: { current_node: 'complete', completed_units: 0, total_units: 21 },
      artifacts: [{ artifact_id: 'candidate-1', stage: 'review_candidate' }],
    }, {
      kind: 'work_order_status', status: 'running', next_actions: [], kb_name: 'ADAS',
      work_order_id: 'wo-blocked', generation_session_id: null,
    });
    expect(refreshed.status).toBe('blocked');
    expect(refreshed.errorMessage).toBe('发布被阻止');
    expect(refreshed.progress?.percent).toBe(0);
    expect(refreshed.artifacts?.[0].stage).toBe('review_candidate');
  });

  it('restores a pending question when a refreshed v2 session is awaiting a plan', () => {
    const task: DocumentChatTaskView = {
      session_id: 76,
      task_id: 'task-clarify-refresh',
      work_order_id: null,
      kb_name: 'hardware',
      job_status: 'needs_clarification',
      created_at: '2026-09-09T02:16:38Z',
      updated_at: '2026-09-09T02:16:51Z',
      status: {
        work_order_id: null,
        status: 'draft',
        phase: 'draft',
        clarification_session_id: 'generation-session-clarify-refresh',
        scope_type: 'knowledge_base',
        knowledge_base_name: 'hardware',
        unit_statuses: {},
        next_actions: ['answer_clarification', 'propose_document_plan'],
        artifacts: [],
      },
    };
    const fallback = documentCardFromChatTask(task);
    expect(fallback.generation_session_id).toBe('generation-session-clarify-refresh');
    const restored = documentCardFromGenerationSession({
      session_id: 'generation-session-clarify-refresh',
      knowledge_base_name: 'hardware',
      status: 'awaiting_plan' as unknown as GenerationSession['status'],
      brief: {
        purpose: '参考模板生成 ICD',
        confirmed: false,
        confidence: 0.9,
        scope: {},
        source_policy: {},
        output_policy: {},
        missing_data_policy: null,
        inference_policy: null,
      },
      messages: [{
        message_id: 'question-outline',
        role: 'assistant',
        content: '需要包含哪些章节或字段？可直接输入名称列表。',
        question_id: 'outline',
        options: [],
        reason: '章节范围决定计划中的语义单元。',
      }],
      document_task_id: 'task-clarify-refresh',
      last_question_id: 'outline',
    }, fallback);

    expect(restored).toMatchObject({
      status: 'needs_clarification',
      generation_session_id: 'generation-session-clarify-refresh',
      question_id: 'outline',
      content: '需要包含哪些章节或字段？可直接输入名称列表。',
      next_actions: ['answer_clarification'],
    });
    expect(canAnswerClarification(restored, true)).toBe(true);
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

  it('does not restore an answered question as the pending composer payload after a page refresh', () => {
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
      next_actions: fallback.next_actions,
      question_id: null,
      options: [],
      reason: null,
    });
    // An answered question must not be resurrected as stale composer content.
    expect(restored.content).toBeNull();
  });

  it('does not resurrect an answered assistant question when clarification payload is missing', () => {
    const restored = documentCardFromGenerationSession(session({
      status: 'needs_clarification',
      last_question_id: 'format',
      messages: [
        { message_id: 'm1', role: 'assistant', content: '请选择输出格式', question_id: 'format', options: ['docx'] },
        { message_id: 'm2', role: 'user', content: 'docx', question_id: 'format', answer: 'docx' },
      ],
    }), fallback);

    expect(restored.status).toBe('needs_clarification');
    expect(restored.question_id).toBeNull();
    expect(restored.content).toBeNull();
    expect(restored.options).toEqual([]);
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

  it('does not expose an answer action when the message history has no question payload', () => {
    const restored = documentCardFromGenerationSession(session({
      status: 'needs_clarification',
      last_question_id: 'pin_range',
      messages: [],
    }), fallback);

    expect(restored.question_id).toBeNull();
    expect(restored.next_actions).toEqual(fallback.next_actions);
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


describe('output_spec_confirmation card', () => {
  const proposalEvent = JSON.stringify({
    card: {
      kind: 'output_spec_confirmation',
      status: 'awaiting_plan_confirmation',
      next_actions: ['confirm_document_plan'],
      kb_name: 'hardware',
      task_id: 'task-1',
      generation_session_id: 'session-1',
      proposal: {
        document_plan_id: 'plan-1',
        document_plan_version: 1,
        plan_hash: 'sha256:plan',
        output_spec_id: 'spec-1',
        output_spec_version: 3,
        output_spec_hash: 'sha256:spec',
        status: 'awaiting_plan_confirmation',
        executable: true,
        deliverables: [{ format: 'xlsx', role: 'primary' }],
        outline_count: 2,
        table_count: 1,
        warnings: ['来源中缺少 pin 定义'],
        blockers: [],
      },
    },
  });

  it('parses the proposal summary and plan hashes', () => {
    const card = parseDocumentCardEvent(proposalEvent);
    expect(card?.kind).toBe('output_spec_confirmation');
    expect(card?.proposal?.plan_hash).toBe('sha256:plan');
    expect(card?.proposal?.output_spec_hash).toBe('sha256:spec');
    expect(card?.proposal?.document_plan_id).toBe('plan-1');
    expect(card?.proposal?.executable).toBe(true);
    expect(card?.proposal?.blockers).toEqual([]);
  });

  it('rejects malformed proposal payloads', () => {
    const card = parseDocumentCardEvent(JSON.stringify({
      card: { kind: 'output_spec_confirmation', status: 'x', proposal: 'nope' },
    }));
    expect(card?.proposal).toBeUndefined();
  });

  it('labels the card 计划确认 and confirm action', () => {
    expect(documentCardTitle('output_spec_confirmation')).toBe('计划确认');
    expect(nextActionLabel('confirm_document_plan')).toBe('确认生成');
    expect(nextActionLabel('propose_document_plan')).toBe('生成计划提案');
  });
});
