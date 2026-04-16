import { defineConfig } from '@playwright/test';
import path from 'path';
import dotenv from 'dotenv';

// Load e2e test credentials from e2e/.env
dotenv.config({ path: path.resolve(__dirname, 'e2e', '.env') });

const authDir = path.join(__dirname, 'e2e', '.auth');
const backendDir = path.resolve(__dirname, '..', '..', 'backend');

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 2 : 0,
  workers: 1,
  reporter: 'html',
  use: {
    baseURL: 'http://localhost:4200',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: 'uv run python src/apis/app_api/main.py',
      cwd: backendDir,
      url: 'http://localhost:8000/health',
      reuseExistingServer: !process.env['CI'],
      timeout: 60_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'uv run python src/apis/inference_api/main.py',
      cwd: backendDir,
      url: 'http://localhost:8001/ping',
      reuseExistingServer: !process.env['CI'],
      timeout: 60_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: 'npm run start -- --port 4200',
      url: 'http://localhost:4200',
      reuseExistingServer: !process.env['CI'],
      timeout: 120_000,
    },
  ],
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
