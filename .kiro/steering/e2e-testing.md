---
inclusion: fileMatch
fileMatchPattern: "frontend/ai.client/e2e/**"
---

# E2E Testing Guidelines (Playwright)

Reference: #[[file:frontend/ai.client/playwright.config.ts]]

## Framework & Configuration

- **Runner**: Playwright Test
- **Test directory**: `frontend/ai.client/e2e/`
- **Base URL**: `http://localhost:4200`
- **Auth setup**: Cognito login via setup projects that save `storageState` to `e2e/.auth/`

## File Naming & Project Matching

The Playwright config routes tests to projects by filename pattern. Use the correct suffix:

| Suffix | Project | Auth | Example |
|---|---|---|---|
| `.spec.ts` | `chromium` (unauthenticated) | None | `login.spec.ts`, `not-found.spec.ts` |
| `.user.spec.ts` | `user` (regular user) | `e2e/.auth/user.json` | `chat.user.spec.ts` |
| `.auth.spec.ts` | `admin` (admin user) | `e2e/.auth/admin.json` | (future admin tests) |
| `.setup.ts` | Setup only | N/A | `auth-user.setup.ts` |

Never mix authenticated and unauthenticated tests in the same file. The project matcher determines which storage state is injected.

## Directory Organization

Group tests by feature area in subdirectories:

```
e2e/
├── auth/               # Login, logout, redirects, guards
├── home-page/          # Chat, model selector, settings panel, file upload
├── assistants/         # Assistant CRUD lifecycle
├── settings/           # Profile, preferences
└── manage-sessions.user.spec.ts   # Top-level if the feature is a single page
```

Place a test file in the subdirectory of the feature it exercises. Only use the top-level `e2e/` directory for single-page features that don't warrant a folder.

## Timeouts

The app talks to real AWS backends (DynamoDB, Bedrock, Cognito). Network latency and cold starts mean UI elements don't appear instantly. Use generous, tiered timeouts:

| What you're waiting for | Timeout |
|---|---|
| Page heading / primary element after `goto` | `15_000` |
| Model selector to finish loading (replaces "System Default") | `30_000` |
| Assistant message to appear (LLM first token) | `60_000` |
| Pulsating loader to disappear (LLM stream complete) | `200_000` |
| Loading spinners / "Loading..." text to disappear | `15_000` |
| UI reaction to a click (dialog open, menu visible) | `5_000` |
| Navigation via `waitForURL` | `10_000` |
| Cognito redirect after login submit | `30_000` |

Always pass an explicit `{ timeout: N }` to `toBeVisible`, `toBeHidden`, `toHaveCount`, `waitForURL`, and similar assertions. Never rely on Playwright's global default — it's too short for this stack.

## Waiting for Page Ready State

After every `page.goto(...)`, wait for a landmark element that proves the page has loaded and hydrated before interacting with anything else. The standard landmarks are:

```typescript
// Home / chat page
await expect(page.locator('textarea#user-message')).toBeVisible({ timeout: 15_000 });

// Manage sessions
await expect(
  page.getByRole('heading', { name: 'Manage Conversations' }),
).toBeVisible({ timeout: 15_000 });

// Assistants list
await expect(
  page.getByRole('heading', { name: 'Assistants' }),
).toBeVisible({ timeout: 15_000 });

// Settings / profile
await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible({ timeout: 15_000 });
```

If the page has a loading state (e.g., "Loading conversations...", "Loading tools...", "Loading files..."), also wait for it to disappear before asserting on data:

```typescript
await expect(page.getByText('Loading conversations...')).toBeHidden({ timeout: 15_000 });
```

## Locator Strategy — Avoiding Double Matches

Angular renders components like `app-session-list`, `app-user-message`, `app-assistant-message`, etc. Some pages render the same component type multiple times (e.g., a preview panel and the main area both contain `textarea#user-message`). To avoid ambiguous matches:

1. **Scope locators to a parent component** when the same element appears in multiple places:
   ```typescript
   // Good — scoped to the preview panel
   const preview = page.locator('app-assistant-preview');
   const previewTextarea = preview.locator('textarea#user-message');

   // Bad — matches both the main chat and the preview
   const textarea = page.locator('textarea#user-message');
   ```

2. **Use `.filter({ hasText: ... })` to narrow down lists** of similar elements:
   ```typescript
   const card = page.locator('.assistant-card').filter({ hasText: ASSISTANT_NAME });
   const sessionLink = page.locator('app-session-list a').filter({ hasText: 'test conversation' });
   ```

3. **Prefer role-based locators** (`getByRole`, `getByLabel`) over CSS selectors when possible. They're more resilient to markup changes and self-documenting:
   ```typescript
   page.getByRole('button', { name: 'Submit message' })
   page.getByRole('heading', { name: 'Manage Conversations' })
   page.getByRole('menuitem', { name: 'Delete' })
   page.getByRole('dialog', { name: 'Share Assistant' })
   page.getByRole('alertdialog').getByRole('button', { name: 'Delete' })
   ```

4. **Use `.first()` / `.nth(N)` deliberately**, not as a band-aid. If you need `.first()`, add a comment explaining why multiple matches are expected:
   ```typescript
   // Multiple session links exist; we want the most recent (first in the list)
   const sessionLink = page.locator('app-session-list a').first();
   ```

## Serial vs. Independent Tests

Use `test.describe.serial` when tests form a lifecycle that must run in order and share state (create → interact → delete). Use independent tests (the default) for everything else.

### Serial test rules

