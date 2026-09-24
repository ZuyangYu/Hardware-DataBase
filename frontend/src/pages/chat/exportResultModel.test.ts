import { describe, expect, it } from 'vitest';

import type { ExportJobView } from '@/api/types';
import {
  buildExportRequest,
  exportFormatLabel,
  isExportTerminal,
  mergeExportJob,
  requestedExportFormats,
} from './exportResultModel';

const job: ExportJobView = {
  export_job_id: 'export-1',
  snapshot_id: 'snapshot-1',
  session_id: 7,
  format: 'md',
  content_shape: 'report',
  status: 'queued',
  attempt: 0,
  error_message: '',
  artifact: null,
  created_at: '2026-09-02T00:00:00Z',
  updated_at: '2026-09-02T00:00:00Z',
  completed_at: null,
  tenant_id: 'default',
  department_id: null,
  knowledge_base_name: 'shared',
};

describe('export result model', () => {
  it('builds a source-bound export request with the correct content shape', () => {
    expect(buildExportRequest('turn-42', 'xlsx', 'request-1')).toEqual({
      source_ref: { kind: 'turn', id: 'turn-42' },
      formats: ['xlsx'],
      content_shape: 'data',
      client_request_id: 'request-1',
    });
    expect(buildExportRequest('turn-42', 'pptx', 'request-2').options).toEqual({
      theme: 'light',
      include_charts: true,
    });
  });

  it('keeps user-facing format names stable across the task center and chat', () => {
    expect(exportFormatLabel('docx')).toBe('Word');
    expect(exportFormatLabel('pdf')).toBe('PDF');
    expect(exportFormatLabel('pptx')).toBe('PowerPoint');
  });

  it('detects explicit output requests without triggering on ordinary format mentions', () => {
    expect(requestedExportFormats('请把这次知识库检索结果导出成 Excel 和 PDF')).toEqual(['xlsx', 'pdf']);
    expect(requestedExportFormats('请输出 Markdown、Word、PPT 格式的报告')).toEqual(['md', 'docx', 'pptx']);
    expect(requestedExportFormats('PDF 文件中这个芯片的额定电压是多少？')).toEqual([]);
  });

  it('recognizes terminal export states', () => {
    expect(isExportTerminal('succeeded')).toBe(true);
    expect(isExportTerminal('failed')).toBe(true);
    expect(isExportTerminal('queued')).toBe(false);
  });

  it('replaces a durable job update without duplicating the card', () => {
    const completed = { ...job, status: 'succeeded' as const, attempt: 1 };
    expect(mergeExportJob([job], completed)).toEqual([completed]);
  });
});
