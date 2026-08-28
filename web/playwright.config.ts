import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  use: { baseURL: 'http://127.0.0.1:5173', trace: 'retain-on-failure' },
  webServer: [
    { command: 'rm -rf /tmp/parseflow-e2e && DATA_DIR=/tmp/parseflow-e2e STORAGE_MIN_FREE_MB=1 uv run --project .. uvicorn app.main:app --app-dir .. --host 127.0.0.1 --port 8011', url: 'http://127.0.0.1:8011/ready', reuseExistingServer: false, timeout: 120_000 },
    { command: 'PARSEFLOW_BACKEND=http://127.0.0.1:8011 npm run dev -- --host 127.0.0.1', url: 'http://127.0.0.1:5173', reuseExistingServer: false, timeout: 120_000 },
  ],
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], channel: process.env.PARSEFLOW_USE_SYSTEM_CHROME ? 'chrome' : undefined } }],
})
