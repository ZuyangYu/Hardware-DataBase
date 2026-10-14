import { expect, test, type Page } from '@playwright/test';

async function openChat(page: Page, unavailableStatus: number, legacyReview = false) {
  const user = { username: 'feedback-http-owner', role: 'employee', department_id: 7, department_name: 'Regression' };
  const errors: string[] = [];
  const submissions: Record<string, unknown>[] = [];
  const unexpected: string[] = [];
  let available = legacyReview;
  const state = { assistant_message_id: 42, answer_revision: 'fixed-answer-revision', case_id: null as string | null,
    status: null as string | null, row_version: null as number | null, rating: null as string | null,
    reason_codes: [] as string[], comment: '', evidence_count: 0, content_available: true, share: {}, review_requests: legacyReview ? [{ request_id: 'legacy-review', status: 'pending' }] : [] };
  if (legacyReview) state.case_id = 'legacy-case';
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript((user) => {
    Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true });
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'feedback-http-regression-only', user }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [{ id: 7, user_id: 101, kb_name: '__general__', title: '历史反馈验收', created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z' }] });
    if (path === '/api/v1/conversations/7/messages') return route.fulfill({ json: [
      { id: 41, session_id: 7, turn_id: 'legacy-feedback-turn', role: 'user', content: '历史问题', created_at: '2026-09-28T00:00:00Z' },
      { id: 42, session_id: 7, turn_id: 'legacy-feedback-turn', role: 'assistant', content: '历史固定回答', created_at: '2026-09-28T00:00:00Z' },
    ] });
    if (path === '/api/v1/chat/turns/legacy-feedback-turn/feedback') {
      if (!available) return route.fulfill({ status: unavailableStatus, json: { detail: 'feedback unavailable' } });
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        submissions.push(body);
        Object.assign(state, { case_id: 'feedback-http-case', status: 'new', row_version: 2, rating: body.rating, reason_codes: body.reason_codes, comment: body.comment });
        return route.fulfill({ json: { event_id: 'feedback-http-event', case_id: state.case_id, deduplicated: false } });
      }
      return route.fulfill({ json: state });
    }
    if (path === '/api/v1/conversations/7/memory-summary') return route.fulfill({ json: { auto_extract_enabled: false, extracted_memories: 0 } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [] } });
    if (path === '/api/v1/plugins/mcp/write-proposals') return route.fulfill({ json: { items: [] } });
    if (path === '/api/v1/exports/formats') return route.fulfill({ json: [] });
    if (path === '/api/v1/document-generation/chat-tasks/current') return route.fulfill({ json: null });
    if (path === '/api/v1/document-generation/chat-tasks/events') return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (['/api/v1/kbs', '/api/v1/exports', '/api/v1/conversations/7/turns', '/api/v1/conversations/7/attachments'].includes(path)) return route.fulfill({ json: [] });
    unexpected.push(path);
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });
  await page.goto('/chat');
  await expect(page.getByText('历史固定回答', { exact: true })).toBeVisible();
  return { errors, submissions, unexpected, restore: () => { available = true; } };
}

test('historical answer shows missing-record explanation, then feedback works after retry on HTTP', async ({ page }) => {
  const fixture = await openChat(page, 404);
  await expect(page.getByText('该回答尚无可用质量记录；历史回答需由管理员补采', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '回答有问题', exact: true })).toHaveCount(0);
  fixture.restore();
  await page.getByRole('button', { name: '重试加载反馈', exact: true }).click();
  await expect(page.getByRole('button', { name: '有帮助', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '回答有问题', exact: true }).click();
  const feedbackDialog = page.getByRole('dialog');
  const feedbackDialogBox = await feedbackDialog.boundingBox();
  expect(feedbackDialogBox?.width).toBeGreaterThanOrEqual(520);
  expect(feedbackDialogBox?.height).toBeLessThanOrEqual(688);
  await page.keyboard.press('Escape');
  await expect(feedbackDialog).toHaveCount(0);
  await page.getByRole('button', { name: '回答有问题', exact: true }).click();
  await page.getByLabel('事实错误', { exact: true }).check();
  await page.getByLabel('补充说明（可选）').fill('核对历史固定回答');
  await page.getByRole('button', { name: '提交工单', exact: true }).click();
  await expect(page.getByRole('button', { name: '回答有问题', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(fixture.submissions).toHaveLength(1);
  expect(fixture.submissions[0]).toMatchObject({ assistant_message_id: 42, answer_revision: 'fixed-answer-revision', rating: 'unhelpful', reason_codes: ['factual_error'], comment: '核对历史固定回答' });
  expect(fixture.submissions[0].client_request_id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(fixture.errors).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('disabled feedback explains why the controls are unavailable', async ({ page }) => {
  const fixture = await openChat(page, 503);
  await expect(page.getByText('回答反馈功能当前未启用', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '重试加载反馈', exact: true })).toBeVisible();
  expect(fixture.submissions).toEqual([]);
  expect(fixture.errors).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('problem submission requires no reviewer selection', async ({ page }) => {
  const fixture = await openChat(page, 404);
  fixture.restore();
  await page.getByRole('button', { name: '重试加载反馈', exact: true }).click();
  await page.getByRole('button', { name: '回答有问题', exact: true }).click();
  await expect(page.getByLabel('联系方式（可选）')).toBeVisible();
  await page.getByRole('button', { name: '提交工单', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(page.getByRole('button', { name: '提交复核材料', exact: true })).toHaveCount(0);
  expect(fixture.submissions[0]).toMatchObject({ share_with_support: true });
  expect(fixture.errors).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('feedback dialog stays inside a narrow mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const fixture = await openChat(page, 404);
  await expect(page.getByRole('button', { name: '打开会话列表', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '打开会话列表', exact: true }).click();
  await expect(page.getByRole('button', { name: '收起会话侧栏', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '收起会话侧栏', exact: true }).click();
  fixture.restore();
  await page.getByRole('button', { name: '重试加载反馈', exact: true }).click();
  await page.getByRole('button', { name: '回答有问题', exact: true }).click();
  const box = await page.getByRole('dialog').boundingBox();
  expect(box).not.toBeNull();
  expect(box?.x).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
  await expect(page.getByLabel('补充说明（可选）')).toBeVisible();
  expect(fixture.errors).toEqual([]);
});

test('historical review state and chat deep link do not open retired review controls', async ({ page }) => {
  const fixture = await openChat(page, 404, true);
  await expect(page.getByRole('button', { name: '回答有问题', exact: true })).toBeVisible();
  await expect(page.getByText('高级质量复核', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('link', { name: '查看质量案例', exact: true })).toHaveCount(0);
  await page.goto('/chat?quality_case=legacy-case');
  await expect(page.getByText('历史固定回答', { exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(fixture.errors).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});
