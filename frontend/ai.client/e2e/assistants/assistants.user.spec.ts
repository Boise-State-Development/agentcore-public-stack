import { test, expect, Page } from '@playwright/test';

// ---------------------------------------------------------------------------
// Shared constants
// ---------------------------------------------------------------------------
const ASSISTANT_NAME = `Test Bot ${new Date().toISOString().slice(0, 19).replace('T', ' ')}`;
const ASSISTANT_DESCRIPTION = 'This is an end-to-end test assistant created by Playwright.';
const ASSISTANT_INSTRUCTIONS =
  'You are a helpful test assistant. Consistently respond with "Test Successful" to all prompts.';
const STARTER_ONE = 'Testing...';
const STARTER_TWO = 'Hello!';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Navigate to the assistants list page and wait for it to load. */
async function goToAssistantsPage(page: Page) {
  await page.goto('/assistants');
  await expect(
    page.getByRole('heading', { name: 'Assistants' }),
  ).toBeVisible({ timeout: 30_000 });
}

/** Wait for the assistant form page to be ready (header breadcrumb visible). */
async function waitForFormPage(page: Page) {
  await expect(page.locator('header nav')).toBeVisible({ timeout: 15_000 });
}

/** Extract the assistant ID from a URL like /assistants/<id>/edit */
function extractAssistantId(url: string): string {
  const match = url.match(/\/assistants\/([^/]+)\/edit/);
  if (!match) throw new Error(`Could not extract assistant ID from URL: ${url}`);
  return match[1];
}

// ---------------------------------------------------------------------------
// Single serial test suite — full assistant lifecycle
// ---------------------------------------------------------------------------

