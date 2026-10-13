import { expect, test } from '@playwright/test';

// HTTP + server IP does not expose crypto.randomUUID. Mock only the API so
// this regression renders the real workbench without touching live records.
test('administrator workbench loads and creates a report without randomUUID', async ({ page }) => {
  const user = { username: 'http-admin', role: 'system_admin', department_id: null, department_name: null };
  const errors: string[] = [];
  const requestIds: string[] = [];
  let reports: unknown[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript((user) => {
    Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true });
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'http-regression-only', user }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/quality/cases') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/reports') return route.fulfill({ json: reports });
    if (path === '/api/v1/quality/analysis-runs') {
      requestIds.push(route.request().postDataJSON().client_request_id);
      reports = [{ report_id: 'http-report', created_at: '2026-10-09T00:00:00Z', input_manifest_sha256: 'http-input',
        verified_statistics: { case_count: 0, costs: { calls: 0, unknown_cost_calls: 0 } }, recommendations: [] }];
      return route.fulfill({ json: { report_id: 'http-report', deduplicated: false } });
    }
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });
  await page.goto('/admin/quality');
  expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe('undefined');
  await expect(page.getByRole('heading', { name: '案例队列' })).toBeVisible();
  await expect(page.getByText('当前范围暂无案例')).toBeVisible();
  await expect(page.getByLabel('案例状态').locator('option[value="triaged"]')).toHaveCount(0);
  await expect(page.getByLabel('案例状态').locator('option[value="dataset_ready"]')).toHaveCount(0);
  await page.getByRole('button', { name: '生成统计报告', exact: true }).click();
  await expect(page.locator('summary').filter({ hasText: '0 案例 · 0 调用 · 0 未知费用' })).toBeVisible();
  expect(requestIds).toHaveLength(1);
  expect(requestIds[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(errors).toEqual([]);
});

test('administrator case detail explains the issue from safe metadata', async ({ page }) => {
  const user = { username: 'http-admin', role: 'system_admin', department_id: null, department_name: null };
  const detail = {
    case_id: 'case-meta', snapshot_id: 'snapshot-meta', turn_id: 'turn-meta', assistant_message_id: 11,
    answer_revision: 'a'.repeat(64), department_id: 47, status: 'reviewed', row_version: 3,
    triggers: ['feedback'], sampling: {}, manifest: { trace_id: 'trace-meta' }, content: {},
    content_access: 'hidden', source_valid: true, share: { active: false },
    feedback: { event_id: 'feedback-meta', rating: 'unhelpful', reason_codes: ['format_mismatch'], comment: '', created_at: '2026-10-09T03:47:01Z' },
    feedback_history: [{ event_id: 'feedback-meta', sequence: 1, rating: 'unhelpful', reason_codes: ['format_mismatch'], created_at: '2026-10-09T03:47:01Z' }, { event_id: 'feedback-meta-2', sequence: 2, rating: 'helpful', reason_codes: [], created_at: '2026-10-09T04:47:01Z' }],
    reviews: [{ review_id: 'review-meta', judgment: 'confirmed', root_cause: 'presentation', correctness: 'correct', supersedes: null, created_at: '2026-10-09T03:47:02Z', content: {} }],
    created_at: '2026-10-09T03:47:00Z', updated_at: '2026-10-09T03:47:02Z', projection_status: [],
  };
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'http-regression-only', user: sessionUser }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/quality/cases') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/cases/case-meta') return route.fulfill({ json: detail });
    if (path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });
  await page.goto('/admin/quality?case=case-meta');
  const caseDialog = page.getByRole('dialog');
  const caseDialogBox = await caseDialog.boundingBox();
  expect(caseDialogBox?.width).toBeGreaterThanOrEqual(900);
  expect(caseDialogBox?.height).toBeLessThanOrEqual(688);
  await expect(page.getByRole('heading', { name: '问题概览（管理员可见）', exact: true })).toBeVisible();
  await expect(page.getByText('反馈指出：格式不符', { exact: true })).toBeVisible();
  await expect(page.getByText('判断：确认问题 · 根因：前端呈现', { exact: true })).toBeVisible();
  await expect(page.getByText('事实正确性：已确认事实正确', { exact: true })).toBeVisible();
  await expect(page.getByText(/第 1 次：没帮助 · 格式不符/)).toBeVisible();
  await expect(page.getByRole('heading', { name: '管理员修复管理', exact: true })).toBeVisible();
  await expect(page.getByText(/前端呈现：检查回答格式、引用展示、字段完整性/)).toBeVisible();
  await expect(page.getByText(/候选必须通过质量门禁/)).toBeVisible();
  await expect(page.getByText(/Private|原始问答内容/)).toHaveCount(0);
  await expect(page.getByText('补充说明原文仅对原用户或明确授权的员工开放。', { exact: true })).toBeVisible();
});

