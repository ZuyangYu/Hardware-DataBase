import { defineConfig } from '@playwright/test';

const port = Number(process.env.HDB_BROWSER_PORT || '5220');
const baseURL = process.env.HDB_BROWSER_BASE_URL || `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './e2e',
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['json', { outputFile: 'test-results/acceptance.json' }]],
  use: { baseURL, browserName: 'chromium', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: process.env.HDB_BROWSER_BASE_URL ? undefined : {
    command: `npm run dev -- --port ${port}`,
    url: baseURL,
    reuseExistingServer: false,
    env: { VITE_API_PROXY_TARGET: process.env.HDB_BROWSER_API_URL || 'http://127.0.0.1:8210' },
  },
});
