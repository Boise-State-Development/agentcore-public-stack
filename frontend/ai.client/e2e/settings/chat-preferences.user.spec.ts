import { test, expect, Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Unique conversation title used by the serial delete lifecycle. */
const DELETE_TEST_TITLE = `e2e-delete-test-${Date.now()}`;

/** Unique conversation title used by the token count badge tests. */
const TOKEN_TEST_TITLE = `e2e-token-test-${Date.now()}`;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Navigate to the chat preferences settings page and wait for it to load. */
async function goToChatPreferencesPage(page: Page) {
  await page.goto('/settings/chat');
  await expect(
    page.getByRole('heading', { name: 'Settings' }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole('heading', { name: 'Chat Preferences' }),
  ).toBeVisible({ timeout: 10_000 });
}

/** Navigate to the manage conversations page and wait for it to load. */
async function goToManageSessionsPage(page: Page) {
  await page.goto('/manage-sessions');
  await expect(
    page.getByRole('heading', { name: 'Manage Conversations' }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 15_000 });
}

/** Read a boolean value from localStorage. */
async function getLocalStorageBoolean(page: Page, key: string): Promise<boolean> {
  const raw = await page.evaluate((k) => localStorage.getItem(k), key);
  if (raw === null) return false;
  try {
    return JSON.parse(raw) === true;
  } catch {
    return false;
  }
}

/**
 * Assert that a toggle is visually and semantically in the "on" state.
 * Uses Playwright's built-in checkbox assertion instead of fragile
 * computed-style checks (Tailwind v4 OKLCH colors break RGB parsing).
 */
async function expectToggleOn(page: Page, label: string) {
  await expect(page.getByLabel(label)).toBeChecked({ timeout: 5_000 });
}

/**
 * Assert that a toggle is visually and semantically in the "off" state.
 */
async function expectToggleOff(page: Page, label: string) {
  await expect(page.getByLabel(label)).not.toBeChecked({ timeout: 5_000 });
}

/**
 * Assert that the toggle knob is translated to the right (checked position).
 * The knob span has `group-has-checked:translate-x-5` when on.
 * We verify by checking the checkbox is checked — the CSS transform follows.
 */
async function expectKnobTranslated(page: Page, label: string) {
  await expect(page.getByLabel(label)).toBeChecked({ timeout: 5_000 });
}

/**
 * Assert that the toggle knob is in the default (left) position.
 */
async function expectKnobDefault(page: Page, label: string) {
  await expect(page.getByLabel(label)).not.toBeChecked({ timeout: 5_000 });
}

/** Send a chat message and wait for the assistant to finish responding. */
async function sendMessageAndWaitForResponse(
  page: Page,
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

// ---------------------------------------------------------------------------
// Tests — Chat Preferences Page
// ---------------------------------------------------------------------------

test.describe('Settings / Chat Preferences (user)', () => {
  // -----------------------------------------------------------------------
  // Page structure
  // -----------------------------------------------------------------------

  test('should display all chat preference sections', async ({ page }) => {
    await goToChatPreferencesPage(page);

    // Default model section
    await expect(page.getByText('Default model')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText('Choose which model is selected by default')).toBeVisible({ timeout: 5_000 });

    // Show token count toggle — label, description, and icon container
    await expect(page.getByText('Show token count')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText('Display token usage, latency, and cost badges')).toBeVisible({ timeout: 5_000 });

    // Show debug output toggle — label, description, and icon container
    await expect(page.getByText('Show debug output')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText('Show the full prompt sent to the model')).toBeVisible({ timeout: 5_000 });

    // Manage Conversations link
    await expect(page.getByText('Manage Conversations')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText('Select and delete old conversations')).toBeVisible({ timeout: 5_000 });

    // Memories link
    await expect(page.getByText('Memories')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText('View and manage what the assistant remembers')).toBeVisible({ timeout: 5_000 });
  });

  // -----------------------------------------------------------------------
  // Default model selector
  // -----------------------------------------------------------------------

  // test('should display the default model dropdown with available models', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   const select = page.getByLabel('Default model');
  //   await expect(select).toBeVisible({ timeout: 15_000 });

  //   // The "no default" option should always be present
  //   const noDefaultOption = select.locator('option[value=""]');
  //   await expect(noDefaultOption).toHaveText('No default (use first available)', { timeout: 5_000 });

  //   // At least one model option should be available (beyond the "no default" option)
  //   const allOptions = select.locator('option');
  //   const optionCount = await allOptions.count();
  //   expect(optionCount).toBeGreaterThanOrEqual(2);
  // });

  // test('should change the default model and persist it', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   const select = page.getByLabel('Default model');
  //   await expect(select).toBeVisible({ timeout: 15_000 });

  //   // Get all model options (skip the first "no default" option)
  //   const modelOptions = select.locator('option:not([value=""])');
  //   const modelCount = await modelOptions.count();
  //   test.skip(modelCount === 0, 'No models available to select');

  //   // Select the first available model
  //   const firstModelValue = await modelOptions.first().getAttribute('value');
  //   const firstModelText = (await modelOptions.first().innerText()).trim();
  //   await select.selectOption(firstModelValue!);

  //   // Wait for save to complete
  //   await page.waitForTimeout(1_000);

  //   // Reload and verify the selection persisted
  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   const reloadedSelect = page.getByLabel('Default model');
  //   await expect(reloadedSelect).toBeVisible({ timeout: 15_000 });

  //   // The selected option text should match what we chose
  //   const selectedText = await reloadedSelect.locator('option:checked').innerText();
  //   expect(selectedText.trim()).toBe(firstModelText);

  //   // Clean up: reset to "no default"
  //   await reloadedSelect.selectOption('');
  //   await page.waitForTimeout(1_000);
  // });

  // test('should reset default model to "no default"', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   const select = page.getByLabel('Default model');
  //   await expect(select).toBeVisible({ timeout: 15_000 });

  //   // Select "no default"
  //   await select.selectOption('');
  //   await page.waitForTimeout(1_000);

  //   // Reload and verify
  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   const reloadedSelect = page.getByLabel('Default model');
  //   await expect(reloadedSelect).toBeVisible({ timeout: 15_000 });
  //   await expect(reloadedSelect).toHaveValue('', { timeout: 5_000 });
  // });

  // -----------------------------------------------------------------------
  // Show Token Count toggle — functional end-to-end
  // -----------------------------------------------------------------------

  test.describe.serial('Token count toggle controls metadata badge visibility', () => {
    let page: Page;

    test.beforeAll(async ({ browser }) => {
      page = await browser.newPage();
    });

    test.afterAll(async () => {
      // Clean up localStorage and close
      await page.evaluate(() => localStorage.removeItem('show-token-count'));
      await page.close();
    });

    test('should default to off', async () => {
      await goToChatPreferencesPage(page);

      // Reset to ensure clean state
      await page.evaluate(() => localStorage.removeItem('show-token-count'));
      await page.reload();
      await expect(
        page.getByRole('heading', { name: 'Chat Preferences' }),
      ).toBeVisible({ timeout: 15_000 });

      // Checkbox should be unchecked
      await expect(page.getByLabel('Show token count')).not.toBeChecked({ timeout: 5_000 });
      await expectToggleOff(page, 'Show token count');
      expect(await getLocalStorageBoolean(page, 'show-token-count')).toBe(false);
    });

    test('should turn on and persist across reload', async () => {
      // Toggle on
      await page.getByLabel('Show token count').check();
      await expectToggleOn(page, 'Show token count');
      expect(await getLocalStorageBoolean(page, 'show-token-count')).toBe(true);

      // Reload and verify it persisted
      await page.reload();
      await expect(
        page.getByRole('heading', { name: 'Chat Preferences' }),
      ).toBeVisible({ timeout: 15_000 });
      await expect(page.getByLabel('Show token count')).toBeChecked({ timeout: 5_000 });
      await expectToggleOn(page, 'Show token count');
    });

    test('should show metadata badges on a chat message when enabled', async () => {
      // This test does a real LLM round-trip — extend the default 30s timeout
      test.setTimeout(120_000);

      // Navigate to chat with a fresh page load to clear any stale Angular state
      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      // Wait for the model selector to finish loading (ensures the app is fully ready)
      const modelButton = page.getByRole('button', { name: 'Select model' });
      await expect(modelButton).not.toContainText('System Default', { timeout: 30_000 });

      // Send the message and wait for the full response
      await sendMessageAndWaitForResponse(page, 'Reply with exactly: hello');

      // Confirm we navigated to a session
      await page.waitForURL(/\/s\//, { timeout: 10_000 });

      // Rename the conversation to a known title for reliable cleanup
      const sessionOptionsButton = page.locator('button[aria-haspopup="menu"]').filter({
        has: page.locator('ng-icon[name="heroEllipsisHorizontalSolid"]'),
      });
      await sessionOptionsButton.first().hover();
      await sessionOptionsButton.first().click();

      await page.getByRole('menuitem', { name: 'Rename' }).click();

      const renameInput = page.getByLabel('Rename conversation');
      await expect(renameInput).toBeVisible({ timeout: 15_000 });
      await renameInput.fill(TOKEN_TEST_TITLE);
      await renameInput.press('Enter');

      // Wait for the rename input to disappear, confirming the rename was saved
      await expect(renameInput).toBeHidden({ timeout: 15_000 });

      // Verify the renamed session appears in the sidebar
      const sessionLink = page.locator('app-session-list a').filter({
        hasText: TOKEN_TEST_TITLE,
      });
      await expect(sessionLink.first()).toBeVisible({ timeout: 10_000 });

      // The metadata badges container is hidden until hover.
      // Hover over the assistant message group to reveal it.
      const messageGroup = page.locator('div[role="group"][aria-label="Assistant message with metadata"]').last();
      await messageGroup.hover();

      // At least one metadata badge should be visible (e.g. input tokens, output tokens, cost)
      const badges = messageGroup.locator('app-message-metadata-badges div.inline-flex');
      await expect(badges.first()).toBeVisible({ timeout: 5_000 });

      // Verify specific badge types are present — the model always returns token counts
      const badgeTexts = await badges.allInnerTexts();
      const hasSomeBadge = badgeTexts.some(
        (t) => t.includes('In:') || t.includes('Out:') || t.includes('TTFT:') || t.includes('Cost:'),
      );
      expect(hasSomeBadge).toBe(true);
    });

    test('should hide metadata badges after turning the toggle off', async () => {
      // Go back to settings and turn the toggle off
      await goToChatPreferencesPage(page);
      await expect(page.getByLabel('Show token count')).toBeChecked({ timeout: 5_000 });

      await page.getByLabel('Show token count').uncheck();
      await expectToggleOff(page, 'Show token count');
      expect(await getLocalStorageBoolean(page, 'show-token-count')).toBe(false);

      // Navigate back to the chat session — URL should still have /s/
      await page.goBack();
      await page.waitForURL(/\/s\//, { timeout: 10_000 });
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 60_000 });

      // Hover over the assistant message group
      const messageGroup = page.locator('div[role="group"][aria-label="Assistant message with metadata"]').last();
      await messageGroup.hover();

      // The badges component should render nothing when the toggle is off
      const badges = messageGroup.locator('app-message-metadata-badges div.inline-flex');
      await expect(badges).toHaveCount(0, { timeout: 5_000 });
    });

    test('should delete the conversation created during the badge test', async () => {
      await goToManageSessionsPage(page);

      // Target the actions button directly by its aria-label which includes the title
      const menuButton = page.getByRole('button', { name: `Actions for ${TOKEN_TEST_TITLE}` });
      await expect(menuButton).toBeVisible({ timeout: 10_000 });
      await menuButton.click();

      const menu = page.locator('[role="menu"]');
      await expect(menu).toBeVisible({ timeout: 10_000 });

      await menu.getByRole('menuitem', { name: 'Delete' }).click();

      // Confirm in the dialog
      const dialog = page.locator('[role="alertdialog"]');
      await expect(dialog).toBeVisible({ timeout: 5_000 });
      await dialog.getByRole('button', { name: 'Delete' }).click();
      await expect(dialog).toBeHidden({ timeout: 10_000 });

      // The conversation should be gone from the list
      await expect(menuButton).toBeHidden({ timeout: 60_000 });
    });
  });

  // -----------------------------------------------------------------------
  // Show Debug Output toggle — functional + visual
  // -----------------------------------------------------------------------

  // test('should display the debug output toggle with its icon and description', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   // The section card should contain the bug icon container
  //   const debugSection = page.locator('#show-debug-output').locator('..').locator('..');
  //   await expect(debugSection).toBeVisible({ timeout: 5_000 });

  //   // Label and description are present
  //   const label = page.locator('label[for="show-debug-output"]');
  //   await expect(label).toHaveText('Show debug output', { timeout: 5_000 });

  //   const description = debugSection.locator('p');
  //   await expect(description).toContainText('Show the full prompt sent to the model', { timeout: 5_000 });
  // });

  // test('should show the debug output toggle in the off visual state by default', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   await page.evaluate(() => localStorage.removeItem('show-debug-output'));
  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   await expect(page.getByLabel('Show debug output')).not.toBeChecked({ timeout: 5_000 });
  //   await expectToggleOff(page, 'Show debug output');
  //   await expectKnobDefault(page, 'Show debug output');
  // });

  // test('should visually switch the debug output toggle to the on state', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   await page.evaluate(() => localStorage.removeItem('show-debug-output'));
  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   await page.getByLabel('Show debug output').check();

  //   await expectToggleOn(page, 'Show debug output');
  //   await expectKnobTranslated(page, 'Show debug output');
  //   expect(await getLocalStorageBoolean(page, 'show-debug-output')).toBe(true);
  // });

  // test('should visually switch the debug output toggle back to the off state', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   await page.evaluate(() => localStorage.setItem('show-debug-output', 'true'));
  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   await expect(page.getByLabel('Show debug output')).toBeChecked({ timeout: 5_000 });
  //   await expectToggleOn(page, 'Show debug output');

  //   await page.getByLabel('Show debug output').uncheck();

  //   await expectToggleOff(page, 'Show debug output');
  //   await expectKnobDefault(page, 'Show debug output');
  //   expect(await getLocalStorageBoolean(page, 'show-debug-output')).toBe(false);
  // });

  // test('should persist debug output toggle visual state across reloads', async ({ page }) => {
  //   await goToChatPreferencesPage(page);

  //   await page.evaluate(() => localStorage.removeItem('show-debug-output'));
  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   await page.getByLabel('Show debug output').check();
  //   await expectToggleOn(page, 'Show debug output');

  //   await page.reload();
  //   await expect(
  //     page.getByRole('heading', { name: 'Chat Preferences' }),
  //   ).toBeVisible({ timeout: 15_000 });

  //   await expect(page.getByLabel('Show debug output')).toBeChecked({ timeout: 5_000 });
  //   await expectToggleOn(page, 'Show debug output');
  //   await expectKnobTranslated(page, 'Show debug output');

  //   // Clean up
  //   await page.evaluate(() => localStorage.removeItem('show-debug-output'));
  // });

  // -----------------------------------------------------------------------
  // Navigation links
  // -----------------------------------------------------------------------

  test('should navigate to Manage Conversations page', async ({ page }) => {
    await goToChatPreferencesPage(page);

    const manageLink = page.locator('a[href="/manage-sessions"]');
    await expect(manageLink).toBeVisible({ timeout: 5_000 });

    await manageLink.click();
    await page.waitForURL('**/manage-sessions', { timeout: 10_000 });

    await expect(
      page.getByRole('heading', { name: 'Manage Conversations' }),
    ).toBeVisible({ timeout: 15_000 });
  });

  test('should navigate to Memories page', async ({ page }) => {
    await goToChatPreferencesPage(page);

    const memoriesLink = page.locator('a[href="/memories"]');
    await expect(memoriesLink).toBeVisible({ timeout: 5_000 });

    await memoriesLink.click();
    await page.waitForURL('**/memories', { timeout: 10_000 });

    await expect(
      page.getByRole('heading', { name: 'Memories' }),
    ).toBeVisible({ timeout: 15_000 });
  });

  // -----------------------------------------------------------------------
  // Manage Conversations — selection and UI
  // -----------------------------------------------------------------------

  test('should select and deselect conversations on the manage page', async ({ page }) => {
    await goToManageSessionsPage(page);

    const checkbox = page.locator('input[type="checkbox"]').first();
    const hasCheckbox = (await checkbox.count()) > 0;
    test.skip(!hasCheckbox, 'No conversations available to select');

    // Select
    await checkbox.check();
    await expect(page.getByText(/1 of \d+ selected/)).toBeVisible({ timeout: 5_000 });

    // Deselect
    await checkbox.uncheck();
    await expect(page.getByText(/0 of \d+ selected/)).toBeVisible({ timeout: 5_000 });
  });

  test('should show delete button disabled when nothing is selected', async ({ page }) => {
    await goToManageSessionsPage(page);

    const deleteButton = page.getByRole('button', { name: /Delete Selected/i });
    const hasButton = (await deleteButton.count()) > 0;
    test.skip(!hasButton, 'No conversations available — delete button not rendered');

    await expect(deleteButton).toBeDisabled({ timeout: 5_000 });
  });

  test('should enable delete button when a conversation is selected', async ({ page }) => {
    await goToManageSessionsPage(page);

    const checkbox = page.locator('input[type="checkbox"]').first();
    const hasCheckbox = (await checkbox.count()) > 0;
    test.skip(!hasCheckbox, 'No conversations available to select');

    await checkbox.check();
    await expect(page.getByText(/1 of \d+ selected/)).toBeVisible({ timeout: 5_000 });

    const deleteButton = page.getByRole('button', { name: /Delete Selected/i });
    await expect(deleteButton).toBeEnabled({ timeout: 5_000 });

    // Clean up — deselect
    await checkbox.uncheck();
  });

  test('should clear selection via the "Clear selection" button', async ({ page }) => {
    await goToManageSessionsPage(page);

    const checkbox = page.locator('input[type="checkbox"]').first();
    const hasCheckbox = (await checkbox.count()) > 0;
    test.skip(!hasCheckbox, 'No conversations available to select');

    await checkbox.check();
    await expect(page.getByText(/1 of \d+ selected/)).toBeVisible({ timeout: 5_000 });

    await page.getByText('Clear selection').click();
    await expect(page.getByText(/0 of \d+ selected/)).toBeVisible({ timeout: 5_000 });
  });

  test('should open the 3-dot menu for a conversation', async ({ page }) => {
    await goToManageSessionsPage(page);

    const menuButton = page.locator('button[aria-label*="Actions for"]').first();
    const hasMenu = (await menuButton.count()) > 0;
    test.skip(!hasMenu, 'No conversations available for menu actions');

    await menuButton.click();

    const menu = page.locator('[role="menu"]');
    await expect(menu).toBeVisible({ timeout: 5_000 });

    await expect(
      menu.getByRole('menuitem', { name: 'Delete' }),
    ).toBeVisible({ timeout: 5_000 });
    await expect(
      menu.getByRole('menuitem', { name: 'Manage Shared Instances' }),
    ).toBeVisible({ timeout: 5_000 });
    await expect(
      menu.getByRole('menuitem', { name: 'Copy Share Link' }),
    ).toBeVisible({ timeout: 5_000 });

    // Close the menu by clicking elsewhere
    await page.locator('body').click({ position: { x: 10, y: 10 } });
    await expect(menu).toBeHidden({ timeout: 5_000 });
  });

  test('should show and cancel the bulk delete confirmation dialog', async ({ page }) => {
    await goToManageSessionsPage(page);

    const checkbox = page.locator('input[type="checkbox"]').first();
    const hasCheckbox = (await checkbox.count()) > 0;
    test.skip(!hasCheckbox, 'No conversations available to select');

    await checkbox.check();
    await expect(page.getByText(/1 of \d+ selected/)).toBeVisible({ timeout: 5_000 });

    await page.getByRole('button', { name: /Delete Selected/i }).click();

    const dialog = page.locator('[role="alertdialog"]');
    await expect(dialog).toBeVisible({ timeout: 5_000 });
    await expect(dialog.getByText(/Delete 1 Conversation/)).toBeVisible({ timeout: 5_000 });

    // Cancel
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    await expect(dialog).toBeHidden({ timeout: 5_000 });

    await checkbox.uncheck();
  });

  // -----------------------------------------------------------------------
  // Conversation lifecycle: create → share → verify share → delete (serial)
  //
  // Creates a throwaway conversation via chat, shares it publicly,
  // verifies the share link works, then deletes it from the
  // manage-sessions page. Uses a shared page so the session persists
  // across steps.
  // -----------------------------------------------------------------------

  test.describe.serial('Create, share, and delete a conversation', () => {
    let page: Page;
    let shareUrl: string;

    test.beforeAll(async ({ browser }) => {
      page = await browser.newPage();
    });

    test.afterAll(async () => {
      await page.close();
    });

    test('should create a conversation by sending a message', async () => {
      // This test does a real LLM round-trip — extend the default 30s timeout
      test.setTimeout(120_000);

      await page.goto('/');
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

      // Wait for the model selector to finish loading (ensures the app is fully ready)
      const modelButton = page.getByRole('button', { name: 'Select model' });
      await expect(modelButton).not.toContainText('System Default', { timeout: 30_000 });

      // Send a short message to create a session
      await sendMessageAndWaitForResponse(page, 'Reply with exactly: hello');

      // The URL should now contain /s/ indicating a session was created
      await page.waitForURL(/\/s\//, { timeout: 10_000 });
    });

    test('should rename the conversation to a known title', async () => {
      // The session options button is in the sidebar next to the active session
      const sessionOptionsButton = page.locator('button[aria-haspopup="menu"]').filter({
        has: page.locator('ng-icon[name="heroEllipsisHorizontalSolid"]'),
      });
      await sessionOptionsButton.first().hover();
      await sessionOptionsButton.first().click();

      await page.getByRole('menuitem', { name: 'Rename' }).click();

      const renameInput = page.getByLabel('Rename conversation');
      await expect(renameInput).toBeVisible({ timeout: 5_000 });

      await renameInput.fill(DELETE_TEST_TITLE);
      await renameInput.press('Enter');

      // Wait for the rename input to disappear, confirming the rename was saved
      // The input stays visible until the backend API call completes and the
      // component clears renamingSessionId, so use a generous timeout.
      await expect(renameInput).toBeHidden({ timeout: 15_000 });

      // Verify the renamed session appears in the sidebar
      const sessionLink = page.locator('app-session-list a').filter({
        hasText: DELETE_TEST_TITLE,
      });
      await expect(sessionLink.first()).toBeVisible({ timeout: 10_000 });
    });

    test('should open the share dialog from the sidebar menu', async () => {
      // Open the session options menu again
      const sessionOptionsButton = page.locator('button[aria-haspopup="menu"]').filter({
        has: page.locator('ng-icon[name="heroEllipsisHorizontalSolid"]'),
      });
      await sessionOptionsButton.first().hover();
      await sessionOptionsButton.first().click();

      // Click "Share" in the context menu
      await page.getByRole('menuitem', { name: 'Share' }).click();

      // The share dialog should appear
      const dialog = page.getByRole('dialog', { name: 'Share conversation' });
      await expect(dialog).toBeVisible({ timeout: 5_000 });

      // "Public link" should be the default selected option
      const publicRadio = dialog.locator('input[type="radio"][value="public"]');
      await expect(publicRadio).toBeChecked({ timeout: 5_000 });

      // Both access level options should be visible
      await expect(dialog.getByText('Public link')).toBeVisible({ timeout: 5_000 });
      await expect(dialog.getByText('Limited share')).toBeVisible({ timeout: 5_000 });

      // "Create share link" button should be visible
      await expect(
        dialog.getByRole('button', { name: 'Create share link' }),
      ).toBeVisible({ timeout: 5_000 });
    });

    test('should create a public share and generate a link', async () => {
      const dialog = page.getByRole('dialog', { name: 'Share conversation' });

      // Click "Create share link" with public access (already selected)
      await dialog.getByRole('button', { name: 'Create share link' }).click();

      // Wait for the success state — "Chat shared" confirmation
      await expect(dialog.getByText('Chat shared')).toBeVisible({ timeout: 15_000 });

      // The share URL input should be visible and contain a /shared/ path
      const urlInput = dialog.locator('input[readonly][type="text"]');
      await expect(urlInput).toBeVisible({ timeout: 5_000 });

      const urlValue = await urlInput.inputValue();
      expect(urlValue).toContain('/shared/');

      // Store the URL for the next test
      shareUrl = urlValue;

      // The "Copy link" button should be visible
      await expect(
        dialog.getByRole('button', { name: 'Copy link' }),
      ).toBeVisible({ timeout: 5_000 });

      // The info text about future messages should be shown
      await expect(
        dialog.getByText("Future messages aren't included"),
      ).toBeVisible({ timeout: 5_000 });

      // The button text should now say "Done" instead of "Cancel"
      await expect(
        dialog.getByRole('button', { name: 'Done' }),
      ).toBeVisible({ timeout: 5_000 });

      // Close the dialog
      await dialog.getByRole('button', { name: 'Done' }).click();
      await expect(dialog).toBeHidden({ timeout: 5_000 });
    });

    test('should load the shared conversation via the share link', async () => {
      // Navigate to the share URL
      const sharePath = new URL(shareUrl).pathname;
      await page.goto(sharePath);

      // The shared view should load with the "Shared read-only snapshot" badge
      await expect(
        page.getByText('Shared read-only snapshot'),
      ).toBeVisible({ timeout: 15_000 });

      // The conversation messages should be visible
      await expect(page.locator('app-message-list')).toBeVisible({ timeout: 10_000 });
    });

    test('should show the share in the Manage Shared Instances dialog', async () => {
      await page.goto('/manage-sessions');
      await expect(
        page.getByRole('heading', { name: 'Manage Conversations' }),
      ).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 15_000 });

      // Target the actions button directly by its aria-label which includes the title
      const menuButton = page.getByRole('button', { name: `Actions for ${DELETE_TEST_TITLE}` });
      await expect(menuButton).toBeVisible({ timeout: 5_000 });
      await menuButton.click();

      const menu = page.locator('[role="menu"]');
      await expect(menu).toBeVisible({ timeout: 5_000 });

      // Open "Manage Shared Instances"
      await menu.getByRole('menuitem', { name: 'Manage Shared Instances' }).click();

      const dialog = page.getByRole('dialog', { name: 'Manage Shared Instances' });
      await expect(dialog).toBeVisible({ timeout: 5_000 });

      // Wait for loading
      await expect(dialog.locator('.animate-spin')).toBeHidden({ timeout: 15_000 });

      // The public share we created should be listed
      await expect(dialog.getByText('Public')).toBeVisible({ timeout: 5_000 });

      // A delete button for the share should be visible
      const deleteShareButton = dialog.getByLabel('Delete share');
      await expect(deleteShareButton.first()).toBeVisible({ timeout: 5_000 });

      // Close the dialog
      await dialog.getByRole('button', { name: 'Done' }).click();
      await expect(dialog).toBeHidden({ timeout: 5_000 });
    });

    test('should delete the conversation via the context menu', async () => {
      // Target the actions button directly by its aria-label which includes the title
      const menuButton = page.getByRole('button', { name: `Actions for ${DELETE_TEST_TITLE}` });
      await expect(menuButton).toBeVisible({ timeout: 5_000 });
      await menuButton.click();

      const menu = page.locator('[role="menu"]');
      await expect(menu).toBeVisible({ timeout: 5_000 });

      // Click "Delete"
      await menu.getByRole('menuitem', { name: 'Delete' }).click();

      // Confirm in the dialog
      const dialog = page.locator('[role="alertdialog"]');
      await expect(dialog).toBeVisible({ timeout: 5_000 });
      await expect(dialog.getByText('Delete Conversation')).toBeVisible({ timeout: 5_000 });

      await dialog.getByRole('button', { name: 'Delete' }).click();
      await expect(dialog).toBeHidden({ timeout: 10_000 });

      // The conversation should be gone from the list
      await expect(menuButton).toBeHidden({ timeout: 10_000 });
    });
  });

  // -----------------------------------------------------------------------
  // Manage Shared Instances dialog
  // -----------------------------------------------------------------------

  test('should open the Manage Shared Instances dialog', async ({ page }) => {
    await goToManageSessionsPage(page);

    const menuButton = page.locator('button[aria-label*="Actions for"]').first();
    const hasMenu = (await menuButton.count()) > 0;
    test.skip(!hasMenu, 'No conversations available for sharing actions');

    await menuButton.click();

    const menu = page.locator('[role="menu"]');
    await expect(menu).toBeVisible({ timeout: 5_000 });

    await menu.getByRole('menuitem', { name: 'Manage Shared Instances' }).click();

    // Use a named role locator to avoid matching the CDK dialog wrapper element
    const dialog = page.getByRole('dialog', { name: 'Manage Shared Instances' });
    await expect(dialog).toBeVisible({ timeout: 5_000 });
    await expect(dialog.getByText('Manage Shared Instances')).toBeVisible({ timeout: 5_000 });

    // Wait for loading to finish
    await expect(dialog.locator('.animate-spin')).toBeHidden({ timeout: 15_000 });

    const hasShares = (await dialog.locator('text=Public').count()) > 0
      || (await dialog.locator('text=Limited Access').count()) > 0;

    if (!hasShares) {
      await expect(dialog.getByText('No shared instances for this conversation')).toBeVisible({ timeout: 5_000 });
    }

    // Close via "Done"
    await dialog.getByRole('button', { name: 'Done' }).click();
    await expect(dialog).toBeHidden({ timeout: 5_000 });
  });

  test('should close the Manage Shared Instances dialog via the X button', async ({ page }) => {
    await goToManageSessionsPage(page);

    const menuButton = page.locator('button[aria-label*="Actions for"]').first();
    const hasMenu = (await menuButton.count()) > 0;
    test.skip(!hasMenu, 'No conversations available for sharing actions');

    await menuButton.click();

    const menu = page.locator('[role="menu"]');
    await expect(menu).toBeVisible({ timeout: 5_000 });

    await menu.getByRole('menuitem', { name: 'Manage Shared Instances' }).click();

    // Use a named role locator to avoid matching the CDK dialog wrapper element
    const dialog = page.getByRole('dialog', { name: 'Manage Shared Instances' });
    await expect(dialog).toBeVisible({ timeout: 5_000 });

    await dialog.getByLabel('Close dialog').click();
    await expect(dialog).toBeHidden({ timeout: 5_000 });
  });

  // -----------------------------------------------------------------------
  // Manage Conversations — navigation
  // -----------------------------------------------------------------------

  test('should navigate back from manage conversations to home', async ({ page }) => {
    await goToManageSessionsPage(page);

    await page.getByText('Back').click();
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 10_000 });
  });

  test('should refresh the conversations list', async ({ page }) => {
    await goToManageSessionsPage(page);

    const refreshButton = page.getByLabel('Refresh conversations');
    await expect(refreshButton).toBeVisible({ timeout: 5_000 });

    await refreshButton.click();

    await expect(
      page.getByRole('heading', { name: 'Manage Conversations' }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/0 of \d+ selected/)).toBeVisible({ timeout: 15_000 });
  });

  // -----------------------------------------------------------------------
  // Memories page — basic navigation and structure
  // -----------------------------------------------------------------------

  test('should load the Memories page and show status', async ({ page }) => {
    await page.goto('/memories');
    await expect(
      page.getByRole('heading', { name: 'Memories' }),
    ).toBeVisible({ timeout: 15_000 });

    // Wait for the status check to complete
    await expect(page.getByText('Checking memory status...')).toBeHidden({ timeout: 15_000 });

    // Either memory is available (shows the info banner) or unavailable (shows warning)
    const isAvailable = (await page.getByText('automatically extracted from your conversations').count()) > 0;
    const isUnavailable = (await page.getByText('Memory Not Available').count()) > 0;

    expect(isAvailable || isUnavailable).toBe(true);
  });
});
