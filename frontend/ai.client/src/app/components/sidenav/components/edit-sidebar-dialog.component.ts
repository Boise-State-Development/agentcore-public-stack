import { ChangeDetectionStrategy, Component, Injector, afterNextRender, inject, signal } from '@angular/core';
import { DialogRef } from '@angular/cdk/dialog';
import { CdkDrag, CdkDragDrop, CdkDragHandle, CdkDropList } from '@angular/cdk/drag-drop';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroAdjustmentsHorizontal,
  heroBars2,
  heroClock,
  heroDocumentText,
  heroFolderOpen,
  heroSparkles,
} from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../../dialog/dialog-shell.component';
import { SidebarEntry, SidebarLayoutService } from '../sidebar-layout.service';

/**
 * Edit sidebar: show, hide and reorder the sidebar's navigation entries.
 *
 * There is no Cancel. Each change applies to the sidebar at once — it is in
 * view beside the dialog — and the opener saves the layout however the
 * dialog closes, so the result is always `undefined`.
 */
@Component({
  selector: 'app-edit-sidebar-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, CdkDropList, CdkDrag, CdkDragHandle, NgIcon],
  providers: [
    provideIcons({
      heroAdjustmentsHorizontal,
      heroBars2,
      heroClock,
      heroDocumentText,
      heroFolderOpen,
      heroSparkles,
    }),
  ],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Edit sidebar"
      description="Choose which items appear in your sidebar, and drag to reorder them. Hidden items stay under More."
      (closed)="close()"
    >
      <!-- Announces keyboard and pointer moves; the list itself doesn't. -->
      <p class="sr-only" aria-live="polite">{{ announcement() }}</p>

      <ul
        cdkDropList
        cdkDropListLockAxis="y"
        (cdkDropListDropped)="onDrop($event)"
        class="-mx-2"
        aria-label="Sidebar items"
      >
        @for (entry of layout.entries(); track entry.id; let i = $index) {
          <li
            cdkDrag
            cdkDragPreviewClass="edit-sidebar-drag-preview"
            class="flex items-center gap-2 rounded-2xl bg-white px-2 dark:bg-gray-800"
          >
            <button
              type="button"
              cdkDragHandle
              [id]="'sidebar-reorder-' + entry.id"
              (keydown)="onHandleKeydown($event, i)"
              [attr.aria-label]="'Reorder ' + entry.label + ', position ' + (i + 1) + ' of ' + layout.entries().length + '. Use the arrow keys to move.'"
              class="flex size-7 shrink-0 cursor-grab touch-none items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 active:cursor-grabbing dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            >
              <ng-icon name="heroBars2" class="size-4" aria-hidden="true" />
            </button>

            <label class="flex min-w-0 flex-1 cursor-pointer items-center gap-3 py-2">
              <input
                type="checkbox"
                [checked]="entry.visible"
                (change)="onToggle(entry, $event)"
                class="size-4 rounded border-gray-300 text-primary-600 focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800"
              />
              <ng-icon [name]="entry.icon" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
              <span class="truncate text-sm/6 text-gray-900 dark:text-white">{{ entry.label }}</span>
            </label>
          </li>
        }
      </ul>

      <div dialogFooter class="flex items-center justify-between gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="layout.reset()"
          [disabled]="layout.isDefault()"
          class="rounded-2xl px-3 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          Reset to default
        </button>
        <button
          type="button"
          (click)="close()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          Done
        </button>
      </div>
    </app-dialog-shell>
  `,
  styles: `
    .cdk-drag-placeholder {
      opacity: 0.35;
    }

    /* The floating copy under the pointer is appended to <body>, away from
       the panel, so it needs its own lift. */
    .edit-sidebar-drag-preview {
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
export class EditSidebarDialogComponent {
  private readonly dialogRef = inject(DialogRef<void>);
  private readonly injector = inject(Injector);
  protected readonly layout = inject(SidebarLayoutService);

  /** Screen-reader announcement for the last move or toggle (a polite live region). */
  protected readonly announcement = signal('');

  protected close(): void {
    this.dialogRef.close();
  }

  protected onToggle(entry: SidebarEntry, event: Event): void {
    const visible = (event.target as HTMLInputElement).checked;
    this.layout.setVisible(entry.id, visible);
    this.announcement.set(`${entry.label} ${visible ? 'shown in the sidebar' : 'moved to More'}.`);
  }

  protected onDrop(event: CdkDragDrop<unknown>): void {
    this.move(event.previousIndex, event.currentIndex);
  }

  /**
   * Keyboard reordering on the drag handle — CDK drag and drop is pointer-only.
   * Arrow keys move one place, Home/End to either end.
   */
  protected onHandleKeydown(event: KeyboardEvent, index: number): void {
    const last = this.layout.entries().length - 1;
    const target =
      event.key === 'ArrowUp' ? index - 1
      : event.key === 'ArrowDown' ? index + 1
      : event.key === 'Home' ? 0
      : event.key === 'End' ? last
      : null;
    if (target === null) {
      return;
    }
    event.preventDefault();
    const id = this.layout.entries()[index]?.id;
    this.move(index, Math.max(0, Math.min(last, target)));

    // The moved row's DOM node may be detached and re-inserted, which drops
    // focus; put it back so the user can keep pressing the arrow key.
    afterNextRender(() => document.getElementById(`sidebar-reorder-${id}`)?.focus(), {
      injector: this.injector,
    });
  }

  private move(from: number, to: number): void {
    if (from === to) {
      return;
    }
    const entries = this.layout.entries();
    const moved = entries[from];
    this.layout.move(from, to);
    this.announcement.set(`${moved.label} moved to position ${to + 1} of ${entries.length}.`);
  }
}
