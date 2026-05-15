import { test, expect } from '@playwright/test';

test.describe('Manage Sessions Page (user)', () => {
  test('should load the manage sessions page', async ({ page }) => {
    await page.goto('/manage-sessions');

    // Page heading
    await expect(
      page.getByRole('heading', { name: 'Manage Conversations' }),
    ).toBeVisible({ timeout: 15_000 });

    // Selection info bar
    await expect(page.getByText(/0 of \d+ selected/)).toBeVisible();
  });

  test('should show sessions or empty state', async ({ page }) => {
    await page.goto('/manage-sessions');
    await expect(
      page.getByRole('heading', { name: 'Manage Conversations' }),
    ).toBeVisible({ timeout: 15_000 });

    // Wait for loading to finish
    await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 15_000 });

    // Either sessions are listed or the empty state is shown
    const hasSession = await page.locator('input[type="checkbox"]').count();
    if (hasSession > 0) {
      // At least one session checkbox is visible
      await expect(page.locator('input[type="checkbox"]').first()).toBeVisible();
    } else {
      await expect(page.getByText('No conversations')).toBeVisible();
    }
  });

  test('should select and deselect a session', async ({ page }) => {
    await page.goto('/manage-sessions');
    await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 15_000 });
    await expect(
      page.locator('input[type="checkbox"]').first().or(page.getByText('No conversations'))
    ).toBeVisible({ timeout: 15_000 });
    
    const checkbox = page.locator('input[type="checkbox"]').first();
    const hasCheckbox = (await checkbox.count()) > 0;
    test.skip(!hasCheckbox, 'No sessions available to select');

    // Select
    await checkbox.check();
    await expect(page.getByText(/1 of \d+ selected/)).toBeVisible();

    // Deselect
    await checkbox.uncheck();
    await expect(page.getByText(/0 of \d+ selected/)).toBeVisible();
  });

  test('should navigate back to home', async ({ page }) => {
    await page.goto('/manage-sessions');
    await expect(
      page.getByRole('heading', { name: 'Manage Conversations' }),
    ).toBeVisible({ timeout: 15_000 });

    await page.getByText('Back').click();
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 10_000 });
  });

  test('should show the delete selected button disabled when nothing is selected', async ({ page }) => {
    await page.goto('/manage-sessions');

    // Wait for the page to fully render before checking anything
    await expect(
      page.getByRole('heading', { name: 'Manage Conversations' }),
    ).toBeVisible({ timeout: 15_000 });

    // Wait for loading to finish
    await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 30_000 });

    // The Delete Selected button is always rendered (not conditional on sessions existing)
    const deleteButton = page.getByRole('button', { name: /Delete Selected/i });
    await expect(deleteButton).toBeVisible({ timeout: 5_000 });
    await expect(deleteButton).toBeDisabled();
  });
});
