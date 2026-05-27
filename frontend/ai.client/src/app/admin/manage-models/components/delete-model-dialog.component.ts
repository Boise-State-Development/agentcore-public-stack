import {
  Component,
  ChangeDetectionStrategy,
  inject,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroExclamationTriangle } from '@ng-icons/heroicons/outline';

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
  imports: [NgIcon],
  providers: [provideIcons({ heroXMark, heroExclamationTriangle })],
  host: {
    'class': 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
      (click)="onCancel()"
    ></div>

    <!-- Dialog Panel -->
    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="delete-model-title"
        aria-describedby="delete-model-description"
      >
        <!-- Header -->
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="flex items-start gap-3 min-w-0">
            <div class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-red-100 dark:bg-red-500/10">
              <ng-icon
                name="heroExclamationTriangle"
                class="size-5 text-red-600 dark:text-red-400"
                aria-hidden="true"
              />
            </div>
            <div class="min-w-0">
              <h2 id="delete-model-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
                Delete {{ data.modelName }}?
              </h2>
              <p id="delete-model-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                This removes the model from the catalog and revokes access for all users.
                This action cannot be undone.
              </p>
            </div>
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

        <!-- Model ID detail -->
        <div class="px-6 py-4">
          <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Model ID
          </p>
          <p class="mt-1 truncate font-mono text-sm/6 text-gray-700 dark:text-gray-300" [title]="data.modelId">
            {{ data.modelId }}
          </p>
        </div>

        <!-- Actions -->
        <div class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="onConfirm()"
            class="inline-flex items-center gap-2 rounded-2xl bg-red-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:bg-red-500 dark:hover:bg-red-600"
          >
            Delete model
          </button>
        </div>
      </div>
    </div>
  `,
  styles: `
    @import "tailwindcss";

    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }

    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.97);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
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
