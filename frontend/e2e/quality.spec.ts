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

test('real API: employee submits → administrator responds → employee reopens → administrator closes and deletes', async ({ page, browser, baseURL }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await login(page, 'quality_owner');
  await page.goto('/chat?kb=shared');
  await expect(page.getByText('验收固定回答：电阻应为 10 kΩ。')).toBeVisible();
  await page.getByRole('button', { name: '回答有问题', exact: true }).click();
  await page.getByLabel('事实错误', { exact: true }).check();
  await page.getByLabel('补充说明（可选）').fill('请核对参考设计阻值');
  await page.getByLabel('联系方式（可选）').fill('企业微信：quality_owner');
  const submitted = page.waitForResponse(response => response.url().endsWith('/feedback') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '提交工单', exact: true }).click();
  const receipt = await submitted;
  expect(receipt.status()).toBe(200);
  const { ticket_id: ticketId } = await receipt.json();
  expect(ticketId).toBeTruthy();
  await page.getByRole('link', { name: '查看工单 · 待处理', exact: true }).click();
  await expect(page.getByRole('heading', { name: '工单详情', exact: true })).toBeVisible();

  const adminContext = await browser.newContext({ baseURL });
  const peerContext = await browser.newContext({ baseURL });
  try {
    const admin = await adminContext.newPage();
    admin.on('pageerror', error => errors.push(error.message));
    await login(admin, 'quality_admin');
    await admin.getByRole('button', { name: '通知（1 条未读）', exact: true }).click();
    await admin.getByRole('button', { name: /有新的反馈工单/ }).click();
    await expect(admin).toHaveURL(`${baseURL}/admin/quality?ticket=${ticketId}`);
    const adminDialog = admin.getByRole('dialog', { name: '工单详情' });
    await expect(adminDialog.getByText('请核对参考设计阻值', { exact: true }).first()).toBeVisible();
    await expect(adminDialog.getByText('企业微信：quality_owner', { exact: true })).toBeVisible();
    await expect(adminDialog.getByText('Quality acceptance', { exact: true })).toBeVisible();
    await adminDialog.getByText('查看原回答', { exact: true }).click();
    await expect(adminDialog.getByText('验收固定回答：电阻应为 10 kΩ。', { exact: true })).toBeVisible();
    await adminDialog.getByRole('button', { name: '认领工单', exact: true }).click();
    await expect(adminDialog.getByText('处理中', { exact: true }).first()).toBeVisible();
    await adminDialog.getByLabel('回复或解决说明').fill('已核对资料，阻值应为 4.7 kΩ');
    await adminDialog.getByRole('button', { name: '标记已解决', exact: true }).click();
    await expect(adminDialog.getByText('已解决', { exact: true }).first()).toBeVisible();

    const peer = await peerContext.newPage();
    await login(peer, 'quality_reviewer');
    await peer.goto(`/admin/quality?ticket=${ticketId}`);
    await expect(peer.getByRole('alert')).toContainText('工单不存在或无权访问');
    await expect(peer.getByText('企业微信：quality_owner', { exact: true })).toHaveCount(0);
    await expect(peer.getByText('请核对参考设计阻值', { exact: true })).toHaveCount(0);

    await page.reload();
    const ownerDialog = page.getByRole('dialog', { name: '工单详情' });
    await expect(ownerDialog.getByText('已解决', { exact: true }).first()).toBeVisible();
    await ownerDialog.getByLabel('回复或解决说明').fill('还需要说明版本差异');
    await ownerDialog.getByRole('button', { name: '重新打开', exact: true }).click();
    await expect(ownerDialog.getByText('待处理', { exact: true }).first()).toBeVisible();
    await admin.reload();
    await adminDialog.getByRole('button', { name: '关闭工单', exact: true }).click();
    await admin.getByRole('alertdialog').getByRole('button', { name: '确认关闭', exact: true }).click();
    await expect(adminDialog.getByText('已关闭', { exact: true }).first()).toBeVisible();
    await adminDialog.getByRole('button', { name: '删除工单', exact: true }).click();
    await admin.getByRole('alertdialog').getByRole('button', { name: '确认删除', exact: true }).click();
    await expect(adminDialog).toBeHidden();
    await expect(admin.getByText('暂无反馈工单', { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByRole('alert')).toContainText('工单不存在或无权访问');
    // Support deletion does not delete the original conversation.
    await page.goto('/chat?kb=shared');
    await expect(page.getByText('验收固定回答：电阻应为 10 kΩ。', { exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    await adminContext.close();
    await peerContext.close();
  }
});

test('cost page scoped totals, purpose filtering, task drilldown and CSV', async ({ page }) => {
  await login(page, 'quality_owner');
  await page.goto('/admin/costs');
  await expect(page.getByRole('heading', { name: '趋势与消耗来源' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '内部观测预算' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'fixture-department-budget', exact: true })).toBeVisible();
  await expect(page.getByText('USD 0.0068', { exact: true }).first()).toBeVisible();
  await page.getByLabel(/显示诊断记录/).check();
  await expect(page.getByText('费用未知：缺少用量', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('cross-department-task')).toHaveCount(0);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出 CSV', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('llm-costs.csv');
  const taskButton = page.locator('tbody button[title^="查看任务 "]').first();
  await taskButton.click();
  await expect(page.getByRole('heading', { name: '任务执行尝试', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('heading', { name: /2 次调用/ })).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await page.locator('summary').filter({ hasText: '高级筛选' }).click();
  await page.getByLabel('调用用途', { exact: true }).selectOption('evaluation_judge');
  await page.getByRole('button', { name: '应用筛选', exact: true }).click();
  await expect(page.getByText('当前范围没有调用记录')).toBeVisible();

});
