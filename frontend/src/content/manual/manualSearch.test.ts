import { describe, expect, it } from 'vitest';

import { buildManualSections, searchManual } from './manualSearch';

const chapter = {
  id: 'account', title: '账号使用',
  content: '账号的基础说明。\n\n### 修改密码\n\n输入旧密码和新密码。\n\n### 导出记录\n\n可下载 PDF 文件。\n![示例图片](/help-assets/private-image.png)',
};

describe('手册小节索引与搜索', () => {
  it('正文命中返回对应小节而非整章开头，并记录渲染所用的标题行', () => {
    const sections = buildManualSections([chapter]);
    const found = searchManual(sections, '旧密码');
    expect(found[0]?.section.id).toBe('account--section-1');
    expect(found[0]?.section.line).toBe(3);
    expect(found[0]?.snippet).toContain('旧密码');
  });

  it('标题命中排在只包含该词的正文之前', () => {
    const sections = buildManualSections([
      { id: 'intro', title: '开始使用', content: '完成操作后可以修改密码。' }, chapter,
    ]);
    expect(searchManual(sections, '修改密码')[0]?.section.title).toBe('修改密码');
  });

  it('忽略大小写、全角字符和首尾空白，多个词同时匹配', () => {
    const sections = buildManualSections([chapter]);
    expect(searchManual(sections, '  ｐｄｆ 下载  ')[0]?.section.id).toBe('account--section-2');
    expect(searchManual(sections, 'PDF 密码')).toEqual([]);
  });

  it('不把图片资源路径纳入正文索引', () => {
    expect(searchManual(buildManualSections([chapter]), 'private-image')).toEqual([]);
  });

  it('代码块内的标题文本不生成伪小节', () => {
    const sections = buildManualSections([{ id: 'code', title: '代码', content: '```md\n### 示例代码\n```\n\n### 实际操作\n检查任务。' }]);
    expect(sections.map((section) => section.id)).toEqual(['code', 'code--section-1']);
    expect(sections[1].line).toBe(5);
  });

  it('空查询和未匹配查询不返回内容，特殊符号按普通文本处理', () => {
    const sections = buildManualSections([chapter]);
    expect(searchManual(sections, '   ')).toEqual([]);
    expect(searchManual(sections, '[不存在]')).toEqual([]);
  });
});
