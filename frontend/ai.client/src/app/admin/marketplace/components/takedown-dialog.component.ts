import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { AdminListingRow } from '../models/marketplace.model';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

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
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      [title]="title"
      description="It leaves the store and stops appearing in search or the store front. The author is notified with your reason and can resubmit once it's addressed."
      (closed)="onCancel()"
    >
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
        cdkFocusInitial
        [value]="reason()"
        (input)="onReasonInput($event)"
        placeholder="What needs to change before this can be listed again?"
        class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
      ></textarea>

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
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
          class="rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Take down
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class TakedownDialogComponent {
  private dialogRef = inject<DialogRef<TakedownDialogResult>>(DialogRef);
  readonly data = inject<TakedownDialogData>(DIALOG_DATA);

  readonly title = `Take down “${this.data.listing.name}”?`;

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
