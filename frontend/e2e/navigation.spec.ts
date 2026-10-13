import { expect, test } from '@playwright/test';

// 导航信息架构回归:只 mock API,不触碰真实用户、部门或治理数据。

const admin = { username: 'navigation-admin', role: 'system_admin', department_id: null, department_name: null };
const employee = { username: 'navigation-employee', role: 'employee', department_id: 47, department_name: 'ADAS' };

function installMocks(page: import('@playwright/test').Page, user: typeof admin | typeof employee) {
  return Promise.all([
    page.addInitScript((sessionUser) => {
      localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'navigation-regression-only', user: sessionUser }));
    }, user),
    page.route('**/api/v1/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/v1/whoami') return route.fulfill({ json: user });
      if (path === '/api/v1/quality/cases' || path === '/api/v1/quality/reports' || path === '/api/v1/kbs') {
        return route.fulfill({ json: [] });
      }
      if (path === '/api/v1/governance/kb-summaries') return route.fulfill({ json: [] });
      if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
      if (path === '/api/v1/memories') return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } });
      if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [] } });
      if (path === '/api/v1/memory-settings') return route.fulfill({ json: {
        opt_in: false, policy_version: 'navigation-test-v1', revoke_generation: 0, updated_at: '2026-10-13T00:00:00Z',
      } });
      if (path === '/api/v1/costs/summary') return route.fulfill({ json: {
        calls: 0, known_amounts: {}, tokens: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0 },
        unknown_cost_calls: 0, usage_coverage: null, price_coverage: null, logical_only_calls: 0,
        unfinished_calls: 0, unattributed_calls: 0, updated_at: null, scope: 'test', by_purpose: {},
      } });
      if (path === '/api/v1/costs/invocations') return route.fulfill({ json: [] });
      if (path === '/api/v1/costs/trends') return route.fulfill({ json: {
        bucket: 'day', timezone: 'UTC', summary: {
          calls: 0, known_amounts: {}, tokens: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0 },
          unknown_cost_calls: 0, usage_coverage: null, price_coverage: null, logical_only_calls: 0,
          unfinished_calls: 0, unattributed_calls: 0, updated_at: null, scope: 'test', by_purpose: {},
        }, buckets: [], rankings_by_currency: {},
      } });
      if (path === '/api/v1/costs/budgets') return route.fulfill({ json: [] });
      return route.fulfill({ status: 404, json: { detail: 'unexpected navigation request' } });
    }),
  ]).then(() => undefined);
}

