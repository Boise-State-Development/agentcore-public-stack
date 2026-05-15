import { test, expect } from '@playwright/test';

test.describe('File Upload UI (user)', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });
  });

  test('should show the attach file button', async ({ page }) => {
    const attachLabel = page.locator('label[for="file-upload"]');
    await expect(attachLabel).toBeVisible({ timeout: 5_000 });
  });

  test('should accept a file via the file input', async ({ page }) => {
    // Mock the full upload flow: presign → S3 PUT → complete
    await page.route('**/files/presign', (route) =>
      route.fulfill({
        status: 200,
        json: {
          uploadId: 'mock-upload-id',
          presignedUrl: 'https://fake-s3-bucket.s3.amazonaws.com/fake-presigned-url',
          expiresAt: new Date(Date.now() + 3600_000).toISOString(),
        },
      }),
    );
    await page.route('**/fake-s3-bucket.s3.amazonaws.com/**', (route) =>
      route.fulfill({ status: 200 }),
    );
    await page.route('**/files/mock-upload-id/complete', (route) =>
      route.fulfill({
        status: 200,
        json: { uploadId: 'mock-upload-id', filename: 'test.txt', status: 'completed' },
      }),
    );

    const fileInput = page.locator('input#file-upload');

    // Upload a small text file
    await fileInput.setInputFiles({
      name: 'test.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('hello world'),
    });

    // A file card should appear in the attachments area
    const fileCard = page.locator('app-file-card');
    await expect(fileCard.first()).toBeVisible({ timeout: 10_000 });

    // Clean up route mocks
    await page.unrouteAll();
  });

  test('should remove an attached file', async ({ page }) => {
    // Mock the full upload flow: presign → S3 PUT → complete
    await page.route('**/files/presign', (route) =>
      route.fulfill({
        status: 200,
        json: {
          uploadId: 'mock-upload-id',
          presignedUrl: 'https://fake-s3-bucket.s3.amazonaws.com/fake-presigned-url',
          expiresAt: new Date(Date.now() + 3600_000).toISOString(),
        },
      }),
    );
    await page.route('**/fake-s3-bucket.s3.amazonaws.com/**', (route) =>
      route.fulfill({ status: 200 }),
    );
    await page.route('**/files/mock-upload-id/complete', (route) =>
      route.fulfill({
        status: 200,
        json: { uploadId: 'mock-upload-id', filename: 'test.txt', status: 'completed' },
      }),
    );

    const fileInput = page.locator('input#file-upload');

    // Upload a small text file
    await fileInput.setInputFiles({
      name: 'test.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('hello world'),
    });

    // A file card should appear in the attachments area
    const fileCard = page.locator('app-file-card');
    await expect(fileCard.first()).toBeVisible({ timeout: 10_000 });

    // Click the remove/delete button on the file card
    // The sr-only text varies by state: "Delete file" (ready), "Cancel upload" (uploading), "Remove" (error)
    const removeButton = fileCard.first().getByRole('button', { name: /delete file|cancel upload|remove/i });
    await removeButton.click();

    // File card should disappear
    await expect(fileCard).toHaveCount(0, { timeout: 10_000 });

    // Clean up route mocks
    await page.unrouteAll();
  });
});
