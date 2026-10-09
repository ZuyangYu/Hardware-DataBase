import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { AdminRemediationGuide, QualityReports } from './QualityWorkflowPanel';
import type { QualityCase } from '@/api/types';

/** SSR 渲染不执行 useEffect,api.get 不会被调用:reports 保持初值空数组。 */
describe('QualityReports', () => {
  it('空报告时显示空状态而不是空白区块', () => {
    const markup = renderToStaticMarkup(<QualityReports />);
    expect(markup).toContain('质量与费用分析报告');
    expect(markup).toContain('生成统计报告');
    expect(markup).toContain('暂无统计报告');
  });
});

describe('AdminRemediationGuide', () => {
  const detail = {
    case_id: 'case-guide', status: 'reviewed', content_access: 'hidden',
    reviews: [{ review_id: 'r1', judgment: 'confirmed', root_cause: 'retrieval', correctness: 'correct', supersedes: null, created_at: '2026-10-09T00:00:00Z', content: {} }],
  } as unknown as QualityCase;

  it('展示当前阶段、下一步与根因动作,并说明管理员权限边界', () => {
    const markup = renderToStaticMarkup(<AdminRemediationGuide detail={detail} />);
    expect(markup).toContain('管理员修复管理');
    expect(markup).toContain('当前阶段');
    expect(markup).toContain('已确认问题');
    expect(markup).toContain('检索');
    expect(markup).toContain('管理员不能直接修改知识库');
  });

  it('未知 root_cause 落到"待确定"而不是崩溃', () => {
    const noCause = { ...detail, reviews: [{ ...detail.reviews[0], root_cause: 'mystery' }] } as unknown as QualityCase;
    const markup = renderToStaticMarkup(<AdminRemediationGuide detail={noCause} />);
    expect(markup).toContain('待确定');
  });
});
