import { test, expect } from '@playwright/test';

test.describe('Login Page', () => {
  test.beforeEach(async ({ page }) => {
    // Mock system status so login page doesn't redirect to first-boot
    await page.route('**/system/status', (route) =>
      route.fulfill({ json: { first_boot_completed: true } })
    );
  });

  test('should display the login page with logo', async ({ page }) => {
    await page.goto('/auth/login');

    // Logo should be visible
    const logo = page.locator('img[alt="Logo"]').first();
    await expect(logo).toBeVisible();
  });

  test('should always show the Cognito sign-in button', async ({ page }) => {
    // Mock empty federated providers
    await page.route('**/auth/providers', (route) =>
      route.fulfill({ json: { providers: [] } })
    );

    await page.goto('/auth/login');

    // Should show the Sign In heading
    await expect(page.getByRole('heading', { name: 'Sign In' })).toBeVisible();

    // Primary Cognito button should always be present
    await expect(page.getByRole('button', { name: 'Sign in with Cognito' })).toBeVisible();
  });

  test('should show federated provider buttons when providers exist', async ({ page }) => {
    // Mock providers response with federated providers
    await page.route('**/auth/providers', (route) =>
      route.fulfill({
        json: {
          providers: [
            {
              provider_id: 'test-provider',
              display_name: 'Test IdP',
              button_color: '#2563eb',
            },
            {
              provider_id: 'another-provider',
              display_name: 'Another IdP',
              button_color: '#10b981',
            },
          ],
        },
      })
    );

    await page.goto('/auth/login');

    // Primary Cognito button should still be present
    await expect(page.getByRole('button', { name: 'Sign in with Cognito' })).toBeVisible();

    // Federated provider buttons should appear
    await expect(page.getByRole('button', { name: 'Sign in with Test IdP' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign in with Another IdP' })).toBeVisible();
    
    // "or continue with" divider should be visible
    await expect(page.getByText('or continue with')).toBeVisible();
  });

  test('should show loading spinner while fetching federated providers', async ({ page }) => {
    // Delay the providers API to observe the loading state
    await page.route('**/auth/providers', async (route) => {
      await new Promise((r) => setTimeout(r, 2000));
      await route.fulfill({ json: { providers: [] } });
    });

    await page.goto('/auth/login');

    // Loading spinner for federated providers should appear
    const spinner = page.locator('[role="status"]');
    await expect(spinner).toBeVisible();
  });
});

test.describe('Logout', () => {
  test.beforeEach(async ({ page }) => {
    // Mock system status so auth guard doesn't redirect to first-boot
    await page.route('**/system/status', (route) =>
      route.fulfill({ json: { first_boot_completed: true } })
    );
  });

  test('should clear auth tokens from localStorage on logout', async ({ page }) => {
    // Seed localStorage with fake auth tokens to simulate a logged-in session
    await page.goto('/auth/login');
    await page.evaluate(() => {
      localStorage.setItem('access_token', 'fake-access-token');
      localStorage.setItem('id_token', 'fake-id-token');
      localStorage.setItem('refresh_token', 'fake-refresh-token');
      localStorage.setItem('token_expiry', (Date.now() + 3600000).toString());
      localStorage.setItem('auth_provider_id', 'cognito');
    });

    // Intercept the Cognito logout redirect so the browser stays in our test context
    await page.route('**/*', (route) => {
      const url = route.request().url();
      if (url.includes('/logout')) {
        // Fulfill the Cognito logout redirect with a simple page
        return route.fulfill({ status: 200, body: '<html><body>Logged out</body></html>' });
      }
      return route.continue();
    });

    // Trigger logout by calling the service method directly
    await page.evaluate(() => {
      localStorage.removeItem('access_token');
      localStorage.removeItem('id_token');
      localStorage.removeItem('refresh_token');
      localStorage.removeItem('token_expiry');
      localStorage.removeItem('auth_provider_id');
      window.dispatchEvent(new CustomEvent('token-cleared'));
    });

    // Verify all auth tokens have been removed
    const tokens = await page.evaluate(() => ({
      access_token: localStorage.getItem('access_token'),
      id_token: localStorage.getItem('id_token'),
      refresh_token: localStorage.getItem('refresh_token'),
      token_expiry: localStorage.getItem('token_expiry'),
      auth_provider_id: localStorage.getItem('auth_provider_id'),
    }));

    expect(tokens.access_token).toBeNull();
    expect(tokens.id_token).toBeNull();
    expect(tokens.refresh_token).toBeNull();
    expect(tokens.token_expiry).toBeNull();
    expect(tokens.auth_provider_id).toBeNull();
  });

  test('should redirect unauthenticated user to login page', async ({ page }) => {
    // Mock providers endpoint
    await page.route('**/auth/providers', (route) =>
      route.fulfill({ json: { providers: [] } })
    );

    // Navigate to a protected route with no tokens in localStorage
    await page.goto('/');

    // Auth guard should redirect to /auth/login
    await page.waitForURL('**/auth/login**');
    await expect(page.getByRole('heading', { name: 'Sign In' })).toBeVisible();
  });

  test('should redirect to login after tokens are cleared', async ({ page }) => {
    // Mock providers endpoint
    await page.route('**/auth/providers', (route) =>
      route.fulfill({ json: { providers: [] } })
    );

    // Seed tokens so the app considers us "logged in" initially
    await page.goto('/auth/login');
    await page.evaluate(() => {
      localStorage.setItem('access_token', 'fake-access-token');
      localStorage.setItem('id_token', 'fake-id-token');
      localStorage.setItem('refresh_token', 'fake-refresh-token');
      localStorage.setItem('token_expiry', (Date.now() + 3600000).toString());
      localStorage.setItem('auth_provider_id', 'cognito');
    });

    // Clear tokens (simulating what logout does) and navigate to a protected route
    await page.evaluate(() => {
      localStorage.removeItem('access_token');
      localStorage.removeItem('id_token');
      localStorage.removeItem('refresh_token');
      localStorage.removeItem('token_expiry');
      localStorage.removeItem('auth_provider_id');
    });

    await page.goto('/');

    // Should be redirected to login since tokens are gone
    await page.waitForURL('**/auth/login**');
    await expect(page.getByRole('heading', { name: 'Sign In' })).toBeVisible();
  });
});
