import { expect, test, type Page } from '@playwright/test';

async function openChat(page: Page, unavailableStatus: number) {
  const user = { username: 'feedback-http-owner', role: 'employee', department_id: 7, department_name: 'Regression' };
  const errors: string[] = [];
  const submissions: Record<string, unknown>[] = [];
  const unexpected: string[] = [];
  let available = false;
  const state = { assistant_message_id: 42, answer_revision: 'fixed-answer-revision', case_id: null as string | null,
    status: null as string | null, row_version: null as number | null, rating: null as string | null,
    reason_codes: [] as string[], comment: '', evidence_count: 0, content_available: true, share: {} };
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

for (const entry of ['feedback', 'notification'] as const) {
test(`owner can process a review request from chat ${entry}`, async ({ page }) => {
  const user = { username: 'feedback-request-owner', role: 'employee', department_id: 7, department_name: 'Regression' };
  const detail = {
    case_id: 'chat-review-request', snapshot_id: 'chat-review-snapshot', turn_id: 'chat-review-turn', assistant_message_id: 42,
    answer_revision: 'fixed-answer-revision', department_id: 7, status: 'new', row_version: 3,
    triggers: ['feedback'], sampling: {}, manifest: { content_available: true }, content: { query: '固定提问', answer: '固定回答', evidence: [] }, content_access: 'owner', source_valid: true,
    share: { active: false }, feedback: null, feedback_history: [], reviews: [],
    review_requests: [{ request_id: 'chat-review-1', status: 'pending', message: '请协助复核', created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', decided_at: null }],
    created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', projection_status: [],
  };
  const state = { assistant_message_id: 42, answer_revision: 'fixed-answer-revision', case_id: detail.case_id, status: 'new', row_version: 3, rating: null, reason_codes: [], comment: '', evidence_count: 0, content_available: true, share: {}, review_requests: detail.review_requests };
  let approved = false;
  const approvals: Record<string, unknown>[] = [];
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'chat-review-request-regression-only', user: sessionUser }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [{ id: 7, user_id: 101, kb_name: '__general__', title: '复核请求验收', created_at: '2026-09-28T00:00:00Z', updated_at: '2026-09-28T00:00:00Z' }] });
    if (path === '/api/v1/conversations/7/messages') return route.fulfill({ json: [
      { id: 41, session_id: 7, turn_id: 'chat-review-turn', role: 'user', content: '固定提问', created_at: '2026-09-28T00:00:00Z' },
      { id: 42, session_id: 7, turn_id: 'chat-review-turn', role: 'assistant', content: '固定回答', created_at: '2026-09-28T00:00:00Z' },
    ] });
    if (path === '/api/v1/chat/turns/chat-review-turn/feedback') return route.fulfill({ json: approved ? { ...state, review_requests: [] } : state });
    if (path === '/api/v1/quality/cases/chat-review-request') return route.fulfill({ json: approved ? { ...detail, row_version: 4, review_requests: [{ ...detail.review_requests[0], status: 'approved' }] } : detail });
    if (path === '/api/v1/quality/reviewers') return route.fulfill({ json: [{ id: 2, username: 'reviewer' }] });
    if (path === '/api/v1/quality/cases/chat-review-request/review-requests/chat-review-1/approve') {
      approvals.push(route.request().postDataJSON());
      approved = true;
      return route.fulfill({ json: { ...detail, row_version: 4, review_requests: [{ ...detail.review_requests[0], status: 'approved' }] } });
    }
    if (path === '/api/v1/quality/cases' || path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: entry === 'notification' ? [{ notification_id: 5, kind: 'quality_review_request', target_type: 'quality_case', target_id: detail.case_id, title: '质量复核通知', body: '请确认共享范围', payload: {}, read: false, created_at: detail.created_at }] : [], unread_count: entry === 'notification' ? 1 : 0 } });
    if (path === '/api/v1/notifications/5/read') return route.fulfill({ json: { ok: true } });
    if (path === '/api/v1/conversations/7/memory-summary') return route.fulfill({ json: { auto_extract_enabled: false, extracted_memories: 0 } });
    if (path === '/api/v1/memory-consents') return route.fulfill({ json: { items: [] } });
    if (path === '/api/v1/plugins/mcp/write-proposals') return route.fulfill({ json: { items: [] } });
    if (path === '/api/v1/exports/formats') return route.fulfill({ json: [] });
    if (path === '/api/v1/document-generation/chat-tasks/current') return route.fulfill({ json: null });
    if (path === '/api/v1/document-generation/chat-tasks/events') return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
    if (['/api/v1/kbs', '/api/v1/exports', '/api/v1/conversations/7/turns', '/api/v1/conversations/7/attachments'].includes(path)) return route.fulfill({ json: [] });
    return route.fulfill({ status: 404, json: { detail: 'unexpected chat review request API request' } });
  });
  await page.goto(entry === 'notification' ? '/admin/quality' : '/chat');
  if (entry === 'notification') {
    await page.getByRole('button', { name: '通知（1 条未读）', exact: true }).click();
    await page.getByRole('button', { name: /质量复核通知/ }).click();
    await expect(page).toHaveURL(/\/chat\?quality_case=chat-review-request$/);
  }
  const surface = entry === 'notification' ? page.getByRole('dialog') : page;
  await expect(surface.getByRole('heading', { name: '待处理的复核请求', exact: true })).toBeVisible();
  await surface.getByLabel('reviewer', { exact: true }).check();
  await surface.getByRole('button', { name: '批准并共享复核材料', exact: true }).click();
  await expect(surface.getByRole('heading', { name: '待处理的复核请求', exact: true })).toBeHidden();
  expect(approvals).toHaveLength(1);
  expect(approvals[0]).toMatchObject({ row_version: 3, reviewer_user_ids: [2], fields: ['query', 'answer'], retention_days: 7 });
});
}

