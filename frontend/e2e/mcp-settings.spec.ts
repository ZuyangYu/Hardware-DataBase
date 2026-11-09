import { expect, test, type Page } from '@playwright/test';

const user = {
  username: 'mcp-user',
  role: 'employee',
  department_id: 1739,
  department_name: '硬件部',
};

async function installMocks(page: Page) {
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'mcp-ui-test', user: sessionUser }));
  }, user);
  await page.context().route('**/api/v1/**', (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/memory-settings') return route.fulfill({ json: {
      opt_in: false, policy_version: 'mcp-ui-test', revoke_generation: 0, updated_at: '2026-11-06T00:00:00Z',
    } });
    if (path === '/api/v1/memory-consents' || path === '/api/v1/memories') {
      return route.fulfill({ json: { items: [], next_cursor: null, total: 0 } });
    }
    if (path === '/api/v1/exports/formats') return route.fulfill({ json: { formats: ['md', 'pdf'] } });
    if (path === '/api/v1/me/mcp-token') {
      if (request.method() === 'GET') {
        return route.fulfill({ json: {
          has_token: false, token_id: '', name: '', masked_token: '',
          created_at: '', expires_at: '', last_used_at: '',
          api_base: 'http://127.0.0.1:8002/api/v1',
          mcp_remote_url: 'http://127.0.0.1:9100/mcp',
        } });
      }
      if (request.method() === 'POST') {
        return route.fulfill({ json: {
          has_token: true,
          token_id: 'test-token-id',
          name: '个人 MCP',
          masked_token: 'hdbmcp_test-token-id_••••',
          created_at: '2026-11-06T00:00:00Z',
          expires_at: '2027-11-06T00:00:00Z',
          last_used_at: '',
          token: 'hdbmcp_test_secret',
          api_base: 'http://127.0.0.1:8002/api/v1',
          mcp_remote_url: 'http://127.0.0.1:9100/mcp',
          command: 'install -d -m 700 ~/.config/hdb && printf %s hdbmcp_test_secret > ~/.config/hdb/mcp_token && opencode mcp add hdb --global -- /repo/scripts/hdb_mcp_stdio.sh',
        } });
      }
      if (request.method() === 'DELETE') return route.fulfill({ json: { ok: true, revoked: 1 } });
    }
    return route.fulfill({ json: [] });
  });
}

test('侧边栏 MCP 接入页可以生成凭证并复制完整接入说明', async ({ page }) => {
  await installMocks(page);
  await page.goto('/');
  await page.getByRole('button', { name: '打开个人菜单' }).click();
  await page.getByRole('menuitem', { name: 'MCP 接入', exact: true }).click();

  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'MCP 接入' })).toBeVisible();
  await expect(page.getByRole('button', { name: '生成长期凭证' })).toBeVisible();
  await page.getByRole('button', { name: '生成长期凭证' }).click();

  await expect(page.getByText('hdbmcp_test_secret', { exact: true })).toBeVisible();
  await expect(page.getByText(/opencode mcp add hdb/)).toBeVisible();
  await expect(page.getByText(/MCP 远程接入（标准 streamable-http）/)).toBeVisible();
  await expect(page.getByText(/远程 MCP URL（streamable-http）/)).toBeVisible();
  await expect(page.getByText('X-HDB-MCP-Token: hdbmcp_test_secret', { exact: true })).toBeVisible();
  await expect(page.getByText('平台 API 地址（HDB_API_BASE，由本地桥使用）')).toBeVisible();
  await expect(page.getByText('http://127.0.0.1:8002/api/v1', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '复制全部' })).toBeVisible();
});
