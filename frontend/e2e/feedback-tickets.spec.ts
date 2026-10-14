import { expect, test, type Page } from '@playwright/test';

async function tickets(page: Page, role: 'employee' | 'system_admin', status = 'open', legacyNotificationKind?: string) {
  const user = { username: role === 'employee' ? 'employee-a' : 'support-admin', role, department_id: role === 'employee' ? 7 : null, department_name: '硬件研发部' };
  const row = {
    ticket_id: 'ticket-browser', case_id: 'case-browser', owner_user_id: 1,
    requester_username: 'employee-a', department_id: 7, department_name: '硬件研发部',
    assignee_user_id: null as number | null, assignee_username: '', status, row_version: 1,
    consent_active: true, content_available: true, reason_codes: ['factual_error'], summary: '电流参数有误',
    comment: '电流参数有误', contact: '企业微信：employee-a', resolution: '',
    content: { query: '额定电流是多少？', answer: '旧回答：1A' },
    created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z',
    events: [{ event_id: 'event-1', actor_user_id: 1, actor_username: 'employee-a', actor_role: 'employee', kind: 'submit', message: '电流参数有误', status: 'open', created_at: '2026-10-14T00:00:00Z' }],
  };
  const actions: Record<string, unknown>[] = [];
  let deleted = false;
  await page.addInitScript(user => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'browser-only', user })), user);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: legacyNotificationKind ? [{ notification_id: 5, kind: legacyNotificationKind, target_type: 'quality_case', target_id: 'case-browser', title: '历史质量通知', body: '', payload: {}, read: false, created_at: row.created_at }] : [], unread_count: legacyNotificationKind ? 1 : 0 } });
    if (path === '/api/v1/notifications/5/read') return route.fulfill({ json: { ok: true } });
    if (path === '/api/v1/quality/ticket-assignees') return route.fulfill({ json: [{ id: 4, username: 'support-admin' }, { id: 5, username: 'support-b' }] });
    if (path === '/api/v1/quality/tickets') return route.fulfill({ json: { items: deleted ? [] : [row], total: deleted ? 0 : 1 } });
    if (path === '/api/v1/quality/tickets/ticket-browser/actions') {
      const body = route.request().postDataJSON();
      actions.push(body);
      if (body.row_version !== row.row_version) return route.fulfill({ status: 409, json: { detail: '工单已更新，请刷新后重试' } });
      row.row_version += 1;
      row.status = ({ claim: 'in_progress', assign: 'in_progress', wait: 'waiting', resolve: 'resolved', reopen: 'open', close: 'closed' } as Record<string, string>)[body.action] ?? row.status;
      if (body.action === 'resolve') row.resolution = body.message;
      if (body.action === 'claim') { row.assignee_user_id = 4; row.assignee_username = 'support-admin'; }
      if (body.action === 'assign') { row.assignee_user_id = 5; row.assignee_username = 'support-b'; }
      row.events.push({ event_id: `event-${row.row_version}`, actor_user_id: 4, actor_username: user.username, actor_role: role, kind: body.action, message: body.message ?? '', status: row.status, created_at: '2026-10-14T01:00:00Z' });
      return route.fulfill({ json: row });
    }
    if (path === '/api/v1/quality/tickets/ticket-browser') {
      if (route.request().method() === 'DELETE') { deleted = true; return route.fulfill({ json: { ok: true } }); }
      return route.fulfill({ json: row });
    }
    return route.fulfill({ json: [] });
  });
  await page.goto('/admin/quality');
  return { row, actions, isDeleted: () => deleted };
}

test('administrator handles, closes and confirms deletion of a ticket without professional evaluation', async ({ page }) => {
  const fixture = await tickets(page, 'system_admin');
  await expect(page.getByRole('heading', { name: '反馈工单', exact: true })).toBeVisible({ timeout: 5000 });
  await expect(page.getByRole('link', { name: '高级质量改进', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '查看工单', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '工单详情' });
  await expect(dialog.getByRole('link', { name: '高级质量改进', exact: true })).toHaveCount(0);
  await expect(dialog.getByText('企业微信：employee-a', { exact: true })).toBeVisible();
  await dialog.getByText('查看原回答', { exact: true }).click();
  await expect(dialog.getByText('旧回答：1A', { exact: true })).toBeVisible();
  await expect(dialog.getByRole('button', { name: '冻结已确认案例版本' })).toHaveCount(0);
  await dialog.getByRole('button', { name: '认领工单', exact: true }).click();
  await expect(dialog.getByText('处理中', { exact: true }).first()).toBeVisible();
  await dialog.getByLabel('回复或解决说明').fill('已联系员工，正确参数为 2A');
  await dialog.getByRole('button', { name: '标记已解决', exact: true }).click();
  await expect(dialog.getByText('已解决', { exact: true }).first()).toBeVisible();
  await dialog.getByRole('button', { name: '关闭工单', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: '确认关闭', exact: true }).click();
  await expect(dialog.getByText('已关闭', { exact: true }).first()).toBeVisible();
  await dialog.getByRole('button', { name: '删除工单', exact: true }).click();
  expect(fixture.isDeleted()).toBe(false);
  await page.getByRole('alertdialog').getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText('暂无反馈工单', { exact: true })).toBeVisible();
  expect(fixture.actions.map(x => x.action)).toEqual(['claim', 'resolve', 'close']);
  expect(fixture.isDeleted()).toBe(true);
});

