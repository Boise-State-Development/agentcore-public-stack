import { test, expect } from '@playwright/test';

test.describe('Navigation & Auth Redirects', () => {
  test.beforeEach(async ({ page }) => {
    // Mock system status as completed so the auth guard redirects to login (not first-boot)
    await page.route('**/system/status', (route) =>
      route.fulfill({ json: { first_boot_completed: true } })
    );
  });

  test('should redirect unauthenticated users to login', async ({ page }) => {
    await page.goto('/');

    // Auth guard should redirect to /auth/login
    await page.waitForURL('**/auth/login**');
    expect(page.url()).toContain('/auth/login');
  });

  test('should redirect protected routes to login', async ({ page }) => {
    await page.goto('/manage-sessions');

    await page.waitForURL('**/auth/login**');
    expect(page.url()).toContain('/auth/login');
  });

  test('should redirect admin routes to login for unauthenticated users', async ({ page }) => {
    await page.goto('/admin');

    await page.waitForURL('**/auth/login**');
    expect(page.url()).toContain('/auth/login');
  });
});
