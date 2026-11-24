import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Router } from '@angular/router';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { FEATURES } from '../../services/features';
import { SearchDialogService } from '../search/search-dialog.service';
import { shortcutAria, withShortcut } from '../shortcuts/shortcuts';
import { TooltipDirective } from '../tooltip/tooltip.directive';

/**
 * The controls that stand in for the sidebar while it is out of view: open
 * it, start a new chat, search.
 *
 * The shell draws this once, fixed to the top-left corner, on a phone (where
 * the sidebar is an overlay) and on desktop while the sidebar is collapsed.
 * It is the same capsule on every page, so nothing moves or changes shape
 * between an empty chat, a conversation and a list page.
 *
 * The conversation header does not draw these buttons. It leaves room for the
 * capsule with `--shell-controls-reserve` (`styles.css`), which is computed
 * from the same button count, size, gap and padding used below. Change one
 * and change the other.
 *
 * Search joins from `sm` up. On a phone the room goes to the conversation
 * title, and search is one tap away at the top of the sidebar.
 */
@Component({
  selector: 'app-shell-controls',
  imports: [TooltipDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    role: 'group',
    'aria-label': 'Sidebar and quick actions',
    class:
      'shell-controls flex items-center gap-0.5 rounded-xl bg-white/90 p-1 shadow-sm ring-1 ring-gray-950/10 backdrop-blur-sm dark:bg-gray-800/90 dark:ring-white/10',
  },
  styles: `
    /* Arrives with a short fade each time it is shown (the sidebar collapsing
       or its phone overlay closing), so it does not pop in over the sidebar
       while that is still sliding away. */
    :host {
      animation: shell-controls-enter 200ms ease-out 120ms both;
    }

    @keyframes shell-controls-enter {
      from {
        opacity: 0;
      }
      to {
        opacity: 1;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      :host {
        animation: none;
      }
    }
  `,
  template: `
    <!-- Phone and tablet: the sidebar is an overlay. -->
    <button
      type="button"
      (click)="sidenavService.open()"
      [class]="iconButton"
      class="lg:hidden"
      aria-label="Open sidebar"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="size-5.5" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2" ry="2" />
        <line x1="9" y1="4" x2="9" y2="20" />
      </svg>
    </button>

    <!-- Desktop: the sidebar is collapsed. -->
    <button
      type="button"
      (click)="sidenavService.expand()"
      [appTooltip]="expandTooltip"
      appTooltipPosition="bottom"
      [class]="iconButton"
      class="max-lg:hidden"
      aria-label="Expand sidebar"
      [attr.aria-keyshortcuts]="sidebarShortcutAria"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="size-5.5" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2" ry="2" />
        <line x1="9" y1="4" x2="9" y2="20" />
      </svg>
    </button>

    <button
      type="button"
      (click)="newChat()"
      [appTooltip]="newChatTooltip"
      appTooltipPosition="bottom"
      class="mx-0.5 flex size-8 items-center justify-center rounded-full bg-secondary-500 text-white hover:bg-secondary-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500"
      aria-label="New chat"
      [attr.aria-keyshortcuts]="newChatShortcutAria"
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="size-5" aria-hidden="true">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
      </svg>
    </button>

    @if (searchOn) {
      <button
        type="button"
        (click)="openSearch()"
        [appTooltip]="searchTooltip"
        appTooltipPosition="bottom"
        [class]="iconButton"
        class="max-sm:hidden"
        aria-label="Search"
        [attr.aria-keyshortcuts]="searchShortcutAria"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" class="size-5.5" aria-hidden="true">
          <path stroke-linecap="round" stroke-linejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
        </svg>
      </button>
    }
  `,
})
export class ShellControlsComponent {
  protected readonly sidenavService = inject(SidenavService);
  private readonly router = inject(Router);
  private readonly searchDialog = inject(SearchDialogService);

  /** Compile-time flag, so the button never pops in after a request. */
  protected readonly searchOn = inject(FEATURES).conversationSearch;

  protected readonly expandTooltip = withShortcut('Expand sidebar', 'toggleSidebar');
  protected readonly sidebarShortcutAria = shortcutAria('toggleSidebar');
  protected readonly newChatTooltip = withShortcut('New chat', 'newConversation');
  protected readonly newChatShortcutAria = shortcutAria('newConversation');
  protected readonly searchTooltip = withShortcut('Search', 'search');
  protected readonly searchShortcutAria = shortcutAria('search');

  /** The quiet icon buttons either side of New chat. 2.25rem square. */
  protected readonly iconButton =
    'flex size-9 items-center justify-center rounded-lg text-gray-600 hover:bg-gray-950/5 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500 dark:text-gray-300 dark:hover:bg-white/10 dark:hover:text-white';

  protected newChat(): void {
    void this.router.navigate(['']);
  }

  protected openSearch(): void {
    void this.searchDialog.open();
  }
}
