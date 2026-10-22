import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroNoSymbol } from '@ng-icons/heroicons/outline';
import { DialogDescriptionDirective } from '../../../components/dialog/dialog-description.directive';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

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
  imports: [DialogDescriptionDirective, DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroNoSymbol })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Decline this submission" (closed)="onCancel()">
      <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        <span class="font-medium">{{ data.name }}</span> will not go into the store, and
        {{ data.ownerName }} sees your reason on their card. Nothing is deleted —
        they can revise and submit again.
      </p>

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
        cdkFocusInitial
        [value]="reason()"
        (input)="onReasonInput($event)"
        placeholder="Be specific — this is the whole message the author receives."
        class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
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
          class="inline-flex items-center gap-1.5 rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <ng-icon name="heroNoSymbol" class="size-4" aria-hidden="true" />
          Decline
        </button>
      </div>
    </app-dialog-shell>
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
