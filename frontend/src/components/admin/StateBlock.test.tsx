import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { EmptyState, LoadingState, RefreshingBadge, TableEmptyRow } from './StateBlock';

describe('管理工作台状态组件', () => {
  it('LoadingState 是 role=status,不渲染"暂无"空态文案', () => {
    const markup = renderToStaticMarkup(<LoadingState label="正在加载质量案例…" />);
    expect(markup).toContain('role="status"');
    expect(markup).toContain('正在加载质量案例…');
    expect(markup).not.toContain('暂无');
  });

  it('RefreshingBadge 标注为 status,用于刷新中保留旧数据的场景', () => {
    const markup = renderToStaticMarkup(<RefreshingBadge label="正在更新列表" />);
    expect(markup).toContain('role="status"');
    expect(markup).toContain('正在更新列表');
  });

  it('EmptyState 与 "未知/无数据" 区分:标题 + 说明', () => {
    const markup = renderToStaticMarkup(<EmptyState title="暂无统计报告">点击生成首份</EmptyState>);
    expect(markup).toContain('暂无统计报告');
    expect(markup).toContain('点击生成首份');
  });

  it('TableEmptyRow 是带 colSpan 的 td,保持表格结构', () => {
    const markup = renderToStaticMarkup(<TableEmptyRow colSpan={6} title="当前范围暂无案例" />);
    expect(markup).toMatch(/col[Ss]pan="6"/);
    expect(markup).toContain('当前范围暂无案例');
  });
});