- Wrap the serial block in `test.describe.serial('Descriptive lifecycle name', () => { ... })`.
- For serial tests that must share a single browser context across all steps, use `beforeAll` / `afterAll` with a shared `page`:
  ```typescript
  test.describe.serial('Full assistant lifecycle', () => {
    let page: Page;
    test.beforeAll(async ({ browser }) => { page = await browser.newPage(); });
    test.afterAll(async () => { await page.close(); });
    // tests use `page` directly, not `{ page }` from the test args
  });
  ```
- For serial tests where each step can use a fresh context but must run in order (e.g., chat lifecycle where state is in the backend), use the normal `({ page })` fixture — Playwright handles context creation. Each test navigates to the page and picks up server-side state from the previous test.
- Always include a cleanup step as the final test in a serial block (delete the conversation, delete the assistant, etc.).
- Keep serial blocks focused. If some tests in a feature are independent, put them outside the serial block in the same `describe`:
  ```typescript
  test.describe('Chat (user)', () => {
    test.describe.serial('Chat lifecycle with Claude Haiku 4.5', () => {
      // create → interact → rename → delete
    });

    // Independent tests below — each gets its own context
    test('should close and reopen the sidebar', async ({ page }) => { ... });
  });
  ```

### Independent test rules

- Each test should be self-contained: navigate to the page, do the thing, assert.
- Don't assume any state from other tests.
- Use `test.skip(condition, reason)` to gracefully skip when preconditions aren't met (e.g., no sessions exist to select).

## Sending Chat Messages and Waiting for Responses

This is the most latency-sensitive flow. Follow this exact pattern:

```typescript
async function sendMessageAndWaitForResponse(
  page: Page,
  message: string,
): Promise<string> {
  const textarea = page.locator('textarea#user-message');
  await expect(textarea).toBeVisible({ timeout: 15_000 });
  await textarea.fill(message);

  await page.getByRole('button', { name: 'Submit message' }).click();

  // Wait for the assistant response to start streaming
  const assistantMessage = page.locator('app-assistant-message').last();
  await expect(assistantMessage).toBeVisible({ timeout: 60_000 });

  // Wait for streaming to finish (loader disappears)
  await expect(page.locator('app-pulsating-loader')).toBeHidden({ timeout: 200_000 });

  return (await assistantMessage.innerText()).trim();
}
```

Key points:
- Use `.last()` on `app-assistant-message` because previous messages already exist in multi-turn conversations.
- Wait for `app-pulsating-loader` to be hidden — this is the streaming indicator. The 200s timeout accounts for slow Bedrock responses.
- If the chat is inside a scoped container (e.g., assistant preview), scope all locators to that container.

## Route Mocking

Use `page.route()` to mock API responses when testing UI behavior that shouldn't depend on real backends (file uploads, error states, provider lists):

```typescript
// Mock a GET endpoint
await page.route('**/sessions*', (route) => {
  if (route.request().method() === 'GET') {
    return route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Database unavailable' }),
    });
  }
  return route.continue();
});

// Mock with a delay to test loading states
await page.route('**/auth/providers', async (route) => {
  await new Promise((r) => setTimeout(r, 2000));
  await route.fulfill({ json: { providers: [] } });
});
```

Rules:
- Always call `route.continue()` for methods you don't want to intercept.
- Call `page.unrouteAll()` at the end of tests that set up complex multi-route mocks to avoid leaking into other tests.
- For unauthenticated tests that hit pages behind auth guards, mock `**/system/status` to return `{ first_boot_completed: true }` so the guard redirects to login instead of a first-boot flow.

## Conditional Skipping

When a test depends on data that may not exist (e.g., sessions, models), use `test.skip()` rather than letting it fail:

```typescript
const checkbox = page.locator('input[type="checkbox"]').first();
const hasCheckbox = (await checkbox.count()) > 0;
test.skip(!hasCheckbox, 'No sessions available to select');
```

## Helper Functions

Extract repeated navigation and interaction patterns into helper functions at the top of the file. Common patterns:

```typescript
/** Navigate to a page and wait for its landmark element. */
async function goToAssistantsPage(page: Page) {
  await page.goto('/assistants');
  await expect(
    page.getByRole('heading', { name: 'Assistants' }),
  ).toBeVisible({ timeout: 15_000 });
}

/** Select a model from the dropdown. */
async function selectModel(page: Page, modelNameSubstring: string) {
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
```

Keep helpers in the same file unless they're shared across multiple spec files. If sharing is needed, create a `e2e/helpers/` directory.

## General Patterns

- **Import only from `@playwright/test`**: `import { test, expect } from '@playwright/test';` — optionally import `Page` type when using shared `page` in serial blocks.
- **Use `test.describe` for grouping**: One top-level `describe` per file, named after the feature and auth level (e.g., `'Chat (user)'`, `'Login Page'`).
- **Use `test.beforeEach` sparingly**: Only for setup that every test in the block needs (e.g., navigating to a page, setting up route mocks). Don't use it if only some tests need the setup.
- **Prefer `page.goto` in each test** over `beforeEach` navigation when tests go to different pages or need different setup.
- **Use `scrollIntoViewIfNeeded()`** before interacting with elements that may be below the fold.
- **Use `hover()` before `click()`** on elements that only appear on hover (e.g., context menu triggers on session list items).
- **Confirm destructive actions**: When testing delete flows, always interact with the confirmation dialog (`alertdialog` role) before asserting the item is gone.
- **Use numeric separators in timeouts**: Write `15_000` not `15000` for readability.
