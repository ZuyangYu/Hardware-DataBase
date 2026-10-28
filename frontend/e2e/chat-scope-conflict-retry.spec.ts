import { expect, test, type Page } from '@playwright/test';

const user = { username: 'scope-conflict-user', role: 'employee', department_id: 7, department_name: '研发部' };

async function installChat(page: Page) {
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'scope-conflict-test', user: sessionUser }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/knowledge-bases' || path === '/api/v1/kbs') {
      return route.fulfill({ json: [{ name: 'hardware', kb_id: 101, department_id: 7, permission: 'write', registered: true }] });
    }
    if (path === '/api/v1/conversations' && route.request().method() === 'GET') {
      return route.fulfill({ json: [{ id: 42, title: '跨库设计', kb_name: 'hardware', user_id: user.username }] });
    }
    if (path === '/api/v1/conversations/42/messages') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: { enabled: false, items: [] } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [], total: 0 } });
    if (path === '/api/v1/exports/formats') return route.fulfill({ json: [] });
    return route.fulfill({ json: [] });
  });
  await page.goto('/chat?kb=hardware');
  await expect(page.locator('textarea').last()).toBeVisible();
}

test('选库范围冲突保留输入草稿，重发使用新会话', async ({ page }) => {
  await installChat(page);
  await page.route('**/conversations/42/turns', (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 409, json: { detail: 'knowledge_scope_changed' } }) : route.fallback());
  const created: Array<Record<string, unknown>> = [];
  await page.route('**/api/v1/conversations', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    created.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: 43, kb_name: 'hardware', title: '新范围任务', user_id: user.username } });
  });
  await page.route('**/conversations/43/turns', (route) => route.fulfill({ status: 503, json: { detail: 'test retry reached new session' } }));
  const composer = page.locator('textarea').last();
  await composer.fill('保留这段跨库设计草稿');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(composer).toHaveValue('保留这段跨库设计草稿');
  const next = page.waitForRequest((request) => request.method() === 'POST' && request.url().includes('/conversations/43/turns'));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await next).postDataJSON().query).toBe('保留这段跨库设计草稿');
  expect(created).toHaveLength(1);
  expect(created[0]).toMatchObject({ kb_name: 'hardware' });
});
