import { test, expect, Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Navigate to the usage settings page and wait for it to load. */
async function goToUsagePage(page: Page) {
  await page.goto('/settings/usage');
  await expect(
    page.getByRole('heading', { name: 'Settings' }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole('heading', { name: 'Usage' }),
  ).toBeVisible({ timeout: 10_000 });
}

/** Wait for any loading indicator to disappear. */
async function waitForDataLoaded(page: Page) {
  await expect(page.getByText('Loading cost data...')).toBeHidden({ timeout: 15_000 });
}

/** Read the "Total Tokens" card value as a raw number (strips formatting commas). */
async function getTotalTokens(page: Page): Promise<number> {
  const card = page.locator('div').filter({ hasText: /^Total Tokens/ });
  const value = card.locator('p.text-2xl');
  await expect(value).toBeVisible({ timeout: 10_000 });
  const text = (await value.innerText()).trim();
  return Number(text.replace(/,/g, ''));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe('Settings / Usage (user)', () => {
  test.describe.serial('Usage page loads and filters work correctly', () => {
    let page: Page;

    test.beforeAll(async ({ browser }) => {
      page = await browser.newPage();
    });

    test.afterAll(async () => {
      await page.close();
    });

    let currentMonthTokens: number;

    test('should load the usage page with current month data', async () => {
      await goToUsagePage(page);
      await waitForDataLoaded(page);

      // The "Current Month" button should be active (blue)
      const currentMonthBtn = page.getByRole('button', { name: 'Current Month' });
      await expect(currentMonthBtn).toBeVisible({ timeout: 5_000 });
      await expect(currentMonthBtn).toHaveClass(/bg-blue-600/, { timeout: 5_000 });

      // All four summary cards should be visible
      await expect(page.getByText('Total Cost')).toBeVisible({ timeout: 5_000 });
      await expect(page.getByText('Total Requests')).toBeVisible({ timeout: 5_000 });
      await expect(page.getByText('Total Tokens')).toBeVisible({ timeout: 5_000 });
      await expect(page.getByText('Token Breakdown')).toBeVisible({ timeout: 5_000 });

      // Capture the current month token count for comparison
      currentMonthTokens = await getTotalTokens(page);
    });

    test('should show different token count for Last 30 Days', async () => {
      // Click the "Last 30 Days" filter
      const last30Btn = page.getByRole('button', { name: 'Last 30 Days' });
      await last30Btn.click();

      // The button should now be active
      await expect(last30Btn).toHaveClass(/bg-blue-600/, { timeout: 5_000 });

      // "Current Month" should no longer be active
      const currentMonthBtn = page.getByRole('button', { name: 'Current Month' });
      await expect(currentMonthBtn).not.toHaveClass(/bg-blue-600/, { timeout: 5_000 });

      // Wait for the data to load
      await waitForDataLoaded(page);

      // The total tokens for last 30 days should differ from the current month
      // (last 30 days spans two calendar months, so unless it's the 1st of the
      // month with zero prior usage, the numbers won't match)
      const last30Tokens = await getTotalTokens(page);
      expect(last30Tokens).not.toBe(currentMonthTokens);
    });

    test('should show 0 tokens for February 2026 via the month dropdown', async () => {
      // Select February 2026 from the previous months dropdown
      // Scope to the month picker (not the settings nav select) via its placeholder text
      const dropdown = page.locator('select').filter({ hasText: 'Previous month...' });
      await expect(dropdown).toBeVisible({ timeout: 5_000 });
      await dropdown.selectOption('2026-02');

      // Wait for the data to load
      await waitForDataLoaded(page);

      // Feb 2026 should have no usage — either 0 tokens or the "No cost data" message
      const noDataMessage = page.getByText('No cost data available for this period');
      const hasNoData = (await noDataMessage.count()) > 0;

      if (hasNoData) {
        // The empty state message is shown — that confirms 0 usage
        await expect(noDataMessage).toBeVisible({ timeout: 5_000 });
      } else {
        // The cards are shown but tokens should be 0
        const tokens = await getTotalTokens(page);
        expect(tokens).toBe(0);
      }
    });
  });
});
