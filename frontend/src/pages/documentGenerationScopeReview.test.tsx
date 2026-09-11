import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ScopeReviewView } from './DocumentGenerationPage';
import type { IcdScopeReview } from '../api/types';

const blockingReview: IcdScopeReview = {
  status: 'pending',
  pending_count: 1,
  exceptions: [{
    exception_id: 'exception-1',
    kind: 'connector_mapping_missing',
    refdes: 'X302',
    recommended_action: 'check_edf_mapping',
    user_instruction: '已确定接插件 X302，但当前冻结来源中未找到其 EDF 管脚映射。',
  }],
};

describe('ScopeReviewView', () => {
  it('explains a blocking connector scope stop and hides the resolve button', () => {
    const markup = renderToStaticMarkup(
      <ScopeReviewView review={blockingReview} kb="ADAS" workOrderId="wo-1" />,
    );

    expect(markup).toContain('connector_mapping_missing');
    expect(markup).toContain('X302');
    expect(markup).toContain('EDF');
    expect(markup).not.toContain('应用处理结果并继续生成');
  });

  it('keeps the batch resolution control for resolvable scope exceptions', () => {
    const markup = renderToStaticMarkup(
      <ScopeReviewView
        review={{
          status: 'pending',
          pending_count: 1,
          exceptions: [{
            exception_id: 'exception-2',
            kind: 'extra_pin_exposure',
            refdes: 'X1900',
            recommended_action: 'mark_pending',
            user_instruction: '确认该脚是否需要在对外 ICD 中暴露。',
          }],
        }}
        kb="ADAS"
        workOrderId="wo-1"
      />,
    );

    expect(markup).toContain('应用处理结果并继续生成');
    expect(markup).not.toContain('不能通过“包含/排除”直接放行');
  });
});
