import { test, expect } from '@playwright/test';

/**
 * Helper: select a model from the model dropdown by matching visible text.
 */
async function selectModel(page: import('@playwright/test').Page, modelNameSubstring: string) {
  const trigger = page.getByRole('button', { name: 'Select model' });
  await expect(trigger).toBeVisible({ timeout: 10_000 });
  await expect(trigger).not.toContainText('System Default', { timeout: 30_000 });

  await trigger.click();

  const menuItem = page.getByRole('menuitem').filter({
    hasText: new RegExp(modelNameSubstring, 'i'),
  });
  await expect(menuItem.first()).toBeVisible({ timeout: 5_000 });
  await menuItem.first().click();
}

/**
 * Helper: send a chat message and wait for the assistant to finish responding.
 */
async function sendMessageAndWaitForResponse(
  page: import('@playwright/test').Page,
  message: string,
): Promise<string> {
  const textarea = page.locator('textarea#user-message');
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(message);

  await page.getByRole('button', { name: 'Submit message' }).click();

  const assistantMessage = page.locator('app-assistant-message').last();
  await expect(assistantMessage).toBeVisible({ timeout: 300_000 });
  await expect(page.locator('app-pulsating-loader')).toBeHidden({ timeout: 300_000 });

  return (await assistantMessage.innerText()).trim();
}


