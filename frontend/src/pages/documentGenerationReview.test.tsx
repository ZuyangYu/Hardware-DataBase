import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import {
  buildDocumentReviewDecisionRequest,
  DocumentReviewPanel,
} from './documentGenerationReview';

describe('documentGenerationReview', () => {
  it('builds a hash-bound idempotent decision for the current review subject', () => {
    expect(buildDocumentReviewDecisionRequest(
      {
        review_id: 'review/a',
        subject_hash: 'subject-current',
      },
      'approved',
      '映射正确',
      'decision-1',
    )).toEqual({
      reviewId: 'review/a',
      body: {
        subject_hash: 'subject-current',
        decision: { outcome: 'approved', comment: '映射正确' },
        status: 'approved',
        client_request_id: 'decision-1',
      },
    });
  });

  it('leaves the dedicated ICD scope flow out of generic review controls', () => {
    const markup = renderToStaticMarkup(
      <DocumentReviewPanel
        knowledgeBaseName="hardware"
        reviews={[{
          review_id: 'review-icd',
          task_id: 'task-1',
          review_kind: 'icd_scope',
          status: 'pending',
          subject_hash: 'subject',
          source_snapshot_hash: 'source',
          schema_hash: 'schema',
        }]}
      />,
    );
    expect(markup).toBe('');
  });
});
