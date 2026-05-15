import { test, expect, Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Navigate to the appearance settings page and wait for it to load. */
async function goToAppearancePage(page: Page) {
  await page.goto('/settings/appearance');
  await expect(
    page.getByRole('heading', { name: 'Settings' }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole('heading', { name: 'Appearance' }),
  ).toBeVisible({ timeout: 10_000 });
}

/** Get the theme button by label text. */
function themeButton(page: Page, label: 'Light' | 'Dark' | 'System') {
  // Each button contains a span with the label text — filter to the right one
  return page.locator('app-appearance-settings button').filter({ hasText: label });
}

/** Assert that the given theme button shows the selected ring style. */
async function expectSelected(page: Page, label: 'Light' | 'Dark' | 'System') {
  const btn = themeButton(page, label);
  // Selected buttons have ring-2 (blue ring) and a checkmark badge
  await expect(btn).toHaveClass(/ring-2/, { timeout: 5_000 });
}

/** Assert that the given theme button is NOT selected. */
async function expectNotSelected(page: Page, label: 'Light' | 'Dark' | 'System') {
  const btn = themeButton(page, label);
  await expect(btn).toHaveClass(/ring-1/, { timeout: 5_000 });
}

/** Check whether the <html> element has the 'dark' class. */
async function isDarkMode(page: Page): Promise<boolean> {
  return page.locator('html').evaluate((el) => el.classList.contains('dark'));
}

/** Read the theme-preference value from localStorage. */
async function getStoredPreference(page: Page): Promise<string | null> {
  return page.evaluate(() => localStorage.getItem('theme-preference'));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Settings / Appearance (user)', () => {
  test('should display all three theme options', async ({ page }) => {
    await goToAppearancePage(page);

    await expect(themeButton(page, 'Light')).toBeVisible({ timeout: 5_000 });
    await expect(themeButton(page, 'Dark')).toBeVisible({ timeout: 5_000 });
    await expect(themeButton(page, 'System')).toBeVisible({ timeout: 5_000 });
  });

  test('should switch to dark theme', async ({ page }) => {
    await goToAppearancePage(page);

    await themeButton(page, 'Dark').click();

    await expectSelected(page, 'Dark');
    await expectNotSelected(page, 'Light');
    await expectNotSelected(page, 'System');

    // <html> should have the 'dark' class
    expect(await isDarkMode(page)).toBe(true);

    // localStorage should persist the choice
    expect(await getStoredPreference(page)).toBe('dark');
  });

  test('should switch to light theme', async ({ page }) => {
    await goToAppearancePage(page);

    // Start by setting dark so we can verify the switch
    await themeButton(page, 'Dark').click();
    expect(await isDarkMode(page)).toBe(true);

    // Now switch to light
    await themeButton(page, 'Light').click();

    await expectSelected(page, 'Light');
    await expectNotSelected(page, 'Dark');
    await expectNotSelected(page, 'System');

    expect(await isDarkMode(page)).toBe(false);
    expect(await getStoredPreference(page)).toBe('light');
  });

  test('should switch to system theme', async ({ page }) => {
    await goToAppearancePage(page);

    // Set an explicit theme first so we know we're changing something
    await themeButton(page, 'Dark').click();
    await expectSelected(page, 'Dark');

    // Switch to system
    await themeButton(page, 'System').click();

    await expectSelected(page, 'System');
    await expectNotSelected(page, 'Light');
    await expectNotSelected(page, 'Dark');

    expect(await getStoredPreference(page)).toBe('system');
  });

  test('should persist theme choice across page reloads', async ({ page }) => {
    await goToAppearancePage(page);

    // Select dark
    await themeButton(page, 'Dark').click();
    await expectSelected(page, 'Dark');
    expect(await isDarkMode(page)).toBe(true);

    // Reload the page
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Appearance' }),
    ).toBeVisible({ timeout: 15_000 });

    // Dark should still be selected and applied
    await expectSelected(page, 'Dark');
    expect(await isDarkMode(page)).toBe(true);
  });

  test('should apply system theme based on prefers-color-scheme', async ({ page }) => {
    // Emulate dark system preference
    await page.emulateMedia({ colorScheme: 'dark' });
    await goToAppearancePage(page);

    await themeButton(page, 'System').click();
    await expectSelected(page, 'System');

    // With system preference set to dark, the page should be in dark mode
    expect(await isDarkMode(page)).toBe(true);

    // Now flip to light system preference
    await page.emulateMedia({ colorScheme: 'light' });

    // The ThemeService listens to matchMedia changes — give it a moment
    await expect.poll(() => isDarkMode(page), { timeout: 5_000 }).toBe(false);
  });
});
