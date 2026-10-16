import { expect, test, type Page } from '@playwright/test';

const user = { username: 'conflict-reviewer', role: 'employee', department_id: 7, department_name: '研发部' };
const conflict = {
  conflict_id: 'conflict-1', fingerprint: 'fingerprint-1', field_key: 'input_voltage',
  subject: '电源模块', severity: 'S2', status: 'open', row_version: 3,
  snapshot_hash: 'snapshot-1', evidence_set_hash: 'evidence-1', affected_unit_ids: ['power'],
  allowed_actions: ['choose_design_candidate', 'defer'],
  claims: [
    { claim_id: 'a', source_kb_id: 101, source_role: 'primary', display_value: '5 V', approval_status: 'approved', source_version_id: 'rev-a', evidence_ids: ['kb:101:5v'] },
    { claim_id: 'b', source_kb_id: 205, source_role: 'reference', display_value: '12 V', approval_status: 'draft', source_version_id: 'rev-b', evidence_ids: ['kb:205:12v'] },
  ],
};

async function installChat(page: Page) {
  await page.addInitScript((user) => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'conflict-browser-test', user })), user);
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/knowledge-bases') return route.fulfill({ json: [{ name: 'hardware', kb_id: 101, department_id: 7, permission: 'write', registered: true }] });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [{ id: 42, title: '电源设计审阅', kb_name: 'hardware', user_id: 'conflict-reviewer' }] });
    if (path === '/api/v1/conversations/42/messages') return route.fulfill({ json: [{
      id: 2, role: 'assistant', content: '有来源冲突待确认', cards: [{ kind: 'work_order_status', status: 'needs_review', next_actions: ['review_document'], kb_name: 'hardware', work_order_id: 'wo-1' }],
    }] });
    if (path === '/api/v1/document-generation/chat-tasks/current') return route.fulfill({ json: { current: null, tasks: [] } });
    if (path === '/api/v1/document-generation/chat-tasks/events') return route.fulfill({ contentType: 'text/event-stream', body: '' });
    if (path.endsWith('/work-orders/wo-1/status')) return route.fulfill({ json: { work_order_id: 'wo-1', status: 'needs_review', phase: 'needs_review', artifacts: [] } });
    if (path === '/api/v1/document-work-orders/wo-1/conflicts') return route.fulfill({ json: [conflict] });
    if (path === '/api/v1/document-work-orders/wo-1/conflicts/conflict-1') return route.fulfill({ json: { ...conflict, decisions: [] } });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/plugins/mcp') return route.fulfill({ json: { enabled: false, items: [] } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [], total: 0 } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/chat?kb=hardware');
  await page.getByRole('button', { name: '查看来源冲突', exact: true }).click();
  await page.getByRole('button', { name: 'input_voltage · 待确认', exact: true }).click();
  await expect(page.getByRole('table', { name: '冲突来源证据对照' })).toContainText('12 V');
}

test('审阅来源证据后逐项提交候选并显示待审批', async ({ page }) => {
  let submitted: Record<string, unknown> | undefined;
  await installChat(page);
  await page.route('**/conflicts/conflict-1/decisions?*', async (route) => {
    submitted = route.request().postDataJSON();
    await route.fulfill({ json: { status: 'approval_pending', decision_id: 'decision-1' } });
  });
  await page.getByLabel('冲突处理方式').selectOption('choose_design_candidate');
  await expect(page.getByRole('button', { name: '保存本项决策' })).toBeDisabled();
  await page.getByLabel('证据候选', { exact: true }).selectOption('b');
  await page.getByLabel('冲突处理理由').fill('作为下一版方案候选');
  await expect(page.getByText(/原来源规格保持原样/)).toBeVisible();
  await page.getByRole('button', { name: '保存本项决策' }).click();
  await expect(page.getByText('设计建议已保存，仍待正式审批。', { exact: true })).toBeVisible();
  expect(submitted).toMatchObject({ expected_conflict_version: 3, selected_claim_id: 'b', source_snapshot_hash: 'snapshot-1', evidence_set_hash: 'evidence-1' });
  expect(submitted?.idempotency_key).toBeTruthy();
  expect(submitted).not.toHaveProperty('approval_status');
  expect(submitted).not.toHaveProperty('sources');
});

test('并发决策过期后移除旧证据和提交表单，要求重新审阅', async ({ page }) => {
  await installChat(page);
  await page.route('**/conflicts/conflict-1/decisions?*', (route) => route.fulfill({ status: 409, json: { detail: 'conflict_decision_stale' } }));
  await page.getByLabel('冲突处理方式').selectOption('defer');
  await page.getByRole('button', { name: '保存本项决策' }).click();
  await expect(page.getByRole('alert')).toContainText('重新加载并审阅');
  await expect(page.getByRole('table', { name: '冲突来源证据对照' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '保存本项决策' })).toHaveCount(0);
});

test('来源撤权后重新加载清除所有旧候选值', async ({ page }) => {
  await installChat(page);
  await page.route('**/document-work-orders/wo-1/conflicts?*', (route) => route.fulfill({ status: 403, json: { detail: 'source_access_revoked' } }));
  await page.getByRole('button', { name: '重新加载来源冲突' }).click();
  await expect(page.getByRole('alert')).toContainText('访问权限');
  await expect(page.getByText('12 V', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('table', { name: '冲突来源证据对照' })).toHaveCount(0);
});

test('网络失败重试复用同一个决策幂等键', async ({ page }) => {
  const bodies: Array<Record<string, unknown>> = [];
  await installChat(page);
  await page.route('**/conflicts/conflict-1/decisions?*', async (route) => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill(bodies.length === 1
      ? { status: 503, json: { detail: '暂时不可用' } }
      : { json: { status: 'deferred', decision_id: 'decision-1' } });
  });
  await page.getByLabel('冲突处理方式').selectOption('defer');
  await page.getByRole('button', { name: '保存本项决策' }).click();
  await expect(page.getByRole('alert')).toContainText('暂时不可用');
  await page.getByRole('button', { name: '保存本项决策' }).click();
  await expect(page.getByText('已暂缓，相关章节继续等待。', { exact: true })).toBeVisible();
  expect(bodies).toHaveLength(2);
  expect(bodies[0]).toEqual(bodies[1]);
});

test('选库范围冲突保留输入草稿，重发使用新会话', async ({ page }) => {
  await installChat(page);
  await page.route('**/conversations/42/turns', (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 409, json: { detail: 'knowledge_scope_changed' } }) : route.fallback());
  const created: Array<Record<string, unknown>> = [];
  await page.route('**/api/v1/conversations', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback();
    created.push(route.request().postDataJSON());
    return route.fulfill({ json: { id: 43, kb_name: 'hardware', title: '新范围任务', user_id: user.username } });
  });
  await page.route('**/conversations/43/turns', (route) => route.fulfill({ status: 503, json: { detail: 'test retry reached new session' } }));
  const composer = page.locator('textarea').last();
  await composer.fill('保留这段跨库设计草稿');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(composer).toHaveValue('保留这段跨库设计草稿');
  const next = page.waitForRequest((request) => request.method() === 'POST' && request.url().includes('/conversations/43/turns'));
  await page.getByRole('button', { name: '发送', exact: true }).click();
  expect((await next).postDataJSON().query).toBe('保留这段跨库设计草稿');
  expect(created).toHaveLength(1);
  expect(created[0]).not.toHaveProperty('document_context');
});
