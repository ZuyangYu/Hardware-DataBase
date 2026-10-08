import { randomUUID } from 'node:crypto';
import { expect, test, type Page, type APIRequestContext } from '@playwright/test';

test.skip(process.env.HDB_E2E !== '1', 'Set HDB_E2E=1 to run against a live acceptance deployment.');

const username = process.env.HDB_BROWSER_USERNAME || 'acceptance_browser';
const password = process.env.HDB_BROWSER_PASSWORD || 'AcceptanceTestPassword123!';

async function login(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByPlaceholder('用户名', { exact: true }).fill(username);
  await page.getByPlaceholder('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => !!localStorage.getItem('hdb_auth_session'))).toBe(true);
  const auth = await page.evaluate(() => JSON.parse(localStorage.getItem('hdb_auth_session')!));
  await page.goto('/chat');
  await expect(page.locator('textarea')).toBeVisible();
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  return { Authorization: `Bearer ${auth.token}` };
}

async function waitTerminal(request: APIRequestContext, id: string, headers: Record<string, string>) {
  await expect.poll(async () => {
    const response = await request.get(`/api/v1/turns/${id}`, { headers });
    expect(response.ok()).toBeTruthy();
    return (await response.json()).status;
  }, { timeout: 150_000, intervals: [250, 500, 1000] }).toMatch(/^(completed|failed|cancelled)$/);
  return (await request.get(`/api/v1/turns/${id}`, { headers })).json();
}

test('live answer survives disconnect, refresh and duplicate submission with one terminal event', async ({ page, context, request }) => {
  const headers = await login(page);
  const query = `请解释欧姆定律，用四个短段落回答，最后写下验收标记 ${randomUUID()}`;
  const submitted = page.waitForResponse(response => response.url().endsWith('/turns') && response.request().method() === 'POST');
  await page.locator('textarea').fill(query);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const response = await submitted;
  expect(response.status()).toBe(201);
  const { turn } = await response.json();
  const originalBody = response.request().postDataJSON();
  expect(originalBody.client_request_id).toBeTruthy();
  const duplicate = await request.post(`/api/v1/conversations/${turn.session_id}/turns`, { headers, data: originalBody });
  expect(duplicate.status()).toBe(201);
  expect((await duplicate.json()).turn.id).toBe(turn.id);

  await context.setOffline(true);
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toBeVisible();
  await context.setOffline(false);
  await page.reload();
  const completed = await waitTerminal(request, turn.id, headers);
  expect(completed.status).toBe('completed');
  expect(completed.answer.length).toBeGreaterThan(20);
  const assistant = page.locator(`[data-message-id="${completed.assistant_message_id}"]`);
  await expect(assistant).toHaveCount(1);
  await expect(assistant).toContainText('欧姆');
  const newerSession = await request.post('/api/v1/conversations', {
    headers, data: { kb_name: '__general__', title: 'Another tab conversation' },
  });
  expect(newerSession.ok()).toBeTruthy();
  await page.reload();
  await expect(page.locator(`[data-message-id="${completed.assistant_message_id}"]`)).toContainText('欧姆');
  expect((await request.delete(`/api/v1/conversations/${(await newerSession.json()).id}`, { headers })).ok()).toBeTruthy();

  const events = await request.get(`/api/v1/turns/${turn.id}/events`, { headers });
  expect(events.ok()).toBeTruthy();
  const stream = await events.text();
  expect(stream.match(/^event: turn_end$/gm)).toHaveLength(1);
  const ids = [...stream.matchAll(/^id: (\d+)$/gm)].map(match => Number(match[1]));
  expect(ids.length).toBeGreaterThan(1);
  expect(ids.every((id, index) => index === 0 || id > ids[index - 1])).toBe(true);
  const cursor = ids[Math.floor(ids.length / 2)];
  const resumed = await request.get(`/api/v1/turns/${turn.id}/events?after=${cursor}`, { headers: { ...headers, 'Last-Event-ID': String(cursor) } });
  const resumedIDs = [...(await resumed.text()).matchAll(/^id: (\d+)$/gm)].map(match => Number(match[1]));
  expect(resumedIDs).toEqual(ids.filter(id => id > cursor));
});

test('stop button persists cancellation after refresh', async ({ page, request }) => {
  const headers = await login(page);
  const submitted = page.waitForResponse(response => response.url().endsWith('/turns') && response.request().method() === 'POST');
  await page.locator('textarea').fill('请用中文逐条解释从1到200的电气工程术语，每条至少100字，请完整输出。');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const { turn } = await (await submitted).json();
  await page.getByRole('button', { name: '停止生成', exact: true }).click();
  const cancelled = await waitTerminal(request, turn.id, headers);
  expect(cancelled.status).toBe('cancelled');
  await page.reload();
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toHaveCount(0);
  expect((await (await request.get(`/api/v1/turns/${turn.id}`, { headers })).json()).status).toBe('cancelled');
});

test('logout in another tab removes access from the open chat', async ({ page, context }) => {
  await login(page);
  const otherTab = await context.newPage();
  await otherTab.goto('/chat');
  await otherTab.evaluate(() => localStorage.removeItem('hdb_auth_session'));
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
  await expect(page.locator('textarea')).toHaveCount(0);
});
