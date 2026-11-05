import { expect, test } from '@playwright/test';

// Real end-to-end smoke: login -> create a task -> wait for the artifact.
// Uses the running dev server + API (no mocks) so it also covers the worker.
const username = process.env.HDB_E2E_USERNAME || '';
const password = process.env.HDB_E2E_PASSWORD || '';

test.skip(
  !username || !password,
  'set HDB_E2E_USERNAME/HDB_E2E_PASSWORD to run the real workbench test',
);

test('workbench creates a task and shows a downloadable artifact', async ({ page }) => {
  await page.goto('/');
  // The login page first shows a hero CTA, then the form.
  const loginCta = page.getByRole('button', { name: '登录', exact: true });
  if (await loginCta.isVisible().catch(() => false)) {
    await loginCta.click();
  }
  await expect(page.getByPlaceholder('用户名')).toBeVisible();
  await page.getByPlaceholder('用户名').fill(username);
  await page.getByPlaceholder('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).last().click();
  // Wait for the auth session to be persisted before navigating; otherwise
  // the next full page load races the login response.
  await expect
    .poll(
      () =>
        page.evaluate(() => localStorage.getItem('hdb_auth_session')),
      { timeout: 30_000 },
    )
    .not.toBeNull();

  await page.goto('/tasks');
  await expect(page.getByText('新建任务')).toBeVisible();
  await expect(page.getByText('任务列表')).toBeVisible();

  const taskText =
    '请直接使用工作区工具完成，不要委派子代理：在工作区 out/ui-test.md 写入 ui test，然后确认。';
  await page
    .getByPlaceholder(/例如：按 TCN2/)
    .fill(taskText);
  await page.getByRole('button', { name: '开始任务' }).click();

  // The task is created and the detail panel takes over.
  await expect(page.getByText('任务已创建')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/未挂载知识库/)).toBeVisible();

  // Only the detail panel for THIS task matters; the list contains older
  // completed tasks that would otherwise produce a false pass.
  await expect(page.getByTestId('task-detail-status')).toHaveText('已完成', {
    timeout: 120_000,
  });
  await expect(
    page.getByTestId('task-detail').getByTestId('task-artifact').first(),
  ).toContainText('ui-test.md', { timeout: 30_000 });
  await expect(page.getByTestId('task-answer')).toContainText('ui-test.md');
});
