import { test, expect } from '@playwright/test';

test.describe('Home Page (authenticated)', () => {
  test('should display the chat interface after login', async ({ page }) => {
    await page.goto('/');

    // Should NOT redirect to login
    await expect(page).not.toHaveURL(/\/auth\/login/);

    // Chat textarea should be visible
    const textarea = page.locator('textarea#user-message');
    await expect(textarea).toBeVisible({ timeout: 10_000 });
    await expect(textarea).toHaveAttribute('placeholder', 'How can I help you today?');
  });

  test('should show a greeting message on empty session', async ({ page }) => {
    await page.goto('/');

    // The greeting message container should be visible
    const greeting = page.locator('[data-testid="greeting-message"]');
    await expect(greeting).toBeVisible({ timeout: 10_000 });
  });
});
