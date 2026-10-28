import { expect, test, type Page } from '@playwright/test';

const user = { username: 'citation-reader', role: 'employee', department_id: 7 };
const processingIdentity = {
  index_revision: 3, processing_artifact_id: 'parse-3', processor_version: 'parser-2', evidence_text_hash: 'b'.repeat(64),
};

async function installChat(page: Page) {
  await page.addInitScript((user) => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'citation-test', user })), user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/knowledge-bases') return route.fulfill({ json: [{ name: 'A', kb_id: 101, department_id: 7, permission: 'write', registered: true }] });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [{ id: 42, title: 'Compare sources', kb_name: 'A', user_id: user.username }] });
    if (path === '/api/v1/conversations/42/messages') return route.fulfill({ json: [{
      id: 2, role: 'assistant', content: 'Two source values', citations: [101, 205].map((kbId) => ({
        source_name: 'same.pdf', content: `stored preview ${kbId}`, locator: { chunk_id: 'shared-chunk' },
        metadata: { kb_id: kbId, local_record_id: 7, source_content_hash: `hash-${kbId}`, ...processingIdentity },
      })),
    }] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: { enabled: false, items: [] } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [], total: 0 } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/chat?kb=A');
  await page.getByRole('button', { name: '参考来源(2)' }).click();
}

test('同名文件的引用按实际 KB 与片段读取原文', async ({ page }) => {
  let submitted: unknown;
  await installChat(page);
  await page.route('**/api/v1/citations/resolve', async (route) => {
    submitted = route.request().postDataJSON();
    return route.fulfill({ json: { ...submitted as object, kb_name: 'B', source_name: 'same.pdf', content: 'B authoritative 12V' } });
  });
  await page.getByRole('button', { name: '查看原文 2：same.pdf', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('B · same.pdf');
  await expect(page.getByRole('dialog')).toContainText('B authoritative 12V');
  expect(submitted).toEqual({
    kb_id: 205, local_record_id: 7, source_content_hash: 'hash-205', locator: { chunk_id: 'shared-chunk' }, ...processingIdentity,
  });
});

test('来源撤权后重新点击不展示之前读到的原文', async ({ page }) => {
  await installChat(page);
  let calls = 0;
  await page.route('**/api/v1/citations/resolve', (route) => {
    calls += 1;
    return route.fulfill(calls === 1
      ? { json: { ...route.request().postDataJSON(), kb_name: 'B', source_name: 'same.pdf', content: 'B private source' } }
      : { status: 403, json: { detail: 'citation_access_denied' } });
  });
  await page.getByRole('button', { name: '查看原文 2：same.pdf', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('B private source');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '查看原文 2：same.pdf', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('访问权限');
  await expect(page.getByRole('dialog')).not.toContainText('B private source');
  expect(calls).toBe(2);
});

test('关闭引用后迟到的旧请求不能覆盖新打开的另一来源', async ({ page }) => {
  await installChat(page);
  let releaseOld: () => void = () => undefined;
  const blocked = new Promise<void>((resolve) => { releaseOld = resolve; });
  await page.route('**/api/v1/citations/resolve', async (route) => {
    const body = route.request().postDataJSON();
    if (body.kb_id === 101) await blocked;
    return route.fulfill({ json: { ...body, kb_name: body.kb_id === 101 ? 'A' : 'B', source_name: 'same.pdf', content: `actual source ${body.kb_id}` } });
  });
  await page.getByRole('button', { name: '查看原文 1：same.pdf', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('正在核验来源');
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '查看原文 2：same.pdf', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('actual source 205');
  const lateResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/citations/resolve') && response.request().postDataJSON().kb_id === 101);
  releaseOld();
  await lateResponse;
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByRole('dialog')).toContainText('actual source 205');
  await expect(page.getByRole('dialog')).not.toContainText('actual source 101');
});
