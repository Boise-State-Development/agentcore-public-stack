import { test, expect } from '@playwright/test';

test.describe('404 Not Found Page', () => {
  test('should display 404 page for unknown routes', async ({ page }) => {
    await page.goto('/some/nonexistent/route');

    // Should show the 404 digits
    await expect(page.getByLabel('Error 404')).toBeVisible();

    // Should show the "Page Not Found" title
    await expect(page.getByRole('heading', { name: 'Page Not Found' })).toBeVisible();
  });

  test('should have a "Return Home" link', async ({ page }) => {
    await page.goto('/this-does-not-exist');

    const homeLink = page.getByRole('link', { name: 'Return Home' });
    await expect(homeLink).toBeVisible();
    await expect(homeLink).toHaveAttribute('href', '/');
  });

  test('should have a "Go Back" button', async ({ page }) => {
    await page.goto('/nope');

    const backButton = page.getByRole('button', { name: 'Go Back' });
    await expect(backButton).toBeVisible();
  });
});
