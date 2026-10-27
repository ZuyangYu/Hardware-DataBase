import type { DocumentConflict, DocumentConflictDecisionInput } from '@/api/types';

export const CONFLICT_ACTION_LABELS: Record<string, string> = {
  retain_primary: '保留权威来源',
  accept_authoritative_source: '采用有效权威依据',
  choose_claim: '选择证据结论',
  choose_design_candidate: '选择设计候选（待审批）',
  report_difference: '并列记录差异',
  keep_comparison: '保留对比',
  request_change_approval: '提交变更审批',
  defer: '暂缓决定',
  mark_not_applicable: '标记不适用',
  integrity_block: '阻止继续使用来源',
};

export function conflictActionNeedsClaim(action: string): boolean {
  return ['choose_claim', 'accept_authoritative_source', 'choose_design_candidate'].includes(action);
}

export function conflictActionDescription(action: string): string {
  if (action === 'choose_design_candidate' || action === 'request_change_approval') {
    return '作为待审批设计建议保存；原来源规格保持原样，正式发布仍需审批。';
  }
  if (action === 'defer') return '保留待办，相关章节继续等待，正式发布仍被阻止。';
  if (action === 'integrity_block') return '停止使用异常来源，需重新核验来源后继续。';
  return '仅记录本事项的处理依据；服务端复核后决定是否恢复生成，不替代正式发布审核。';
}

export function buildConflictDecision(
  conflict: DocumentConflict, action: string, claimId: string, rationale: string, idempotencyKey: string,
): DocumentConflictDecisionInput {
  if (!CONFLICT_ACTION_LABELS[action] || !conflict.allowed_actions?.includes(action)) {
    throw new Error('请选择服务端允许的处理方式');
  }
  if (!conflict.fingerprint || !conflict.snapshot_hash || !conflict.evidence_set_hash
    || !Number.isSafeInteger(conflict.row_version) || conflict.row_version! < 1) {
    throw new Error('冲突版本或来源信息不完整，请重新加载');
  }
  const needsClaim = conflictActionNeedsClaim(action);
  if (needsClaim && !conflict.claims?.some((claim) => claim.claim_id === claimId)) {
    throw new Error('请选择本事项中的证据候选');
  }
  return {
    expected_fingerprint: conflict.fingerprint,
    source_snapshot_hash: conflict.snapshot_hash,
    evidence_set_hash: conflict.evidence_set_hash,
    expected_conflict_version: conflict.row_version,
    action,
    ...(needsClaim ? { selected_claim_id: claimId } : {}),
    rationale: rationale.trim(),
    idempotency_key: idempotencyKey,
  };
}

export const CONFLICT_STATUS_LABELS: Record<string, string> = {
  open: '待确认', resolved: '已处理', dismissed: '已排除', blocked: '来源异常',
  deferred: '暂缓处理', approval_pending: '待审批',
};
