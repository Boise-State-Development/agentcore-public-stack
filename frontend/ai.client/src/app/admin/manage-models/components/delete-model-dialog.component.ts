import {
  Component,
  ChangeDetectionStrategy,
  inject,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

/**
 * Data passed to the delete model dialog.
 */
export interface DeleteModelDialogData {
  modelId: string;
  modelName: string;
}

/**
 * Result returned when the dialog closes.
 * - `true` — admin confirmed deletion.
 * - `undefined` — admin cancelled (Escape, backdrop, Cancel button).
 */
export type DeleteModelDialogResult = true | undefined;

@Component({
  selector: 'app-delete-model-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroExclamationTriangle })],
  host: { class: 'block' },
  template: `
    <!-- An alertdialog: focus starts on Cancel, so a stray Enter never deletes. -->
    <app-dialog-shell
      [title]="'Delete ' + data.modelName + '?'"
      description="This removes the model from the catalog and revokes access for all users. This action cannot be undone."
      dialogRole="alertdialog"
      (closed)="onCancel()"
    >
      <div
        dialogIcon
        class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-state-danger-100 dark:bg-state-danger-500/10"
      >
        <ng-icon
          name="heroExclamationTriangle"
          class="size-5 text-state-danger-600 dark:text-state-danger-400"
          aria-hidden="true"
        />
      </div>

      <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
        Model ID
      </p>
      <p class="mt-1 truncate font-mono text-sm/6 text-gray-700 dark:text-gray-300" [title]="data.modelId">
        {{ data.modelId }}
      </p>

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          cdkFocusInitial
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="onConfirm()"
          class="inline-flex items-center gap-2 rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500"
        >
          Delete model
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class DeleteModelDialogComponent {
  protected readonly dialogRef = inject(DialogRef<DeleteModelDialogResult>);
  protected readonly data = inject<DeleteModelDialogData>(DIALOG_DATA);

  onConfirm(): void {
    this.dialogRef.close(true);
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
