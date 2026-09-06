import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark, heroNoSymbol } from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../../../components/dialog/dialog-dismiss.directive';

export interface DeclineSubmissionDialogData {
  /** Just enough to word the copy — the queue row and the review page both supply it. */
  name: string;
  ownerName: string;
}

/** The reason, or undefined if cancelled. */
export type DeclineSubmissionDialogResult = string | undefined;

/**
 * Declines a submission for the store, with a reason.
 *
 * **The third decision, and the one the queue was missing.** Approve and request-changes
 * were the only exits, so an admin who judged a submission not a fit had to publish it or
 * say "fix this" — which promises a review they do not intend to give, leaves the author
 * revising toward an approval that is not coming, and puts the same submission back in the
 * queue every round.
 *
 * ⚠️ Deliberately **not** worded as a permanent block, because it is not one: the author
 * may revise and submit again (`rejected → in_review`). Making it terminal would need an
 * appeal path and an admin escape hatch, and none of that is worth building before someone
 * needs it. What this buys now is an honest "no" the author can read and answer.
 *
 * The reason is required for the same reason it is on request-changes: it renders on the
 * author's own card, and a decline with no reason is the one outcome they cannot act on.
 */
@Component({
  selector: 'app-decline-submission-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, NgIcon],
  providers: [provideIcons({ heroXMark, heroNoSymbol })],
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
        aria-labelledby="decline-submission-title"
        aria-describedby="decline-submission-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2 id="decline-submission-title" class="text-lg/7 font-semibold text-gray-900 dark:text-white">
              Decline this submission
            </h2>
            <p id="decline-submission-description" class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              <span class="font-medium">{{ data.name }}</span> will not go into the store, and
              {{ data.ownerName }} sees your reason on their card. Nothing is deleted —
              they can revise and submit again.
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
          <label for="decline-reason" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
            Why is this not a fit?
          </label>
          <!-- The copy steers away from "fix X" on purpose. An admin who writes a to-do
               list here has picked the wrong control, and the author will read it as one. -->
          <p class="text-xs/5 text-gray-500 dark:text-gray-400">
            If the answer is "it needs work", use Request changes instead — that says you
            want it once it is fixed.
          </p>
          <textarea
            id="decline-reason"
            rows="4"
            [value]="reason()"
            (input)="onReasonInput($event)"
            placeholder="Be specific — this is the whole message the author receives."
            class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
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
            class="inline-flex items-center gap-1.5 rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-state-danger-500 dark:hover:bg-state-danger-600"
          >
            <ng-icon name="heroNoSymbol" class="size-4" aria-hidden="true" />
            Decline
          </button>
        </div>
      </div>
    </div>
  `,
})
export class DeclineSubmissionDialogComponent {
  private dialogRef = inject<DialogRef<DeclineSubmissionDialogResult>>(DialogRef);
  readonly data = inject<DeclineSubmissionDialogData>(DIALOG_DATA);

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
