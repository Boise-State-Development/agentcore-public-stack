import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogDescriptionDirective } from '../../../components/dialog/dialog-description.directive';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';

export interface RequestChangesDialogData {
  /**
   * Just the two fields the copy names.
   *
   * Narrowed from the full `AdminListingRow` when the submission review page arrived: that
   * page holds an `AdminSubmissionReview`, not a queue row, and both surfaces send the same
   * decision. Widening the type to a union would have been the other option and is worse —
   * the dialog would then know about two shapes to render one sentence.
   */
  listing: { name: string; ownerName: string };
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
  imports: [DialogDescriptionDirective, DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Request changes" (closed)="onCancel()">
      <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Returns <span class="font-medium">{{ data.listing.name }}</span> to
        {{ data.listing.ownerName }}. Your note appears on their card, so they can
        act on it without asking.
      </p>

      <label for="change-reason" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
        What needs to change?
      </label>
      <textarea
        id="change-reason"
        rows="4"
        cdkFocusInitial
        [value]="reason()"
        (input)="onReasonInput($event)"
        placeholder="Be specific — this is the whole message the author receives."
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
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Send to author
        </button>
      </div>
    </app-dialog-shell>
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
