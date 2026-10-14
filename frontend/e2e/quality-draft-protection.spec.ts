import { expect, test } from '@playwright/test';

// 草稿保护回归:15 秒轮询在员工编辑复核表单期间不得静默覆盖草稿;
// 案例版本真的变化时提示,由用户主动载入。
// 仅 mock API,不触碰真实案例记录。

test('员工复核草稿不被轮询覆盖,版本变化时提示并可主动载入', async ({ page }) => {
  test.setTimeout(90_000);
  const employee = { username: 'drafter', role: 'employee', department_id: 9, department_name: '硬件部' };
  const row = {
    case_id: 'case-draft', snapshot_id: 'snap-draft', turn_id: 'turn-draft', assistant_message_id: 1,
    answer_revision: 'rev-draft', department_id: 9, status: 'reviewed', row_version: 1,
    triggers: ['feedback'], sampling: {}, manifest: {}, source_valid: true, projection_status: [],
    content: { query: '固定提问', answer: '固定回答' },
    content_access: 'owner', share: { active: true },
    feedback: null, feedback_history: [], reviews: [],
    created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z',
  };
  const detail = (version: number) => ({
    ...row,
    row_version: version,
    reviews: version > 1
      ? [{ review_id: 'r-other', judgment: 'confirmed', root_cause: 'retrieval', correctness: null, supersedes: null, created_at: '2026-10-09T00:16:00Z', content: { notes: '他人已提交的复核说明', reference_answer: '', rubric: '' } }]
      : [],
  });
  let detailVersion = 1;
  await page.addInitScript((user) => {
    localStorage.setItem('hdb_auth_session', JSON.stringify({ token: 'draft-regression-only', user }));
  }, employee);
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/v1/whoami') return route.fulfill({ json: employee });
    if (path === '/api/v1/quality/cases') return route.fulfill({ json: [row] });
    if (path === '/api/v1/quality/cases/case-draft') {
      return route.fulfill({ json: detail(detailVersion) });
    }
    if (path === '/api/v1/quality/reports') return route.fulfill({ json: [] });
    if (path === '/api/v1/notifications') return route.fulfill({ json: { items: [], unread_count: 0 } });
    return route.fulfill({ status: 404, json: { detail: 'unexpected regression API request' } });
  });

  await page.goto('/admin/quality');
  await page.getByTitle('case-draft', { exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();

  // 写入未提交草稿;随后案例在别处被更新到 v2(轮询下一次拉取即返回 v2)
  const notes = page.getByLabel('复核说明');
  await notes.fill('我尚未提交的草稿');
  detailVersion = 2;

  // 轮询(15s 间隔)带回 v2:不得覆盖草稿,应出现版本变化提示
  await expect(page.getByText('案例数据已更新')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('正在编辑的草稿未被覆盖')).toBeVisible();
  await expect(notes).toHaveValue('我尚未提交的草稿');
  await expect(page.getByLabel('复核说明')).not.toHaveValue('他人已提交的复核说明');

  // 用户主动载入:草稿被替换为最新版本,提示消失
  await page.getByRole('button', { name: /载入最新版本/ }).click();
  await expect(notes).toHaveValue('他人已提交的复核说明');
  await expect(page.getByText('案例数据已更新')).toBeHidden();
});
