import { useState } from 'react';

import {
  createClientRequestId,
  submitDocumentReviewDecision,
} from '../api/documentAuthoring';
import type { DocumentReview } from '../api/types';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';

export type DocumentReviewDecisionStatus = 'approved' | 'changes_requested' | 'rejected';

export function buildDocumentReviewDecisionRequest(
  review: Pick<DocumentReview, 'review_id' | 'subject_hash'>,
  status: DocumentReviewDecisionStatus,
  comment: string,
  clientRequestId: string,
): {
  reviewId: string;
  body: {
    subject_hash: string;
    decision: { outcome: DocumentReviewDecisionStatus; comment: string };
    status: DocumentReviewDecisionStatus;
    client_request_id: string;
  };
} {
  return {
    reviewId: review.review_id,
    body: {
      subject_hash: review.subject_hash,
      decision: { outcome: status, comment: comment.trim() },
      status,
      client_request_id: clientRequestId,
    },
  };
}

type Props = {
  knowledgeBaseName: string;
  reviews: DocumentReview[];
  onChanged?: () => void;
};

const REVIEW_KIND_LABELS: Record<string, string> = {
  field_mapping: '字段映射',
  protected_field: '受保护字段',
  missing_policy: '缺失数据策略',
  derivation: '推导结果',
  low_confidence: '低置信度结果',
  icd_scope: 'ICD 范围',
};

/** Task-bound review controls. Hashes remain hidden but are always sent back. */
export function DocumentReviewPanel({ knowledgeBaseName, reviews, onChanged }: Props) {
  const pending = reviews.filter((review) => (
    review.review_kind !== 'icd_scope'
    && (review.status === 'pending' || review.status === 'changes_requested')
  ));
  const [comments, setComments] = useState<Record<string, string>>({});
  const [busyReviewId, setBusyReviewId] = useState<string | null>(null);

  if (pending.length === 0) return null;

  async function decide(review: DocumentReview, status: DocumentReviewDecisionStatus) {
    if (busyReviewId) return;
    setBusyReviewId(review.review_id);
    try {
      const request = buildDocumentReviewDecisionRequest(
        review,
        status,
        comments[review.review_id] ?? '',
        createClientRequestId(),
      );
      await submitDocumentReviewDecision(knowledgeBaseName, request.reviewId, request.body);
      notify.success(status === 'approved' ? '审核已通过。' : '审核意见已记录。');
      onChanged?.();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '提交审核决定失败');
    } finally {
      setBusyReviewId(null);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-amber-200 bg-amber-50/50 p-3 text-sm">
      <div>
        <p className="font-semibold">待处理审核</p>
        <p className="mt-1 text-muted-foreground">审核绑定当前任务、来源快照和 Schema；内容变化后需重新审核。</p>
      </div>
      {pending.map((review) => {
        const label = REVIEW_KIND_LABELS[review.review_kind] ?? review.review_kind;
        const busy = busyReviewId === review.review_id;
        return (
          <div key={review.review_id} className="space-y-2 rounded-md border bg-background p-2">
            <p className="font-medium">{label}</p>
            <p className="text-xs text-muted-foreground">审核项：{review.review_id}</p>
            <Textarea
              placeholder="可选：填写审核说明"
              value={comments[review.review_id] ?? ''}
              onChange={(event) => setComments((previous) => ({
                ...previous,
                [review.review_id]: event.target.value,
              }))}
              disabled={busy}
            />
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={() => void decide(review, 'approved')} disabled={busy}>
                {busy ? '提交中…' : '通过审核'}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void decide(review, 'changes_requested')}
                disabled={busy}
              >
                要求修改
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void decide(review, 'rejected')}
                disabled={busy}
              >
                拒绝
              </Button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
