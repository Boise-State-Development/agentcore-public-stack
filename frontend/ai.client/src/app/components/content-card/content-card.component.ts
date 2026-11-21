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

/**
 * `lg` is what the agent made (a generated file, an artifact, the page it
 * browsed); `md` is what the user attached, in a sent message; `sm` is the
 * composer's, which has no chrome row. Inputs stay compact on the user's side;
 * outputs run wider in the assistant's column but lie on their side, the tray
 * filling the left half and the name and actions beside it, so a deliverable
 * shows what it is without towering over the answer. The tray, sheet, chrome
 * row and footer are the same parts in every size, so the two read as one
 * family told apart by shape and side rather than by a second look.
 */
export type ContentCardSize = 'lg' | 'md' | 'sm';

/**
 * The host width every output card shares (use with `size="lg"`): the
 * assistant column's full width, wider than any input card.
 */
export const OUTPUT_CARD_HOST_CLASS = 'block w-full';

/**
 * The card shape shared by everything the conversation shows as "a thing you
 * can open": a gray tray holding a framed sheet (the preview), then a footer
 * with the title, a `meta · status` line, a primary action and an overflow
 * menu. The browser card and the attachment card are both drawn with it, so
 * a page, a file and later an artifact read as one family.
 *
 * Presentational only. Slots:
 * - `[contentCardChrome]`: the sheet's top row (window dots and host for a
 *   page, the type icon and a locator for a file). Not drawn at `sm`.
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
  // An output card measures itself, so it can stand up again when it has no
  // room to lie on its side (a phone, a narrow split view).
  host: { '[class]': "size() === 'lg' ? 'block @container' : 'block'" },
  template: `
    <article
      class="relative isolate flex h-full overflow-hidden rounded-2xl bg-white dark:bg-gray-900"
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
            [class]="sheetClass()"
          >
            @if (size() !== 'sm') {
              <div
                class="flex shrink-0 items-center gap-1 border-b border-gray-100 dark:border-white/5"
                [class]="size() === 'lg' ? 'px-2 py-1' : 'px-2.5 py-1.5'"
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
        class="flex min-w-0 flex-1 items-center"
        [class]="footClass()"
      >
        <div class="min-w-0" [class]="textClass()">
          <p
            class="font-semibold text-gray-900 dark:text-gray-100"
            [class]="titleClass()"
            [title]="title()"
          >
            {{ title() }}
          </p>
          <p
            class="flex min-w-0 items-center gap-1.5"
            [class]="nameMetaClass()"
          >
            <ng-container [ngTemplateOutlet]="metaLine" />
          </p>
        </div>

        <!-- Beside the name everywhere but a wide output card, where they
             sit in a row under it: the meta line on the left, the actions
             pushed right, so the name has the half's whole width and the
             card one row less height. -->
        <div [class]="actionsClass()">
        @if (size() === 'lg') {
          <p
            class="mr-auto hidden min-w-0 items-center gap-1.5 @md:flex"
            [class]="metaClass()"
          >
            <ng-container [ngTemplateOutlet]="metaLine" />
          </p>
        }
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
      </div>
    </article>

    <ng-template #metaLine>
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
    </ng-template>

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

  protected readonly frameClass = computed(() => {
    const ring =
      this.tone() === 'danger'
        ? 'ring-1 ring-state-danger-300 dark:ring-state-danger-800'
        : 'ring-1 ring-gray-200 dark:ring-white/10';
    return `${ring} ${this.size() === 'lg' ? 'flex-col @md:flex-row' : 'flex-col'}`;
  });

  protected readonly trayClass = computed(() => {
    const size = this.size();
    // An output's tray is the left half of the card, so its height is the
    // card's; the others' trays sit on top at full width, as an output's does
    // too when the card is under 28rem.
    const box = size === 'lg' ? 'h-24 w-full @md:h-auto @md:min-h-24 @md:w-1/2' : size === 'md' ? 'h-36' : 'h-21';
    const pad = this.bleed() ? '' : size === 'lg' ? 'px-4 pt-3.5' : size === 'md' ? 'px-6 pt-5' : 'px-3.5 pt-3';
    const bg =
      this.tone() === 'danger'
        ? 'bg-state-danger-50 dark:bg-state-danger-950/30'
        : 'bg-gray-100 dark:bg-gray-800/70';
    return `${box} ${pad} ${bg}`;
  });

  /**
   * A wide output card's tray stretches to whatever the name and actions
   * need, so its sheet is laid over the tray rather than in it: a preview's
   * own rows must never make the card taller.
   */
  protected readonly sheetClass = computed(() =>
    this.size() === 'lg' ? '@md:absolute @md:inset-x-4 @md:top-3.5 @md:bottom-0 @md:h-auto' : '',
  );

  protected readonly footClass = computed(() => {
    const border =
      this.tone() === 'danger'
        ? 'border-state-danger-200 dark:border-state-danger-900'
        : 'border-gray-200 dark:border-white/10';
    const size = this.size();
    const side = size === 'lg' ? 'border-t @md:border-t-0 @md:border-l' : 'border-t';
    const pad = size === 'sm' ? 'gap-1.5 py-1.5 pl-3 pr-2' : 'gap-3 px-4 py-3';
    // A wide output card stacks its name over its actions.
    const flow = size === 'lg' ? '@md:flex-col @md:items-start @md:justify-center @md:gap-2' : '';
    return `${side} ${border} ${pad} ${flow}`;
  });

  /** Takes the row's free width; in a wide output card, the whole column's. */
  protected readonly textClass = computed(() =>
    this.size() === 'lg' ? 'flex-1 @md:w-full @md:flex-none' : 'flex-1',
  );

  /** Inline in the footer row, except stacked under the name in a wide output card. */
  protected readonly actionsClass = computed(() =>
    this.size() === 'lg' ? 'flex shrink-0 items-center gap-2 @md:w-full @md:shrink' : 'contents',
  );

  /** The meta line under the name; a wide output card shows it in its actions row instead. */
  protected readonly nameMetaClass = computed(() =>
    this.size() === 'lg' ? `${this.metaClass()} @md:hidden` : this.metaClass(),
  );

  /**
   * An output has the width to show most of a long name, so it wraps to two
   * lines (breaking inside a hyphenated filename if it must) before it
   * truncates; the narrow input cards keep one line.
   */
  protected readonly titleClass = computed(() => {
    const size = this.size();
    if (size === 'lg') return 'line-clamp-2 text-sm/5 [overflow-wrap:anywhere]';
    return size === 'sm' ? 'truncate text-[13px]/5' : 'truncate text-sm/6';
  });

  protected readonly metaClass = computed(() => {
    const text = this.size() === 'sm' ? 'text-[11.5px]/[18px]' : 'text-xs/5';
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

/**
 * A smaller primary action, for a card whose footer also holds an icon
 * action, so the labelled button does not outweigh the row. Still 24px tall,
 * the WCAG 2.5.8 minimum target.
 */
export const CONTENT_CARD_COMPACT_ACTION_CLASS =
  'inline-flex shrink-0 items-center gap-1 rounded-md px-2.5 py-0.5 text-xs/5 font-medium text-gray-900 no-underline! ring-1 ring-gray-300 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-accessible disabled:cursor-not-allowed disabled:opacity-60 dark:text-gray-100 dark:ring-white/20 dark:hover:bg-white/10';
