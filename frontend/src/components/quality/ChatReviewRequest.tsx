import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { api } from '@/api/client';
import type { QualityCase } from '@/api/types';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ErrorState, LoadingState } from '@/components/admin/StateBlock';
import ReviewRequestApproval from './ReviewRequestApproval';

/** Notifications open the exact case, independently of the active chat session. */
export default function ChatReviewRequest() {
  const [params, setParams] = useSearchParams();
  const caseId = params.get('quality_case');
  const [detail, setDetail] = useState<QualityCase | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError('');
    if (!caseId) return;
    void api.get<QualityCase>(`/api/v1/quality/cases/${encodeURIComponent(caseId)}`).then((value) => {
      if (!cancelled) setDetail(value);
    }).catch(() => {
      if (!cancelled) setError('无法读取该案例，请确认原回答用户身份或重试。');
    });
    return () => { cancelled = true; };
  }, [caseId, attempt]);
  const close = useCallback(() => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      next.delete('quality_case');
      return next;
    });
  }, [setParams]);
  return <Dialog open={Boolean(caseId)} onOpenChange={(open) => { if (!open) close(); }}>
    <DialogContent className="max-h-[calc(100dvh-32px)] w-[min(640px,calc(100vw-32px))] max-w-none overflow-y-auto sm:max-w-none">
      <DialogHeader><DialogTitle>处理质量复核请求</DialogTitle></DialogHeader>
      {error ? <ErrorState message={error} onRetry={() => setAttempt((value) => value + 1)} /> : !detail ? <LoadingState label="正在读取复核请求…" /> : detail.content_access !== 'owner' ? <p>该请求只能由原回答用户处理，或材料已失效。</p> : <>
        {detail.content.query != null && <details><summary className="cursor-pointer text-sm">查看固定提问与回答</summary><div className="mt-2 grid gap-2 whitespace-pre-wrap break-words text-sm"><p>{detail.content.query}</p><p>{detail.content.answer}</p></div></details>}
        {detail.review_requests?.some((item) => item.status === 'pending')
          ? <ReviewRequestApproval key={`${detail.case_id}:${detail.review_requests.find((item) => item.status === 'pending')?.request_id}`} detail={detail} onRefresh={async () => {
            setDetail(await api.get<QualityCase>(`/api/v1/quality/cases/${encodeURIComponent(detail.case_id)}`));
          }} />
          : <p>该案例的复核请求已处理。</p>}
      </>}
    </DialogContent>
  </Dialog>;
}