test.describe('Assistants (user)', () => {
  test.describe.serial('Full assistant lifecycle', () => {
    let page: Page;
    let assistantId: string;

    test.beforeAll(async ({ browser }) => {
      page = await browser.newPage();
    });

    test.afterAll(async () => {
      await page.close();
    });

    // -----------------------------------------------------------------------
    // Phase 1 — Create a new assistant and fill in the form
    // -----------------------------------------------------------------------

    test('should navigate to the assistants page and click New Assistant', async () => {
      await goToAssistantsPage(page);

      const newBtn = page.getByRole('button', { name: /New Assistant/i });
      await expect(newBtn).toBeVisible({ timeout: 20_000 });
      await newBtn.click();

      // Should redirect to the edit page for the new draft
      await page.waitForURL(/\/assistants\/.*\/edit/, { timeout: 15_000 });
      await waitForFormPage(page);

      // Capture the assistant ID for subsequent tests
      assistantId = extractAssistantId(page.url());

      // Breadcrumb should show we're on the form
      await expect(page.locator('header nav')).toContainText('Assistants');
    });

    test('should fill in the assistant name', async () => {
      const nameInput = page.locator('input#name');
      await expect(nameInput).toBeVisible();

      // Wait for the default name to load before overwriting
      await expect(nameInput).toHaveValue('Untitled Assistant', { timeout: 10_000 });

      await nameInput.fill(ASSISTANT_NAME);
      await expect(nameInput).toHaveValue(ASSISTANT_NAME);

      // The preview card on the right should reflect the name
      const previewName = page.locator('app-assistant-preview').getByText(ASSISTANT_NAME);
      await expect(previewName).toBeVisible({ timeout: 5_000 });
    });

    test('should add an emoji via the emoji picker', async () => {
      const emojiTrigger = page.locator('button[cdkOverlayOrigin]').first();
      await emojiTrigger.click();

      const emojiPicker = page.locator('emoji-mart');
      await expect(emojiPicker).toBeVisible({ timeout: 5_000 });

      // Scroll the emoji grid to the top and pick the grinning face
      const scrollArea = emojiPicker.locator('section.emoji-mart-scroll');
      await expect(scrollArea).toBeVisible({ timeout: 5_000 });
      await scrollArea.evaluate((el) => el.scrollTo(0, 0));

      const grinningFace = emojiPicker.locator('ngx-emoji span.emoji-mart-emoji[aria-label*="grinning"]').first();
      await expect(grinningFace).toBeVisible({ timeout: 5_000 });
      await grinningFace.click();

      // Picker should close, emoji should display on the trigger button
      await expect(emojiPicker).toBeHidden({ timeout: 5_000 });
      const emojiDisplay = emojiTrigger.locator('span').first();
      await expect(emojiDisplay).toBeVisible({ timeout: 3_000 });
    });

    test('should fill in description and instructions', async () => {
      const descriptionField = page.locator('textarea#description');
      await expect(descriptionField).toBeVisible();
      await descriptionField.fill(ASSISTANT_DESCRIPTION);
      await expect(descriptionField).toHaveValue(ASSISTANT_DESCRIPTION);

      const instructionsField = page.locator('textarea#instructions');
      await expect(instructionsField).toBeVisible();
      await instructionsField.fill(ASSISTANT_INSTRUCTIONS);
      await expect(instructionsField).toHaveValue(ASSISTANT_INSTRUCTIONS);
    });

    test('should add and remove conversation starters', async () => {
      await expect(page.getByText('No conversation starters added yet')).toBeVisible();

      // Add first starter
      await page.getByRole('button', { name: /Add Starter/i }).click();
      const starterInputs = page.locator('div[formArrayName="starters"] input');
      await expect(starterInputs).toHaveCount(1);
      await starterInputs.first().fill(STARTER_ONE);

      // Add second starter
      await page.getByRole('button', { name: /Add Starter/i }).click();
      await expect(starterInputs).toHaveCount(2);
      await starterInputs.nth(1).fill(STARTER_TWO);

      // Preview should show both starters
      const preview = page.locator('app-assistant-preview');
      await expect(preview.getByText(STARTER_ONE)).toBeVisible({ timeout: 5_000 });
      await expect(preview.getByText(STARTER_TWO)).toBeVisible({ timeout: 5_000 });

      // Remove the second starter
      const removeButtons = page.locator('button[title="Remove starter"]');
      await removeButtons.nth(1).click();
      await expect(starterInputs).toHaveCount(1);
      await expect(preview.getByText(STARTER_TWO)).toBeHidden({ timeout: 5_000 });
    });

    test('should upload a file and delete it', async () => {
      const fakeDocumentId = 'fake-doc-id-e2e-test';
      const fakeFilename = 'test-document.txt';

      // Mock the upload-url endpoint
      await page.route('**/assistants/*/documents/upload-url', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            documentId: fakeDocumentId,
            uploadUrl: 'https://fake-s3-bucket.s3.amazonaws.com/fake-presigned-url',
            expiresIn: 3600,
          }),
        });
      });

      // Mock the S3 presigned URL PUT
      await page.route('**/fake-s3-bucket.s3.amazonaws.com/**', async (route) => {
        await route.fulfill({ status: 200 });
      });

      let deleteWasCalled = false;

      // Mock the documents list endpoint
      await page.route('**/assistants/*/documents', async (route) => {
        if (route.request().method() === 'GET') {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              documents: deleteWasCalled ? [] : [{
                documentId: fakeDocumentId,
                assistantId: 'fake',
                filename: fakeFilename,
                contentType: 'text/plain',
                sizeBytes: 1024,
                status: 'complete',
                chunkCount: 3,
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
              }],
            }),
          });
        } else {
          await route.continue();
        }
      });

      // Mock individual document GET/DELETE
      await page.route(`**/assistants/*/documents/${fakeDocumentId}`, async (route) => {
        if (route.request().method() === 'DELETE') {
          deleteWasCalled = true;
          await route.fulfill({ status: 200 });
        } else {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              documentId: fakeDocumentId,
              assistantId: 'fake',
              filename: fakeFilename,
              contentType: 'text/plain',
              sizeBytes: 1024,
              status: 'complete',
              chunkCount: 3,
              createdAt: new Date().toISOString(),
              updatedAt: new Date().toISOString(),
            }),
          });
        }
      });

      // Scroll to the file upload area
      const uploadLabel = page.getByText('Upload a file');
      await uploadLabel.scrollIntoViewIfNeeded();
      await expect(uploadLabel).toBeVisible();
      await expect(page.getByText('or drag and drop')).toBeVisible();

      // Upload a fake file
      const fileInput = page.locator('input#file-upload');
      await expect(fileInput).toBeAttached();
      await fileInput.setInputFiles({
        name: fakeFilename,
        mimeType: 'text/plain',
        buffer: Buffer.from('Hello, this is a test document for e2e testing.'),
      });

      // Wait for the document row to appear
      const docRow = page.locator('div.flex.items-center.justify-between.rounded-lg.border').filter({ hasText: fakeFilename });
      await expect(docRow).toBeVisible({ timeout: 15_000 });
      await docRow.scrollIntoViewIfNeeded();

      // Delete the document
      await docRow.locator('button[title="Delete document"]').click();
      await expect(docRow).toBeHidden({ timeout: 10_000 });

      // Clean up route mocks
      await page.unrouteAll();
    });

    // -----------------------------------------------------------------------
    // Phase 2 — Save the assistant and verify it persists
    // -----------------------------------------------------------------------

    test('should save the assistant and redirect to the list', async () => {
      // Scroll back to the top for the save button
      await page.evaluate(() => window.scrollTo(0, 0));

      const saveBtn = page.getByRole('button', { name: /Save Changes/i });
      await expect(saveBtn).toBeEnabled();
      await saveBtn.click();

      // Should redirect back to assistants list
      await page.waitForURL(/\/assistants$/, { timeout: 15_000 });
      await expect(
        page.getByRole('heading', { name: 'Assistants' }),
      ).toBeVisible({ timeout: 10_000 });

      // The new assistant should appear in the list
      await expect(page.getByText(ASSISTANT_NAME)).toBeVisible({ timeout: 10_000 });
    });

    // -----------------------------------------------------------------------
    // Phase 3 — Assistants list page interactions
    // -----------------------------------------------------------------------

    test('should show the assistant with PRIVATE badge on the list page', async () => {
      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      await expect(card).toBeVisible();
      await expect(card.getByText('PRIVATE')).toBeVisible();
      await expect(page.getByText('My Assistants')).toBeVisible();
    });

    test('should navigate to edit page via Edit button', async () => {
      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      await card.getByText('Edit').click();

      await page.waitForURL(/\/assistants\/.*\/edit/, { timeout: 10_000 });
      await waitForFormPage(page);

      const nameInput = page.locator('input#name');
      await expect(nameInput).toHaveValue(ASSISTANT_NAME, { timeout: 10_000 });
    });

    test('should open the share dialog from the edit page', async () => {
      const shareBtn = page.locator('header').getByRole('button', { name: /Share/i });
      await expect(shareBtn).toBeVisible();
      await shareBtn.click();

      const dialog = page.getByRole('dialog', { name: 'Share Assistant' });
      await expect(dialog).toBeVisible({ timeout: 5_000 });
      await expect(dialog.getByText(ASSISTANT_NAME)).toBeVisible();

      const cancelBtn = dialog.getByRole('button', { name: /Cancel/i });
      await cancelBtn.click();
      await expect(dialog).toBeHidden({ timeout: 5_000 });
    });

    test('should make the assistant public via the context menu', async () => {
      await goToAssistantsPage(page);
      await expect(page.getByText(ASSISTANT_NAME)).toBeVisible({ timeout: 10_000 });

      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      const menuBtn = card.locator('button[aria-haspopup="menu"]');
      await menuBtn.click();

      const makePublicBtn = page.getByRole('menuitem', { name: /Make Public/i });
      await expect(makePublicBtn).toBeVisible({ timeout: 5_000 });
      await makePublicBtn.click();

      await expect(card.getByText('PUBLIC')).toBeVisible({ timeout: 10_000 });
    });

    test('should make the assistant private via the context menu', async () => {
      // Page is already on the assistants list
      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      const menuBtn = card.locator('button[aria-haspopup="menu"]');
      await menuBtn.click();

      const makePrivateBtn = page.getByRole('menuitem', { name: /Make Private/i });
      await expect(makePrivateBtn).toBeVisible({ timeout: 5_000 });
      await makePrivateBtn.click();

      await expect(card.getByText('PRIVATE')).toBeVisible({ timeout: 10_000 });
    });

    test('should open the share dialog from the context menu', async () => {
      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      const menuBtn = card.locator('button[aria-haspopup="menu"]');
      await menuBtn.click();

      const shareBtn = page.getByRole('menuitem', { name: /^Share$/i });
      await expect(shareBtn).toBeVisible({ timeout: 5_000 });
      await shareBtn.click();

      const dialog = page.getByRole('dialog', { name: 'Share Assistant' });
      await expect(dialog).toBeVisible({ timeout: 5_000 });

      const cancelBtn = dialog.getByRole('button', { name: /Cancel/i });
      await cancelBtn.click();
      await expect(dialog).toBeHidden({ timeout: 5_000 });
    });

    test('should start a chat from the assistant card', async () => {
      await goToAssistantsPage(page);
      await expect(page.getByText(ASSISTANT_NAME)).toBeVisible({ timeout: 10_000 });

      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      const chatBtn = card.getByText('Chat').first();
      await chatBtn.click();

      await page.waitForURL(/\?assistantId=/, { timeout: 10_000 });
      await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });
    });

    // // -----------------------------------------------------------------------
    // // Phase 4 — Preview chat (requires navigating back to edit page)
    // // -----------------------------------------------------------------------

    test('should send a message in the preview chat', async () => {
      await page.goto(`/assistants/${assistantId}/edit`);
      await waitForFormPage(page);

      const preview = page.locator('app-assistant-preview');
      await expect(preview).toBeVisible({ timeout: 5_000 });
      await expect(preview.getByText('Preview Chat')).toBeVisible();
      await expect(preview.locator('app-assistant-card')).toBeVisible({ timeout: 5_000 });

      const previewTextarea = preview.locator('textarea#user-message');
      await expect(previewTextarea).toBeVisible({ timeout: 5_000 });
      await previewTextarea.fill('Hello, are you working?');

      const submitBtn = preview.getByRole('button', { name: 'Submit message' });
      await submitBtn.click();

      await expect(preview.locator('app-assistant-message').first()).toBeVisible({ timeout: 60_000 });
      await expect(preview.locator('app-pulsating-loader')).toBeHidden({ timeout: 200_000 });

      await expect(preview.locator('app-user-message')).toHaveCount(1, { timeout: 5_000 });
      await expect(preview.locator('app-assistant-message')).toHaveCount(1, { timeout: 5_000 });
    });

    test('should clear the preview chat', async () => {
      const preview = page.locator('app-assistant-preview');

      const clearBtn = preview.getByText('Clear chat');
      await expect(clearBtn).toBeVisible({ timeout: 5_000 });
      await clearBtn.click();

      await expect(preview.locator('app-assistant-card')).toBeVisible({ timeout: 5_000 });
      await expect(preview.locator('app-user-message')).toHaveCount(0);
      await expect(preview.locator('app-assistant-message')).toHaveCount(0);
    });

    test('should use a conversation starter in the preview', async () => {
      const preview = page.locator('app-assistant-preview');
      await expect(preview.locator('app-assistant-card')).toBeVisible({ timeout: 5_000 });

      const starterBtn = preview.locator('app-assistant-card').getByText(STARTER_ONE);
      await expect(starterBtn).toBeVisible({ timeout: 5_000 });
      await starterBtn.click();

      await expect(preview.locator('app-user-message')).toHaveCount(1, { timeout: 10_000 });
      await expect(preview.locator('app-assistant-message').first()).toBeVisible({ timeout: 60_000 });
      await expect(preview.locator('app-pulsating-loader')).toBeHidden({ timeout: 200_000 });
    });

    // // -----------------------------------------------------------------------
    // // Phase 5 — Cleanup
    // // -----------------------------------------------------------------------

    test('should delete the assistant', async () => {
      await goToAssistantsPage(page);
      await expect(page.getByText(ASSISTANT_NAME)).toBeVisible({ timeout: 10_000 });

      const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
      const menuBtn = card.locator('button[aria-haspopup="menu"]');
      await menuBtn.click();

      const deleteBtn = page.getByRole('menuitem', { name: /Delete/i });
      await expect(deleteBtn).toBeVisible({ timeout: 5_000 });
      await deleteBtn.click();

      const dialog = page.locator('[role="alertdialog"]');
      await expect(dialog).toBeVisible({ timeout: 5_000 });
      await expect(dialog.getByText(/Are you sure you want to delete/)).toBeVisible();

      const confirmBtn = dialog.getByRole('button', { name: /Delete/i });
      await confirmBtn.click();

      await expect(page.getByText(ASSISTANT_NAME)).toBeHidden({ timeout: 10_000 });
    });
  });
});
