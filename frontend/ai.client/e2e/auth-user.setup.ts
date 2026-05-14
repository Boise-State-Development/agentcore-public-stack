import { test as setup, expect } from '@playwright/test';
import path from 'path';

const USER_FILE = path.join(__dirname, '.auth', 'user.json');

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

  // Intercept the /auth/session request to see what's happening
  let sessionResponseStatus = 0;
  let sessionResponseBody = '';
  let sessionRequestCookies = '';
  // Track the callback redirect to diagnose cookie-domain issues
  let callbackResponseUrl = '';
  let callbackSetCookies: string[] = [];
  page.on('response', async (response) => {
    if (response.url().includes('/auth/session')) {
      sessionResponseStatus = response.status();
      sessionRequestCookies = response.request().headers()['cookie'] || 'NO COOKIE HEADER';
      try { sessionResponseBody = await response.text(); } catch { sessionResponseBody = '<unreadable>'; }
    }
    // Capture the callback response to see where cookies are being set
    if (response.url().includes('/auth/callback')) {
      callbackResponseUrl = response.url();
      const headers = response.headers();
      // Collect all set-cookie headers (may be multiple)
      const setCookie = headers['set-cookie'] || '';
      if (setCookie) callbackSetCookies.push(setCookie);
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
      `Session response: ${sessionResponseStatus} ${sessionResponseBody.substring(0, 100)} | ` +
      `Cookie header sent: ${sessionRequestCookies.substring(0, 150)} | ` +
      `BFF cookies in jar: ${cookieDetails || 'NONE'} | ` +
      `All cookie domains: ${[...new Set(cookies.map(c => c.domain))].join(', ')}`,
    );
  }

  await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 10_000 });
  await page.context().storageState({ path: storageStatePath });
}

setup('authenticate as user', async ({ page }) => {
  const username = process.env['USER_USERNAME'];
  const password = process.env['USER_PASSWORD'];
  if (!username || !password) {
    throw new Error('USER_USERNAME and USER_PASSWORD must be set in e2e/.env');
  }
  await cognitoLogin(page, username, password, USER_FILE);
});
