import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
} from '@angular/core';
import { Clipboard } from '@angular/cdk/clipboard';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroGlobeAlt } from '@ng-icons/heroicons/outline';

import { ToastService } from '../../../../../services/toast/toast.service';
import {
  BROWSER_CARD_ACTION_CLASS,
  BrowserCardComponent,
  BrowserCardMenuItem,
} from './browser-card.component';
import type { BrowserPageSummary } from './browser-page';

/**
 * The page a run of `browse_web` calls left the browser on, shown after the
 * tool rail that produced it.
 *
 * "Open" opens the page itself in a new tab. It does not open a live view:
 * the agent's browser is only streamable while the user holds it (the
 * sign-in takeover), because app-api can resolve a conversation's browser
 * session only from the projection written at takeover.
 */
@Component({
  selector: 'app-browser-page-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, BrowserCardComponent],
  providers: [provideIcons({ heroGlobeAlt })],
  host: { class: 'block' },
  template: `
    <app-browser-card
      [title]="title()"
      [host]="page().host"
      [status]="status()"
      [thumbnail]="page().screenshot"
      [live]="page().browsing"
      [menuItems]="menuItems"
      (menuSelect)="onMenu($event)"
    >
      <div
        browserCardPreview
        class="flex h-full flex-col gap-2 px-4 pt-4"
        aria-hidden="true"
      >
        <span class="flex items-center gap-2 text-gray-400 dark:text-gray-500">
          <ng-icon name="heroGlobeAlt" size="16" />
          <span class="h-2 w-1/2 rounded-full bg-gray-200 dark:bg-white/10"></span>
        </span>
        <span class="h-2 w-5/6 rounded-full bg-gray-100 dark:bg-white/5"></span>
        <span class="h-2 w-2/3 rounded-full bg-gray-100 dark:bg-white/5"></span>
        <span class="h-2 w-3/4 rounded-full bg-gray-100 dark:bg-white/5"></span>
      </div>

      <a
        browserCardAction
        [class]="actionClass"
        [href]="page().url"
        target="_blank"
        rel="noopener noreferrer"
        [attr.aria-label]="'Open ' + title() + ' in a new tab'"
      >
        Open
      </a>
    </app-browser-card>
  `,
})
export class BrowserPageCardComponent {
  readonly page = input.required<BrowserPageSummary>();

  private readonly clipboard = inject(Clipboard);
  private readonly toast = inject(ToastService);

  protected readonly actionClass = BROWSER_CARD_ACTION_CLASS;

  protected readonly menuItems: BrowserCardMenuItem[] = [
    { id: 'copy', label: 'Copy link' },
  ];

  protected readonly title = computed(() => this.page().title || this.page().host);

  protected readonly status = computed(() => {
    const page = this.page();
    if (page.browsing) return 'Browsing';
    if (page.closed) return 'Browser closed';
    return 'Opened in browser';
  });

  protected onMenu(id: string): void {
    if (id !== 'copy') return;
    if (this.clipboard.copy(this.page().url)) {
      this.toast.success('Link copied');
    }
  }
}
