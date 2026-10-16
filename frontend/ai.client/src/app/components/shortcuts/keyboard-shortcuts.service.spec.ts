// @vitest-environment jsdom
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FEATURES } from '../../services/features';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ToastService } from '../../services/toast/toast.service';
import { SearchDialogService } from '../search/search-dialog.service';
import { KeyboardShortcutsService, ShortcutContext } from './keyboard-shortcuts.service';
import { ShortcutTargetsService } from './shortcut-targets.service';
import { expectNamedDialog } from '../../../testing/cdk-dialog';

@Component({ selector: 'app-other-dialog', template: '<p>other</p>' })
class OtherDialog {}

const press = (init: KeyboardEventInit) =>
  new KeyboardEvent('keydown', { cancelable: true, bubbles: true, ...init });

const IN_APP: ShortcutContext = { chromeHidden: false };

describe('KeyboardShortcutsService', () => {
  let service: KeyboardShortcutsService;
  let navigate: ReturnType<typeof vi.fn>;
  let search: { isOpen: ReturnType<typeof vi.fn>; open: ReturnType<typeof vi.fn> };
  let toastSuccess: ReturnType<typeof vi.fn>;
  let sidenav: SidenavService;
  let targets: ShortcutTargetsService;

  function setUp(features: { conversationSearch: boolean } = { conversationSearch: true }): void {
    navigate = vi.fn().mockResolvedValue(true);
    search = { isOpen: vi.fn().mockReturnValue(false), open: vi.fn().mockResolvedValue(undefined) };
    toastSuccess = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        { provide: Router, useValue: { navigate } },
        { provide: SearchDialogService, useValue: search },
        { provide: ToastService, useValue: { success: toastSuccess } },
        { provide: FEATURES, useValue: features },
      ],
    });
    service = TestBed.inject(KeyboardShortcutsService);
    sidenav = TestBed.inject(SidenavService);
    targets = TestBed.inject(ShortcutTargetsService);
  }

  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    TestBed.resetTestingModule();
  });

  describe('with nothing in the way', () => {
    beforeEach(() => setUp());

    it('Cmd/Ctrl+K opens search', () => {
      const event = press({ key: 'k', metaKey: true });
      service.handle(event, IN_APP);
      expect(search.open).toHaveBeenCalledOnce();
      expect(event.defaultPrevented).toBe(true);
    });

    it('Cmd/Ctrl+Shift+O starts a new conversation and closes the mobile sidebar', () => {
      sidenav.open();
      const event = press({ key: 'O', ctrlKey: true, shiftKey: true });
      service.handle(event, IN_APP);
      expect(navigate).toHaveBeenCalledWith(['']);
      expect(sidenav.isOpen()).toBe(false);
      expect(event.defaultPrevented).toBe(true);
    });

    it('Shift+Esc focuses the composer', () => {
      const composer = document.createElement('textarea');
      document.body.appendChild(composer);
      const unregister = targets.registerComposer(() => composer);
      try {
        const event = press({ key: 'Escape', shiftKey: true });
        service.handle(event, IN_APP);
        expect(document.activeElement).toBe(composer);
        expect(event.defaultPrevented).toBe(true);
      } finally {
        unregister();
        composer.remove();
      }
    });

    it('Shift+Esc with no composer on the page leaves the key alone', () => {
      const event = press({ key: 'Escape', shiftKey: true });
      service.handle(event, IN_APP);
      expect(event.defaultPrevented).toBe(false);
    });

    it('Cmd/Ctrl+Shift+S collapses and expands the docked sidebar on desktop', () => {
      vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('min-width'), media: q }));
      try {
        service.handle(press({ key: 's', metaKey: true, shiftKey: true }), IN_APP);
        expect(sidenav.isCollapsed()).toBe(true);
        service.handle(press({ key: 's', metaKey: true, shiftKey: true }), IN_APP);
        expect(sidenav.isCollapsed()).toBe(false);
      } finally {
        vi.unstubAllGlobals();
      }
    });

    it('Cmd/Ctrl+Shift+S opens the overlay sidebar below the desktop breakpoint', () => {
      vi.stubGlobal('matchMedia', (q: string) => ({ matches: false, media: q }));
      try {
        service.handle(press({ key: 's', ctrlKey: true, shiftKey: true }), IN_APP);
        expect(sidenav.isOpen()).toBe(true);
        expect(sidenav.isCollapsed()).toBe(false);
      } finally {
        vi.unstubAllGlobals();
      }
    });

    it('Cmd/Ctrl+Shift+C copies the newest response and says so', async () => {
      const older = { canCopy: () => true, copy: vi.fn().mockResolvedValue(true) };
      const newest = { canCopy: () => true, copy: vi.fn().mockResolvedValue(true) };
      const unregisterOlder = targets.registerLatestResponse(older);
      const unregisterNewest = targets.registerLatestResponse(newest);
      try {
        const event = press({ key: 'c', metaKey: true, shiftKey: true });
        service.handle(event, IN_APP);
        expect(event.defaultPrevented).toBe(true);
        await vi.waitFor(() => expect(toastSuccess).toHaveBeenCalledOnce());
        expect(newest.copy).toHaveBeenCalledOnce();
        expect(older.copy).not.toHaveBeenCalled();
      } finally {
        unregisterOlder();
        unregisterNewest();
      }
    });

    it('Cmd/Ctrl+Shift+C with nothing to copy leaves the key to the browser', () => {
      const unregister = targets.registerLatestResponse({ canCopy: () => false, copy: vi.fn() });
      try {
        const event = press({ key: 'c', metaKey: true, shiftKey: true });
        service.handle(event, IN_APP);
        expect(event.defaultPrevented).toBe(false);
      } finally {
        unregister();
      }
    });

    it('Cmd/Ctrl+Shift+C stays quiet when the clipboard refuses', async () => {
      const copy = vi.fn().mockResolvedValue(false);
      const unregister = targets.registerLatestResponse({ canCopy: () => true, copy });
      try {
        service.handle(press({ key: 'c', metaKey: true, shiftKey: true }), IN_APP);
        await vi.waitFor(() => expect(copy).toHaveBeenCalled());
        await Promise.resolve();
        expect(toastSuccess).not.toHaveBeenCalled();
      } finally {
        unregister();
      }
    });

    it('Cmd/Ctrl+/ opens the shortcuts sheet, and pressing it again closes it', async () => {
      service.handle(press({ key: '/', metaKey: true }), IN_APP);
      await vi.waitFor(() => expect(TestBed.inject(Dialog).openDialogs.length).toBe(1));
      // The shell names the container a tick after it mounts.
      await new Promise(resolve => setTimeout(resolve, 0));
      const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
      expectNamedDialog(containers[containers.length - 1], { name: 'Keyboard shortcuts' });

      const again = press({ key: '/', metaKey: true });
      service.handle(again, IN_APP);
      expect(again.defaultPrevented).toBe(true);
      await vi.waitFor(() => expect(TestBed.inject(Dialog).openDialogs.length).toBe(0));
    });

    it('opening the sheet twice never stacks a second one', async () => {
      await Promise.all([service.openHelp(), service.openHelp()]);
      expect(TestBed.inject(Dialog).openDialogs.length).toBe(1);
    });

    it('does nothing for a key another component already handled', () => {
      const event = press({ key: 'o', metaKey: true, shiftKey: true });
      event.preventDefault();
      service.handle(event, IN_APP);
      expect(navigate).not.toHaveBeenCalled();
    });

    it('ignores keys that are not shortcuts', () => {
      const event = press({ key: 'o', metaKey: true });
      service.handle(event, IN_APP);
      expect(event.defaultPrevented).toBe(false);
      expect(navigate).not.toHaveBeenCalled();
    });

    it('leaves Cmd/Ctrl+K to a rich-text editor', () => {
      const editor = document.createElement('div');
      editor.setAttribute('contenteditable', 'true');
      document.body.appendChild(editor);
      try {
        const event = press({ key: 'k', metaKey: true });
        editor.dispatchEvent(event);
        service.handle(event, IN_APP);
        expect(search.open).not.toHaveBeenCalled();
        expect(event.defaultPrevented).toBe(false);
      } finally {
        editor.remove();
      }
    });
  });

  describe('while a dialog is open', () => {
    beforeEach(() => setUp());

    it('stays out of the way', () => {
      TestBed.inject(Dialog).open(OtherDialog);
      for (const init of [
        { key: 'k', metaKey: true },
        { key: 'o', metaKey: true, shiftKey: true },
        { key: 's', metaKey: true, shiftKey: true },
        { key: '/', metaKey: true },
      ]) {
        const event = press(init);
        service.handle(event, IN_APP);
        expect(event.defaultPrevented, init.key).toBe(false);
      }
      expect(search.open).not.toHaveBeenCalled();
      expect(navigate).not.toHaveBeenCalled();
      expect(sidenav.isCollapsed()).toBe(false);
    });

    it('a second Cmd/Ctrl+K refocuses search when search is the dialog', () => {
      search.isOpen.mockReturnValue(true);
      TestBed.inject(Dialog).open(OtherDialog);
      const event = press({ key: 'k', ctrlKey: true });
      service.handle(event, IN_APP);
      expect(search.open).toHaveBeenCalledOnce();
      expect(event.defaultPrevented).toBe(true);
    });
  });

  describe('on a page without the app chrome (login, a shared artifact)', () => {
    beforeEach(() => setUp());

    it('offers no new conversation, sidebar or shortcuts sheet', () => {
      const hidden: ShortcutContext = { chromeHidden: true };
      for (const init of [
        { key: 'o', metaKey: true, shiftKey: true },
        { key: 's', metaKey: true, shiftKey: true },
        { key: '/', metaKey: true },
      ]) {
        const event = press(init);
        service.handle(event, hidden);
        expect(event.defaultPrevented, init.key).toBe(false);
      }
      expect(navigate).not.toHaveBeenCalled();
      expect(TestBed.inject(Dialog).openDialogs.length).toBe(0);
    });
  });

  describe('with conversation search off', () => {
    beforeEach(() => setUp({ conversationSearch: false }));

    it('leaves Cmd/Ctrl+K to the browser', () => {
      const event = press({ key: 'k', metaKey: true });
      service.handle(event, IN_APP);
      expect(search.open).not.toHaveBeenCalled();
      expect(event.defaultPrevented).toBe(false);
    });
  });
});
