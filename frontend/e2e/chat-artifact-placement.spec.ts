import { expect, test } from '@playwright/test';

const username = process.env.HDB_E2E_USERNAME || '';
const password = process.env.HDB_E2E_PASSWORD || '';

test.skip(
  !username || !password,
  'set HDB_E2E_USERNAME/HDB_E2E_PASSWORD to run the real chat artifact test',
);

test('artifact filename renders above the helpful feedback action', async ({ page }) => {
  await page.goto('/');
  const loginCta = page.getByRole('button', { name: '登录', exact: true });
  if (await loginCta.isVisible().catch(() => false)) {
    await loginCta.click();
  }
  await expect(page.getByPlaceholder('用户名')).toBeVisible();
  await page.getByPlaceholder('用户名').fill(username);
  await page.getByPlaceholder('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).last().click();
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem('hdb_auth_session')), {
      timeout: 30_000,
    })
    .not.toBeNull();

  await page.goto('/chat?kb=AAA');
  const input = page.getByPlaceholder(/向「AAA」提问/);
  await expect(input).toBeVisible({ timeout: 30_000 });
  await input.fill(
    '查询 TCN2 网表 U1800 的型号和封装，用 task_publish_artifact 发布 ui-check.md，' +
      '内容包含型号、封装和引用编号；不要写路径。',
  );
  await input.press('Enter');

  const artifactRow = page.getByTestId('message-artifacts').last();
  await expect(artifactRow).toContainText('ui-check.md', { timeout: 180_000 });

  const helpful = page.getByRole('button', { name: '有帮助' }).last();
  await expect(helpful).toBeVisible({ timeout: 30_000 });

  const artifactBox = await artifactRow.boundingBox();
  const helpfulBox = await helpful.boundingBox();
  expect(artifactBox).not.toBeNull();
  expect(helpfulBox).not.toBeNull();
  // The filename row must be physically above the feedback action.
  expect(artifactBox!.y).toBeLessThan(helpfulBox!.y);
});
