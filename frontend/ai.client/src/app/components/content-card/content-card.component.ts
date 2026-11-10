import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroEllipsisVertical } from '@ng-icons/heroicons/outline';

/** One entry in a content card's overflow menu. */
export interface ContentCardMenuItem {
  id: string;
  label: string;
  /** Destructive or declining action, drawn in the danger colour. */
  danger?: boolean;
}

/** `md` is the conversation size; `sm` is the composer's, which has no chrome row. */
export type ContentCardSize = 'md' | 'sm';

/**
 * The card shape shared by everything the conversation shows as "a thing you
 * can open": a gray tray holding a framed sheet (the preview), then a footer
 * with the title, a `meta · status` line, a primary action and an overflow
 * menu. The browser card and the attachment card are both drawn with it, so
 * a page, a file and later an artifact read as one family.
 *
 * Presentational only. Slots:
 * - `[contentCardChrome]`: the sheet's top row (window dots and host for a
 *   page, the type icon and a locator for a file). Only drawn at `md`.
 * - `[contentCardPreview]`: the sheet's body.
 * - `[contentCardCorner]`: a control pinned to the tray's top-right corner,
 *   such as the composer's remove button.
 * - `[contentCardAction]`: the footer's primary action.
 *
 * With `bleed`, the preview fills the tray edge to edge with no sheet, for
 * images. `progress` draws a bar along the tray's bottom edge, above the
 * sheet. Every layer is stacked inside the card (`isolate`), so nothing in it
 * can rise above a sticky bar or a neighbouring card.
 */
@Component({
  selector: 'app-content-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenu, CdkMenuItem, CdkMenuTrigger, NgIcon, NgTemplateOutlet],
  providers: [provideIcons({ heroEllipsisVertical })],
  host: { class: 'block' },
  template: `
    <article
      class="relative isolate flex h-full flex-col overflow-hidden rounded-2xl bg-white dark:bg-gray-900"
      [class]="frameClass()"
      [attr.aria-label]="label() || title()"
    >
      <div class="relative shrink-0 overflow-hidden" [class]="trayClass()">
        @if (bleed()) {
          <div class="absolute inset-0">
            <ng-container [ngTemplateOutlet]="previewSlot" />
          </div>
        } @else {
          <div
            class="relative z-[1] flex h-full flex-col overflow-hidden rounded-t-lg bg-white shadow-sm ring-1 ring-gray-900/5 dark:bg-gray-950 dark:ring-white/10"
          >
            @if (size() === 'md') {
              <div
                class="flex shrink-0 items-center gap-1 border-b border-gray-100 px-2.5 py-1.5 dark:border-white/5"
              >
                <ng-content select="[contentCardChrome]" />
              </div>
            }
            <div class="relative min-h-0 flex-1 overflow-hidden">
              <ng-container [ngTemplateOutlet]="previewSlot" />
            </div>
          </div>
        }

        @if (progress() !== null) {
          <div
            class="absolute inset-x-0 bottom-0 z-[2] h-0.5 bg-gray-200 dark:bg-gray-700"
            aria-hidden="true"
            data-testid="content-card-progress"
          >
            <div
              class="h-full bg-primary-500 transition-[width] duration-200"
              [style.width.%]="progress()"
            ></div>
          </div>
        }

        <div class="absolute right-2 top-2 z-[3] empty:hidden">
          <ng-content select="[contentCardCorner]" />
        </div>
      </div>

      <div
        class="flex min-w-0 flex-1 items-center border-t"
        [class]="footClass()"
      >
        <div class="min-w-0 flex-1">
          <p
            class="truncate font-semibold text-gray-900 dark:text-gray-100"
            [class]="size() === 'md' ? 'text-sm/6' : 'text-[13px]/5'"
            [title]="title()"
          >
            {{ title() }}
          </p>
          <p
            class="flex min-w-0 items-center gap-1.5"
            [class]="metaClass()"
          >
            @if (live()) {
              <span
                class="size-1.5 shrink-0 rounded-full bg-state-warning-500 motion-safe:animate-pulse"
                aria-hidden="true"
              ></span>
            }
            <span class="truncate">
              {{ meta() }}@if (status()) {
                @if (meta()) {
                  <span aria-hidden="true"> · </span><span class="sr-only">, </span>
                }{{ status() }}
              }
            </span>
          </p>
        </div>

        <ng-content select="[contentCardAction]" />

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

    <!-- One slot, drawn in whichever frame applies: Angular projects a
         selector into a single ng-content, so two would leave one empty. -->
    <ng-template #previewSlot><ng-content select="[contentCardPreview]" /></ng-template>

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
export class ContentCardComponent {
  readonly title = input.required<string>();
  /** The accessible name of the card, when it should say more than the title. */
  readonly label = input<string>('');
  /** The first part of the line under the title: a host, or a file's type and size. */
  readonly meta = input<string>('');
  readonly status = input<string>('');
  /** Something is happening now: browsing, uploading, preparing. */
  readonly live = input(false);
  readonly tone = input<'default' | 'danger'>('default');
  readonly size = input<ContentCardSize>('md');
  readonly bleed = input(false);
  /** 0–100 draws the bar; null draws none. */
  readonly progress = input<number | null>(null);
  readonly menuItems = input<ContentCardMenuItem[]>([]);

  readonly menuSelect = output<string>();

  protected readonly menuPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'end', overlayY: 'top', offsetY: 4 },
    { originX: 'end', originY: 'top', overlayX: 'end', overlayY: 'bottom', offsetY: -4 },
  ];

  protected readonly frameClass = computed(() =>
    this.tone() === 'danger'
      ? 'ring-1 ring-state-danger-300 dark:ring-state-danger-800'
      : 'ring-1 ring-gray-200 dark:ring-white/10',
  );

  protected readonly trayClass = computed(() => {
    const height = this.size() === 'md' ? 'h-36' : 'h-21';
    const pad = this.bleed() ? '' : this.size() === 'md' ? 'px-6 pt-5' : 'px-3.5 pt-3';
    const bg =
      this.tone() === 'danger'
        ? 'bg-state-danger-50 dark:bg-state-danger-950/30'
        : 'bg-gray-100 dark:bg-gray-800/70';
    return `${height} ${pad} ${bg}`;
  });

  protected readonly footClass = computed(() => {
    const border =
      this.tone() === 'danger'
        ? 'border-state-danger-200 dark:border-state-danger-900'
        : 'border-gray-200 dark:border-white/10';
    const pad = this.size() === 'md' ? 'gap-3 px-4 py-3' : 'gap-1.5 py-1.5 pl-3 pr-2';
    return `${border} ${pad}`;
  });

  protected readonly metaClass = computed(() => {
    const text = this.size() === 'md' ? 'text-xs/5' : 'text-[11.5px]/[18px]';
    const colour =
      this.tone() === 'danger'
        ? 'text-state-danger-700 dark:text-state-danger-300'
        : 'text-gray-500 dark:text-gray-400';
    return `${text} ${colour}`;
  });
}

/** Shared look for a card's primary action, so every consumer matches. */
export const CONTENT_CARD_ACTION_CLASS =
  'inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1 text-sm/6 font-medium text-gray-900 no-underline! ring-1 ring-gray-300 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-100 dark:ring-white/20 dark:hover:bg-white/10';
