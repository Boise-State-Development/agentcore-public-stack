import {
  Component,
  ChangeDetectionStrategy,
  inject,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { DialogDescriptionDirective } from '../../../components/dialog/dialog-description.directive';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

/**
 * Data passed to the delete tool dialog.
 */
export interface DeleteToolDialogData {
  toolId: string;
  displayName: string;
}

/**
 * Result returned when the dialog is closed.
 * Returns true if user confirms deletion, undefined otherwise.
 */
export type DeleteToolDialogResult = boolean | undefined;

@Component({
  selector: 'app-delete-tool-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDescriptionDirective, DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <!-- An alertdialog: deleting is irreversible. Focus starts on Cancel, so a stray Enter never deletes. -->
    <app-dialog-shell title="Delete Tool" dialogRole="alertdialog" (closed)="onCancel()">
      <div
        dialogIcon
        class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-state-danger-100 dark:bg-state-danger-500/10"
      >
        <ng-icon name="heroExclamationTriangle" class="size-5 text-state-danger-600 dark:text-state-danger-400" aria-hidden="true" />
      </div>

      <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Are you sure you want to delete <span class="font-medium">{{ data.displayName }}</span>?
        This will disable the tool and remove it from the catalog. This action cannot be undone.
      </p>

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
          class="rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-semibold text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500"
        >
          Delete
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class DeleteToolDialogComponent {
  protected readonly dialogRef = inject(DialogRef<DeleteToolDialogResult>);
  protected readonly data = inject<DeleteToolDialogData>(DIALOG_DATA);

  onConfirm(): void {
    this.dialogRef.close(true);
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}

