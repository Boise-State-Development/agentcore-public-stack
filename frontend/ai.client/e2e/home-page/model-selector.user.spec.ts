import { test, expect } from '@playwright/test';

test.describe('Model Selector (user)', () => {
  test('should display the model selector with a loaded model', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    const trigger = page.getByRole('button', { name: 'Select model' });
    await expect(trigger).toBeVisible({ timeout: 10_000 });

    // Should eventually show a real model name (not "Loading..." or "System Default")
    await expect(trigger).not.toContainText('Default', { timeout: 30_000 });
  });

  test('should open the model dropdown and list available models', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    const trigger = page.getByRole('button', { name: 'Select model' });
    await expect(trigger).not.toContainText('Default', { timeout: 30_000 });
    await trigger.click();

    // Menu items should appear
    const menuItems = page.getByRole('menuitem');
    await expect(menuItems.first()).toBeVisible({ timeout: 5_000 });
    expect(await menuItems.count()).toBeGreaterThan(0);
  });

  test('should persist model selection after navigation', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    const trigger = page.getByRole('button', { name: 'Select model' });
    await expect(trigger).not.toContainText('Default', { timeout: 30_000 });

    // Open dropdown and pick a different model (if available)
    await trigger.click();
    const menuItems = page.getByRole('menuitem');
    const count = await menuItems.count();
    test.skip(count < 2, 'Only one model available — cannot test switching');

    // Pick the second model (different from current)
    const secondModel = menuItems.nth(1);
    const secondModelName = (await secondModel.innerText()).trim();
    await secondModel.click();

    // Verify the trigger updated
    await expect(trigger).toContainText(secondModelName.split('\n')[0], { timeout: 5_000 });

    // Navigate away and back
    await page.goto('/manage-sessions');
    await expect(page.getByRole('heading', { name: 'Manage Conversations' })).toBeVisible({ timeout: 15_000 });

    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    // Model should still be the one we selected
    const afterNav = page.getByRole('button', { name: 'Select model' });
    await expect(afterNav).not.toContainText('Default', { timeout: 30_000 });
    await expect(afterNav).toContainText(secondModelName.split('\n')[0]);
  });
});
