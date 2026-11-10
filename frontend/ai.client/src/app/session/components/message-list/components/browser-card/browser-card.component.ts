import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from '@angular/core';

import {
  CONTENT_CARD_ACTION_CLASS,
  ContentCardComponent,
  type ContentCardMenuItem,
} from '../../../../../components/content-card/content-card.component';

/** One entry in the card's overflow menu. */
export type BrowserCardMenuItem = ContentCardMenuItem;

/**
 * The card a browser surface shows in the conversation: a framed preview of
 * the page on top, then its title, `host · status`, a primary action and an
 * overflow menu. Drawn with the shared content card, which the attachment
 * card also uses.
 *
 * Presentational only. Two things use it — the sign-in prompt, whose primary
 * action opens the docked viewer, and the `browse_web` page card, whose
 * primary action opens the page itself — so the action is projected
 * (`[browserCardAction]`) rather than modelled here, and the preview body is
 * projected (`[browserCardPreview]`) for when there is no screenshot to show.
 *
 * The miniature window chrome and the screenshot are decoration and hidden
 * from assistive tech; a projected preview body is not, since the sign-in
 * card puts the agent's reason there.
 */
@Component({
  selector: 'app-browser-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ContentCardComponent],
  host: { class: 'block w-full max-w-md' },
  template: `
    <app-content-card
      [title]="title()"
      [meta]="host()"
      [status]="status()"
      [live]="live()"
      [menuItems]="menuItems()"
      (menuSelect)="menuSelect.emit($event)"
    >
      <div contentCardChrome class="flex min-w-0 flex-1 items-center gap-1" aria-hidden="true">
        <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
        <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
        <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
        <span
          class="ml-2 min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-500 dark:bg-white/5 dark:text-gray-400"
          >{{ host() }}</span
        >
      </div>
      <div contentCardPreview class="h-full">
        @if (thumbnail(); as src) {
          <img class="w-full object-cover object-top" [src]="src" alt="" />
        } @else {
          <ng-content select="[browserCardPreview]" />
        }
      </div>
      <div contentCardAction class="contents">
        <ng-content select="[browserCardAction]" />
      </div>
    </app-content-card>
  `,
})
export class BrowserCardComponent {
  readonly title = input.required<string>();
  readonly host = input.required<string>();
  readonly status = input<string>('');
  /** A screenshot of the page, as a `data:image/...` URL. */
  readonly thumbnail = input<string | null>(null);
  /** Something is happening now — the agent is browsing, or waiting on the user. */
  readonly live = input(false);
  readonly menuItems = input<BrowserCardMenuItem[]>([]);

  readonly menuSelect = output<string>();
}

/** Shared look for a card's primary action, so both consumers match. */
export const BROWSER_CARD_ACTION_CLASS = CONTENT_CARD_ACTION_CLASS;
