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
  await expect(assistantMessage).toBeVisible({ timeout: 60_000 });
  await expect(page.locator('app-pulsating-loader')).toBeHidden({ timeout: 200_000 });

  return (await assistantMessage.innerText()).trim();
}


test.describe.serial('Chat with Claude Haiku 4.5 (user)', () => {
  test('should select Haiku, send a message, and receive a response', async ({ page }) => {
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
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

    // Click into the most recent conversation (created by the previous test)
    const sessionLink = page.locator('app-session-list a').first();
    await sessionLink.click();
    await page.waitForURL(/\/s\//, { timeout: 10_000 });

    // Should already have 1 user message and 1 assistant message from the first test
    await expect(page.locator('app-user-message')).toHaveCount(1, { timeout: 10_000 });
    await expect(page.locator('app-assistant-message')).toHaveCount(1, { timeout: 10_000 });

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
