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

  it('renders a blocking ICD scope gate with the operator instruction', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'needs_review',
          next_actions: ['submit_icd_scope_resolution', 'open_document_workbench'],
          kb_name: 'ADAS',
          work_order_id: 'wo-89b3',
          generation_session_id: null,
          gateReview: {
            reviewKind: 'icd_scope',
            status: 'pending',
            scopePendingCount: 1,
            scopeBlocking: true,
            scopeExceptions: [{
              kind: 'connector_mapping_missing',
              refdes: 'X302',
              user_instruction: '已确定接插件 X302，但当前冻结来源中未找到其 EDF 管脚映射。',
              suggested_refdes: ['X1900', 'X1902'],
            }],
          },
        }}
      />,
    );

    expect(markup).toContain('ICD 范围异常待办');
    expect(markup).toContain('X302');
    expect(markup).toContain('EDF');
    expect(markup).toContain('X1900');
    expect(markup).toContain('请直接回复“确认”');
    expect(markup).toContain('按 EDF 实际位号生成');
    expect(markup).not.toContain('候选文档已生成');
  });

  it('offers a chat resolution hint for non-blocking scope exceptions', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'needs_review',
          next_actions: ['submit_icd_scope_resolution', 'open_document_workbench'],
          kb_name: 'ADAS',
          work_order_id: 'wo-99',
          generation_session_id: null,
          gateReview: {
            reviewKind: 'icd_scope',
            status: 'pending',
            scopePendingCount: 1,
            scopeBlocking: false,
            scopeExceptions: [{
              kind: 'connector_scope_ambiguous',
              refdes: 'X301',
              user_instruction: '请确认使用 X301 还是 X302。',
            }],
          },
        }}
      />,
    );

    expect(markup).toContain('ICD 范围异常待办');
    expect(markup).toContain('X301');
    expect(markup).toContain('包含');
    expect(markup).toContain('排除');
    expect(markup).toContain('实际情况');
    expect(markup).toContain('待处理：X301');
  });

  it('renders a generic review gate without ICD include/exclude copy', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'needs_review',
          next_actions: ['review_document', 'open_document_workbench'],
          kb_name: 'ADAS',
          work_order_id: 'wo-77',
          generation_session_id: null,
          gateReview: {
            reviewKind: 'artifact_approval:artifact-a',
            status: 'pending',
            scopePendingCount: 0,
            scopeBlocking: false,
            scopeExceptions: [],
          },
        }}
      />,
    );

    expect(markup).toContain('待处理人工审核');
    expect(markup).toContain('工作台');
    expect(markup).not.toContain('包含');
    expect(markup).not.toContain('排除');
  });

  it('keeps durable clarification content in the conversation, not the status card', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'generation_session',
          status: 'needs_clarification',
          next_actions: ['answer_clarification'],
          kb_name: 'hardware',
          task_id: 'task-clarify-1',
          generation_session_id: 'session-clarify-1',
          question_id: 'scope',
          content: '请选择生成范围',
          options: ['当前发布版本', '最新上传版本'],
        }}
      />,
    );

    expect(markup).not.toContain('请选择生成范围');
    expect(markup).not.toContain('当前发布版本');
    expect(markup).not.toContain('最新上传版本');
  });

  it('keeps clarification interaction in the main conversation composer', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'generation_session',
          status: 'needs_clarification',
          next_actions: ['answer_clarification'],
          kb_name: 'hardware',
          task_id: 'task-private',
          generation_session_id: 'session-private',
          question_id: 'scope',
          content: '请选择生成范围',
          options: ['当前发布版本', '最新上传版本'],
        }}
      />,
    );

    expect(markup).not.toContain('请在下方对话输入框回复');
    expect(markup).not.toContain('当前发布版本');
    expect(markup).not.toContain('最新上传版本');
    expect(markup).not.toContain('textarea');
    expect(markup).not.toContain('提交回答');
    expect(markup).not.toContain('aria-label="澄清候选项"');
    expect(markup).not.toContain('href="/document-generation?kb=hardware&amp;session=session-private"');
    // Internal identifiers are never rendered as visible card copy.
    expect(markup).not.toContain('>task-private<');
    expect(markup).not.toContain('>session-private<');
    expect(markup).not.toContain('scope');
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

  it('renders live unit progress and the terminal execution error inline', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'failed',
          next_actions: [],
          kb_name: 'hardware',
          work_order_id: 'wo-failed',
          errorCode: 'document_job_failed',
          errorMessage: '执行范围不在白名单中',
          retryable: false,
          progress: {
            currentNode: 'fill_fields',
            completedUnits: 7,
            totalUnits: 21,
            percent: 33,
          },
        }}
      />,
    );

    expect(markup).toContain('已失败');
    expect(markup).toContain('字段生成进度 7 / 21');
    expect(markup).toContain('33%');
    expect(markup).toContain('执行范围不在白名单中');
    expect(markup).toContain('role="progressbar"');
  });

  it('explains blocked generation in the conversation and marks review candidates', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'blocked',
          next_actions: ['view_error', 'provide_value'],
          kb_name: 'ADAS',
          work_order_id: 'wo-blocked',
          errorMessage: '21 个字段尚未完成，无法发布',
          artifacts: [{ artifact_id: 'candidate-1', stage: 'review_candidate' }],
        }}
      />,
    );

    expect(markup).toContain('生成被阻止');
    expect(markup).toContain('请在下方对话中补充或确认缺失信息');
    expect(markup).toContain('候选文档（待审核）');
    expect(markup).not.toContain('0 / 210%');
  });

  it('does not call a blocked terminal node completed', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'blocked',
          next_actions: ['view_error'],
          kb_name: 'ADAS',
          work_order_id: 'wo-blocked-terminal',
          progress: {
            currentNode: 'complete',
            completedUnits: 0,
            totalUnits: 21,
            percent: 0,
          },
        }}
      />,
    );

    expect(markup).toContain('执行进度 0 / 21');
    expect(markup).not.toContain('生成完成 0 / 21');
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
