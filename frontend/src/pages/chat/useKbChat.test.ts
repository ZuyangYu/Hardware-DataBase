import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';
import {
  isPlanConfirmationStale,
  mergeDocumentCardsFromSseEvent,
  planConfirmationStaleKey,
} from './useKbChat';

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
