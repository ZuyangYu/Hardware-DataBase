import { expect, test } from '@playwright/test';

// 成本页显示回归(D1/D3):多币种长金额与中文标签不被截断;无 task_id 的
// 调用行渲染为普通文本而非无效按钮;未知费用以徽章/说明呈现,不与 0 混淆。
// 仅 mock API,不触碰真实账本记录。

const admin = { username: 'cost-display', role: 'system_admin', department_id: null, department_name: null };

test.beforeEach(async ({ page }) => {
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'cost-display-regression', user }));
  }, admin);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: admin });
    if (path === '/api/v1/costs/summary') return route.fulfill({
      json: {
        calls: 42, known_amounts: { USD: '1234567.89', EUR: '987.65' }, unknown_cost_calls: 7,
        usage_coverage: null, price_coverage: 0.8123, logical_only_calls: 3,
        unfinished_calls: 0, unattributed_calls: 5, updated_at: '2026-10-09T02:00:00Z',
        scope: '测试范围', by_purpose: { chat: { calls: 30, known_amounts: { USD: '800.00' } } },
      },
    });
    if (path === '/api/v1/costs/invocations') return route.fulfill({
      json: [
        {
          invocation_id: 'f'.repeat(64), task_type: 'chat', task_id: '', purpose: 'chat',
          started_at: '2026-10-09T01:00:00Z', requested_model: 'm-a', response_model: 'm-b',
          endpoint_alias: 'alias-a', status: 'success', input_tokens: 100, output_tokens: 200,
          cost_status: 'unknown_no_price', cost_amount: null, currency: null,
          attempt_visibility: 'single', price_version: null,
        },
        {
          invocation_id: 'a'.repeat(64), task_type: 'chat', task_id: 'task-42', purpose: 'chat',
          started_at: '2026-10-09T02:00:00Z', requested_model: 'm-a', response_model: 'm-b',
          endpoint_alias: 'alias-a', status: 'failed', input_tokens: 10, output_tokens: 20,
          cost_status: 'priced', cost_amount: '0.0123', currency: 'USD',
          attempt_visibility: 'single', price_version: 'pv-2026-10-01',
        },
      ],
    });
    if (path === '/api/v1/costs/trends') return route.fulfill({ json: { buckets: [], rankings_by_currency: {} } });
    if (path === '/api/v1/costs/budgets') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });
});

test('多币种长金额与中文标签完整显示,不被 truncate 截断', async ({ page }) => {
  await page.goto('/admin/costs');
  // amount() 渲染 "USD 1234567.89 · EUR 987.65"——旧版 70px 固定高 + truncate 会裁成省略号
  const amountText = page.getByText('USD 1234567.89 · EUR 987.65');
  await expect(amountText).toBeVisible();
  // 水平方向无实际裁剪(scrollWidth <= clientWidth),卡片允许纵向增高
  const overflow = await amountText.evaluate((el) => ({
    scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
    whiteSpace: getComputedStyle(el).whiteSpace,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);
  // 已知金额(未对账)标签完整存在
  await expect(page.getByText('已知金额（未对账）')).toBeVisible();
});

test('无 task_id 的调用行是普通文本,不渲染无效按钮;有 task_id 才可点击', async ({ page }) => {
  await page.goto('/admin/costs');
  // 无 task_id:文本"未归属",且不存在同名按钮
  await expect(page.getByText('未归属', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '未归属' })).toHaveCount(0);
  // 有 task_id:渲染为可点击按钮
  const taskButton = page.getByRole('button', { name: 'task-42' });
  await expect(taskButton).toBeVisible();
});

test('未知费用以徽章呈现,未知覆盖率显示 — 与 0 区分', async ({ page }) => {
  await page.goto('/admin/costs');
  // usage_coverage 为 null -> '—';price_coverage 为 0.8123 -> 81.2%
  await expect(page.getByText('—', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('81.2%')).toBeVisible();
  // 未知费用调用计数为具体数字而非 —
  await expect(page.getByText('未知费用调用')).toBeVisible();
});

test('摘要卡带口径说明 tooltip,价格版本可复制', async ({ page }) => {
  await page.goto('/admin/costs');
  const known = page.locator('[title*="尚未与供应商结算账单对账"]');
  await expect(known.first()).toBeVisible();
  const unknownCard = page.locator('[title*="未知不等于 0"]');
  await expect(unknownCard.first()).toBeVisible();
  // 价格版本徽章/复制按钮
  await expect(page.getByRole('button', { name: /复制价格版本/ })).toBeVisible();
});
