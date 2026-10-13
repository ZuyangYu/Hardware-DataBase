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
        calls: 42, known_amounts: { USD: '1234567.89', EUR: '987.65' },
        tokens: { input_tokens: 110, output_tokens: 220, cached_input_tokens: 0, reasoning_tokens: 0 },
        unknown_cost_calls: 7, usage_coverage: null, price_coverage: 0.8123,
        logical_only_calls: 3, unfinished_calls: 0, unattributed_calls: 5,
        updated_at: '2026-10-09T02:00:00Z', scope: '测试范围',
        by_purpose: { chat: { calls: 30, known_amounts: { USD: '800.00' } }, memory: { calls: 2, known_amounts: {} } },
      },
    });
    if (path === '/api/v1/costs/invocations') return route.fulfill({
      json: [
        {
          invocation_id: 'f'.repeat(64), task_type: 'chat', task_id: '', purpose: 'chat',
          started_at: '2026-10-09T01:00:00Z', requested_model: 'm-a', response_model: 'm-b',
          endpoint_alias: 'alias-a', status: 'success', input_tokens: 100, output_tokens: 200,
          cost_status: 'unknown_price', cost_amount: null, currency: null,
          attempt_visibility: 'single', price_version: null, usage_status: 'reported',
          model_alias: 'm-a', cached_input_tokens: 0, reasoning_tokens: 0,
        },
        {
          invocation_id: 'a'.repeat(64), task_type: 'chat', task_id: 'task-42', purpose: 'chat',
          started_at: '2026-10-09T02:00:00Z', requested_model: 'm-a', response_model: 'm-b',
          endpoint_alias: 'alias-a', status: 'failed', input_tokens: 10, output_tokens: 20,
          cost_status: 'priced', cost_amount: '0.0123', currency: 'USD',
          attempt_visibility: 'single', price_version: 'pv-2026-10-01', usage_status: 'reported',
          model_alias: 'm-a', cached_input_tokens: 0, reasoning_tokens: 0,
        },
      ],
    });
    if (path === '/api/v1/costs/trends') return route.fulfill({ json: {
      bucket: 'day', timezone: 'UTC',
      summary: {
        calls: 42, known_amounts: { USD: '1234567.89', EUR: '987.65' },
        tokens: { input_tokens: 110, output_tokens: 220, cached_input_tokens: 0, reasoning_tokens: 0 },
        unknown_cost_calls: 7, usage_coverage: null, price_coverage: 0.8123,
        logical_only_calls: 3, unfinished_calls: 0, unattributed_calls: 5,
        updated_at: '2026-10-09T02:00:00Z', scope: '测试范围', by_purpose: {},
      }, buckets: [], rankings_by_currency: {},
    } });
    if (path === '/api/v1/costs/budgets') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });
});

test('多币种长金额与中文标签完整显示,不被 truncate 截断', async ({ page }) => {
  await page.goto('/admin/costs');
  // amount() 渲染完整多币种金额；新版摘要卡允许长值换行，不再依赖旧版固定高度。
  const amountText = page.getByText('USD 1234567.89 · EUR 987.65', { exact: true });
  await expect(amountText).toBeVisible();
  // 水平方向无实际裁剪(scrollWidth <= clientWidth),卡片允许纵向增高
  const overflow = await amountText.evaluate((el) => ({
    scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
    whiteSpace: getComputedStyle(el).whiteSpace,
  }));
  expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);
  // 新版页面明确标注为应用侧 Token 估算，而非供应商结算账单。
  await expect(page.getByText('已知费用', { exact: true })).toBeVisible();
  await expect(page.getByText(/金额是应用侧 Token 估算，不是供应商结算账单/)).toBeVisible();
});

test('无 task_id 的调用行是普通文本,不渲染无效按钮;有 task_id 才可点击', async ({ page }) => {
  await page.goto('/admin/costs');
  // 无 task_id:普通文本"未归属任务",且不存在同名按钮
  await expect(page.getByText('未归属任务', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '未归属任务' })).toHaveCount(0);
  // 有 task_id:渲染为可点击按钮
  const taskButton = page.getByRole('button', { name: 'task-42' });
  await expect(taskButton).toBeVisible();
});

test('未知费用以徽章呈现,未知覆盖率显示 — 与 0 区分', async ({ page }) => {
  await page.goto('/admin/costs');
  // usage_coverage 为 null -> '—';price_coverage 为 0.8123 -> 81.2%
  await expect(page.getByText('Usage 覆盖：—', { exact: true })).toBeVisible();
  await expect(page.getByText('计价覆盖：81.2%', { exact: true }).first()).toBeVisible();
  // 未知费用调用计数为具体数字而非 —
  await expect(page.getByText('7 次调用费用未知，未知不等于 0', { exact: true })).toBeVisible();
});

test('摘要卡显示费用口径说明,价格版本可复制', async ({ page }) => {
  await page.goto('/admin/costs');
  await expect(page.getByText(/未知费用不等于 0/)).toBeVisible();
  // 价格版本徽章/复制按钮
  await expect(page.getByRole('button', { name: /复制价格版本/ })).toBeVisible();
});

test('用途筛选保留 memory API 值但显示业务文案', async ({ page }) => {
  await page.goto('/admin/costs');
  const memoryOption = page.getByLabel('调用用途').locator('option[value="memory"]');
  await expect(memoryOption).toHaveText('记忆提取/整理');
  await expect(page.locator('span').filter({ hasText: '记忆提取/整理' }).last()).toBeVisible();
  await expect(page.getByText(/本页只统计已捕获的模型\/Embedding provider 调用；记忆读写等非模型操作不计入/)).toBeVisible();
});
