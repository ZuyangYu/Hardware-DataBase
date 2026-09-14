import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import ExportMenu, { exportFormatFromValue, exportJobStatusLabel } from './ExportMenu';

describe('ExportMenu', () => {
  it('offers only released formats and reports the selected format', () => {
    const onExport = vi.fn();
    const markup = renderToStaticMarkup(<ExportMenu disabled={false} onExport={onExport} />);

    expect(markup).toContain('导出结果');
    expect(markup).toContain('Markdown');
    expect(markup).toContain('Excel');
    expect(markup).toContain('Word');
    expect(markup).toContain('PDF');
    expect(markup).toContain('PowerPoint');

    expect(exportFormatFromValue('xlsx')).toBe('xlsx');
    expect(exportFormatFromValue('docx')).toBe('docx');
    expect(onExport).not.toHaveBeenCalled();
  });

  it('maps durable job states to actionable Chinese labels', () => {
    expect(exportJobStatusLabel('queued')).toBe('排队中');
    expect(exportJobStatusLabel('running')).toBe('生成中');
    expect(exportJobStatusLabel('succeeded')).toBe('可下载');
    expect(exportJobStatusLabel('failed')).toBe('生成失败');
  });

  it('renders the server capability subset when rollout flags hide formats', () => {
    const markup = renderToStaticMarkup(
      <ExportMenu formats={['md', 'pdf']} onExport={() => undefined} />,
    );

    expect(markup).toContain('Markdown');
    expect(markup).toContain('PDF');
    expect(markup).not.toContain('Excel');
    expect(markup).not.toContain('PowerPoint');
  });
});
