import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import ExportMenu, {
  exportFormatFromValue,
  exportJobStatusLabel,
  visibleExportFormats,
} from './ExportMenu';

describe('ExportMenu', () => {
  it('renders the 导出对话 entry and keeps the format chooser behind a click', () => {
    const onExport = vi.fn();
    const markup = renderToStaticMarkup(<ExportMenu disabled={false} onExport={onExport} />);

    expect(markup).toContain('导出对话');
    expect(markup).not.toContain('选择导出格式');
    expect(onExport).not.toHaveBeenCalled();
  });

  it('offers only released formats', () => {
    expect(visibleExportFormats().map((item) => item.value)).toEqual([
      'md',
      'xlsx',
      'docx',
      'pdf',
      'pptx',
    ]);
    expect(exportFormatFromValue('xlsx')).toBe('xlsx');
    expect(exportFormatFromValue('docx')).toBe('docx');
    expect(exportFormatFromValue('csv')).toBeNull();
  });

  it('maps durable job states to actionable Chinese labels', () => {
    expect(exportJobStatusLabel('queued')).toBe('排队中');
    expect(exportJobStatusLabel('running')).toBe('生成中');
    expect(exportJobStatusLabel('succeeded')).toBe('可下载');
    expect(exportJobStatusLabel('failed')).toBe('生成失败');
  });

  it('renders the server capability subset when rollout flags hide formats', () => {
    expect(visibleExportFormats(['md', 'pdf']).map((item) => item.label)).toEqual([
      'Markdown',
      'PDF',
    ]);
  });
});
