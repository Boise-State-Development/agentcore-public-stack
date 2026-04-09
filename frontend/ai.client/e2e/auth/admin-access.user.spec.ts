import { test, expect } from '@playwright/test';

test.describe('Admin access (non-admin user)', () => {
  test('should redirect non-admin user away from /admin', async ({ page }) => {
    await page.goto('/admin');

    // adminGuard redirects non-admin users to home page (/)
    await page.waitForURL((url) => !url.pathname.startsWith('/admin'), { timeout: 15_000 });

    // Should land on the home page with the chat interface
    const textarea = page.locator('textarea#user-message');
    await expect(textarea).toBeVisible({ timeout: 10_000 });
  });
});
