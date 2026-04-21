import { test, expect, Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Navigate to the settings/profile page and wait for it to load. */
async function goToProfilePage(page: Page) {
  await page.goto('/settings/profile');
  await expect(
    page.getByRole('heading', { name: 'Settings' }),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('heading', { name: 'Profile' })).toBeVisible({ timeout: 10_000 });
}

/** Navigate to the My Files page and wait for it to load. */
// async function goToFilesPage(page: Page) {
//   await page.goto('/files');
//   await expect(
//     page.getByRole('heading', { name: 'My Files' }),
//   ).toBeVisible({ timeout: 15_000 });
//   // Wait for loading to finish
//   await expect(page.getByText('Loading files...')).toBeHidden({ timeout: 15_000 });
// }

/** Send a chat message and wait for the assistant to finish responding. */
// async function sendMessageAndWaitForResponse(page: Page, message: string) {
//   const textarea = page.locator('textarea#user-message');
//   await expect(textarea).toBeVisible({ timeout: 15_000 });
//   await textarea.fill(message);

//   await page.getByRole('button', { name: 'Submit message' }).click();

//   const assistantMessage = page.locator('app-assistant-message').last();
//   await expect(assistantMessage).toBeVisible({ timeout: 60_000 });
//   await expect(page.locator('app-pulsating-loader')).toBeHidden({ timeout: 200_000 });
// }

// ---------------------------------------------------------------------------
// Independent profile page tests
// ---------------------------------------------------------------------------

test.describe('Settings / Profile (user)', () => {
  test('should display the profile page with correct user info', async ({ page }) => {
    await goToProfilePage(page);

    // Verify user name (in the avatar/name header section)
    await expect(page.getByRole('heading', { name: 'test_user' })).toBeVisible({ timeout: 10_000 });

    // Verify email (in the avatar/name header section, scoped to avoid the details row)
    await expect(page.locator('dd').filter({ hasText: 'oscarfilson@boisestate.edu' })).toBeVisible({ timeout: 10_000 });

    // Verify managed by Entra ID badge
    await expect(page.getByText('Managed by Entra ID')).toBeVisible({ timeout: 10_000 });
  });

  test('should show the full name in the read-only details', async ({ page }) => {
    await goToProfilePage(page);

    // The "Full name" label and value should be visible in the details section
    const fullNameLabel = page.getByText('Full name');
    await expect(fullNameLabel).toBeVisible({ timeout: 10_000 });

    // The name value should appear in the details row
    await expect(page.locator('dd').filter({ hasText: 'test_user' })).toBeVisible({ timeout: 10_000 });
  });

  test('should show the email in the read-only details', async ({ page }) => {
    await goToProfilePage(page);

    const emailLabel = page.locator('dt').filter({ hasText: 'Email' });
    await expect(emailLabel).toBeVisible({ timeout: 10_000 });

    await expect(page.locator('dd').filter({ hasText: 'oscarfilson@boisestate.edu' })).toBeVisible({ timeout: 10_000 });
  });

  test('should show the My Files link and navigate to files page', async ({ page }) => {
    await goToProfilePage(page);

    const myFilesLink = page.getByRole('link', { name: /My Files/i });
    await expect(myFilesLink).toBeVisible({ timeout: 10_000 });

    await myFilesLink.click();
    await page.waitForURL(/\/files/, { timeout: 10_000 });
    await expect(
      page.getByRole('heading', { name: 'My Files' }),
    ).toBeVisible({ timeout: 15_000 });
  });

  test('should navigate back to chat from settings', async ({ page }) => {
    await goToProfilePage(page);

    const backLink = page.getByRole('link', { name: /Back to Chat/i });
    await expect(backLink).toBeVisible({ timeout: 10_000 });
    await backLink.click();

    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });
  });

//   // -------------------------------------------------------------------------
//   // Serial: File upload → My Files → Delete conversation → File gone
//   // -------------------------------------------------------------------------

// const TEST_FILE_NAME = 'e2e-profile-test.txt';
// const TEST_FILE_CONTENT = 'Hello from Playwright e2e profile test.';

//   test.describe.serial('File lifecycle via chat and My Files', () => {
//     let page: Page;

//     test.beforeAll(async ({ browser }) => {
//       page = await browser.newPage();
//     });

//     test.afterAll(async () => {
//       await page.close();
//     });

//     test('should upload a file in a new chat and send a message', async () => {
//       await page.goto('/');
//       await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

//       // Attach a real text file
//       const fileInput = page.locator('input#file-upload');
//       await fileInput.setInputFiles({
//         name: TEST_FILE_NAME,
//         mimeType: 'text/plain',
//         buffer: Buffer.from(TEST_FILE_CONTENT),
//       });

//       // Wait for the file card to appear (upload in progress or completed)
//       const fileCard = page.locator('app-file-card');
//       await expect(fileCard.first()).toBeVisible({ timeout: 15_000 });

//       // Send a message to create the conversation
//       await sendMessageAndWaitForResponse(page, 'Describe the attached file in one sentence.');

//       // We should now be in a session URL
//       await page.waitForURL(/\/s\//, { timeout: 15_000 });
//     });

//     test('should see the uploaded file in My Files', async () => {
//       await goToFilesPage(page);

//       // The uploaded file should appear in the list
//       await expect(page.getByText(TEST_FILE_NAME)).toBeVisible({ timeout: 15_000 });
//     });

//     test('should navigate back and delete the conversation', async () => {
//       // Go to manage sessions
//       await page.goto('/manage-sessions');
//       await expect(
//         page.getByRole('heading', { name: 'Manage Conversations' }),
//       ).toBeVisible({ timeout: 15_000 });
//       await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 30_000 });

//       // Find the most recent conversation and select it
//       const checkbox = page.locator('input[type="checkbox"]').first();
//       await expect(checkbox).toBeVisible({ timeout: 10_000 });
//       await checkbox.check();

//       // Click Delete Selected
//       const deleteBtn = page.getByRole('button', { name: /Delete Selected/i });
//       await expect(deleteBtn).toBeEnabled({ timeout: 5_000 });
//       await deleteBtn.click();

//       // Confirm the deletion dialog
//       const confirmBtn = page.getByRole('button', { name: /^Delete$/i }).last();
//       await expect(confirmBtn).toBeVisible({ timeout: 5_000 });
//       await confirmBtn.click();

//       // Wait for deletion to complete (toast or list update)
//       await expect(page.getByText(/Deleted|deleted/)).toBeVisible({ timeout: 15_000 });
//     });

//     test('should no longer see the file in My Files', async () => {
//       await goToFilesPage(page);

//       // The file should no longer be listed
//       await expect(page.getByText(TEST_FILE_NAME)).toBeHidden({ timeout: 15_000 });
//     });
//   });
});
