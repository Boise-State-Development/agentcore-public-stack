import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { AdminListingRow } from '../models/marketplace.model';

export interface RequestChangesDialogData {
  listing: AdminListingRow;
}

/** The reason, or undefined if cancelled. */
export type RequestChangesDialogResult = string | undefined;

/**
 * Returns a submission to its author with a reason.
 *
 * The reason is required, not optional: it renders on the author's own card, which is the
 * whole point — the author never has to ask what happened. (The design mockup decided
 * without one; the spec requires it, so this dialog exists.)
 */
@Component({
  selector: 'app-request-changes-dialog',
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

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0">
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="request-changes-title"
        aria-describedby="request-changes-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="request-changes-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Request changes
            </h2>
            <p id="request-changes-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Returns <span class="font-medium">{{ data.listing.name }}</span> to
              {{ data.listing.ownerName }}. Your note appears on their card, so they can
              act on it without asking.
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
          <label for="change-reason" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
            What needs to change?
          </label>
          <textarea
            id="change-reason"
            rows="4"
            [value]="reason()"
            (input)="onReasonInput($event)"
            placeholder="Be specific — this is the whole message the author receives."
            class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>
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
            [disabled]="!reason().trim()"
            (click)="onSubmit()"
            class="rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            Send to author
          </button>
        </div>
      </div>
    </div>
  `,
})
export class RequestChangesDialogComponent {
  private dialogRef = inject<DialogRef<RequestChangesDialogResult>>(DialogRef);
  readonly data = inject<RequestChangesDialogData>(DIALOG_DATA);

  readonly reason = signal('');

  onReasonInput(event: Event): void {
    this.reason.set((event.target as HTMLTextAreaElement).value);
  }

  onSubmit(): void {
    const reason = this.reason().trim();
    if (reason) {
      this.dialogRef.close(reason);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
