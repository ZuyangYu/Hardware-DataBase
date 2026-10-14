import { expect, test, type Page } from '@playwright/test';

async function accounts(page: Page) {
  const user = { username: 'account-admin', role: 'system_admin', department_id: 1, department_name: 'system' };
  const rows = [{ id: 1, ...user, is_active: true }];
  const submitted: Record<string, unknown>[] = [];
  let fail = false;
  await page.addInitScript(user => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'account-ui-test-only', user })), user);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/departments') return route.fulfill({ json: [{ id: 1, name: 'system' }, { id: 7, name: '研发部' }] });
    if (path === '/api/v1/users') {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        submitted.push(body);
        if (fail) return route.fulfill({ status: 400, json: { detail: '用户名已存在' } });
        const row = { id: 9, username: body.username, role: body.role, department_id: body.role === 'system_admin' ? 1 : body.department_id,
          department_name: body.role === 'system_admin' ? 'system' : '研发部', is_active: true };
        rows.push(row);
        return route.fulfill({ json: row });
      }
      return route.fulfill({ json: rows });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto('/admin/users');
  await expect(page.getByRole('button', { name: '创建账号', exact: true })).toBeVisible({ timeout: 5000 });
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  return { dialog: page.getByRole('dialog', { name: '创建账号' }), submitted, reject: () => { fail = true; } };
}

test('administrator creates another administrator without a business department', async ({ page }) => {
  const fixture = await accounts(page);
  const dialog = fixture.dialog;
  await expect(dialog.getByLabel('账号角色', { exact: true })).toContainText('员工');
  await dialog.getByLabel('业务部门', { exact: true }).click();
  await page.getByRole('option', { name: '研发部', exact: true }).click();
  await dialog.getByLabel('账号角色', { exact: true }).click();
  await page.getByRole('option', { name: '系统管理员', exact: true }).click();
  await expect(dialog.getByLabel('业务部门', { exact: true })).toHaveCount(0);
  await expect(dialog).toContainText('知识库正文');
  await dialog.getByLabel('用户名', { exact: true }).fill('support-admin');
  await dialog.getByLabel('密码', { exact: true }).fill('BrowserAccountTest123!');
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole('row').filter({ hasText: 'support-admin' })).toContainText('系统管理员');
  expect(fixture.submitted).toEqual([{ username: 'support-admin', password: 'BrowserAccountTest123!', role: 'system_admin', department_id: null }]);
});

test('employee creation still requires a department and excludes the system department', async ({ page }) => {
  const fixture = await accounts(page);
  const dialog = fixture.dialog;
  await dialog.getByLabel('用户名', { exact: true }).fill('new-employee');
  await dialog.getByLabel('密码', { exact: true }).fill('BrowserAccountTest123!');
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await expect(page.getByText('请选择业务部门', { exact: true })).toBeVisible();
  expect(fixture.submitted).toEqual([]);
  await dialog.getByLabel('业务部门', { exact: true }).click();
  await expect(page.getByRole('option', { name: 'system', exact: true })).toHaveCount(0);
  await page.getByRole('option', { name: '研发部', exact: true }).click();
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(fixture.submitted).toEqual([{ username: 'new-employee', password: 'BrowserAccountTest123!', role: 'employee', department_id: 7 }]);
});

test('creation errors retain the form and reopening starts a fresh employee account', async ({ page }) => {
  const fixture = await accounts(page);
  fixture.reject();
  const dialog = fixture.dialog;
  await dialog.getByLabel('账号角色', { exact: true }).click();
  await page.getByRole('option', { name: '系统管理员', exact: true }).click();
  await dialog.getByLabel('用户名', { exact: true }).fill('existing-admin');
  await dialog.getByLabel('密码', { exact: true }).fill('BrowserAccountTest123!');
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await expect(page.getByText('用户名已存在', { exact: true })).toBeVisible();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel('账号角色', { exact: true })).toContainText('系统管理员');
  await expect(dialog.getByLabel('用户名', { exact: true })).toHaveValue('existing-admin');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '创建账号', exact: true }).click();
  await expect(dialog.getByLabel('账号角色', { exact: true })).toContainText('员工');
  await expect(dialog.getByLabel('用户名', { exact: true })).toHaveValue('');
  await expect(dialog.getByLabel('密码', { exact: true })).toHaveValue('');
});
