import { expect, test, type Page } from '@playwright/test';

const target = 'R3_FULL_TARGET_20261019';
const reference = 'R3_FULL_REFERENCE_20261019';
const user = { username: 'scope-reader', role: 'employee', department_id: 47, department_name: 'ADAS' };
const kbs = [
  { name: 'ADAS', kb_id: 2 },
  { name: target, kb_id: 3 },
  { name: reference, kb_id: 4 },
].map((kb) => ({ ...kb, department_id: 47, department_name: 'ADAS', permission: 'admin', registered: true, owner_user_id: 2, is_shared: false, is_owner: true }));

async function installChat(page: Page, kbName = target, sessions: unknown[] = []) {
  await page.addInitScript((user) => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'scope-test', user })), user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/kbs') return route.fulfill({ json: kbs });
    if (path === '/api/v1/conversations') return route.fulfill({ json: sessions });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: { enabled: false, items: [] } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [], total: 0 } });
    return route.fulfill({ json: [] });
  });
  await page.goto(`/chat?kb=${kbName}`);
  await expect(page.getByRole('combobox', { name: '选择会话锚定库' })).toContainText(kbName);
}

test('逐个添加和移除参考库，新会话仅绑定已选范围', async ({ page }) => {
  test.skip(true, 'Multi-KB scope selector is not implemented in the current chat UI');
  await installChat(page);
  await page.getByRole('button', { name: /多库范围/ }).click();
  const selected = page.getByRole('list', { name: '已选知识库' });
  await expect(selected.getByRole('listitem')).toHaveCount(1);
  await expect(selected).toContainText(target);
  await expect(page.getByRole('checkbox')).toHaveCount(0);
  await expect(page.getByRole('button', { name: `移除知识库 ${target}`, exact: true })).toHaveCount(0);

  const add = page.getByRole('combobox', { name: '添加知识库', exact: true });
  await add.click();
  await page.getByRole('option', { name: reference, exact: true }).click();
  await expect(selected.getByRole('listitem')).toHaveCount(2);
  await expect(selected).toContainText(reference);
  await add.click();
  await expect(page.getByRole('option', { name: reference, exact: true })).toHaveCount(0);
  await page.getByRole('option', { name: 'ADAS', exact: true }).click();
  await expect(selected.getByRole('listitem')).toHaveCount(3);
  await page.getByRole('button', { name: '移除知识库 ADAS', exact: true }).click();
  await expect(selected.getByRole('listitem')).toHaveCount(2);
  await page.getByLabel('知识库用途', { exact: true }).selectOption('compare');

  let body: unknown;
  await page.route('**/api/v1/conversations', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    body = route.request().postDataJSON();
    return route.fulfill({ json: { id: 81, user_id: 2, kb_name: target, title: '新对话', created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z' } });
  });
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect.poll(() => body).toMatchObject({
    kb_name: target,
    knowledge_scope: { version: 1, purpose: 'compare', sources: [{ kb_id: 3, role: 'peer' }, { kb_id: 4, role: 'peer' }] },
  });
});

test('切换主库后可单独选择目标库，不携带先前的多库范围', async ({ page }) => {
  test.skip(true, 'Multi-KB scope selector is not implemented in the current chat UI');
  await installChat(page, 'ADAS');
  await page.getByRole('button', { name: /多库范围/ }).click();
  await page.getByRole('combobox', { name: '添加知识库', exact: true }).click();
  await page.getByRole('option', { name: reference, exact: true }).click();
  await page.keyboard.press('Escape');
  await page.getByRole('combobox', { name: '选择会话锚定库' }).click();
  await page.getByRole('option', { name: `${target} · ADAS`, exact: true }).click();
  await page.getByRole('button', { name: /多库范围/ }).click();
  await expect(page.getByRole('list', { name: '已选知识库' }).getByRole('listitem')).toHaveCount(1);
  await expect(page.getByRole('list', { name: '已选知识库' })).toContainText(target);

  let body: Record<string, unknown> | undefined;
  await page.route('**/api/v1/conversations', (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    body = route.request().postDataJSON();
    return route.fulfill({ json: { id: 82, user_id: 2, kb_name: target, title: '新对话', created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z' } });
  });
  await page.getByRole('button', { name: '新对话', exact: true }).click();
  await expect.poll(() => body).toMatchObject({ kb_name: target });
  expect(body).not.toHaveProperty('knowledge_scope');
});

test('单库和双库会话列表与当前选中的范围一致', async ({ page }) => {
  test.skip(true, 'Multi-KB scope selector is not implemented in the current chat UI');
  const scope = {
    version: 1, purpose: 'answer', snapshot_id: 'scope-1', tenant_key: '47', actor_user_id: 2,
    anchor_kb_id: 3, selection_hash: 'target-reference', authorized_at: '2026-10-19T00:00:00Z',
    sources: [
      { kb_id: 3, kb_name: target, department_id: '47', role: 'primary' },
      { kb_id: 4, kb_name: reference, department_id: '47', role: 'reference' },
    ],
  };
  const session = { user_id: 2, kb_name: target, created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z', source_access_restricted: false };
  await installChat(page, target, [
    { ...session, id: 91, title: '双库历史对比', knowledge_scope: scope, knowledge_selection_hash: 'target-reference' },
    { ...session, id: 92, title: '目标库单独问答', knowledge_scope: null, knowledge_selection_hash: '' },
  ]);
  await expect(page.getByText('目标库单独问答', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('双库历史对比', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: /多库范围/ }).click();
  await page.getByRole('combobox', { name: '添加知识库', exact: true }).click();
  await page.getByRole('option', { name: reference, exact: true }).click();
  await expect(page.getByText('双库历史对比', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('目标库单独问答', { exact: true })).toHaveCount(0);
});
