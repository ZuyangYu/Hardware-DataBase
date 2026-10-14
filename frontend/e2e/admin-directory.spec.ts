import { expect, test, type Page } from '@playwright/test';

const users = [
  { id: 1, username: 'account-admin', role: 'system_admin', department_id: 1, department_name: 'system', is_active: true },
  { id: 7, username: 'alice', role: 'employee', department_id: 7, department_name: '研发部', is_active: true },
  { id: 8, username: 'bob', role: 'employee', department_id: 7, department_name: '研发部', is_active: false },
  { id: 9, username: 'carol', role: 'employee', department_id: 9, department_name: '测试部', is_active: true },
];

async function installMocks(page: Page) {
  const user = users[0];
  await page.addInitScript(value => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'directory-test-only', user: value }));
  }, user);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/departments') return route.fulfill({ json: [{ id: 1, name: 'system' }, { id: 7, name: '研发部' }, { id: 9, name: '测试部' }] });
    if (path === '/api/v1/users') return route.fulfill({ json: users });
    return route.fulfill({ json: [] });
  });
}

test('department names expand to show their members', async ({ page }) => {
  await installMocks(page);
  await page.goto('/admin/departments');
  const departmentRow = page.getByRole('row').filter({ hasText: '研发部' });
  await expect(departmentRow).toBeVisible();
  await expect(departmentRow.getByRole('button', { name: '研发部', exact: true })).toHaveAttribute('aria-expanded', 'false');
  await departmentRow.getByRole('button', { name: '研发部', exact: true }).click();
  await expect(departmentRow.getByRole('button', { name: '研发部', exact: true })).toHaveAttribute('aria-expanded', 'true');
  await expect(departmentRow).toContainText('alice');
  await expect(departmentRow).toContainText('bob');
  await expect(departmentRow).not.toContainText('carol');
});

test('user management filters users by username and department', async ({ page }) => {
  await installMocks(page);
  await page.goto('/admin/users');
  const search = page.getByPlaceholder('查找用户名、部门或角色');
  await expect(search).toBeVisible();
  await search.fill('alice');
  await expect(page.getByRole('row').filter({ hasText: 'alice' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'bob' })).toHaveCount(0);
  await search.fill('研发部');
  await expect(page.getByRole('row').filter({ hasText: 'alice' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'bob' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'carol' })).toHaveCount(0);
});
