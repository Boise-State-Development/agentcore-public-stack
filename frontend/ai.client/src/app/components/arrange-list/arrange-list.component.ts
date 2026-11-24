import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';
import { CdkDrag, CdkDragDrop, CdkDragHandle, CdkDropList } from '@angular/cdk/drag-drop';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowDown, heroArrowUp, heroBars2, heroLockClosed } from '@ng-icons/heroicons/outline';

/** One row the user can arrange. */
export interface ArrangeRow {
  id: string;
  name: string;
  /** One quiet line under the name, such as the user's role or "Shared with you". */
  detail?: string;
  hidden?: boolean;
  /** A row whose place is not the user's (a locked role pin): shown first, never moved. */
  locked?: boolean;
  /** Offer no Hide box on this row, even when the list allows hiding. */
  cannotHide?: boolean;
}

/** What Done hands back: every movable row top first, and which ones are hidden. */
export interface ArrangeResult {
  ids: string[];
  hiddenIds: ReadonlySet<string>;
}

let nextArrangeListId = 0;

/**
 * Arrange mode (docs/specs/agent-project-organization.md §6.3): one column of rows the user
 * reorders by dragging the handle, by the arrow keys on it, or by Move up / Move down, and
 * (when `allowHide`) hides with a checkbox. Nothing is saved until **Done**; **Cancel** and
 * Escape discard. The Edit sidebar dialog's interaction, as a full-width mode.
 *
 * Rows are not links here, so a drag can never open an item.
 */
@Component({
  selector: 'app-arrange-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkDropList, CdkDrag, CdkDragHandle, NgIcon],
  providers: [provideIcons({ heroArrowDown, heroArrowUp, heroBars2, heroLockClosed })],
  host: { class: 'block', '(keydown.escape)': 'cancelled.emit()' },
  template: `
    <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
      <p class="text-sm/6 text-gray-600 dark:text-gray-400">
        Drag the handles or use the arrow keys to set your order.@if (allowHide()) { Tick a box to hide it from your list. }
      </p>
      <div class="flex items-center gap-2">
        <button
          type="button"
          (click)="cancelled.emit()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 ring-1 ring-gray-300 ring-inset hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-200 dark:ring-gray-600 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="finish()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          Done
        </button>
      </div>
    </div>

    <!-- Announces keyboard and pointer moves; the list itself doesn't. -->
    <p class="sr-only" aria-live="polite">{{ announcement() }}</p>

    @if (lockedRows().length > 0) {
      <ul [attr.aria-label]="label() + ' your role keeps first'" class="mb-2 divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-gray-50 dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800/60">
        @for (row of lockedRows(); track row.id) {
          <li class="flex items-center gap-3 px-3 py-2.5">
            <span class="grid size-8 shrink-0 place-items-center text-gray-400 dark:text-gray-500">
              <ng-icon name="heroLockClosed" class="size-4" aria-hidden="true" />
            </span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ row.name }}</span>
              <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">Pinned for your role and kept first</span>
            </span>
          </li>
        }
      </ul>
    }

    <ul
      cdkDropList
      cdkDropListLockAxis="y"
      (cdkDropListDropped)="onDrop($event)"
      [attr.aria-label]="label() + ' in your order'"
      class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800"
    >
      @for (row of rows(); track row.id; let i = $index, first = $first, last = $last) {
        <li cdkDrag cdkDragPreviewClass="arrange-list-drag-preview" class="flex items-center gap-2 bg-white px-2 py-1.5 dark:bg-gray-800">
          <button
            type="button"
            cdkDragHandle
            [id]="handleId(row.id)"
            (keydown)="onHandleKeydown($event, i)"
            [attr.aria-label]="'Reorder ' + row.name + ', position ' + (i + 1) + ' of ' + rows().length + '. Use the arrow keys to move.'"
            class="flex size-8 shrink-0 cursor-grab touch-none items-center justify-center rounded-xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 active:cursor-grabbing dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroBars2" class="size-4" aria-hidden="true" />
          </button>

          <span class="min-w-0 flex-1 py-1">
            <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white" [class.opacity-60]="row.hidden">{{ row.name }}</span>
            @if (row.detail) {
              <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ row.detail }}</span>
            }
          </span>

          @if (allowHide() && !row.cannotHide) {
            <label class="flex shrink-0 cursor-pointer items-center gap-2 rounded-xl px-2 py-1 text-xs/5 text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700">
              <input
                type="checkbox"
                [checked]="row.hidden"
                (change)="onToggleHidden(row, $event)"
                [attr.aria-label]="'Hide ' + row.name"
                class="size-4 rounded border-gray-300 text-primary-600 focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800"
              />
              <span aria-hidden="true">Hide</span>
            </label>
          }

          <span class="flex shrink-0 items-center">
            <button
              type="button"
              (click)="moveBy(i, -1)"
              [disabled]="first"
              [attr.aria-label]="'Move ' + row.name + ' up'"
              class="grid size-8 place-items-center rounded-xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-30 disabled:hover:bg-transparent dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <ng-icon name="heroArrowUp" class="size-4" aria-hidden="true" />
            </button>
            <button
              type="button"
              (click)="moveBy(i, 1)"
              [disabled]="last"
              [attr.aria-label]="'Move ' + row.name + ' down'"
              class="grid size-8 place-items-center rounded-xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-30 disabled:hover:bg-transparent dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <ng-icon name="heroArrowDown" class="size-4" aria-hidden="true" />
            </button>
          </span>
        </li>
      }
    </ul>
  `,
  styles: `
    .cdk-drag-placeholder {
      opacity: 0.35;
    }

    /* The floating copy under the pointer is appended to <body>, away from the list. */
    .arrange-list-drag-preview {
      box-sizing: border-box;
      box-shadow:
        0 10px 15px -3px rgb(0 0 0 / 0.1),
        0 4px 6px -4px rgb(0 0 0 / 0.1);
      list-style: none;
    }

    .cdk-drop-list-dragging .cdk-drag:not(.cdk-drag-placeholder),
    .cdk-drag-animating {
      transition: transform 200ms cubic-bezier(0, 0, 0.2, 1);
    }

    @media (prefers-reduced-motion: reduce) {
      .cdk-drop-list-dragging .cdk-drag:not(.cdk-drag-placeholder),
      .cdk-drag-animating {
        transition: none;
      }
    }
  `,
})
export class ArrangeListComponent {
  private readonly injector = inject(Injector);
  private readonly instance = nextArrangeListId++;

