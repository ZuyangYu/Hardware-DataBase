import { expect, test } from '@playwright/test';

// 响应式回归:390/768/1024/1440 四档视口。
// 仅 mock API 渲染真实管理工作台,不触碰真实案例/费用记录。

const VIEWPORTS = [
  { name: 'mobile-390', width: 390, height: 844 },
  { name: 'tablet-768', width: 768, height: 1024 },
  { name: 'laptop-1024', width: 1024, height: 768 },
  { name: 'desktop-1440', width: 1440, height: 900 },
];

const admin = { username: 'viewport-admin', role: 'system_admin', department_id: null, department_name: null };

test.beforeEach(async ({ page }) => {
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'viewport-regression-only', user }));
  }, admin);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: admin });
    if (path === '/api/v1/quality/cases') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/analysis-runs') return route.fulfill({ json: { report_id: 'r' } });
    if (path === '/api/v1/costs/summary') return route.fulfill({ json: { calls: 0, known_amounts: {}, unknown_cost_calls: 0, usage_coverage: null, price_coverage: null, logical_only_calls: 0, unfinished_calls: 0, unattributed_calls: 0, updated_at: null, scope: 'test', by_purpose: {} } });
    if (path === '/api/v1/costs/invocations') return route.fulfill({ json: [] });
    if (path === '/api/v1/costs/trends') return route.fulfill({ json: { buckets: [], rankings_by_currency: {} } });
    if (path === '/api/v1/costs/budgets') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });
});

const PAGES = ['/admin/quality', '/admin/costs'];

for (const vp of VIEWPORTS) {
  test(`${vp.name}: 质量与成本页面无页面级横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    for (const path of PAGES) {
      await page.goto(path);
      await expect(page.getByRole('heading', { level: 1 }).first()).toBeVisible();
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth, `${path} @ ${vp.name}`).toBeLessThanOrEqual(overflow.clientWidth);
    }
  });
}

test('窄屏显示"打开导航"触发器,桌面隐藏且固定侧栏可用', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/admin/quality');
  await expect(page.getByRole('button', { name: '打开导航' })).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 900 });
  // 桌面固定侧栏先出现(说明 isMobile 已切换),再断言抽屉触发器消失
  await expect(page.getByRole('button', { name: '质量工作台' })).toBeVisible();
  await expect(page.getByRole('button', { name: '打开导航' })).toBeHidden();
});

test('移动抽屉: 打开、 Escape 关闭、焦点返回触发按钮', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/admin/quality');
  const trigger = page.getByRole('button', { name: '打开导航' });
  await trigger.click();
  const drawer = page.getByRole('dialog');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole('heading', { name: '导航' })).toBeVisible();
  await expect(drawer.getByRole('button', { name: /质量工作台/ })).toBeVisible();
  await expect(drawer.getByRole('button', { name: /成本与用量/ })).toBeVisible();
  // 打开时背景滚动锁定(Radix Dialog 默认行为)
  await page.keyboard.press('Escape');
  await expect(drawer).toBeHidden();
  await expect(page.locator('#admin-nav-trigger')).toBeFocused();
  await trigger.click();
  await expect(drawer).toBeVisible();
  // 点击导航项后抽屉自动收起
  await drawer.getByRole('button', { name: /成本与用量/ }).click();
  await expect(drawer).toBeHidden();
});

test('抽屉内导航跳转到成本页后,宽表在窄屏内部滚动且出现滑动提示', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/admin/quality');
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('dialog').getByRole('button', { name: /成本与用量/ }).click();
  await expect(page.getByRole('heading', { name: '筛选调用账本' })).toBeVisible();
  await expect(page.getByText('左右滑动查看全部列').first()).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  expect(overflow).toBe(true);
});

test('表头 bg-muted 上文字对比度 >= 4.5:1(WCAG AA)', async ({ page }) => {
  await page.goto('/admin/quality');
  await expect(page.locator('thead.bg-muted')).toBeVisible();
  const ratio = await page.evaluate(() => {
    const parse = (color: string) => {
      const m = color.match(/[\d.]+/g)!;
      return [Number(m[0]), Number(m[1]), Number(m[2])] as [number, number, number];
    };
    const lum = ([r, g, b]: [number, number, number]) => {
      const c = [r, g, b].map((v) => v / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    };
    const th = document.querySelector('thead.bg-muted th')!;
    const fg = getComputedStyle(th).color;
    const bg = getComputedStyle(th.closest('thead')!).backgroundColor;
    const [l1, l2] = [lum(parse(fg)), lum(parse(bg))].sort((a, b) => b - a);
    return (l1 + 0.05) / (l2 + 0.05);
  });
  expect(ratio).toBeGreaterThanOrEqual(4.5);
});

test('筛选等原生控件键盘聚焦时有可见 focus ring', async ({ page }) => {
  await page.goto('/admin/quality');
  const select = page.getByLabel('状态');
  await select.focus();
  const outline = await select.evaluate((el) => {
    const s = getComputedStyle(el);
    return { style: s.outlineStyle, width: s.outlineWidth };
  });
  expect(outline.style).not.toBe('none');
  expect(parseFloat(outline.width)).toBeGreaterThanOrEqual(2);
});
