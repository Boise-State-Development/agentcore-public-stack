import { Component, ChangeDetectionStrategy, inject } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

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
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <!-- An alertdialog: saving reaches everyone in the role, so focus starts on Cancel. -->
    <app-dialog-shell
      [title]="title"
      description="This unpins for everyone in this role who has not pinned it themselves. Anyone who added it to their own agents keeps it."
      dialogRole="alertdialog"
      (closed)="onCancel()"
    >
      <ul class="flex flex-col gap-1">
        @for (name of data.removed; track name) {
          <li class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ name }}</li>
        }
      </ul>
      <div class="mt-4 rounded-2xl bg-state-warning-50 px-4 py-3 dark:bg-state-warning-900/20">
        <p class="text-sm/6 text-gray-700 dark:text-gray-300">
          Default pins resolve live — there is no "new members only". Removing one takes
          effect for current members on their next page load.
        </p>
      </div>

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          cdkFocusInitial
          (click)="onCancel()"
          class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="onConfirm()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          Save changes
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class RolePinSaveDialogComponent {
  private dialogRef = inject<DialogRef<RolePinSaveDialogResult>>(DialogRef);
  readonly data = inject<RolePinSaveDialogData>(DIALOG_DATA);

  private readonly pins =
    this.data.removed.length === 1 ? 'a default pin' : `${this.data.removed.length} default pins`;
  readonly title = `Remove ${this.pins} from ${this.data.roleLabel}?`;

  onConfirm(): void {
    this.dialogRef.close(true);
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
