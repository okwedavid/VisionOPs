import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end tests run against a **real** backend serving the built SPA.
 *
 * There is no stub server and no mocked API: the point of these tests is to
 * catch the wiring failures that unit tests structurally cannot see — a route
 * the backend does not have, a DTO the UI misreads, a WebSocket that never
 * opens. Everything else in the suite is faster and cheaper.
 *
 * The backend runs on a throwaway SQLite database in a temp directory, seeded
 * by the global setup with a known API key. Nothing here touches a real camera
 * or a real deployment.
 */
const PORT = Number(process.env.E2E_PORT ?? 8399)
const BASE_URL = `http://127.0.0.1:${PORT}`

/**
 * Prefer a Playwright-managed Chromium, but allow an already-installed Chrome.
 * CI runs `npx playwright install --with-deps chromium`; a developer machine
 * that already has Chrome can just set E2E_CHROME=/path/to/chrome.
 */
const SYSTEM_CHROME = process.env.E2E_CHROME ?? undefined

export default defineConfig({
  testDir: './e2e',
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',

  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    // The webcam path is the one feature that genuinely needs a device. Chromium's
    // fake capture device gives the browser a real MediaStream to negotiate, so
    // getUserMedia/permission/frame-push code paths run for real.
    permissions: ['camera'],
    launchOptions: {
      ...(SYSTEM_CHROME ? { executablePath: SYSTEM_CHROME } : {}),
      args: [
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
        '--autoplay-policy=no-user-gesture-required',
        // Required in containers, where /dev/shm is typically 64 MB.
        '--disable-dev-shm-usage',
      ],
    },
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
