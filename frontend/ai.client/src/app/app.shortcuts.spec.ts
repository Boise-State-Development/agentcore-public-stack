// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

/**
 * The shell forwards every document keydown to the shortcut service, with
 * whether the page hides the app chrome. The shortcut rules themselves are
 * pinned in `components/shortcuts/keyboard-shortcuts.service.spec.ts`.
 */
const IMPORT_TIMEOUT_MS = 30_000;

describe('App: keyboard shortcuts', () => {
  it(
    'hands each key press to the shortcut service with the chrome state',
    async () => {
      const { App } = await import('./app');
      const handle = vi.fn();
      const event = new KeyboardEvent('keydown', { key: 'o', metaKey: true, shiftKey: true });

      App.prototype.onDocumentKeydown.call({ shortcuts: { handle }, chromeHidden: () => true }, event);
      App.prototype.onDocumentKeydown.call({ shortcuts: { handle }, chromeHidden: () => false }, event);

      expect(handle).toHaveBeenNthCalledWith(1, event, { chromeHidden: true });
      expect(handle).toHaveBeenNthCalledWith(2, event, { chromeHidden: false });
    },
    IMPORT_TIMEOUT_MS,
  );
});
