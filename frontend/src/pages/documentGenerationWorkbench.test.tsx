import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import {
  ClarificationPanel,
  DocumentGenerationWorkbench,
  RunStatusPanel,
} from './documentGenerationWorkbench';
import DocumentGenerationPage, { CreateSection, StatusView } from './DocumentGenerationPage';

describe('document generation workbench', () => {
  it('uses the workbench shell on the document generation route', () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={['/document-generation']}>
        <DocumentGenerationPage
          auth={{ token: 'token', user: { username: 'u', role: 'user', department_id: 1, department_name: '研发部' } }}
          kbs={[{ name: 'hardware', kb_id: 1, department_id: 1, department_name: '研发部', permission: 'write', registered: true }]}
          onLogout={() => undefined}
        />
      </MemoryRouter>,
    );

    expect(html).toContain('文档生成工作台');
    expect(html).toContain('aria-label="文档生成流程"');
    expect(html).toContain('模板与证据摘要');
  });

  it('embeds the clarification conversation in the create workspace', () => {
    const html = renderToStaticMarkup(
      <CreateSection
        kbs={[{ name: 'hardware', kb_id: 1, department_id: 1, department_name: '研发部', permission: 'write', registered: true }]}
      />,
    );

    expect(html).toContain('AI 需求澄清');
    expect(html).toContain('选择模板和 Schema 后');
  });

  it('renders the phase rail, chat workspace and inspector', () => {
    const html = renderToStaticMarkup(
      <DocumentGenerationWorkbench
        activePhase="needs_clarification"
        inspector={<p>42 个可填字段</p>}
      >
        <ClarificationPanel
          messages={[{ id: 'm1', role: 'assistant', content: '请确认使用哪个项目版本？' }]}
          reply=""
          confirmedItems={['输出格式：xlsx']}
          pendingItems={['项目版本']}
        />
      </DocumentGenerationWorkbench>,
    );

    expect(html).toContain('需要补充需求');
    expect(html).toContain('请确认使用哪个项目版本？');
    expect(html).toContain('aria-label="回复 AI"');
    expect(html).toContain('42 个可填字段');
  });

  it('shows an actionable label instead of the raw retrieving status', () => {
    const html = renderToStaticMarkup(
      <RunStatusPanel
        status={{
          work_order_id: 'wo-1',
          status: 'retrieving',
          scope_type: 'knowledge_base',
          unit_statuses: {},
          artifacts: [],
        }}
      />,
    );

    expect(html).toContain('正在检索资料');
    expect(html).not.toContain('状态：retrieving');
  });

  it('renders lifecycle controls permitted by a paused work order', () => {
    const html = renderToStaticMarkup(
      <RunStatusPanel
        status={{
          work_order_id: 'wo-1',
          status: 'paused',
          scope_type: 'knowledge_base',
          unit_statuses: {},
          can_resume: true,
          can_cancel: true,
          artifacts: [],
        }}
      />,
    );

    expect(html).toContain('任务已暂停');
    expect(html).toContain('继续生成');
    expect(html).toContain('取消任务');
    expect(html).not.toContain('暂停任务');
  });

  it('renders deletion only when the server permits a terminal deletion', () => {
    const html = renderToStaticMarkup(
      <RunStatusPanel
        status={{
          work_order_id: 'wo-1',
          status: 'complete',
          scope_type: 'knowledge_base',
          unit_statuses: {},
          can_delete: true,
          artifacts: [],
        }}
      />,
    );

    expect(html).toContain('删除任务');
  });

  it('uses the actionable status panel in the actual task view', () => {
    const html = renderToStaticMarkup(
      <StatusView
        kb="hardware"
        status={{
          work_order_id: 'wo-1',
          status: 'blocked',
          scope_type: 'knowledge_base',
          target_format: 'xlsx',
          unit_statuses: {},
          error_code: 'renderer_safety_violation',
          error_message: 'duplicate long value fan-out',
          next_actions: ['replace_template'],
          artifacts: [],
        }}
      />,
    );

    expect(html).toContain('生成被阻止');
    expect(html).toContain('duplicate long value fan-out');
    expect(html).not.toContain('状态：blocked');
  });
});
