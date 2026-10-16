// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ShortcutTargetsService } from './shortcut-targets.service';

function field(): HTMLTextAreaElement {
  const el = document.createElement('textarea');
  document.body.appendChild(el);
  return el;
}

describe('ShortcutTargetsService', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  describe('the composer', () => {
    it('focuses the newest one', () => {
      const targets = new ShortcutTargetsService();
      const page = field();
      const preview = field();
      targets.registerComposer(() => page);
      targets.registerComposer(() => preview);

      expect(targets.focusComposer()).toBe(true);
      expect(document.activeElement).toBe(preview);
    });

    it('skips one that is gone or out of reach, and falls back to the one before', () => {
      const targets = new ShortcutTargetsService();
      const page = field();
      const collapsed = field();
      const wrapper = document.createElement('div');
      wrapper.setAttribute('inert', '');
      wrapper.appendChild(collapsed);
      document.body.appendChild(wrapper);
      targets.registerComposer(() => page);
      targets.registerComposer(() => collapsed);
      targets.registerComposer(() => document.createElement('textarea'));
      targets.registerComposer(() => undefined);

      expect(targets.focusComposer()).toBe(true);
      expect(document.activeElement).toBe(page);
    });

    it('reports when there is none', () => {
      const targets = new ShortcutTargetsService();
      const unregister = targets.registerComposer(() => field());
      unregister();
      expect(targets.focusComposer()).toBe(false);
    });
  });

  describe('the latest response', () => {
    it('copies the newest registered one', async () => {
      const targets = new ShortcutTargetsService();
      const older = { canCopy: () => true, copy: vi.fn().mockResolvedValue(true) };
      const newest = { canCopy: () => true, copy: vi.fn().mockResolvedValue(true) };
      targets.registerLatestResponse(older);
      targets.registerLatestResponse(newest);

      expect(await targets.copyLatestResponse()).toBe(true);
      expect(newest.copy).toHaveBeenCalledOnce();
      expect(older.copy).not.toHaveBeenCalled();
    });

    it('hands back to the previous one when the newest unregisters', async () => {
      const targets = new ShortcutTargetsService();
      const older = { canCopy: () => true, copy: vi.fn().mockResolvedValue(true) };
      targets.registerLatestResponse(older);
      const unregister = targets.registerLatestResponse({ canCopy: () => false, copy: vi.fn() });

      expect(targets.canCopyLatestResponse()).toBe(false);
      unregister();
      expect(targets.canCopyLatestResponse()).toBe(true);
      await targets.copyLatestResponse();
      expect(older.copy).toHaveBeenCalledOnce();
    });

    it('has nothing to copy before any response exists', async () => {
      const targets = new ShortcutTargetsService();
      expect(targets.canCopyLatestResponse()).toBe(false);
      expect(await targets.copyLatestResponse()).toBe(false);
    });
  });
});