for (const role of ['employee', 'system_admin'] as const) {
  test(`${role} old advanced and case links return to feedback tickets`, async ({ page }) => {
    const requests: string[] = [];
    page.on('request', request => { if (/\/api\/v1\/quality\/(cases|reports|reviewers)/.test(request.url())) requests.push(request.url()); });
    await tickets(page, role);
    for (const url of ['/admin/quality/advanced?case=case-browser', '/admin/quality?case=case-browser']) {
      await page.goto(url);
      await expect(page).toHaveURL(/\/admin\/quality$/);
      await expect(page.getByRole('heading', { name: role === 'system_admin' ? '反馈工单' : '我的反馈', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '查看工单', exact: true })).toBeVisible();
    }
    expect(requests).toEqual([]);
  });
}

for (const kind of ['quality_review_request', 'quality_case_updated']) {
  test(`legacy ${kind} notification opens feedback tickets`, async ({ page }) => {
    await tickets(page, 'employee', 'open', kind);
    await page.getByRole('button', { name: '通知（1 条未读）', exact: true }).click();
    await page.getByRole('button', { name: /历史质量通知/ }).click();
    await expect(page).toHaveURL(/\/admin\/quality$/);
    await expect(page.getByRole('heading', { name: '我的反馈', exact: true })).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });
}

test('employee can reopen and supplement their feedback without administrator controls', async ({ page }) => {
  const fixture = await tickets(page, 'employee', 'resolved');
  await expect(page.getByRole('heading', { name: '我的反馈', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看工单', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '工单详情' });
  await expect(dialog.getByRole('button', { name: '删除工单' })).toHaveCount(0);
  await expect(dialog.getByRole('button', { name: '标记已解决' })).toHaveCount(0);
  await dialog.getByLabel('回复或解决说明').fill('仍然存在问题');
  await dialog.getByRole('button', { name: '重新打开', exact: true }).click();
  await dialog.getByLabel('回复或解决说明').fill('补充型号 A');
  await dialog.getByRole('button', { name: '发送补充', exact: true }).click();
  await expect(dialog.getByText('补充型号 A', { exact: true })).toBeVisible();
  expect(fixture.actions.map(x => x.action)).toEqual(['reopen', 'reply']);
});

test('chat problem submission explicitly shares only the submitted answer and creates a ticket', async ({ page }) => {
  const user = { username: 'employee-a', role: 'employee', department_id: 7, department_name: '硬件研发部' };
  const state = { assistant_message_id: 42, answer_revision: 'a'.repeat(64), case_id: null, status: null, row_version: null,
    rating: null, reason_codes: [], comment: '', share: {}, evidence_count: 0, content_available: true, ticket_id: null, ticket_status: null };
  let submitted: Record<string, unknown> | null = null;
  await page.addInitScript(user => localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'browser-only', user })), user);
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/conversations') return route.fulfill({ json: [{ id: 7, user_id: 1, kb_name: '__general__', title: '工单反馈', created_at: '', updated_at: '' }] });
    if (path === '/api/v1/conversations/7/messages') return route.fulfill({ json: [
      { id: 41, session_id: 7, turn_id: 'turn-ticket', role: 'user', content: '额定电流是多少？', created_at: '' },
      { id: 42, session_id: 7, turn_id: 'turn-ticket', role: 'assistant', content: '额定电流 1A', created_at: '' },
    ] });
    if (path === '/api/v1/chat/turns/turn-ticket/feedback') {
      if (route.request().method() === 'POST') {
        submitted = route.request().postDataJSON();
        Object.assign(state, { rating: 'unhelpful', comment: submitted?.comment, ticket_id: 'ticket-created', ticket_status: 'open' });
        return route.fulfill({ json: { event_id: 'feedback-created', ticket_id: 'ticket-created', case_id: 'case-created', deduplicated: false } });
      }
      return route.fulfill({ json: state });
    }
    if (path.endsWith('/current')) return route.fulfill({ json: null });
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: ': idle\n\n' });
    if (path === '/api/v1/memory-consents' || path === '/api/v1/plugins/mcp/write-proposals') return route.fulfill({ json: { items: [] } });
    return route.fulfill({ json: [] });
  });
  await page.goto('/chat');
  await page.getByRole('button', { name: '回答有问题', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('提交后，处理管理员可查看本次提问、回答和补充说明，用于联系你解决问题。', { exact: true })).toBeVisible();
  await dialog.getByLabel('补充说明（可选）').fill('电流应为 2A');
  await dialog.getByLabel('联系方式（可选）').fill('企业微信：employee-a');
  await dialog.getByRole('button', { name: '提交工单', exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole('link', { name: '查看工单 · 待处理', exact: true })).toHaveAttribute('href', '/admin/quality?ticket=ticket-created');
  expect(submitted).toMatchObject({ rating: 'unhelpful', share_with_support: true, comment: '电流应为 2A', support_contact: '企业微信：employee-a' });
  await expect(page.getByRole('button', { name: '提交复核材料' })).toHaveCount(0);
});