test.describe('Chat (user)', () => {
  test.describe.serial('Chat lifecycle with Claude Haiku 4.5', () => {
    test('should select Haiku, send a message, and receive a response', async ({ page }) => {
      test.setTimeout(60_000); // 60s for this test
      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      await selectModel(page, 'Haiku');

      const trigger = page.getByRole('button', { name: 'Select model' });
      await expect(trigger).toContainText(/Haiku/i);

      const response = await sendMessageAndWaitForResponse(page, 'Reply with exactly one word.');

      const userMessage = page.locator('app-user-message').last();
      await expect(userMessage).toContainText('Reply with exactly one word.');

      expect(response.length).toBeGreaterThan(0);
    });

    test('should send a second message in the same session', async ({ page }) => {
      test.setTimeout(60_000); // 60s for this test
      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      // Wait for the session list to finish loading before clicking
      await expect(page.getByText('Loading sessions...')).toBeHidden({ timeout: 15_000 });

      // Click into the most recent conversation (created by the previous test)
      const sessionLink = page.locator('app-session-list a').first();
      await expect(sessionLink).toBeVisible({ timeout: 10_000 });
      await sessionLink.click();
      await page.waitForURL(/\/s\//, { timeout: 10_000 });

      // The assistant message from the previous test may not be persisted in
      // DynamoDB yet when the page first loads messages. If only the user
      // message is present, reload to re-fetch from the backend.
      await expect(page.locator('app-user-message')).toHaveCount(1, { timeout: 15_000 });

      const assistantCount = await page.locator('app-assistant-message').count();
      if (assistantCount === 0) {
        // Assistant message not yet persisted — wait briefly then reload
        await page.waitForTimeout(3_000);
        await page.reload();
        await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });
        await expect(page.locator('app-user-message')).toHaveCount(1, { timeout: 15_000 });
      }
      await expect(page.locator('app-assistant-message')).toHaveCount(1, { timeout: 30_000 });

      // Send a second message
      const response = await sendMessageAndWaitForResponse(page, 'Reply with exactly one word.');

      expect(response.length).toBeGreaterThan(0);

      // Should now have 2 of each
      await expect(page.locator('app-user-message')).toHaveCount(2, { timeout: 5_000 });
      await expect(page.locator('app-assistant-message')).toHaveCount(2, { timeout: 5_000 });
    });

    test('should rename the conversation', async ({ page }) => {
      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      const sessionOptionsButton = page.locator('button[aria-haspopup="menu"]').filter({
        has: page.locator('ng-icon[name="heroEllipsisHorizontalSolid"]'),
      });
      await sessionOptionsButton.first().hover();
      await sessionOptionsButton.first().click();

      await page.getByRole('menuitem', { name: 'Rename' }).click();

      const renameInput = page.getByLabel('Rename conversation');
      await expect(renameInput).toBeVisible({ timeout: 5_000 });

      await renameInput.fill('test conversation');
      await renameInput.press('Enter');

      const sessionLink = page.locator('app-session-list a').filter({
        hasText: 'test conversation',
      });
      await expect(sessionLink.first()).toBeVisible({ timeout: 10_000 });
    });

    test('should start a new conversation from the sidebar', async ({ page }) => {
      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      // Click into the existing conversation so we're not already on home
      const sessionLink = page.locator('app-session-list a').filter({
        hasText: 'test conversation',
      });
      await sessionLink.first().click();
      await page.waitForURL(/\/s\//, { timeout: 10_000 });

      // Click "New Session" in the sidebar
      await page.getByRole('button', { name: 'New Session' }).click();

      // Should navigate to the home screen (root URL, no /s/ path)
      await page.waitForURL((url) => !url.pathname.startsWith('/s/'), { timeout: 10_000 });
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 10_000 });
    });

    test('should delete the conversation', async ({ page }) => {
      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      // Find the "test conversation" session and open its options menu
      const sessionItem = page.locator('li.group').filter({
        hasText: 'test conversation',
      });
      const optionsButton = sessionItem.locator('button[aria-haspopup="menu"]');
      await optionsButton.hover();
      await optionsButton.click();

      // Click "Delete" from the context menu
      await page.getByRole('menuitem', { name: 'Delete' }).click();

      // Confirm the deletion in the dialog
      const confirmButton = page.getByRole('alertdialog').getByRole('button', { name: 'Delete' });
      await expect(confirmButton).toBeVisible({ timeout: 5_000 });
      await confirmButton.click();

      // Verify the conversation is gone from the sidebar
      const deletedSession = page.locator('app-session-list a').filter({
        hasText: 'test conversation',
      });
      await expect(deletedSession).toHaveCount(0, { timeout: 10_000 });
    });
  });

  // Add independent (non-serial) home page tests below.
  // These run with their own isolated browser context and don't depend on the serial block.

  test('should close and reopen the sidebar using the collapse button', async ({ page }) => {
    // Ensure viewport is wide enough for the desktop sidebar (Tailwind lg = 1024px)
    await page.setViewportSize({ width: 1280, height: 720 });

    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    // Sidebar should be visible initially
    const sidenav = page.locator('app-sidenav');
    await expect(sidenav.first()).toBeVisible({ timeout: 5_000 });

    // Click the collapse button inside the sidebar
    const collapseButton = page.getByRole('button', { name: 'Collapse sidebar' });
    await expect(collapseButton).toBeVisible({ timeout: 5_000 });
    await collapseButton.click();

    // The expand button in app.html should now appear (top-left, outside the sidebar)
    const expandButton = page.getByRole('button', { name: 'Expand sidebar' });
    await expect(expandButton).toBeVisible({ timeout: 10_000 });

    // Click expand to reopen
    await expandButton.click();

    // Sidebar should be visible again
    await expect(sidenav.first()).toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible({
      timeout: 5_000,
    });
  });

  test('should navigate to the assistants page when clicking the Assistants link', async ({
    page,
  }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    const assistantsLink = page.getByRole('link', { name: 'Assistants' });
    await expect(assistantsLink).toBeVisible({ timeout: 5_000 });
    await assistantsLink.click();

    await page.waitForURL(/\/assistants/, { timeout: 10_000 });
    expect(page.url()).toContain('/assistants');
  });

  test('should open and close the user menu by clicking the user button', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    const userMenuButton = page.getByRole('button', { name: 'User menu' });
    await expect(userMenuButton).toBeVisible({ timeout: 5_000 });

    // Open the user menu
    await userMenuButton.click();

    const settingsItem = page.getByRole('menuitem', { name: 'Settings' });
    await expect(settingsItem).toBeVisible({ timeout: 5_000 });

    // Close the user menu by clicking the button again
    await userMenuButton.click();
    await expect(settingsItem).toBeHidden({ timeout: 5_000 });
  });

  test('should navigate to the settings page from the user menu', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    // Open the user menu
    const userMenuButton = page.getByRole('button', { name: 'User menu' });
    await expect(userMenuButton).toBeVisible({ timeout: 5_000 });
    await userMenuButton.click();

    // Click Settings
    const settingsItem = page.getByRole('menuitem', { name: 'Settings' });
    await expect(settingsItem).toBeVisible({ timeout: 5_000 });
    await settingsItem.click();

    await page.waitForURL(/\/settings/, { timeout: 10_000 });
    expect(page.url()).toContain('/settings');
  });
});