  /** The rows in the order to start from. Locked ones are shown first and never move. */
  readonly items = input.required<ArrangeRow[]>();
  /** What the rows are, for the lists' accessible names ("Agents", "Projects"). */
  readonly label = input.required<string>();
  /** Offer a Hide box on each row. */
  readonly allowHide = input(true);

  readonly done = output<ArrangeResult>();
  readonly cancelled = output<void>();

  protected readonly lockedRows = computed(() => this.items().filter((row) => row.locked));

  /** The movable rows as the user is arranging them; reset whenever the input changes. */
  protected readonly rows = linkedSignal(() => this.items().filter((row) => !row.locked).map((row) => ({ ...row })));

  /** Screen-reader announcement for the last move or hide (a polite live region). */
  protected readonly announcement = signal('');

  protected handleId(id: string): string {
    return `arrange-${this.instance}-${id}`;
  }

  protected finish(): void {
    const rows = this.rows();
    this.done.emit({
      ids: rows.map((row) => row.id),
      hiddenIds: new Set(rows.filter((row) => row.hidden).map((row) => row.id)),
    });
  }

  protected onToggleHidden(row: ArrangeRow, event: Event): void {
    const hidden = (event.target as HTMLInputElement).checked;
    this.rows.update((rows) => rows.map((r) => (r.id === row.id ? { ...r, hidden } : r)));
    this.announcement.set(`${row.name} ${hidden ? 'will be hidden' : 'will be shown'}.`);
  }

  protected onDrop(event: CdkDragDrop<unknown>): void {
    this.move(event.previousIndex, event.currentIndex);
  }

  protected moveBy(index: number, delta: number): void {
    this.move(index, index + delta);
  }

  /**
   * Keyboard reordering on the drag handle — CDK drag and drop is pointer-only.
   * Arrow keys move one place, Home/End to either end.
   */
  protected onHandleKeydown(event: KeyboardEvent, index: number): void {
    const last = this.rows().length - 1;
    const target =
      event.key === 'ArrowUp' ? index - 1
      : event.key === 'ArrowDown' ? index + 1
      : event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : null;
    if (target === null) return;
    event.preventDefault();
    const id = this.rows()[index]?.id;
    this.move(index, target);
    // The moved row's node may be re-inserted, which drops focus; put it back so the user
    // can keep pressing the arrow key.
    afterNextRender(() => document.getElementById(this.handleId(id))?.focus(), { injector: this.injector });
  }

  private move(from: number, to: number): void {
    const rows = this.rows();
    const target = Math.max(0, Math.min(rows.length - 1, to));
    if (from === target || from < 0 || from >= rows.length) return;
    const next = [...rows];
    const [moved] = next.splice(from, 1);
    next.splice(target, 0, moved);
    this.rows.set(next);
    this.announcement.set(`${moved.name} moved to position ${target + 1} of ${rows.length}.`);
  }
}
