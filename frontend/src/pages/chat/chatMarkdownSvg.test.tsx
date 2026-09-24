import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { renderMarkdownBlocks } from './chatHelpers';

function render(markdown: string): string {
  return renderToStaticMarkup(<>{renderMarkdownBlocks(markdown)}</>);
}

describe('markdown SVG 代码块渲染', () => {
  it('completed svg fence becomes a figure instead of source code', () => {
    const html = render('```svg\n<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>\n```');
    expect(html).toContain('data-svg-figure');
    expect(html).not.toContain('rect width=');
  });

  it('unclosed svg fence shows a drawing placeholder (streaming)', () => {
    const html = render('```svg\n<svg viewBox="0 0 10 10">');
    expect(html).toContain('正在绘图');
    expect(html).not.toContain('data-svg-figure');
  });

  it('keeps other code fences as code blocks', () => {
    const html = render('```python\nprint("hi")\n```');
    expect(html).toContain('print');
    expect(html).not.toContain('data-svg-figure');
  });

  it('oversized svg falls back to source with a hint', () => {
    const big = `<svg>${'x'.repeat(200 * 1024)}</svg>`;
    const html = render('```svg\n' + big + '\n```');
    expect(html).toContain('已按源码显示');
    expect(html).toContain('data-svg-figure');
  });

  it('svg inside plain text without fence is untouched', () => {
    const html = render('这里提到 <svg> 标签但不在代码块里');
    expect(html).not.toContain('data-svg-figure');
  });
});
