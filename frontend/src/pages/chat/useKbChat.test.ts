import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';
import {
  documentTaskPollDelay,
  isPlanConfirmationStale,
  latestDocumentChatTasks,
  mergeDocumentCardsFromSseEvent,
  planConfirmationStaleKey,
} from './useKbChat';

describe('document task polling lifecycle', () => {
  it('keeps polling an empty active session so a task created after confirmation is discovered', () => {
    expect(documentTaskPollDelay([], true)).toBe(3000);
    expect(documentTaskPollDelay([], false)).toBeNull();
  });

  it('stops only after every discovered task reaches a terminal user-visible phase', () => {
    const task = (phase: string) => ({
      session_id: 85,
      task_id: `task-${phase}`,
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: phase,
      created_at: '',
      updated_at: '',
      status: {
        work_order_id: null,
        status: phase,
        phase,
        scope_type: 'knowledge_base',
        unit_statuses: {},
        artifacts: [],
      },
    });

    expect(documentTaskPollDelay([task('running')])).toBe(3000);
    expect(documentTaskPollDelay([task('needs_review')])).toBeNull();
  });

  it('keeps the status tray on the newest task instead of an older pending confirmation', () => {
    const task = (taskId: string, phase: string, updatedAt: string) => ({
      session_id: 85,
      task_id: taskId,
      work_order_id: null,
      kb_name: 'ADAS',
      job_status: phase,
      created_at: updatedAt,
      updated_at: updatedAt,
      status: {
        work_order_id: null,
        status: phase,
        phase,
        scope_type: 'knowledge_base',
        unit_statuses: {},
        artifacts: [],
      },
    });

    const selected = latestDocumentChatTasks([
      task('task-older', 'awaiting_plan_confirmation', '2026-09-09T09:38:16Z'),
      task('task-newer', 'needs_review', '2026-09-09T09:40:35Z'),
    ]);

    expect(selected).toHaveLength(1);
    expect(selected[0].task_id).toBe('task-newer');
    expect(selected[0].status.phase).toBe('needs_review');
  });
});

describe('useKbChat document clarification SSE projection', () => {
  it('uses the same task card for question, ready, and duplicate replay events', () => {
    const question = JSON.stringify({
      generation_session_id: 'gs-hook-1',
      document_task_id: 'task-hook-1',
      question_id: 'output-format',
      options: ['docx', 'xlsx'],
      reason: 'format_required',
      content: '请选择输出格式',
    });
    const ready = JSON.stringify({
      generation_session_id: 'gs-hook-1',
      document_task_id: 'task-hook-1',
      reason: 'user_confirmed',
    });

    let cards = mergeDocumentCardsFromSseEvent([], 'document_clarification_question', question, 'hardware');
    cards = mergeDocumentCardsFromSseEvent(cards, 'document_clarification_question', question, 'hardware');
    cards = mergeDocumentCardsFromSseEvent(cards, 'document_clarification_ready', ready, 'hardware');

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({
      task_id: 'task-hook-1',
      generation_session_id: 'gs-hook-1',
      status: 'ready_to_generate',
      question_id: null,
    });
  });

  it('coalesces a document_card and clarification event sharing a session or task reference', () => {
    const statusCard = JSON.stringify({
      card: {
        kind: 'work_order_status',
        status: 'needs_clarification',
        next_actions: ['answer_clarification'],
        kb_name: 'hardware',
        generation_session_id: 'gs-hook-2',
        work_order_id: 'wo-hook-2',
      },
    });
    const clarification = JSON.stringify({
      generation_session_id: 'gs-hook-2',
      document_task_id: 'task-hook-2',
      question_id: 'scope',
      options: ['全部'],
      reason: 'scope_required',
      content: '请选择范围',
    });

    let cards = mergeDocumentCardsFromSseEvent([], 'document_card', statusCard, 'hardware');
    cards = mergeDocumentCardsFromSseEvent(cards, 'document_clarification_question', clarification, 'hardware');

    expect(cards).toHaveLength(1);
    expect(cards[0]).toMatchObject({
      task_id: 'task-hook-2',
      work_order_id: 'wo-hook-2',
      generation_session_id: 'gs-hook-2',
      question_id: 'scope',
    });
  });

  it('leaves cards unchanged for unrelated or malformed SSE events', () => {
    const existing = [{
      kind: 'work_order_status',
      status: 'running',
      next_actions: [],
      kb_name: 'hardware',
      task_id: 'task-existing',
    }];
    expect(mergeDocumentCardsFromSseEvent(existing, 'stage', '{}', 'hardware')).toBe(existing);
    expect(mergeDocumentCardsFromSseEvent(existing, 'document_clarification_question', '{}', 'hardware')).toBe(existing);
    expect(mergeDocumentCardsFromSseEvent(existing, 'document_clarification_ready', 'not-json', 'hardware')).toBe(existing);
  });
});


describe('plan confirmation submission guard', () => {
  it('marks only 409 responses as stale', () => {
    expect(isPlanConfirmationStale(new ApiError(409, 'stale plan', 'Conflict'))).toBe(true);
    expect(isPlanConfirmationStale(new ApiError(403, 'no access', 'Forbidden'))).toBe(false);
    expect(isPlanConfirmationStale(new Error('network'))).toBe(false);
  });

  it('stale keys follow the owning session identity', () => {
    const card = {
      kind: 'output_spec_confirmation',
      status: 'awaiting_plan_confirmation',
      next_actions: [],
      kb_name: 'hardware',
      generation_session_id: 'session-1',
    } as const;
    expect(planConfirmationStaleKey(card)).toBe('session-1');
  });
});