test('historical answer shows missing-record explanation, then feedback works after retry on HTTP', async ({ page }) => {
  const fixture = await openChat(page, 404);
  await expect(page.getByText('该回答尚无可用质量记录；历史回答需由管理员补采', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '没帮助', exact: true })).toHaveCount(0);
  fixture.restore();
  await page.getByRole('button', { name: '重试加载反馈', exact: true }).click();
  await expect(page.getByRole('button', { name: '有帮助', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '没帮助', exact: true }).click();
  const feedbackDialog = page.getByRole('dialog');
  const feedbackDialogBox = await feedbackDialog.boundingBox();
  expect(feedbackDialogBox?.width).toBeGreaterThanOrEqual(520);
  expect(feedbackDialogBox?.height).toBeLessThanOrEqual(688);
  await page.keyboard.press('Escape');
  await expect(feedbackDialog).toHaveCount(0);
  await page.getByRole('button', { name: '没帮助', exact: true }).click();
  await page.getByLabel('事实错误', { exact: true }).check();
  await page.getByLabel('补充说明（可选）').fill('核对历史固定回答');
  await page.getByRole('button', { name: '提交反馈', exact: true }).click();
  await expect(page.getByRole('button', { name: '没帮助', exact: true })).toHaveAttribute('aria-pressed', 'true');
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

test('feedback share dialog has a usable wide scrollable layout', async ({ page }) => {
  const fixture = await openChat(page, 404);
  fixture.restore();
  await page.getByRole('button', { name: '重试加载反馈', exact: true }).click();
  await page.getByRole('button', { name: '没帮助', exact: true }).click();
  await page.getByRole('button', { name: '提交反馈', exact: true }).click();
  await expect(page.getByRole('button', { name: '提交复核材料', exact: true })).toBeVisible();
  await page.route('**/api/v1/quality/reviewers', (route) => route.fulfill({ json: Array.from({ length: 18 }, (_, id) => ({ id: id + 1, username: `reviewer-${id + 1}` })) }));
  await page.getByRole('button', { name: '提交复核材料', exact: true }).click();
  const shareDialog = page.getByRole('dialog');
  const shareDialogBox = await shareDialog.boundingBox();
  expect(shareDialogBox?.width).toBeGreaterThanOrEqual(600);
  expect(shareDialogBox?.height).toBeLessThanOrEqual(688);
  await expect(page.getByText('reviewer-18', { exact: true })).toBeVisible();
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
  await page.getByRole('button', { name: '没帮助', exact: true }).click();
  const box = await page.getByRole('dialog').boundingBox();
  expect(box).not.toBeNull();
  expect(box?.x).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(390);
  await expect(page.getByLabel('补充说明（可选）')).toBeVisible();
  expect(fixture.errors).toEqual([]);
});
