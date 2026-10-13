import { expect, test, type Page } from '@playwright/test';

const employee = { username: 'help-reader', role: 'employee', department_id: 1, department_name: '示例部门' };
const admin = { username: 'help-admin', role: 'system_admin', department_id: null, department_name: null };

async function authenticate(page: Page, user = employee as typeof employee | typeof admin) {
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'help-test-session', user: sessionUser }));
  }, user);
  await page.context().route('**/api/v1/**', (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/memory-settings') return route.fulfill({ json: {
      opt_in: false, policy_version: 'help-example', revoke_generation: 0, updated_at: '2026-10-13T00:00:00Z',
    } });
    if (path === '/api/v1/memory-consents' || path === '/api/v1/memories') {
      return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } });
    }
    if (path === '/api/v1/exports/formats') return route.fulfill({ json: { formats: ['md', 'pdf'] } });
    if (path.endsWith('/wiki/status')) return route.fulfill({ json: { status: 'idle' } });
    return route.fulfill({ json: [] });
  });
}

for (const user of [employee, admin]) {
  test(`${user.role}: 使用帮助入口在展开和收起侧栏均可用，并在新标签页阅读`, async ({ page, context }) => {
    await authenticate(page, user);
    await page.goto('/admin/governance');
    const link = page.getByRole('link', { name: '使用帮助', exact: true });
    await expect(link).toBeVisible();
    await page.getByRole('button', { name: '收起边栏' }).click();
    await expect(link).toBeVisible();
    const popup = context.waitForEvent('page');
    await link.click();
    const help = await popup;
    await expect(help).toHaveURL(/\/help$/);
    await expect(help.getByRole('heading', { name: '使用帮助', exact: true })).toBeVisible();
    await expect(help.getByRole('navigation', { name: '手册目录' })).toBeVisible();
    await expect(help.getByRole('link', { name: '下载完整手册（PDF）' })).toBeVisible();
    await expect(help.getByRole('button', { name: '打开个人菜单' })).toHaveCount(0);
    await expect(help.getByRole('button', { name: '治理与运营', exact: true })).toHaveCount(0);
    await expect(help.getByRole('link', { name: '返回系统', exact: true })).toHaveAttribute('href', '/');
    await expect(page).toHaveURL(/\/admin\/governance$/);
    await help.getByRole('link', { name: '返回系统', exact: true }).click();
    await expect(help).toHaveURL(user.role === 'system_admin' ? /\/admin\/governance$/ : /\/assets$/);
    await expect(help.getByRole('button', { name: '打开个人菜单' })).toBeVisible();
    await help.close();
  });
}

