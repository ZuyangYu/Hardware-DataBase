import { expect, test, type Page } from '@playwright/test';

const user = {
  username: 'alice',
  role: 'employee',
  department_id: 7,
  department_name: '研发部',
};

const longDirectoryName = '超长知识库目录名称'.repeat(40);

async function installMocks(page: Page) {
  await page.addInitScript(value => {
    localStorage.setItem(
      'hdb_auth_session',
      JSON.stringify({ token: 'directory-overflow-test-only', user: value }),
    );
  }, user);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') {
      return route.fulfill({ json: { items: [], unread_count: 0 } });
    }
    if (path === '/api/v1/kbs') {
      return route.fulfill({
        json: [
          {
            name: 'AAA',
            kb_id: 1,
            department_id: 7,
            department_name: '研发部',
            permission: 'admin',
            registered: true,
            owner_user_id: 7,
            is_shared: false,
            is_owner: true,
          },
        ],
      });
    }
    if (path === '/api/v1/kbs/AAA/files') return route.fulfill({ json: [] });
    if (path === '/api/v1/kbs/AAA/directories') {
      return route.fulfill({
        json: [
          {
            id: 1,
            kb_id: 1,
            parent_id: 0,
            name: longDirectoryName,
            path: longDirectoryName,
            depth: 1,
            child_directory_count: 0,
            file_count: 0,
            created_by: 'alice',
            created_at: '',
            updated_at: '',
          },
        ],
      });
    }
    if (path === '/api/v1/kbs/AAA/structured/spreadsheets') {
      return route.fulfill({ json: { totals: {}, rows: [] } });
    }
    if (path === '/api/v1/kbs/AAA/structured/circuit-designs') {
      return route.fulfill({ json: { designs: [], failed_logs: [] } });
    }
    if (path === '/api/v1/kbs/AAA/parse-tasks') return route.fulfill({ json: [] });
    if (path === '/api/v1/kbs/AAA/directory-imports') {
      return route.fulfill({ json: [] });
    }
    return route.fulfill({ json: [] });
  });
}

test('long directory names stay inside the directory sidebar', async ({ page }) => {
  await installMocks(page);
  await page.goto('/kbs/AAA/files');

  const panel = page.getByTestId('kb-directory-panel');
  const name = page.getByTestId('kb-directory-name-1');
  await expect(name).toBeVisible();

  const panelBox = await panel.boundingBox();
  const nameBox = await name.boundingBox();
  expect(panelBox).not.toBeNull();
  expect(nameBox).not.toBeNull();
  expect(Math.round(nameBox!.x + nameBox!.width)).toBeLessThanOrEqual(
    Math.round(panelBox!.x + panelBox!.width) + 1,
  );
  // The name must be ellipsized rather than wrapped or overflowing.
  expect(await name.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
  expect(await name.evaluate(element => getComputedStyle(element).textOverflow)).toBe('ellipsis');

  // Hover reveals 改名/移动/删除; they must not push the row out of the panel.
  await page.getByTestId('kb-directory-row-1').hover();
  await expect(page.getByText('改名', { exact: true })).toBeVisible();
  const metrics = await panel.evaluate(element => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(metrics.scroll).toBeLessThanOrEqual(metrics.client + 1);
});
