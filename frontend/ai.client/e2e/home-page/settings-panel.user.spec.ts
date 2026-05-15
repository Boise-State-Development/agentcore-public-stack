import { test, expect } from '@playwright/test';

test.describe('Settings Panel — Tool Toggles (user)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });
  });

  // test('should open and close the settings panel', async ({ page }) => {
  //   // Open settings
  //   await page.getByLabel('Open settings').click();

  //   const panel = page.getByRole('dialog', { name: 'Settings' });
  //   await expect(panel).toBeVisible({ timeout: 5_000 });
  //   await expect(panel.getByText('Settings')).toBeVisible();

  //   await page.pause()

  //   // Close via the X button
  //   await panel.getByLabel('Close settings panel').click();
  //   await expect(panel).toBeHidden({ timeout: 5_000 });

  //   await page.pause()
  // });

  // test('should display tools with toggle switches', async ({ page }) => {
  //   await page.getByLabel('Open settings').click();

  //   const panel = page.getByRole('dialog', { name: 'Settings' });
  //   await expect(panel).toBeVisible({ timeout: 5_000 });

  //   // Wait for tools to load (not showing "Loading tools...")
  //   await expect(panel.getByText('Loading tools...')).toBeHidden({ timeout: 15_000 });

  //   // Should have at least one tool toggle
  //   const toggles = panel.getByRole('switch');
  //   await expect(toggles.first()).toBeVisible({ timeout: 5_000 });

  //   // Enabled count should be visible
  //   await expect(panel.getByText(/\d+ enabled/)).toBeVisible();
  // });

  // test('should toggle a tool on and off', async ({ page }) => {
  //   await page.getByLabel('Open settings').click();

  //   const panel = page.getByRole('dialog', { name: 'Settings' });
  //   await expect(panel.getByText('Loading tools...')).toBeHidden({ timeout: 15_000 });

  //   const firstToggle = panel.getByRole('switch').first();
  //   await expect(firstToggle).toBeVisible({ timeout: 5_000 });

  //   // Read initial state
  //   const initialState = await firstToggle.getAttribute('aria-checked');
  //   const flippedState = initialState === 'true' ? 'false' : 'true';

  //   // Toggle it
  //   await firstToggle.click();
  //   await expect(firstToggle).toHaveAttribute('aria-checked', flippedState, { timeout: 5_000 });

  //   // Toggle it back
  //   await firstToggle.click();
  //   await expect(firstToggle).toHaveAttribute('aria-checked', initialState!, { timeout: 5_000 });
  // });

  test('should close settings panel by clicking backdrop', async ({ page }) => {
    await page.getByLabel('Open settings').click();

    const panel = page.getByRole('dialog', { name: 'Settings' });
    await expect(panel).toBeVisible({ timeout: 5_000 });

    // Click the backdrop (center of viewport, away from the slide-over panel)
    const viewport = page.viewportSize()!;
    await page.mouse.click(viewport.width / 2, viewport.height / 2);
    await expect(panel).toBeHidden({ timeout: 5_000 });
  });
});
