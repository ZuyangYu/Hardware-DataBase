import { expect, test } from '@playwright/test';

const username = process.env.HDB_E2E_USERNAME || '';
const password = process.env.HDB_E2E_PASSWORD || '';
const apiBase = process.env.HDB_E2E_API_BASE || 'http://127.0.0.1:8002/api/v1';

test.skip(
  !username || !password,
  'set HDB_E2E_USERNAME/HDB_E2E_PASSWORD to run the real chat card test',
);

test('MCP write card is inline, survives reload, confirms and downloads', async ({ page, request }) => {
  await page.goto('/');
  const loginCta = page.getByRole('button', { name: '登录', exact: true });
  if (await loginCta.isVisible().catch(() => false)) {
    await loginCta.click();
  }
  await page.getByPlaceholder('用户名').fill(username);
  await page.getByPlaceholder('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).last().click();
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem('hdb_auth_session')), {
      timeout: 30_000,
    })
    .not.toBeNull();

  const auth = await page.evaluate(() => JSON.parse(localStorage.getItem('hdb_auth_session') || '{}'));
  const headers = { Authorization: `Bearer ${auth.token}` };
  const title = `card-e2e-${Date.now()}`;
  const session = await request.post(`${apiBase}/conversations`, {
    headers,
    data: { kb_name: '__general__', title },
  });
  expect(session.ok()).toBeTruthy();
  const sessionId = (await session.json()).id as number;

  const turn = await request.post(`${apiBase}/conversations/${sessionId}/turns`, {
    headers,
    data: { query: '画一个开始到结束的最简流程图，生成 card-e2e.drawio 文件。' },
  });
  expect(turn.ok()).toBeTruthy();
  const turnId = (await turn.json()).turn.id as string;
  await request.post(`${apiBase}/turns/${turnId}/start`, { headers });

  await expect
    .poll(async () => {
      const response = await request.get(`${apiBase}/plugins/mcp/write-proposals?statuses=proposed`, { headers });
      return ((await response.json()).items || []).length;
    }, { timeout: 180_000 })
    .toBeGreaterThan(0);

  await page.goto('/chat');
  await expect(page.getByRole('complementary').getByText(title)).toBeVisible({ timeout: 30_000 });
  await page.getByRole('complementary').getByText(title).click();

  const card = page.locator('[data-mcp-write-card]').last();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card).toContainText('需要你确认：生成外部文件');
  await expect(card.getByRole('button', { name: '确认生成文件' })).toBeVisible();
  await expect(card.getByRole('button', { name: '暂不生成' })).toBeVisible();

  // The card must be attached to the assistant message, not a global panel.
  const inMessage = page.locator('[data-message-role="assistant"] [data-mcp-write-card]').last();
  await expect(inMessage).toBeVisible();

  await page.reload();
  await expect(page.getByRole('complementary').getByText(title)).toBeVisible({ timeout: 30_000 });
  await page.getByRole('complementary').getByText(title).click();
  const reloadedCard = page.locator('[data-message-role="assistant"] [data-mcp-write-card]').last();
  await expect(reloadedCard).toBeVisible({ timeout: 30_000 });
  await expect(reloadedCard).toContainText('需要你确认：生成外部文件');

  await reloadedCard.getByRole('button', { name: '确认生成文件' }).click();
  const downloadButton = page.getByRole('button', { name: '下载文件' }).last();
  await expect(downloadButton).toBeVisible({ timeout: 180_000 });
  const downloadPromise = page.waitForEvent('download');
  await downloadButton.click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toContain('card-e2e');
});
