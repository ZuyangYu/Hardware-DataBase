/** 回答反馈与工单共用的问题分类。 */
import type { QualityReason } from '@/api/types';

export const QUALITY_REASONS: Record<QualityReason, string> = {
  factual_error: '事实错误', unsupported_evidence: '证据不支持', not_found: '资料未找到',
  version_conflict: '资料版本冲突', incomplete_task: '未完成任务', format_mismatch: '格式不符',
};
