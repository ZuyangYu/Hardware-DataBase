import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import PasswordInput from './PasswordInput';

describe('PasswordInput', () => {
  it('默认掩码渲染,并提供显示密码的切换按钮', () => {
    const html = renderToStaticMarkup(
      <PasswordInput defaultValue="secret" placeholder="初始密码" />,
    );
    expect(html).toContain('type="password"');
    expect(html).toContain('aria-label="显示密码"');
    expect(html).not.toContain('type="text"');
  });
});
