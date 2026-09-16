import { describe, expect, it } from 'vitest';

import type { TurnView } from '../../api/types';
import { turnQueueNoticeFromTurn } from './useKbChat';

function turn(overrides: Partial<TurnView>): TurnView {
  return {
    id: 't1',
    session_id: 1,
    user_message_id: 1,
    assistant_message_id: 2,
    kb_name: '__general__',
    query: 'q',
    query_mode: 'fast',
    status: 'pending',
    cancel_requested: false,
    last_event_seq: 0,
    answer: '',
    summary: {},
    footer: '',
    metrics: {},
    error_message: '',
    created_at: '2026-09-15T00:00:00+00:00',
    started_at: null,
    finished_at: null,
    ...overrides,
  } as TurnView;
}

describe('turn queue notice (P0-004)', () => {
  it('maps a blocked pending turn to its blocker and reason', () => {
    const notice = turnQueueNoticeFromTurn(
      turn({ queue_reason: 'queued_behind_previous_turn', blocked_by_turn_id: 't0' }),
    );
    expect(notice).toEqual({
      reason: 'queued_behind_previous_turn',
      blockedByTurnId: 't0',
      waitDeadline: null,
    });
  });

  it('maps an attachment wait with its deadline', () => {
    const notice = turnQueueNoticeFromTurn(
      turn({ status: 'waiting_for_attachments', wait_deadline: '2026-09-15T00:10:00+00:00' }),
    );
    expect(notice?.reason).toBe('waiting_for_attachments');
    expect(notice?.waitDeadline).toBe('2026-09-15T00:10:00+00:00');
  });

  it('clears for running and terminal turns', () => {
    expect(turnQueueNoticeFromTurn(turn({ status: 'streaming' }))).toBeNull();
    expect(turnQueueNoticeFromTurn(turn({ status: 'completed' }))).toBeNull();
  });
});
