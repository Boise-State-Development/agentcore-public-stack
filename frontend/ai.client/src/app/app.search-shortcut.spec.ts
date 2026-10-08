// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';

/**
 * Cmd/Ctrl+K on the app shell (conversation-search §6), driven through the
 * handler with stand-ins for what it reads, so the rules are pinned without
 * rendering the whole shell.
 */
const IMPORT_TIMEOUT_MS = 30_000;

function shell(opts: { enabled?: boolean; searchOpen?: boolean; otherDialogs?: number } = {}) {
  const searchDialog = { isOpen: vi.fn(() => opts.searchOpen ?? false), open: vi.fn().mockResolvedValue(undefined) };
  return {
    conversationSearchOn: opts.enabled ?? true,
    searchDialog,
    dialog: { openDialogs: Array.from({ length: opts.otherDialogs ?? 0 }) },
  };
}

function press(target: EventTarget, init: KeyboardEventInit = { key: 'k', metaKey: true }): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
  Object.defineProperty(event, 'target', { value: target });
  return event;
}

describe('App: Cmd/Ctrl+K', () => {
  it(
    'opens search from the page',
    async () => {
      const { App } = await import('./app');
      const self = shell();
      const event = press(document.body);
      App.prototype.onDocumentKeydown.call(self, event);
      expect(event.defaultPrevented).toBe(true);
      expect(self.searchDialog.open).toHaveBeenCalledWith();
    },
    IMPORT_TIMEOUT_MS,
  );

  it(
    'does nothing with the flag off, or for another key',
    async () => {
      const { App } = await import('./app');
      const off = shell({ enabled: false });
      App.prototype.onDocumentKeydown.call(off, press(document.body));
      const on = shell();
      App.prototype.onDocumentKeydown.call(on, press(document.body, { key: 'j', metaKey: true }));
      expect(off.searchDialog.open).not.toHaveBeenCalled();
      expect(on.searchDialog.open).not.toHaveBeenCalled();
    },
    IMPORT_TIMEOUT_MS,
  );

  it(
    'a second press while open refocuses it',
    async () => {
      const { App } = await import('./app');
      const self = shell({ searchOpen: true, otherDialogs: 1 });
      const event = press(document.createElement('input'));
      App.prototype.onDocumentKeydown.call(self, event);
      expect(event.defaultPrevented).toBe(true);
      expect(self.searchDialog.open).toHaveBeenCalledWith();
    },
    IMPORT_TIMEOUT_MS,
  );

  it(
    'stays out of the way while another dialog is open, and inside a rich-text editor',
    async () => {
      const { App } = await import('./app');
      const withDialog = shell({ otherDialogs: 1 });
      App.prototype.onDocumentKeydown.call(withDialog, press(document.body));
      const inEditor = shell();
      const editor = document.createElement('div');
      editor.setAttribute('contenteditable', 'true');
      const event = press(editor);
      App.prototype.onDocumentKeydown.call(inEditor, event);
      expect(withDialog.searchDialog.open).not.toHaveBeenCalled();
      expect(inEditor.searchDialog.open).not.toHaveBeenCalled();
      expect(event.defaultPrevented).toBe(false);
    },
    IMPORT_TIMEOUT_MS,
  );

  it(
    'opens from the composer, which has focus on every conversation page',
    async () => {
      const { App } = await import('./app');
      const composer = document.createElement('textarea');
      composer.value = 'draft';
      const self = shell();
      App.prototype.onDocumentKeydown.call(self, press(composer));
      expect(self.searchDialog.open).toHaveBeenCalledWith();
    },
    IMPORT_TIMEOUT_MS,
  );
});
