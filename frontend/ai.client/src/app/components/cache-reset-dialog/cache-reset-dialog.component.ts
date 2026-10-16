import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../dialog/dialog-shell.component';

export interface CacheResetDialogData {
  /** "Switch model?" / "Change effort?" */
  title: string;
  /** What the change does to this conversation's next message, in a sentence or two. */
  message: string;
  /** The confirm button's label, e.g. "Switch model". */
  confirmText: string;
}

/** Confirmed. `undefined` from `dialogRef.closed` means cancelled. */
export interface CacheResetDialogResult {
  /** The user ticked "Don't ask again". */
  dontAskAgain: boolean;
}

/**
 * Asks before a change that makes an open conversation's next message
 * re-process the whole conversation instead of reading it from the prompt
 * cache. Opened by `PromptCacheGuardService`, which decides whether the
 * change costs enough to be worth asking about.
 */
@Component({
  selector: 'app-cache-reset-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <!-- Focus starts on Cancel, so a stray Enter keeps the conversation as it is. -->
    <app-dialog-shell [title]="data.title" [description]="data.message" dialogRole="alertdialog" (closed)="onCancel()">
      <label class="flex cursor-pointer items-center gap-2 text-sm/6 text-gray-700 dark:text-gray-300">
        <input
          type="checkbox"
          [checked]="dontAskAgain()"
          (change)="onDontAskAgainChange($event)"
          class="size-4 rounded border-gray-300 text-primary-600 focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800"
        />
        Don't ask again
      </label>

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          cdkFocusInitial
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="onConfirm()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >
          {{ data.confirmText }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class CacheResetDialogComponent {
  protected readonly dialogRef = inject<DialogRef<CacheResetDialogResult>>(DialogRef);
  protected readonly data = inject<CacheResetDialogData>(DIALOG_DATA);

  protected readonly dontAskAgain = signal(false);

  protected onDontAskAgainChange(event: Event): void {
    this.dontAskAgain.set((event.target as HTMLInputElement).checked);
  }

  protected onConfirm(): void {
    this.dialogRef.close({ dontAskAgain: this.dontAskAgain() });
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
