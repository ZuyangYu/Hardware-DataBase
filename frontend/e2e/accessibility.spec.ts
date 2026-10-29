import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

const admin = { username: 'axe-admin', role: 'system_admin', department_id: null, department_name: null };
const employee = { username: 'axe-employee', role: 'employee', department_id: 7, department_name: '研发' };
const kb = { name: 'hardware', kb_id: 7, department_id: 7, department_name: '研发', permission: 'write', registered: true, is_owner: true };

async function mockAdminSession(page: Page) {
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'axe-regression-only', user }));
  }, admin);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: admin });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/quality/tickets') return route.fulfill({ json: { items: [], total: 0 } });
    if (path === '/api/v1/quality/ticket-assignees' || path === '/api/v1/departments') return route.fulfill({ json: [] });
    if (path === '/api/v1/governance/kb-summaries') return route.fulfill({ json: [] });
    if (path === '/api/v1/costs/summary') {
      return route.fulfill({ json: {
        calls: 0,
        known_amounts: {},
        tokens: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0 },
        unknown_cost_calls: 0,
        usage_coverage: null,
        price_coverage: null,
        logical_only_calls: 0,
        unfinished_calls: 0,
        unattributed_calls: 0,
        updated_at: null,
        scope: 'test',
        by_purpose: {},
      } });
    }
    if (path === '/api/v1/costs/invocations') return route.fulfill({ json: [] });
    if (path === '/api/v1/costs/trends') return route.fulfill({ json: {
      bucket: 'day',
      timezone: 'UTC',
      summary: { calls: 0, known_amounts: {}, tokens: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0 }, unknown_cost_calls: 0, usage_coverage: null, price_coverage: null, logical_only_calls: 0, unfinished_calls: 0, unattributed_calls: 0, updated_at: null, scope: 'test', by_purpose: {} },
      buckets: [],
      rankings_by_currency: {},
    } });
    if (path === '/api/v1/costs/budgets') return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: { detail: 'unexpected accessibility API request' } });
  });
}

async function mockEmployeeSession(page: Page) {
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'axe-regression-only', user }));
  }, employee);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: employee });
    if (path === '/api/v1/kbs') return route.fulfill({ json: [kb] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [] });
    if (path.endsWith('/messages')) return route.fulfill({ json: [] });
    if (path.endsWith('/memory-summary')) return route.fulfill({ json: { auto_extract_enabled: false, extracted_memories: 0 } });
    if (path === '/api/v1/memory-settings') return route.fulfill({ json: { opt_in: false, policy_version: 'axe-test', revoke_generation: 0, updated_at: null } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [] } });
    if (path === '/api/v1/memories') return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } });
    if (path.endsWith('/doc-assets')) return route.fulfill({ json: [] });
    if (path.endsWith('/files')) return route.fulfill({ json: [] });
    if (path.endsWith('/parse-tasks')) return route.fulfill({ json: [] });
    if (path.endsWith('/wiki/status')) return route.fulfill({ json: { running: false, stage: '空闲' } });
    if (path.endsWith('/wiki/lint')) {
      return route.fulfill({ json: {
        kb_id: kb.kb_id,
        health_score: 100,
        stats: { pages: 0, entity: 0, concept: 0, summary: 0, index: 0, links: 0 },
        by_type: {},
        issues: [],
        summary: '自动扫描测试数据',
      } });
    }
    if (path.endsWith('/wiki/pages')) return route.fulfill({ json: [] });
    if (path.endsWith('/attachments')) return route.fulfill({ json: [] });
    if (path === '/api/v1/exports') return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: { detail: 'unexpected accessibility API request' } });
  });
}

async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze();
  expect(results.violations, `${label}: ${JSON.stringify(results.violations)}`).toEqual([]);
}

test('登录表单通过 WCAG 自动扫描', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByLabel('用户名')).toBeVisible();
  await expectAccessible(page, 'login');
});

for (const path of ['/admin/quality', '/admin/costs']) {
  test(`${path} 通过 WCAG 自动扫描`, async ({ page }) => {
    await mockAdminSession(page);
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
    await expectAccessible(page, path);
  });
}

for (const [path, heading] of [
  ['/chat?kb=hardware', '新对话'],
  ['/assets?kb=hardware', '资产中心'],
  ['/kbs/hardware/files', '知识库工作台 · hardware'],
  ['/memory', '长期记忆'],
  ['/wiki?kb=hardware', '知识 Wiki'],
] as const) {
  test(`${path} 通过 WCAG 自动扫描`, async ({ page }) => {
    await mockEmployeeSession(page);
    await page.goto(path);
    await expect(page.getByText(heading, { exact: true }).first()).toBeVisible();
    await expectAccessible(page, path);
  });
}