test('system_admin sees only primary workspaces and admin secondary navigation', async ({ page }) => {
  await installMocks(page, admin);
  await page.setViewportSize({ width: 1440, height: 900 });
  // 使用无业务含义的 query 验证深链接不会被侧栏重构吞掉；`case` query
  // 会触发现有质量复核弹窗，不属于本导航回归范围。
  await page.goto('/admin/quality?from=navigation-test');
  await expect(page).toHaveURL(/\/admin\/quality\?from=navigation-test$/);

  await expect(page.getByRole('button', { name: '治理与运营', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '管理', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '员工管理' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '部门管理' })).toHaveCount(0);
  const secondary = page.getByRole('navigation', { name: '页面二级导航' });
  await expect(secondary.getByRole('link', { name: '质量工作台', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(secondary.getByRole('link', { name: '日志中心', exact: true })).toBeVisible();
  await expect(secondary.getByRole('link', { name: '成本与用量', exact: true })).toBeVisible();
  await expect(secondary.getByRole('link', { name: 'RAGAS 评估', exact: true })).toBeVisible();
  await expect(page.getByLabel('当前导航分组')).toHaveText('治理与运营');
});

test('employee sees compact primary navigation and permitted secondary links', async ({ page }) => {
  await installMocks(page, employee);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/admin/quality');

  await expect(page.getByRole('button', { name: '工作台' })).toBeVisible();
  await expect(page.getByRole('button', { name: '对话' })).toBeVisible();
  await expect(page.getByRole('button', { name: '个人扩展' })).toBeVisible();
  await expect(page.getByRole('button', { name: '治理与运营' })).toBeVisible();
  const secondary = page.getByRole('navigation', { name: '页面二级导航' });
  await expect(secondary.getByRole('link', { name: '质量工作台', exact: true })).toHaveAttribute('aria-current', 'page');
  await expect(secondary.getByRole('link', { name: '日志中心', exact: true })).toBeVisible();
  await expect(secondary.getByRole('link', { name: '成本与用量', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '员工管理' })).toHaveCount(0);
  await expect(secondary.getByRole('link', { name: 'RAGAS 评估', exact: true })).toHaveCount(0);
});

test('only the current primary destination has selected styling', async ({ page }) => {
  await installMocks(page, employee);
  await page.goto('/admin/quality');

  const selected = page.getByRole('button', { name: '治理与运营', exact: true });
  const unselected = page.getByRole('button', { name: '个人扩展', exact: true });
  await expect(selected).toHaveAttribute('aria-current', 'page');
  const selectedColor = await selected.evaluate((element) => getComputedStyle(element).backgroundColor);
  const unselectedColor = await unselected.evaluate((element) => getComputedStyle(element).backgroundColor);
  expect(selectedColor).not.toBe(unselectedColor);
});

test('collapsed sidebar keeps long-term memory in the personal menu', async ({ page }) => {
  await installMocks(page, employee);
  await page.goto('/admin/quality');
  await page.getByRole('button', { name: '收起边栏' }).click();
  await page.getByRole('button', { name: '打开个人菜单' }).click();
  await page.getByRole('menuitem', { name: '长期记忆' }).click();
  await expect(page).toHaveURL(/\/memory$/);
  await expect(page.getByRole('heading', { name: '长期记忆', exact: true })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: '允许个人记忆提炼' })).toBeVisible();
});

for (const collapsed of [false, true]) {
  test(`system_admin personal menu excludes long-term memory (${collapsed ? 'collapsed' : 'expanded'})`, async ({ page }) => {
    await installMocks(page, admin);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/admin/governance');
    if (collapsed) await page.getByRole('button', { name: '收起边栏' }).click();
    await page.getByRole('button', { name: '打开个人菜单' }).click();

    await expect(page.getByRole('menuitem', { name: '个人设置', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: '退出登录', exact: true })).toBeVisible();
    await expect(page.getByRole('menu').getByText('长期记忆', { exact: true })).toHaveCount(0);
  });
}

test('system_admin direct access to memory redirects without loading personal memory', async ({ page }) => {
  await installMocks(page, admin);
  const memoryRequests: string[] = [];
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (['/api/v1/memories', '/api/v1/memory-settings', '/api/v1/memory-consents'].includes(path)) {
      memoryRequests.push(path);
    }
  });
  await page.goto('/memory');

  await expect(page).toHaveURL(/\/admin\/governance$/);
  await expect(page.getByRole('heading', { name: '治理总览', exact: true })).toBeVisible();
  await expect(page.getByRole('checkbox', { name: '允许个人记忆提炼' })).toHaveCount(0);
  expect(memoryRequests).toEqual([]);
});

test('system_admin distinguishes platform extensions from personal extensions', async ({ page }) => {
  await installMocks(page, admin);
  await page.goto('/admin/plugins');

  await expect(page.getByRole('button', { name: '个人扩展', exact: true })).toHaveCount(0);
  const secondary = page.getByRole('navigation', { name: '页面二级导航' });
  await expect(secondary.getByRole('link', { name: '平台扩展', exact: true })).toHaveAttribute('aria-current', 'page');
});

test('employee direct access to system admin route remains protected', async ({ page }) => {
  await installMocks(page, employee);
  await page.goto('/admin/users');
  await expect(page).toHaveURL(/\/kbs$/);
});

test('governance overview exposes only existing workbench links', async ({ page }) => {
  await installMocks(page, employee);
  await page.goto('/admin/governance');

  await expect(page.getByRole('heading', { name: '运营入口' })).toBeVisible();
  await expect(page.getByRole('link', { name: '质量工作台', exact: true })).toHaveAttribute('href', '/admin/quality');
  await expect(page.getByRole('link', { name: '成本与用量', exact: true })).toHaveAttribute('href', '/admin/costs');
  await expect(page.getByRole('link', { name: '日志中心', exact: true })).toHaveAttribute('href', '/admin/logs');
  await expect(page.getByRole('link', { name: /RAGAS 评估/ })).toHaveCount(0);
});
