import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

const evidenceDir = process.env.P10_BROWSER_EVIDENCE_DIR
  ? path.resolve(process.env.P10_BROWSER_EVIDENCE_DIR)
  : path.resolve('..', '.local', 'phase10', 'p10-07-playwright-local', 'browser');
export default defineConfig({
  testDir: './tests/e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  outputDir: path.join(evidenceDir, 'test-output'),
  reporter: [
    ['list'],
    ['json', { outputFile: path.join(evidenceDir, 'playwright-report.json') }],
  ],
  use: {
    baseURL: process.env.P10_BASE_URL ?? 'http://127.0.0.1:5173',
    headless: process.env.P10_HEADFUL !== '1',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    ignoreHTTPSErrors: false,
    trace: process.env.P10_CAPTURE_TRACE === '1' ? 'retain-on-failure' : 'off',
    screenshot: 'only-on-failure',
    video: 'off',
    // Chromium and Edge are existing system installations.  No Playwright
    // browser download is required by this project configuration.
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], channel: 'chrome' },
    },
    {
      name: 'edge',
      use: { ...devices['Desktop Edge'], channel: 'msedge' },
    },
  ],
});
