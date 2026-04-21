/**
 * Playwright CI configuration for running E2E tests against a deployed stack.
 *
 * Unlike the local config (playwright.config.ts), this does NOT start any
 * web servers — the App API, Inference API, and frontend are already running
 * on the deployed nightly stack. The base URL is provided via E2E_BASE_URL.
 *
 * Usage:
 *   E2E_BASE_URL=https://nightly-develop-api.example.com \
 *   ADMIN_USERNAME=... ADMIN_PASSWORD=... \
 *   USER_USERNAME=... USER_PASSWORD=... \
 *   npx playwright test --config=playwright.ci.config.ts
 */
import { defineConfig } from '@playwright/test';
import path from 'path';

const baseURL = process.env['E2E_BASE_URL'];
if (!baseURL) {
  throw new Error('E2E_BASE_URL environment variable is required for CI config');
}

const authDir = path.join(__dirname, 'e2e', '.auth');

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: true,
  retries: 2,
  workers: 1,
  reporter: [['html', { open: 'never' }], ['list']],
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  /* No webServer — tests run against the already-deployed stack */

  projects: [
    // --- Setup projects (login & save storage state) ---
    {
      name: 'admin-setup',
      testMatch: /auth-admin\.setup\.ts/,
    },
    {
      name: 'user-setup',
      testMatch: /auth-user\.setup\.ts/,
    },

    // --- Unauthenticated tests (no login needed) ---
    {
      name: 'chromium',
      testIgnore: /\.setup\.ts|\.auth\./,
      testMatch: /(?:login|navigation|not-found)\.spec\.ts/,
      use: { browserName: 'chromium' },
    },

    // --- Authenticated tests (admin) ---
    {
      name: 'admin',
      testMatch: /\.auth\.spec\.ts/,
      dependencies: ['admin-setup'],
      use: {
        browserName: 'chromium',
        storageState: path.join(authDir, 'admin.json'),
      },
    },

    // --- Authenticated tests (regular user) ---
    {
      name: 'user',
      testMatch: /\.user\.spec\.ts/,
      dependencies: ['user-setup'],
      use: {
        browserName: 'chromium',
        storageState: path.join(authDir, 'user.json'),
      },
    },
  ],
});
