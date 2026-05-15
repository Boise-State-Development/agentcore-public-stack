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

  // Wait for Cognito managed login page
  await page.getByRole('textbox', { name: 'Username' }).waitFor({ timeout: 15_000 });
  await page.getByRole('textbox', { name: 'Username' }).fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);
  await page.getByRole('button', { name: 'submit' }).click();

  // Fast-fail if Cognito rejects credentials (avoids 30s timeout)
  const loginError = page.getByText('Incorrect username or password.');
  const errorVisible = await loginError.isVisible({ timeout: 3_000 }).catch(() => false);
  if (errorVisible) {
    throw new Error(
      `Cognito login failed for "${username}" — user may not exist in this User Pool or password is incorrect`,
    );
  }

  // Wait for the browser to leave Cognito and return to our app.
  // After Cognito submit, the redirect chain is:
  //   Cognito → /api/auth/callback → BFF token exchange → 302 to /
  // If the BFF callback fails, it redirects to /?auth_error=... or /auth/login
  // If cookies land on the wrong domain (ALB instead of CloudFront), the
  // APP_INITIALIZER gets 401 and redirects back to /auth/login.

  // Track the callback to diagnose cookie-domain issues
  let callbackResponseUrl = '';
  page.on('response', async (response) => {
    if (response.url().includes('/auth/callback')) {
      callbackResponseUrl = response.url();
    }
  });

  try {
    await page.waitForURL('**/', { timeout: 45_000 });
  } catch {
    const finalUrl = page.url();
    const cookies = await page.context().cookies();
    const bffCookies = cookies.filter(c => c.name.startsWith('__Host-bff'));
    const cookieDetails = bffCookies.map(c => `${c.name}(domain=${c.domain},path=${c.path},secure=${c.secure})`).join('; ');
    throw new Error(
      `OAuth redirect chain failed. Final URL: ${finalUrl} | ` +
      `Callback response URL: ${callbackResponseUrl || 'NEVER HIT'} | ` +
      `BFF cookies: ${cookieDetails || 'NONE'} | ` +
      `All cookie domains: ${[...new Set(cookies.map(c => c.domain))].join(', ')}`,
    );
  }

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
