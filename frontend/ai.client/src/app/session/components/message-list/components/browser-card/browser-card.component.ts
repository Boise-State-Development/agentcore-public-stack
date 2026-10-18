import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from '@angular/core';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroEllipsisVertical } from '@ng-icons/heroicons/outline';

/** One entry in the card's overflow menu. */
export interface BrowserCardMenuItem {
  id: string;
  label: string;
  /** Destructive or declining action, drawn in the danger colour. */
  danger?: boolean;
}

/**
 * The card a browser surface shows in the conversation: a framed preview of
 * the page on top, then its title, `host · status`, a primary action and an
 * overflow menu.
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
  imports: [CdkMenu, CdkMenuItem, CdkMenuTrigger, NgIcon],
  providers: [provideIcons({ heroEllipsisVertical })],
  host: { class: 'block w-full max-w-md' },
  template: `
    <article
      class="overflow-hidden rounded-2xl bg-white ring-1 ring-gray-200 dark:bg-gray-900 dark:ring-white/10"
      [attr.aria-label]="title()"
    >
      <div class="h-36 overflow-hidden bg-gray-100 px-6 pt-5 dark:bg-gray-800/70">
        <div
          class="flex h-full flex-col overflow-hidden rounded-t-lg bg-white shadow-sm ring-1 ring-gray-900/5 dark:bg-gray-950 dark:ring-white/10"
        >
          <div
            class="flex shrink-0 items-center gap-1 border-b border-gray-100 px-2.5 py-1.5 dark:border-white/5"
            aria-hidden="true"
          >
            <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
            <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
            <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
            <span
              class="ml-2 min-w-0 flex-1 truncate rounded-full bg-gray-100 px-2 text-[10px]/4 text-gray-500 dark:bg-white/5 dark:text-gray-400"
              >{{ host() }}</span
            >
          </div>
          <div class="relative min-h-0 flex-1 overflow-hidden">
            @if (thumbnail(); as src) {
              <img
                class="w-full object-cover object-top"
                [src]="src"
                alt=""
              />
            } @else {
              <ng-content select="[browserCardPreview]" />
            }
          </div>
        </div>
      </div>

      <div
        class="flex items-center gap-3 border-t border-gray-200 px-4 py-3 dark:border-white/10"
      >
        <div class="min-w-0 flex-1">
          <p
            class="truncate text-sm/6 font-semibold text-gray-900 dark:text-gray-100"
            [title]="title()"
          >
            {{ title() }}
          </p>
          <p
            class="flex min-w-0 items-center gap-1.5 text-xs/5 text-gray-500 dark:text-gray-400"
          >
            @if (live()) {
              <span
                class="size-1.5 shrink-0 rounded-full bg-state-warning-500 motion-safe:animate-pulse"
                aria-hidden="true"
              ></span>
            }
            <span class="truncate">
              {{ host() }}@if (status()) {
                <span aria-hidden="true"> · </span
                ><span class="sr-only">, </span>{{ status() }}
              }
            </span>
          </p>
        </div>

        <ng-content select="[browserCardAction]" />

        @if (menuItems().length > 0) {
          <button
            type="button"
            class="flex size-8 shrink-0 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
            [attr.aria-label]="'More actions for ' + title()"
            [cdkMenuTriggerFor]="menu"
            [cdkMenuPosition]="menuPositions"
          >
            <ng-icon name="heroEllipsisVertical" size="18" />
          </button>
        }
      </div>
    </article>

    <ng-template #menu>
      <div
        cdkMenu
        class="min-w-44 rounded-xl bg-white p-1 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
      >
        @for (item of menuItems(); track item.id) {
          <button
            cdkMenuItem
            type="button"
            class="flex w-full items-center rounded-lg px-3 py-1.5 text-left text-sm/6 outline-hidden hover:bg-gray-100 focus:bg-gray-100 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
            [class]="
              item.danger
                ? 'text-state-danger-600 dark:text-state-danger-400'
                : 'text-gray-700 dark:text-gray-200'
            "
            (cdkMenuItemTriggered)="menuSelect.emit(item.id)"
          >
            {{ item.label }}
          </button>
        }
      </div>
    </ng-template>
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

  protected readonly menuPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'end', overlayY: 'top', offsetY: 4 },
    { originX: 'end', originY: 'top', overlayX: 'end', overlayY: 'bottom', offsetY: -4 },
  ];
}

/** Shared look for a card's primary action, so both consumers match. */
export const BROWSER_CARD_ACTION_CLASS =
  'inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1 text-sm/6 font-medium text-gray-900 no-underline! ring-1 ring-gray-300 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-100 dark:ring-white/20 dark:hover:bg-white/10';
