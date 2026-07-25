import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';

export interface RolePinSaveDialogData {
  roleLabel: string;
  /** Agent names this save takes off the role's seed list. */
  removed: string[];
}

/** `true` to save, `undefined` if cancelled. */
export type RolePinSaveDialogResult = true | undefined;

/**
 * Confirms a save that removes seeded agents (D9.1).
 *
 * The copy is load-bearing and comes straight from the spec: role pins resolve live, so
 * removing one **unpins for everyone in this role who has not pinned it themselves**.
 * There is no "apply to new members only" — live resolution makes that unrepresentable,
 * and it was dropped on purpose. An admin who believes removal only affects future
 * members will use this control for something it does not do.
 *
 * It appears on Save rather than on the row's `✕`, because the editor is staged: the row
 * control changes a local list, and the moment anything reaches other people is this one.
 */
@Component({
  selector: 'app-role-pin-save-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
    >
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="role-pin-save-title"
        aria-describedby="role-pin-save-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2
              id="role-pin-save-title"
              class="text-lg/7 font-semibold text-gray-900 dark:text-white"
            >
              Remove
              {{ data.removed.length === 1 ? 'a default pin' : data.removed.length + ' default pins' }}
              from {{ data.roleLabel }}?
            </h2>
            <p
              id="role-pin-save-description"
              class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400"
            >
              This unpins for everyone in this role who has not pinned it themselves.
              Anyone who added it to their own agents keeps it.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="px-6 py-4">
          <ul class="flex flex-col gap-1">
            @for (name of data.removed; track name) {
              <li class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ name }}</li>
            }
          </ul>
          <div class="mt-4 rounded-2xl bg-amber-50 px-4 py-3 dark:bg-amber-900/20">
            <p class="text-sm/6 text-gray-700 dark:text-gray-300">
              Default pins resolve live — there is no "new members only". Removing one takes
              effect for current members on their next page load.
            </p>
          </div>
        </div>

        <div class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="onConfirm()"
            class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            Save changes
          </button>
        </div>
      </div>
    </div>
  `,
})
export class RolePinSaveDialogComponent {
  private dialogRef = inject<DialogRef<RolePinSaveDialogResult>>(DialogRef);
  readonly data = inject<RolePinSaveDialogData>(DIALOG_DATA);

  onConfirm(): void {
    this.dialogRef.close(true);
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
