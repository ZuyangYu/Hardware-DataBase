import { expect, test } from '@playwright/test';

const VIEWPORTS = [
  { name: 'mobile-390', width: 390, height: 844 },
  { name: 'desktop-1440', width: 1440, height: 900 },
];

const employee = { username: 'responsive-employee', role: 'employee', department_id: 7, department_name: '研发' };
const kb = { name: 'hardware', kb_id: 7, department_id: 7, department_name: '研发', permission: 'write', registered: true, is_owner: true };
const PAGES = ['/assets?kb=hardware', '/kbs/hardware/files', '/memory', '/chat?kb=hardware'];

test.beforeEach(async ({ page }) => {
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'core-responsive-only', user }));
  }, employee);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: employee });
    if (path === '/api/v1/kbs') return route.fulfill({ json: [kb] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [] });
    if (path.endsWith('/messages')) return route.fulfill({ json: [] });
    if (path.endsWith('/memory-summary')) return route.fulfill({ json: { auto_extract_enabled: false, extracted_memories: 0 } });
    if (path === '/api/v1/memory-settings') return route.fulfill({ json: { opt_in: false, policy_version: 'responsive-test', revoke_generation: 0, updated_at: null } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [] } });
    if (path === '/api/v1/memories') return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } });
    if (path.endsWith('/doc-assets')) return route.fulfill({ json: [] });
    if (path.endsWith('/files')) return route.fulfill({ json: [] });
    if (path.endsWith('/parse-tasks')) return route.fulfill({ json: [] });
    if (path.endsWith('/wiki/status')) return route.fulfill({ json: { running: false, stage: '空闲' } });
    if (path.endsWith('/attachments')) return route.fulfill({ json: [] });
    if (path === '/api/v1/exports') return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: { detail: 'unexpected core responsive API request' } });
  });
});

for (const viewport of VIEWPORTS) {
  test(`${viewport.name}: 核心工作区无页面级横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    for (const path of PAGES) {
      await page.goto(path);
      await expect(page.locator('body')).not.toContainText('页面暂时无法显示');
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(overflow.scrollWidth, `${path} @ ${viewport.name}`).toBeLessThanOrEqual(overflow.clientWidth);
    }
  });
}
