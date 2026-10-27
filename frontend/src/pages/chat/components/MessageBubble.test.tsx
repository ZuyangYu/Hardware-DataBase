import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { MessageView } from '@/api/types';
import MessageBubble from './MessageBubble';

describe('complex turn meta panels', () => {
  const baseMessage: MessageView = {
    id: 42,
    session_id: 7,
    turn_id: 'turn-42',
    role: 'assistant',
    content: '候选 BOM [1]',
    created_at: '2026-10-26T00:00:00Z',
  };

  it('renders the agent plan with progress', () => {
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={{ ...baseMessage, content: '' }}
        streaming
        streamingText="候选 BOM [1]"
        plan={{
          revision: 1,
          items: [
            { content: '读取上一代 BOM', status: 'completed' },
            { content: '检索知识库约束', status: 'in_progress' },
            { content: '输出推荐表', status: 'pending' },
          ],
        }}
      />,
    );
    expect(html).toContain('执行计划');
    expect(html).toContain('1/3');
    expect(html).toContain('读取上一代 BOM');
    expect(html).toContain('进行中');
  });

  it('renders the budget-exhausted partial result with continue actions', () => {
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={{
          ...baseMessage,
          budget: {
            status: 'exhausted',
            exhausted: true,
            tool_calls: 41,
            evidence_count: 41,
            partial_answer: true,
            recursion_limit: 72,
            complexity: { level: 'complex', score: 12 },
            context_compressed: { evidence_count: 41, compressed_bytes: 20480 },
          },
        }}
        onContinue={() => undefined}
        onNarrowScope={() => undefined}
      />,
    );
    expect(html).toContain('本轮执行预算已用完');
    expect(html).toContain('41 次工具调用');
    expect(html).toContain('继续完成');
    expect(html).toContain('缩小范围');
    expect(html).toContain('20 KB');
  });

  it('renders a partial delivery that did not exhaust the loop budget', () => {
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={{
          ...baseMessage,
          budget: {
            status: 'partial',
            exhausted: false,
            partial_answer: true,
            tool_calls: 12,
            evidence_count: 20,
          },
        }}
        onContinue={() => undefined}
      />,
    );
    expect(html).toContain('本轮为部分交付');
    expect(html).toContain('已基于已有证据生成本轮可交付的部分结果');
    expect(html).toContain('继续完成');
  });

  it('renders a context-window notice without treating it as a budget failure', () => {
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={{
          ...baseMessage,
          budget: {
            status: 'ok',
            exhausted: false,
            context_window: {
              max_prompt_tokens: 58000,
              window_tokens: 65536,
              threshold_tokens: 55705,
            },
          },
        }}
      />,
    );
    expect(html).toContain('上下文预算');
    expect(html).toContain('58000 tokens');
    expect(html).not.toContain('本轮执行预算已用完');
  });

  it('renders verification checks and gaps, including model checks', () => {
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={{
          ...baseMessage,
          verification: {
            status: 'partial',
            checks: [
              { key: 'answer_present', label: '回答非空', status: 'pass', detail: '1,949 字' },
              {
                key: 'task_completion',
                label: '五类覆盖',
                status: 'warn',
                detail: '连接器待确认',
                source: 'model_rubric',
              },
            ],
            gaps: ['缺少车规密封连接器型号'],
            budget_exhausted: true,
          },
        }}
      />,
    );
    expect(html).toContain('系统校验');
    expect(html).toContain('部分完成');
    expect(html).toContain('五类覆盖');
    expect(html).toContain('模型');
    expect(html).toContain('缺少车规密封连接器型号');
  });

  it('renders a read-only research subtask without raw context', () => {
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={{ ...baseMessage, content: '' }}
        streaming
        streamingText="候选 BOM [1]"
        subagents={[{
          name: 'research',
          description: '检索 AAA 知识库中的电源与温度约束',
          status: 'completed',
          result_chars: 420,
        }]}
      />,
    );
    expect(html).toContain('检索子任务');
    expect(html).toContain('research');
    expect(html).toContain('已完成');
  });

  it('does not render meta panels for a simple answer', () => {
    const html = renderToStaticMarkup(<MessageBubble msg={baseMessage} />);
    expect(html).not.toContain('执行计划');
    expect(html).not.toContain('系统校验');
    expect(html).not.toContain('本轮执行预算已用完');
  });
});

