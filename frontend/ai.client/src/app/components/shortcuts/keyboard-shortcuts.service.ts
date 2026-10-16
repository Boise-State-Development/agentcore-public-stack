import { Injectable, inject } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog, DialogRef } from '@angular/cdk/dialog';
import { FEATURES } from '../../services/features';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ToastService } from '../../services/toast/toast.service';
import { SearchDialogService } from '../search/search-dialog.service';
import { ShortcutTargetsService } from './shortcut-targets.service';
import { ShortcutId, shortcutFor, targetKeepsShortcut } from './shortcuts';

/** What the shell knows that decides whether a shortcut applies. */
export interface ShortcutContext {
  /** The page hides the app chrome (login, a shared artifact): no sidebar, no conversation. */
  chromeHidden: boolean;
}

/** Tailwind's `lg`, where the sidebar is docked and collapses instead of overlaying. */
const DESKTOP_QUERY = '(min-width: 64rem)';

/**
 * Runs the global shortcuts in `shortcuts.ts`. The shell forwards every
 * document keydown to `handle`.
 *
 * A shortcut that has nothing to act on (no response to copy, no composer on
 * the page) leaves the event alone, so the browser's own binding for that key
 * still works there.
 */
@Injectable({ providedIn: 'root' })
export class KeyboardShortcutsService {
  private readonly router = inject(Router);
  private readonly dialog = inject(Dialog);
  private readonly sidenav = inject(SidenavService);
  private readonly toast = inject(ToastService);
  private readonly search = inject(SearchDialogService);
  private readonly targets = inject(ShortcutTargetsService);
  private readonly searchOn = inject(FEATURES).conversationSearch;

  private helpRef: DialogRef<unknown> | null = null;
  private helpOpening: Promise<void> | null = null;

  handle(event: KeyboardEvent, context: ShortcutContext): void {
    const s = shortcutFor(event);
    if (!s) return;

    // The two shortcuts that own a dialog still answer while it is up: a
    // second Cmd/Ctrl+K refocuses search, a second Cmd/Ctrl+/ closes the sheet.
    if (s.id === 'search' && this.searchOn && this.search.isOpen()) {
      event.preventDefault();
      void this.search.open();
      return;
    }
    if (s.id === 'help' && this.helpIsOpen()) {
      event.preventDefault();
      this.helpRef?.close();
      return;
    }

    if (this.dialog.openDialogs.length > 0) return;
    // A component already acted on the key (the composer closing its `@`
    // menu on Escape, say); one key press does one thing.
    if (event.defaultPrevented) return;
    if (targetKeepsShortcut(event.target, s)) return;

    if (this.run(s.id, context)) event.preventDefault();
  }

  /** Opens the shortcuts sheet, or does nothing if it is already up. */
  async openHelp(): Promise<void> {
    if (this.helpIsOpen()) return this.helpOpening ?? undefined;
    this.helpOpening = (async () => {
      try {
        const { ShortcutsDialogComponent } = await import('./shortcuts-dialog.component');
        const ref = this.dialog.open<unknown, void, unknown>(ShortcutsDialogComponent, {
          ariaModal: true,
        });
        this.helpRef = ref;
        ref.closed.subscribe(() => {
          if (this.helpRef === ref) this.helpRef = null;
        });
      } finally {
        this.helpOpening = null;
      }
    })();
    return this.helpOpening;
  }

  private helpIsOpen(): boolean {
    return this.helpRef !== null || this.helpOpening !== null;
  }

  /** Does what `id` means here; false when there is nothing for it to do. */
  private run(id: ShortcutId, context: ShortcutContext): boolean {
    switch (id) {
      case 'search':
        if (!this.searchOn) return false;
        void this.search.open();
        return true;

      case 'newConversation':
        if (context.chromeHidden) return false;
        this.sidenav.close();
        void this.router.navigate(['']);
        return true;

      case 'focusComposer':
        return this.targets.focusComposer();

      case 'toggleSidebar':
        if (context.chromeHidden) return false;
        if (isDesktop()) this.sidenav.toggleCollapsed();
        else this.sidenav.toggle();
        return true;

      case 'copyLastResponse':
        if (!this.targets.canCopyLatestResponse()) return false;
        void this.targets.copyLatestResponse().then(copied => {
          // The copy button may be scrolled out of view, so say it somewhere
          // that is on screen (and announced).
          if (copied) this.toast.success('Copied the last response', undefined, { duration: 2000 });
        });
        return true;

      case 'help':
        if (context.chromeHidden) return false;
        void this.openHelp();
        return true;
    }
  }
}

function isDesktop(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(DESKTOP_QUERY).matches
    : true;
}
