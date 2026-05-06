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
  // Track navigations for debugging
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) {
      console.log(`[nav] ${frame.url()}`);
    }
  });

  await page.goto('/auth/login');
  await page.getByRole('button', { name: 'Sign in with Cognito' }).click();

  // Wait for Cognito managed login page
  await page.getByRole('textbox', { name: 'Username' }).waitFor({ timeout: 15_000 });
  console.log(`[cognito] Login form visible at: ${page.url()}`);

  await page.getByRole('textbox', { name: 'Username' }).fill(username);
  await page.getByRole('textbox', { name: 'Password' }).fill(password);

  // Log available buttons for debugging Cognito UI changes
  const buttons = await page.getByRole('button').all();
  const buttonNames = await Promise.all(buttons.map(async (b) => {
    const name = await b.textContent().catch(() => '');
    const type = await b.getAttribute('type').catch(() => '');
    return `"${name?.trim()}" (type=${type})`;
  }));
  console.log(`[cognito] Available buttons: ${buttonNames.join(', ')}`);

  await page.getByRole('button', { name: 'submit' }).click();
  console.log(`[cognito] Submit clicked, waiting for navigation...`);

  // Fast-fail if Cognito rejects credentials (avoids 30s timeout)
  const loginError = page.getByText('Incorrect username or password.');
  const errorVisible = await loginError.isVisible({ timeout: 3_000 }).catch(() => false);
  if (errorVisible) {
    throw new Error(
      `Cognito login failed for "${username}" — user may not exist in this User Pool or password is incorrect`,
    );
  }

  // Wait for the OAuth callback to complete and land on the app
  // Use a longer timeout to account for the full redirect chain:
  // Cognito → /api/auth/callback → BFF token exchange → redirect to /
  await page.waitForURL('**/', { timeout: 30_000 });

  // Debug: log cookies and final URL
  const cookies = await page.context().cookies();
  const bffCookies = cookies.filter(c => c.name.startsWith('__Host-bff'));
  console.log(`[auth] Final URL: ${page.url()}`);
  console.log(`[auth] BFF cookies present: ${bffCookies.map(c => c.name).join(', ') || 'NONE'}`);

  await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 10_000 });
  await page.context().storageState({ path: storageStatePath });
}

setup('authenticate as user', async ({ page }) => {
  setup.setTimeout(60_000); // Allow extra time for the full OAuth redirect chain
  const username = process.env['USER_USERNAME'];
  const password = process.env['USER_PASSWORD'];
  if (!username || !password) {
    throw new Error('USER_USERNAME and USER_PASSWORD must be set in e2e/.env');
  }
  await cognitoLogin(page, username, password, USER_FILE);
});
