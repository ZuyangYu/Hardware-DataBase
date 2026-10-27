import { describe, expect, it } from 'vitest';
import type { DocumentConflict } from '@/api/types';
import { buildConflictDecision, conflictActionDescription, conflictActionNeedsClaim } from './conflictReviewModel';

const conflict: DocumentConflict = {
  conflict_id: 'c-1', fingerprint: 'fingerprint', snapshot_hash: 'snapshot',
  evidence_set_hash: 'evidence', row_version: 3,
  claims: [{ claim_id: 'a' }, { claim_id: 'b' }],
  allowed_actions: ['choose_design_candidate', 'defer'],
};

describe('conflict review decisions', () => {
  it('binds a single explicit choice to the server version and source hashes', () => {
    expect(buildConflictDecision(conflict, 'choose_design_candidate', 'b', '下一版候选', 'retry-key'))
      .toEqual({
        expected_fingerprint: 'fingerprint', source_snapshot_hash: 'snapshot',
        evidence_set_hash: 'evidence', expected_conflict_version: 3,
        action: 'choose_design_candidate', selected_claim_id: 'b',
        rationale: '下一版候选', idempotency_key: 'retry-key',
      });
    expect(conflictActionDescription('choose_design_candidate')).toContain('待审批');
  });

  it('rejects unknown actions, unlisted evidence and incomplete server assertions', () => {
    expect(() => buildConflictDecision(conflict, 'approve', 'b', '', 'key')).toThrow();
    expect(() => buildConflictDecision(conflict, 'choose_design_candidate', 'foreign', '', 'key')).toThrow();
    expect(() => buildConflictDecision({ ...conflict, row_version: undefined }, 'defer', '', '', 'key')).toThrow();
    expect(() => buildConflictDecision({ ...conflict, snapshot_hash: '' }, 'defer', '', '', 'key')).toThrow();
  });

  it('defers without inventing a candidate or submitting all conflicts together', () => {
    const body = buildConflictDecision(conflict, 'defer', 'b', '等待补充', 'key');
    expect(body.selected_claim_id).toBeUndefined();
    expect(conflictActionNeedsClaim('defer')).toBe(false);
    expect(conflictActionNeedsClaim('choose_design_candidate')).toBe(true);
  });
});
