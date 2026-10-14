import { expect, test, type Page } from '@playwright/test';

// 管理后台页面骨架一致性回归:所有管理页的页头(h1 标题)必须落在同一位置、
// 同一字号,页头整体高度一致 —— 防止再出现"质量工作台/治理总览标题不齐"。
// 只 mock API,不触碰真实数据。

const admin = { username: 'skeleton-admin', role: 'system_admin', department_id: null, department_name: null };

const PAGES: Array<{ path: string; name: string }> = [
  { path: '/admin/governance', name: '治理总览' },
  { path: '/admin/quality', name: '质量工作台' },
  { path: '/admin/costs', name: '成本与用量' },
  { path: '/admin/logs', name: '日志中心' },
  { path: '/admin/evaluation', name: 'RAGAS 评估' },
  { path: '/admin/departments', name: '部门管理' },
  { path: '/admin/users', name: '员工管理' },
  { path: '/admin/kb-mount', name: '知识库挂载' },
  { path: '/admin/status', name: '系统状态' },
  { path: '/admin/plugins', name: '平台扩展' },
  { path: '/admin/config', name: '系统配置' },
];

const COST_SUMMARY = {
  calls: 0, known_amounts: {}, unknown_cost_calls: 0, usage_coverage: null, price_coverage: null,
  logical_only_calls: 0, unfinished_calls: 0, unattributed_calls: 0, updated_at: null, scope: 'test', by_purpose: {},
  tokens: { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, reasoning_tokens: 0 },
};

async function installMocks(page: Page) {
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'skeleton-regression-only', user }));
  }, admin);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: admin });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/governance/kb-summaries') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/cases' || path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/analysis-runs') return route.fulfill({ json: { report_id: 'r' } });
    if (path === '/api/v1/costs/summary' || path === '/api/v1/costs/trends') {
      return route.fulfill({ json: path.endsWith('/trends')
        ? { bucket: 'day', timezone: 'UTC', summary: COST_SUMMARY, buckets: [], rankings_by_currency: {} }
        : COST_SUMMARY });
    }
    if (path === '/api/v1/costs/invocations' || path === '/api/v1/costs/budgets') return route.fulfill({ json: [] });
    if (path === '/api/v1/users' || path === '/api/v1/departments' || path === '/api/v1/kbs') return route.fulfill({ json: [] });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: {
      enabled: false, write_enabled: false, prerequisites_met: false, prerequisites_reason: '', items: [],
    } });
    if (path === '/api/v1/system/status') return route.fulfill({ json: {} });
    if (path === '/api/v1/config') return route.fulfill({ json: {} });
    return route.fulfill({ json: [] });
  });
}

type HeaderGeometry = { x: number; y: number; height: number; font: string; weight: string };

async function readHeader(page: Page): Promise<HeaderGeometry> {
  return page.evaluate(() => {
    const h1 = document.querySelector('header h1');
    const header = document.querySelector('header');
    if (!h1 || !header) throw new Error('header h1 missing');
    const h1Rect = h1.getBoundingClientRect();
    const headerRect = header.getBoundingClientRect();
    const style = getComputedStyle(h1);
    return {
      x: Math.round(h1Rect.x),
      y: Math.round(h1Rect.y),
      height: Math.round(headerRect.height),
      font: style.fontSize,
      weight: style.fontWeight,
    };
  });
}

test('所有管理页页头几何一致(位置/字号/高度)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installMocks(page);

  const geometries: Array<{ name: string; geometry: HeaderGeometry }> = [];
  for (const item of PAGES) {
    await page.goto(item.path);
    const h1 = page.locator('header h1');
    await expect(h1).toHaveText(item.name);
    await page.evaluate(() => document.fonts?.ready);
    geometries.push({ name: item.name, geometry: await readHeader(page) });
  }

  const reference = geometries[0].geometry;
  for (const { name, geometry } of geometries) {
    expect(geometry, `${name} 的页头应与「${PAGES[0].name}」一致,实际 ${JSON.stringify(geometry)} 基准 ${JSON.stringify(reference)}`).toEqual(reference);
  }
});

test('所有管理页标题都有描述行,页头结构一致(避免块高忽高忽低)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installMocks(page);

  for (const item of PAGES) {
    await page.goto(item.path);
    await expect(page.locator('header h1')).toHaveText(item.name);
    // 分组标识 + 描述:确保每个页面都有同样的三行页头(分组/标题/描述)
    await expect(page.locator('header p[aria-label="当前导航分组"]')).toHaveText(/治理与运营|组织与平台管理/);
    await expect(page.locator('header p').nth(1)).not.toBeEmpty();
  }
});

test('窄屏下页头不被贴到左边缘(与其他内容同内边距)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installMocks(page);

  const xs: number[] = [];
  for (const item of PAGES.slice(0, 4)) {
    await page.goto(item.path);
    await expect(page.locator('header h1')).toBeVisible();
    const geometry = await readHeader(page);
    expect(geometry.x, `${item.name} 页头不应贴边`).toBeGreaterThan(0);
    xs.push(geometry.x);
  }
  expect(new Set(xs).size, `窄屏各页标题横向位置应一致,实际 ${xs.join(',')}`).toBe(1);
});