test('目录跳转和直接刷新均定位到章节，无效锚点回到手册开头', async ({ page }) => {
  await authenticate(page);
  await page.goto('/help');
  await page.getByRole('navigation', { name: '手册目录' }).getByRole('link', { name: '7 通用对话与文档产物', exact: true }).click();
  await expect(page).toHaveURL(/#general-chat$/);
  await expect(page.getByRole('heading', { name: '7 通用对话与文档产物', exact: true })).toBeInViewport();
  await page.reload();
  await expect(page.getByRole('heading', { name: '7 通用对话与文档产物', exact: true })).toBeInViewport();
  await page.goto('/help#missing-chapter');
  await expect(page.getByRole('heading', { name: '使用帮助', exact: true })).toBeInViewport();
});

test('截图支持键盘放大，Escape 关闭后恢复焦点', async ({ page }) => {
  await authenticate(page);
  await page.goto('/help#roles');
  const trigger = page.getByRole('button', { name: '放大图片：图 01 员工工作台与知识库入口' });
  await trigger.focus();
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('img')).toBeVisible();
  await expect.poll(() => dialog.getByRole('img').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test('完整手册下载返回真实 PDF，正文图片资源可读取', async ({ page }) => {
  await authenticate(page);
  await page.goto('/help');
  const pdf = page.getByRole('link', { name: '下载完整手册（PDF）' });
  const downloadPromise = page.waitForEvent('download');
  await pdf.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('Hardware_DataBase_observability_图文用户使用手册_V2.2.pdf');
  expect(await download.failure()).toBeNull();
  const response = await page.request.get((await pdf.getAttribute('href'))!);
  expect(response.headers()['content-type']).toContain('application/pdf');
  const bytes = await response.body();
  expect(bytes.subarray(0, 5).toString()).toBe('%PDF-');
  expect(bytes.length).toBeGreaterThan(10_000);
  const image = page.locator('main img').first();
  const imageResponse = await page.request.get((await image.getAttribute('src'))!);
  expect(imageResponse.ok()).toBe(true);
  expect(imageResponse.headers()['content-type']).toContain('image/');
});

test('从对话打开帮助后原页面草稿仍保留', async ({ page, context }) => {
  await authenticate(page);
  await page.goto('/chat');
  const input = page.locator('textarea').first();
  await input.fill('尚未发送的操作草稿');
  const popup = context.waitForEvent('page');
  await page.getByRole('link', { name: '使用帮助', exact: true }).click();
  const help = await popup;
  await expect(help.getByRole('heading', { name: '使用帮助', exact: true })).toBeVisible();
  await help.close();
  await expect(input).toHaveValue('尚未发送的操作草稿');
  await expect(page).toHaveURL(/\/chat$/);
});

test('章节加载失败提供重新加载入口，恢复后可阅读', async ({ page, context }) => {
  await authenticate(page);
  let blocked = false;
  await context.route(/\/(?:src\/content\/manual\/chapters\/03-login\.md|assets\/03-login-[^/]+\.js)(?:\?.*)?$/, (route) => {
    if (!blocked) { blocked = true; return route.abort('failed'); }
    return route.continue();
  });
  await page.goto('/help');
  await expect(page.getByRole('alert')).toContainText('手册暂时无法加载');
  await page.getByRole('button', { name: '重新加载', exact: true }).click();
  await expect(page.getByRole('heading', { name: '3 登录与首次使用', exact: true })).toBeVisible();
});

test('未登录访问帮助时使用现有登录入口', async ({ page }) => {
  await page.goto('/help');
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
  await expect(page.getByRole('navigation', { name: '手册目录' })).toHaveCount(0);
});

test('搜索标题和正文可定位到具体小节，刷新保留定位', async ({ page }) => {
  await authenticate(page);
  await page.goto('/help');
  const search = page.getByRole('searchbox', { name: '搜索使用手册' });
  await search.fill('修改密码');
  const results = page.getByRole('region', { name: '手册搜索结果' });
  const result = results.getByRole('link').filter({ hasText: '修改密码' }).first();
  await expect(result).toBeVisible();
  await result.click();
  await expect(page).toHaveURL(/#login--section-2$/);
  await expect(page.getByRole('heading', { name: '修改密码', exact: true })).toBeInViewport();
  await expect(results).toBeHidden();
  await page.reload();
  await expect(page.getByRole('heading', { name: '修改密码', exact: true })).toBeInViewport();
  await search.fill('部分可读');
  await results.getByRole('link').filter({ hasText: '添加会话附件' }).click();
  await expect(page.getByRole('heading', { name: '添加会话附件', exact: true })).toBeInViewport();
});

test('搜索支持键盘定位、无结果提示及清除后继续目录阅读', async ({ page }) => {
  await authenticate(page);
  await page.goto('/help');
  const search = page.getByRole('searchbox', { name: '搜索使用手册' });
  await expect(search).toBeEnabled();
  await page.keyboard.press('Control+k');
  await expect(search).toBeFocused();
  await search.fill('不存在的手册词条');
  await expect(page.getByRole('region', { name: '手册搜索结果' })).toContainText('没有找到相关内容');
  await page.getByRole('button', { name: '清除搜索', exact: true }).click();
  await expect(search).toHaveValue('');
  await expect(page.getByRole('navigation', { name: '手册目录' })).toBeVisible();
  await search.fill('导出本轮对话');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { name: '导出本轮对话', exact: true })).toBeInViewport();
  await search.fill('密码');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('region', { name: '手册搜索结果' })).toBeHidden();
});

test('独立手册阅读不请求知识库列表', async ({ page }) => {
  await authenticate(page);
  const requests: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/v1/kbs') requests.push(request.url());
  });
  await page.goto('/help');
  await expect(page.getByRole('heading', { name: '1 使用前准备', exact: true })).toBeVisible();
  expect(requests).toEqual([]);
});

test('离开已定位位置后，重复提交相同搜索仍会定位', async ({ page }) => {
  await authenticate(page);
  await page.goto('/help');
  const search = page.getByRole('searchbox', { name: '搜索使用手册' });
  await search.fill('修改密码');
  await page.keyboard.press('Enter');
  const heading = page.getByRole('heading', { name: '修改密码', exact: true });
  await expect(heading).toBeInViewport();
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(heading).not.toBeInViewport();
  await search.focus();
  await page.keyboard.press('Enter');
  await expect(heading).toBeInViewport();
});
