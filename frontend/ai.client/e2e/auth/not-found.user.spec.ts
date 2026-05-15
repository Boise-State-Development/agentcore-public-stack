import { test, expect } from '@playwright/test';

test.describe('404 Not Found Page', () => {
  test('should display 404 page for unknown routes', async ({ page }) => {
    await page.goto('/some/nonexistent/route');

    await expect(page.getByLabel('Error 404')).toBeVisible({ timeout: 15_000 });

    await expect(
      page.getByRole('heading', { name: 'Page Not Found' }),
    ).toBeVisible({ timeout: 15_000 });
  });

  test('should have a "Return Home" link', async ({ page }) => {
    await page.goto('/this-does-not-exist');

    await expect(page.getByLabel('Error 404')).toBeVisible({ timeout: 15_000 });

    const homeLink = page.getByRole('link', { name: 'Return Home' });
    await expect(homeLink).toBeVisible({ timeout: 5_000 });
    await expect(homeLink).toHaveAttribute('href', '/');
  });

  test('should have a "Go Back" button', async ({ page }) => {
    await page.goto('/nope');

    await expect(page.getByLabel('Error 404')).toBeVisible({ timeout: 15_000 });

    const backButton = page.getByRole('button', { name: 'Go Back' });
    await expect(backButton).toBeVisible({ timeout: 5_000 });
  });
});