test('administrator can request owner review without receiving private content', async ({ page }) => {
  const user = { username: 'http-admin', role: 'system_admin', department_id: null, department_name: null };
  const detail = {
    case_id: 'case-request', snapshot_id: 'snapshot-request', turn_id: 'turn-request', assistant_message_id: 12,
    answer_revision: 'b'.repeat(64), department_id: 47, status: 'new', row_version: 1,
    triggers: ['feedback'], sampling: {}, manifest: {}, content: {}, content_access: 'hidden', source_valid: true,
    share: { active: false }, feedback: null, feedback_history: [], reviews: [], review_requests: [],
    created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', projection_status: [],
  };
  let requestBody: Record<string, unknown> | null = null;
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'request-regression-only', user: sessionUser }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/quality/cases') return route.fulfill({ json: [] });
    if (path === '/api/v1/quality/cases/case-request') return route.fulfill({ json: detail });
    if (path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/quality/cases/case-request/review-requests' && route.request().method() === 'POST') {
      requestBody = route.request().postDataJSON();
      return route.fulfill({ json: { request_id: 'request-1', status: 'pending', created: true } });
    }
    return route.fulfill({ status: 404, json: { detail: 'unexpected review request API request' } });
  });
  await page.goto('/admin/quality?case=case-request');
  await page.getByRole('button', { name: '向原回答用户发起复核请求', exact: true }).click();
  await expect(page.getByText('复核请求已发出，等待原回答用户确认共享范围。', { exact: true })).toBeVisible();
  expect(requestBody).toMatchObject({ row_version: 1, message: expect.any(String) });
});

test('owner can choose reviewers and approve a review request', async ({ page }) => {
  const user = { username: 'http-owner', role: 'employee', department_id: 47, department_name: 'ADAS' };
  const pendingDetail = {
    case_id: 'case-owner-request', snapshot_id: 'snapshot-owner-request', turn_id: 'turn-owner-request', assistant_message_id: 13,
    answer_revision: 'c'.repeat(64), department_id: 47, status: 'new', row_version: 2,
    triggers: ['feedback'], sampling: {}, manifest: {}, content: { query: 'q', answer: 'a', evidence: [] }, content_access: 'owner', source_valid: true,
    share: { active: false }, feedback: null, feedback_history: [], reviews: [],
    review_requests: [{ request_id: 'request-owner-1', status: 'pending', message: '请协助复核', created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', decided_at: null }],
    created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', projection_status: [],
  };
  let approved = false;
  let approvalBody: Record<string, unknown> | null = null;
  await page.addInitScript((sessionUser) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'owner-request-regression-only', user: sessionUser }));
  }, user);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: user });
    if (path === '/api/v1/quality/cases') return route.fulfill({ json: [pendingDetail] });
    if (path === '/api/v1/quality/cases/case-owner-request') {
      return route.fulfill({ json: approved ? { ...pendingDetail, row_version: 3, review_requests: [{ ...pendingDetail.review_requests[0], status: 'approved', decided_at: '2026-10-09T00:01:00Z' }], share: { active: true, reviewer_user_ids: [2] } } : pendingDetail });
    }
    if (path === '/api/v1/quality/reviewers') return route.fulfill({ json: [{ id: 2, username: 'reviewer' }] });
    if (path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    if (path === '/api/v1/quality/cases/case-owner-request/review-requests/request-owner-1/approve' && route.request().method() === 'POST') {
      approvalBody = route.request().postDataJSON();
      approved = true;
      return route.fulfill({ json: { ...pendingDetail, row_version: 3, review_requests: [{ ...pendingDetail.review_requests[0], status: 'approved' }], share: { active: true, reviewer_user_ids: [2] } } });
    }
    return route.fulfill({ status: 404, json: { detail: 'unexpected owner review API request' } });
  });
  await page.goto('/admin/quality?case=case-owner-request');
  await expect(page.getByRole('heading', { name: '待处理的复核请求', exact: true })).toBeVisible();
  await page.getByLabel('reviewer', { exact: true }).check();
  await page.getByRole('button', { name: '批准并共享复核材料', exact: true }).click();
  await expect(page.getByRole('heading', { name: '待处理的复核请求', exact: true })).toBeHidden();
  expect(approvalBody).toMatchObject({ row_version: 2, reviewer_user_ids: [2], fields: ['query', 'answer'], retention_days: 7 });
});
