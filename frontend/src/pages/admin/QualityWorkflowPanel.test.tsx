import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { AdminRemediationGuide, QualityReports, ReviewRequestApproval } from './QualityWorkflowPanel';
import type { QualityCase } from '@/api/types';

/** SSR 渲染不执行 useEffect,api.get 不会被调用:reports 保持初值空数组。 */
describe('QualityReports', () => {
  it('首屏未取得报告前显示加载状态，不提前显示暂无报告', () => {
    const markup = renderToStaticMarkup(<QualityReports />);
    expect(markup).toContain('质量与费用分析报告');
    expect(markup).toContain('生成统计报告');
    expect(markup).toContain('正在读取统计报告');
    expect(markup).not.toContain('暂无统计报告');
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

describe('ReviewRequestApproval', () => {
  it('为 owner 展示待处理请求和材料范围选择', () => {
    const detail = {
      case_id: 'case-owner', row_version: 2, content_access: 'owner',
      content: { query: 'q', answer: 'a', evidence: [] },
      review_requests: [{ request_id: 'request-1', status: 'pending', message: '请协助复核', created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', decided_at: null }],
    } as unknown as QualityCase;
    const markup = renderToStaticMarkup(<ReviewRequestApproval detail={detail} onRefresh={async () => undefined} />);
    expect(markup).toContain('待处理的复核请求');
    expect(markup).toContain('批准并共享复核材料');
    expect(markup).toContain('固定回答');
  });
});
