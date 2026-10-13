import { expect, test, type Page } from '@playwright/test';

test.skip(process.env.HDB_QUALITY_E2E !== '1', 'Run with the isolated quality_acceptance.py server.');

async function login(page: Page, username: string) {
  await page.goto('/');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByPlaceholder('用户名', { exact: true }).fill(username);
  await page.getByPlaceholder('密码', { exact: true }).fill('BrowserTestPassword123!');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => Boolean(localStorage.getItem('hdb_auth_session')))).toBe(true);
}

test('fixed answer → negative feedback → named copy → review → revoke', async ({ page, browser, baseURL }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await login(page, 'quality_owner');
  await page.goto('/chat?kb=shared');
  await expect(page.getByText('验收固定回答：电阻应为 10 kΩ。')).toBeVisible();
  await page.getByRole('button', { name: '没帮助', exact: true }).click();
  await page.getByLabel('事实错误', { exact: true }).check();
  await page.getByLabel('补充说明（可选）').fill('请核对参考设计阻值');
  const feedbackResponse = page.waitForResponse((response) => response.url().endsWith('/feedback') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '提交反馈', exact: true }).click();
  const response = await feedbackResponse;
  expect(response.status()).toBe(200);
  const { case_id: caseId } = await response.json();
  await page.reload();
  await expect(page.getByRole('button', { name: '没帮助', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: '提交复核材料', exact: true }).click();
  await page.getByLabel('quality_reviewer', { exact: true }).check();
  await page.getByLabel('固定参考来源 [1]', { exact: true }).check();
  await page.getByRole('button', { name: '共享所选材料', exact: true }).click();
  await expect(page.getByRole('button', { name: '撤销材料共享', exact: true })).toBeVisible();

  const context = await browser.newContext({ baseURL });
  const reviewer = await context.newPage();
  reviewer.on('pageerror', (error) => errors.push(error.message));
  await login(reviewer, 'quality_reviewer');
  await reviewer.goto(`/admin/quality?case=${caseId}`);
  await expect(reviewer.getByText('验收固定回答：电阻应为 10 kΩ。')).toBeVisible();
  await expect(reviewer.getByText('冻结证据：参考设计使用 4.7 kΩ。')).toBeVisible();
  await expect(reviewer.getByText('请核对参考设计阻值')).toHaveCount(0);
  await reviewer.getByLabel('事实正确性').selectOption('incorrect');
  await reviewer.getByLabel('参考答案（经确认后填写）').fill('参考设计使用 4.7 kΩ。');
  await reviewer.getByLabel('复核说明', { exact: true }).fill('已复核冻结证据');
  await reviewer.getByRole('button', { name: '保存复核结论', exact: true }).click();
  await expect(reviewer.getByText('已保存复核结论')).toBeVisible();
  await reviewer.getByRole('button', { name: '冻结已确认案例版本', exact: true }).click();
  await expect(reviewer.getByText('已冻结数据集来源版本')).toBeVisible();
  await reviewer.getByRole('button', { name: '读取当前授权的固定评测材料', exact: true }).click();
  await expect(reviewer.getByText('评测样本 / 生产回答快照')).toBeVisible();
  // Refresh restores the current optimistic-lock version before revocation.
  await page.reload();
  await page.getByRole('button', { name: '撤销材料共享', exact: true }).click();
  await expect(page.getByText('已撤销复核材料共享')).toBeVisible();
  await reviewer.reload();
  await expect(reviewer.getByText('当前仅有元数据访问权限，正文与证据未共享或共享已失效。')).toBeVisible();
  await expect(reviewer.getByText('验收固定回答：电阻应为 10 kΩ。')).toHaveCount(0);
  await expect(reviewer.getByRole('button', { name: '保存复核结论', exact: true })).toHaveCount(0);
  await expect(reviewer.getByText('已复核冻结证据')).toHaveCount(0);
  await expect(reviewer.getByRole('button', { name: '读取当前授权的固定评测材料', exact: true })).toHaveCount(0);
  expect(errors).toEqual([]);
  await context.close();
});

test('cost page scoped totals, purpose filtering, task drilldown and CSV', async ({ page }) => {
  await login(page, 'quality_owner');
  await page.goto('/admin/costs');
  await expect(page.getByRole('heading', { name: '费用趋势（UTC 日桶）' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '模型已知费用排行（按币种）' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '版本化费用预算' })).toBeVisible();
  await expect(page.getByRole('heading', { name: /fixture-department-budget · 已知金额超预算/ })).toBeVisible();
  await expect(page.getByText('USD 0.0068', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('unknown_usage', { exact: true })).toBeVisible();
  await expect(page.getByText('cross-department-task')).toHaveCount(0);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 CSV', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('llm-costs.csv');
  const taskButton = page.locator('tbody button').first();
  await taskButton.click();
  await expect(page.getByRole('heading', { name: /任务全部执行尝试：2 调用/ })).toBeVisible();
  await page.getByLabel('调用用途', { exact: true }).selectOption('evaluation_judge');
  await expect(page.getByText('当前范围没有调用记录')).toBeVisible();
  await page.goto('/admin/quality');
  await page.getByRole('button', { name: '生成统计报告', exact: true }).click();
  await expect(page.locator('summary').filter({ hasText: /案例 · 2 调用 · 1 未知费用/ })).toBeVisible();
});
