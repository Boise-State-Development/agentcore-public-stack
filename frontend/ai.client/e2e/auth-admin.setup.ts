import { test as setup, expect } from '@playwright/test';
import path from 'path';

const ADMIN_FILE = path.join(__dirname, '.auth', 'admin.json');

/**
 * Logs in via the Cognito managed login UI and saves browser storage state.
 *
 * Flow: App login page → click "Sign in with Cognito" → Cognito managed login
 * → fill username/password → submit → redirected back to /auth/callback → home.
 */
async function cognitoLogin(
  page: import('@playwright/test').Page,
  username: string,
  password: string,
  storageStatePath: string,
) {
  await page.goto('/auth/login');
  await page.getByRole('button', { name: 'Sign in with Cognito' }).click();
  await page.getByRole('textbox', { name: 'Username' }).waitFor({ timeout: 15_000 });
  await page.getByRole('textbox', { name: 'Username' }).fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: 'submit' }).click();
  await page.waitForURL('**/', { timeout: 30_000 });
  await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 10_000 });
  await page.context().storageState({ path: storageStatePath });
}

setup('authenticate as admin', async ({ page }) => {
  const username = process.env['ADMIN_USERNAME'];
  const password = process.env['ADMIN_PASSWORD'];
  if (!username || !password) {
    throw new Error('ADMIN_USERNAME and ADMIN_PASSWORD must be set in e2e/.env');
  }
  await cognitoLogin(page, username, password, ADMIN_FILE);
});
