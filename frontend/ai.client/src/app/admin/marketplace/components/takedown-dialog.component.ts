import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { AdminListingRow } from '../models/marketplace.model';
import { DialogDismissDirective } from '../../../components/dialog/dialog-dismiss.directive';

export interface TakedownDialogData {
  listing: AdminListingRow;
}

/** The reason, or undefined if cancelled. */
export type TakedownDialogResult = string | undefined;

/**
 * Delists a published agent.
 *
 * The callout is load-bearing copy, carried from the design mockup: a takedown is a
 * *delisting, not a revocation*. Reviewers who believe it recalls the agent will use it
 * for things it does not do.
 */
@Component({
  selector: 'app-takedown-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <div class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()">
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="takedown-title"
        aria-describedby="takedown-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="takedown-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Take down “{{ data.listing.name }}”?
            </h2>
            <p id="takedown-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              It leaves the store and stops appearing in search or the store front. The author
              is notified with your reason and can resubmit once it's addressed.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="px-6 py-4">
          <div class="rounded-2xl bg-state-warning-50 px-4 py-3 dark:bg-state-warning-900/20">
            <h3 class="text-sm/6 font-semibold text-state-warning-800 dark:text-state-warning-300">
              What a takedown does not do
            </h3>
            <p class="mt-1 text-sm/6 text-gray-700 dark:text-gray-300">
              Conversations already underway keep running, existing pins keep working, and the
              agent stays reachable by direct link. A takedown is a delisting, not a revocation.
            </p>
          </div>

          <label
            for="takedown-reason"
            class="mt-4 block text-sm/6 font-medium text-gray-900 dark:text-white"
          >
            Reason sent to {{ publisherLabel() }}
          </label>
          <textarea
            id="takedown-reason"
            rows="3"
            [value]="reason()"
            (input)="onReasonInput($event)"
            placeholder="What needs to change before this can be listed again?"
            class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>
        </div>

        <div class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            [disabled]="!reason().trim()"
            (click)="onSubmit()"
            class="rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-state-danger-500 dark:hover:bg-state-danger-600"
          >
            Take down
          </button>
        </div>
      </div>
    </div>
  `,
})
export class TakedownDialogComponent {
  private dialogRef = inject<DialogRef<TakedownDialogResult>>(DialogRef);
  readonly data = inject<TakedownDialogData>(DIALOG_DATA);

  readonly reason = signal('');

  onReasonInput(event: Event): void {
    this.reason.set((event.target as HTMLTextAreaElement).value);
  }

  /** The attribution if there is one, else the author — never an empty "sent to". */
  publisherLabel(): string {
    return this.data.listing.publisher?.label || this.data.listing.ownerName;
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
