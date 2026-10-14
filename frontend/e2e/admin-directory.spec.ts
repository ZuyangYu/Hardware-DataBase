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
  // 成员行三列对齐:角色列起点一致,且与名字列保持间距(不再挤成一团)
  const memberRows = departmentRow.locator('[id^="department-members-"] > div');
  await expect(memberRows).toHaveCount(2);
  const nameBox = await memberRows.nth(0).locator('span').nth(0).boundingBox();
  const roleBoxes = await Promise.all([0, 1].map((i) => memberRows.nth(i).locator('span').nth(1).boundingBox()));
  expect(Math.round(roleBoxes[0]!.x)).toBe(Math.round(roleBoxes[1]!.x));
  expect(Math.round(roleBoxes[0]!.x - (nameBox!.x + nameBox!.width))).toBeGreaterThanOrEqual(10);
});

test('user management filters users by username and department', async ({ page }) => {
  await installMocks(page);
  await page.goto('/admin/users');
  const search = page.getByPlaceholder('查找用户名、部门或角色');
  await expect(search).toBeVisible();
  await search.fill('alice');
  await expect(page.getByRole('row').filter({ hasText: 'alice' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'bob' })).toHaveCount(0);
  // 计数提示应紧跟搜索框(不再被推到行尾)
  const hint = page.getByText('找到 1 个用户', { exact: true });
  await expect(hint).toBeVisible();
  const inputBox = await search.boundingBox();
  const hintBox = await hint.boundingBox();
  expect(Math.round(hintBox!.x - (inputBox!.x + inputBox!.width))).toBeLessThanOrEqual(24);
  await search.fill('研发部');
  await expect(page.getByRole('row').filter({ hasText: 'alice' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'bob' })).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: 'carol' })).toHaveCount(0);
});
