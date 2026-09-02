import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import DocumentGenerationPage from '../../DocumentGenerationPage';
import DocumentStatusCard from './DocumentStatusCard';
import { parseDocumentCardEvent, type DocumentCardData } from './documentCardModel';

const auth = {
  token: 'test-token',
  user: {
    username: 'alice',
    role: 'user' as const,
    department_id: 1,
    department_name: '研发部',
  },
};

const hardwareKb = {
  name: 'hardware',
  kb_id: 1,
  department_id: 1,
  department_name: '研发部',
  permission: 'write' as const,
  registered: true,
};

function cardEvent(card: Record<string, unknown>): string {
  return JSON.stringify({ card });
}

const workOrderCard: DocumentCardData = {
  kind: 'work_order_created',
  status: 'queued',
  next_actions: ['get_document_generation_status'],
  kb_name: 'hardware',
  work_order_id: 'wo-123',
  generation_session_id: null,
};

describe('DocumentStatusCard', () => {
  it('renders work order card with deep link from document_card event', () => {
    const card = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_created',
      status: 'queued',
      next_actions: ['get_document_generation_status'],
      kb_name: 'hardware',
      work_order_id: 'wo-123',
    }));
    expect(card).toEqual(workOrderCard);

    const markup = renderToStaticMarkup(<DocumentStatusCard card={card!} onRefreshStatus={() => undefined} />);
    expect(markup).toContain('工单已创建');
    expect(markup).toContain('排队中');
    expect(markup).toContain('wo-123');
    expect(markup).toContain('刷新状态');
    expect(markup).toContain('前往工作台');
    expect(markup).toMatch(/href="\/document-generation\?kb=hardware(&amp;|&)workOrder=wo-123"/);
  });

  it('refresh status button calls REST endpoint directly, not the agent', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard card={workOrderCard} onRefreshStatus={() => undefined} />,
    );
    expect(markup).toMatch(/aria-label="刷新工单状态 wo-123"/);

    const refreshing = renderToStaticMarkup(
      <DocumentStatusCard card={workOrderCard} refreshing onRefreshStatus={() => undefined} />,
    );
    expect(refreshing).toContain('刷新中…');
    expect(refreshing).not.toContain('刷新状态<');

    const sessionCard = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'generation_session',
          status: 'needs_clarification',
          next_actions: ['answer_clarification'],
          kb_name: 'hardware',
          work_order_id: null,
          generation_session_id: 'gs-1',
        }}
      />,
    );
    expect(sessionCard).not.toContain('刷新状态');
    expect(sessionCard).not.toContain('document-generation?');
  });

  it('renders one download button per artifact, none without artifacts or work order', () => {
    const withArtifacts = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 'succeeded',
      next_actions: ['view_result'],
      kb_name: 'hardware',
      work_order_id: 'wo-123',
      target_format: 'xlsx',
      artifacts: [
        { artifact_id: 'a-1', stage: 'draft' },
        { artifact_id: 'a-2', stage: 'final' },
      ],
    }));
    const markup = renderToStaticMarkup(
      <DocumentStatusCard card={withArtifacts!} onRefreshStatus={() => undefined} />,
    );
    expect(markup).toContain('下载 draft');
    expect(markup).toContain('下载 final');
    expect(markup).toContain('aria-label="下载 draft（工单 wo-123）"');
    expect(markup).toContain('aria-label="下载 final（工单 wo-123）"');

    const noArtifacts = renderToStaticMarkup(
      <DocumentStatusCard card={workOrderCard} onRefreshStatus={() => undefined} />,
    );
    expect(noArtifacts).not.toContain('下载');

    const noWorkOrder = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'succeeded',
          next_actions: [],
          kb_name: 'hardware',
          work_order_id: null,
          generation_session_id: null,
          artifacts: [{ artifact_id: 'a-1', stage: 'draft' }],
        }}
        onRefreshStatus={() => undefined}
      />,
    );
    expect(noWorkOrder).not.toContain('下载');
  });

  it('workbench preselects kb and work order from query params', () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={['/document-generation?kb=hardware&workOrder=wo-42']}>
        <DocumentGenerationPage auth={auth} kbs={[hardwareKb]} onLogout={() => undefined} />
      </MemoryRouter>,
    );

    expect(html).toContain('任务与下载');
    expect(html).not.toContain('上传并分析受控模板');
    expect(html).toMatch(/<option value="hardware"[^>]*selected/);
    expect(html).toContain('已从会话预选工作单 wo-42');
  });
});
