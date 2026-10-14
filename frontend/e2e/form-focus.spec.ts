import { expect, test, type Page } from '@playwright/test';
import { expectVisibleFormFocus } from './helpers/form-focus';

async function installMocks(page: Page, role: 'employee' | 'system_admin' = 'system_admin') {
  const user = { username: 'focus-reader', role, department_id: role === 'employee' ? 7 : null, department_name: '研发部' };
  await page.addInitScript(user => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'focus-test-only', user })), user);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/departments') return route.fulfill({ json: [{ id: 7, name: '研发部' }] });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: { enabled: false, write_enabled: false, prerequisites_met: false, prerequisites_reason: '', items: [] } });
    if (path === '/api/v1/memory-settings') return route.fulfill({ json: { opt_in: false, policy_version: 'focus-test', revoke_generation: 0, updated_at: '2026-10-14T00:00:00Z' } });
    if (path === '/api/v1/memories' || path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } });
    return route.fulfill({ json: [] });
  });
}

test('shared input and select show focus when registering an employee', async ({ page }) => {
  await installMocks(page);
  await page.goto('/admin/users');
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '创建账号' });
  await expectVisibleFormFocus(dialog.getByPlaceholder('登录用户名'), 'mouse');
  await expectVisibleFormFocus(dialog.getByPlaceholder('登录用户名'), 'keyboard');
  await expectVisibleFormFocus(dialog.getByPlaceholder('初始密码'), 'mouse');
  await expectVisibleFormFocus(dialog.getByLabel('业务部门', { exact: true }), 'keyboard');
  await expect(dialog.getByRole('button', { name: '创建', exact: true })).toBeVisible();
});

test('shared form action buttons show complete keyboard focus', async ({ page }) => {
  await installMocks(page);
  await page.goto('/admin/users');
  await expectVisibleFormFocus(page.getByRole('button', { name: '创建账号', exact: true }), 'keyboard');
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '创建账号' });
  await expectVisibleFormFocus(dialog.getByRole('button', { name: '取消', exact: true }), 'keyboard');
  await expectVisibleFormFocus(dialog.getByRole('button', { name: '创建', exact: true }), 'keyboard');
});

test('memory status and project knowledge selectors show keyboard focus', async ({ page }) => {
  await installMocks(page, 'employee');
  await page.goto('/memory');
  await expect(page.getByRole('heading', { name: '长期记忆', exact: true })).toBeVisible();
  await expectVisibleFormFocus(page.getByLabel('记忆状态', { exact: true }), 'keyboard');
  await page.getByRole('button', { name: '项目记忆', exact: true }).click();
  await expectVisibleFormFocus(page.getByLabel('选择知识库', { exact: true }), 'keyboard');
});

test('shared input and textarea show complete focus inside the MCP registration dialog', async ({ page }) => {
  await installMocks(page);
  await page.goto('/admin/plugins');
  await page.getByRole('button', { name: '登记服务', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '登记 MCP 服务' });
  await expectVisibleFormFocus(dialog.getByLabel('服务名称', { exact: true }), 'mouse');
  await expectVisibleFormFocus(dialog.getByPlaceholder('Authorization: Bearer …'), 'mouse');
  await expectVisibleFormFocus(dialog.getByPlaceholder('Authorization: Bearer …'), 'keyboard');
});
