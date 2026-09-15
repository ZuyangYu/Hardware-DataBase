import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import type { MessageView } from '@/api/types';
import MessageBubble from './MessageBubble';

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
