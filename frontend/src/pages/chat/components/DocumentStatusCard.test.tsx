import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

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
  owner_user_id: 1,
  is_shared: false,
  is_owner: true,
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
  it('shows a conflict review entry point for a waiting work order', () => {
    const card = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 'needs_review',
      next_actions: ['review_document'],
      kb_name: 'shared',
      work_order_id: 'wo-1',
    }));

    const markup = renderToStaticMarkup(<DocumentStatusCard card={card!} />);

    expect(markup).toContain('data-conflict-review-panel="wo-1"');
  });

  it('renders work order card from document_card event without a workbench link', () => {
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
    expect(markup).not.toContain('前往工作台');
    expect(markup).not.toContain('/document-generation');
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
    expect(markup).toContain('按 EDF 实际位号生成');
    expect(markup).not.toContain('请直接回复“确认”');
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

  it('renders aggregated decisions and lets the user give a natural-language group instruction', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'blocked',
          next_actions: [],
          kb_name: 'ADAS',
          work_order_id: 'wo-grouped',
          gateReview: {
            reviewKind: 'icd_scope',
            status: 'pending',
            scopePendingCount: 20,
            scopeBlocking: false,
            scopeExceptions: [],
          },
          pendingDecisions: [
            {
              decision_id: 'decision:pin_exposure:X1900',
              group_key: 'X1900',
              kind: 'pin_exposure',
              title: '位号 X1900 待确认',
              options: ['include', 'exclude'],
              default_option: 'include',
              count: 15,
              blocking: false,
            },
            {
              decision_id: 'decision:pin_exposure:X1902',
              group_key: 'X1902',
              kind: 'pin_exposure',
              title: '位号 X1902 待确认',
              options: ['include', 'exclude'],
              default_option: 'include',
              count: 5,
              blocking: false,
            },
          ],
        }}
      />,
    );

    expect(markup).toContain('位号 X1900 待确认（15 项）');
    expect(markup).toContain('位号 X1902 待确认（5 项）');
    expect(markup).toContain('待处理：X1900（15 项）、X1902（5 项）');
    expect(markup).toContain('自然语言');
    expect(markup).toContain('X1900 全部包含');
    expect(markup).not.toContain('包含/排除 &lt;位号&gt;，继续生成');
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
    expect(markup).toContain('下载来源与决策记录（产物 a-1）');
    expect(markup).toContain('下载来源与决策记录（产物 a-2）');

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

  it('renders an attachment revision candidate with download and no work order', () => {
    const card = parseDocumentCardEvent(cardEvent({
      kind: 'attachment_revision',
      status: 'review_candidate',
      next_actions: [],
      kb_name: 'hardware',
      candidate_state: 'available',
      release_state: 'not_requested',
      content: '已创建附件修订候选；原附件未修改，可下载候选文件复核。',
      artifacts: [{
        artifact_id: 'att-child-1',
        stage: 'review_candidate',
        output_format: 'docx',
        download_url: '/api/v1/conversations/7/attachments/att-child-1/download',
      }],
    }));

    const markup = renderToStaticMarkup(<DocumentStatusCard card={card!} />);

    expect(markup).toContain('附件修订候选');
    expect(markup).toContain('候选已生成');
    expect(markup).toContain('候选附件');
    expect(markup).toContain('aria-label="下载 候选文档（待审核）"');
    expect(markup).toContain('下载 候选文档（待审核）（DOCX）');
    expect(markup).toContain('原附件未修改');
    expect(markup).not.toContain('工单');
  });

  it('shows attachment release review state as the next conversational step', () => {
    const pending = parseDocumentCardEvent(cardEvent({
      kind: 'attachment_revision',
      status: 'review_candidate',
      next_actions: [],
      kb_name: 'hardware',
      candidate_state: 'available',
      release_state: 'pending',
    }));
    const pendingMarkup = renderToStaticMarkup(
      <DocumentStatusCard card={pending!} />,
    );
    expect(pendingMarkup).toContain('data-attachment-release-state="pending"');
    expect(pendingMarkup).toContain('已提交正式发布审核');
    expect(pendingMarkup).toContain('候选仍可下载');

    const rejected = parseDocumentCardEvent(cardEvent({
      kind: 'attachment_revision',
      status: 'review_candidate',
      next_actions: [],
      kb_name: 'hardware',
      candidate_state: 'available',
      release_state: 'rejected',
    }));
    const rejectedMarkup = renderToStaticMarkup(
      <DocumentStatusCard card={rejected!} />,
    );
    expect(rejectedMarkup).toContain('data-attachment-release-state="rejected"');
    expect(rejectedMarkup).toContain('发布申请被拒绝');
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

    expect(markup).toContain('候选已生成（发布受阻）');
    expect(markup).toContain('候选文档已生成，可在上方下载');
    expect(markup).toContain('候选文档（待审核）');
    expect(markup).not.toContain('0 / 210%');
  });

  it('labels a blocked downloadable candidate as a release gate, not a failed generation', () => {
    const card = parseDocumentCardEvent(cardEvent({
      kind: 'work_order_status',
      status: 'blocked',
      next_actions: ['download_candidate', 'request_release'],
      kb_name: 'ADAS',
      work_order_id: 'wo-candidate-blocked',
      candidate_state: 'available',
      release_state: 'pending',
      artifacts: [{ artifact_id: 'candidate-2', stage: 'review_candidate' }],
    }));
    const markup = renderToStaticMarkup(<DocumentStatusCard card={card!} />);

    expect(markup).toContain('候选已生成（发布受阻）');
    expect(markup).toContain('候选文档已生成，可在上方下载');
    expect(markup).not.toContain('生成被阻止');
  });

  it('offers direct candidate download and an explicit release-review request', () => {
    const markup = renderToStaticMarkup(
      <DocumentStatusCard
        card={{
          kind: 'work_order_status',
          status: 'blocked',
          next_actions: ['download_candidate', 'request_release'],
          kb_name: 'ADAS',
          work_order_id: 'wo-direct-candidate',
          candidateState: 'available',
          releaseState: 'not_requested',
          artifacts: [{
            artifact_id: 'candidate-direct-1',
            stage: 'review_candidate',
            output_format: 'docx',
          }],
        }}
        onRequestRelease={() => undefined}
      />,
    );

    expect(markup).toContain('下载 候选文档（待审核）');
    expect(markup).toContain('申请正式发布审核');
    expect(markup).toContain('aria-label="下载 候选文档（待审核）（工单 wo-direct-candidate）"');
    expect(markup).toContain('aria-label="申请正式发布审核（工单 wo-direct-candidate）"');
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

});
