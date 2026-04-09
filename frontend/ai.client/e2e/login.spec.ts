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