describe('assistant result exports', () => {
  it('shows the export action only when the assistant message has a durable turn', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      created_at: '2026-09-02T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <MessageBubble msg={message} onExport={() => undefined} exportJobs={[]} />,
    );
    expect(html).toContain('导出对话');
    expect(html).not.toContain('选择导出格式');
  });

  it('renders 创建个人记忆 alongside 导出对话 in the answer action row', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      created_at: '2026-09-02T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={message}
        onCreateMemory={() => undefined}
        memorySourceMessageIds={[41, 42]}
        onExport={() => undefined}
        exportJobs={[]}
      />,
    );
    expect(html).toContain('创建个人记忆');
    expect(html).toContain('导出对话');
  });

  it('renders 记入项目记忆 when a project handler is provided', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      created_at: '2026-09-02T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={message}
        onCreateProjectMemory={() => undefined}
        memorySourceMessageIds={[41, 42]}
      />,
    );
    expect(html).toContain('记入项目记忆');
  });

  it('hides 记入项目记忆 when no project handler is provided', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      created_at: '2026-09-02T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <MessageBubble msg={message} memorySourceMessageIds={[41, 42]} />,
    );
    expect(html).not.toContain('记入项目记忆');
  });

  it('places the export entry below the answer and above the retrieval overview', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      footer: '检索来源：spec.docx',
      created_at: '2026-09-02T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <MessageBubble msg={message} onExport={() => undefined} exportJobs={[]} />,
    );
    const exportIndex = html.indexOf('导出对话');
    const overviewIndex = html.indexOf('检索概览');
    expect(exportIndex).toBeGreaterThan(html.indexOf('检索结果'));
    expect(overviewIndex).toBeGreaterThan(exportIndex);
  });

  it('renders a downloadable artifact state from the durable export job', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      created_at: '2026-09-02T00:00:00Z',
    };
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={message}
        onExport={() => undefined}
        exportJobs={[{
          export_job_id: 'export-1',
          snapshot_id: 'snapshot-1',
          session_id: 7,
          format: 'md',
          content_shape: 'report',
          status: 'succeeded',
          attempt: 1,
          error_message: '',
          artifact: {
            artifact_id: 'artifact-1',
            export_job_id: 'export-1',
            session_id: 7,
            format: 'md',
            filename: 'result.md',
            mime_type: 'text/markdown; charset=utf-8',
            size: 20,
            sha256: 'hash',
            preview: {},
            created_at: '2026-09-02T00:00:00Z',
            tenant_id: 'default',
            department_id: null,
            knowledge_base_name: 'shared',
            preview_url: '/api/v1/artifacts/artifact-1/preview',
            download_url: '/api/v1/artifacts/artifact-1/download',
          },
          created_at: '2026-09-02T00:00:00Z',
          updated_at: '2026-09-02T00:00:00Z',
          completed_at: '2026-09-02T00:00:01Z',
          tenant_id: 'default',
          department_id: null,
          knowledge_base_name: 'shared',
        }]}
        onDownload={() => undefined}
      />,
    );
    expect(html).toContain('可下载');
    expect(html).toContain('下载 result.md');
  });

  it('renders a bounded authenticated artifact preview when loaded', () => {
    const message: MessageView = {
      id: 42,
      session_id: 7,
      turn_id: 'turn-42',
      role: 'assistant',
      content: '检索结果',
      created_at: '2026-09-02T00:00:00Z',
    };
    const job = {
      export_job_id: 'export-1',
      snapshot_id: 'snapshot-1',
      session_id: 7,
      format: 'xlsx' as const,
      content_shape: 'data' as const,
      status: 'succeeded' as const,
      attempt: 1,
      error_message: '',
      artifact: {
        artifact_id: 'artifact-1',
        export_job_id: 'export-1',
        session_id: 7,
        format: 'xlsx' as const,
        filename: 'result.xlsx',
        mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        size: 20,
        sha256: 'hash',
        preview: {},
        created_at: '2026-09-02T00:00:00Z',
        tenant_id: 'default',
        department_id: null,
        knowledge_base_name: 'shared',
        preview_url: '/api/v1/artifacts/artifact-1/preview',
        download_url: '/api/v1/artifacts/artifact-1/download',
      },
      created_at: '2026-09-02T00:00:00Z',
      updated_at: '2026-09-02T00:00:00Z',
      completed_at: '2026-09-02T00:00:01Z',
      tenant_id: 'default',
      department_id: null,
      knowledge_base_name: 'shared',
    };
    const html = renderToStaticMarkup(
      <MessageBubble
        msg={message}
        onExport={() => undefined}
        exportJobs={[job]}
        onPreview={() => undefined}
        exportPreviews={{
          'export-1': {
            ...job.artifact,
            preview: { format: 'xlsx', sheets: [{ name: '结果', rows: [['型号', 'U1']] }] },
          },
        }}
      />,
    );
    expect(html).toContain('刷新预览');
    expect(html).toContain('工作表：结果（1 行）');
  });
});
