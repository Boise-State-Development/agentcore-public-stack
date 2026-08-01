import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { AdminListingRow } from '../models/marketplace.model';
import { DialogDismissDirective } from '../../../components/dialog/dialog-dismiss.directive';

export interface WithdrawalDecisionDialogData {
  listing: AdminListingRow;
  decision: 'grant' | 'decline';
}

/** The note, or undefined if cancelled. An empty string is a valid note-less grant. */
export type WithdrawalDecisionDialogResult = string | undefined;

/**
 * Confirms an admin's answer to a withdrawal request (§5.1).
 *
 * **Why a dialog at all, when Approve on a submission has none.** The two decisions are not
 * symmetric. Approving a submission puts something new on a shelf and is trivially undone
 * by a takedown; granting a withdrawal takes a live listing away from everyone who pinned
 * it, and the author is the only person who asked for it. The consequence deserves a
 * sentence naming it before the click lands.
 *
 * **A note is required to decline and optional to grant.** Declining refuses something the
 * author asked for, so they are owed a reason — the same rule request-changes follows, for
 * the same reason. Granting gives them what they wanted, so silence is a fine answer.
 */
@Component({
  selector: 'app-withdrawal-decision-dialog',
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

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()"
    >
      <div
        class="dialog-panel relative w-full overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="withdrawal-decision-title"
        aria-describedby="withdrawal-decision-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2
              id="withdrawal-decision-title"
              class="text-lg/7 font-semibold text-gray-900 dark:text-white"
            >
              {{ isGrant() ? 'Grant this withdrawal?' : 'Decline this withdrawal?' }}
            </h2>
            <p
              id="withdrawal-decision-description"
              class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400"
            >
              @if (isGrant()) {
                <span class="font-medium">{{ data.listing.name }}</span> leaves the store and
                becomes private. It revokes nothing retroactively: anyone who already pinned
                it keeps it, conversations underway keep running, and it stays reachable by
                direct link. {{ data.listing.ownerName }} can submit it again later.
              } @else {
                <span class="font-medium">{{ data.listing.name }}</span> stays in the store.
                It never came down while the request was pending, so nothing is restored —
                your note tells {{ data.listing.ownerName }} why it is staying.
              }
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
          <label
            for="withdrawal-note"
            class="block text-sm/6 font-medium text-gray-900 dark:text-white"
          >
            {{ isGrant() ? 'Note to the author (optional)' : 'Why is it staying up?' }}
          </label>
          <textarea
            id="withdrawal-note"
            rows="3"
            [value]="note()"
            (input)="onNoteInput($event)"
            [attr.placeholder]="
              isGrant()
                ? 'Anything they should know — otherwise leave this blank.'
                : 'Be specific — this is the whole message the author receives.'
            "
            class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>
        </div>

        <div
          class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700"
        >
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            [disabled]="!canSubmit()"
            (click)="onSubmit()"
            [class]="
              isGrant()
                ? 'rounded-2xl bg-rose-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-rose-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50'
                : 'rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600'
            "
          >
            {{ isGrant() ? 'Take it down' : 'Keep it published' }}
          </button>
        </div>
      </div>
    </div>
  `,
})
export class WithdrawalDecisionDialogComponent {
  private dialogRef = inject<DialogRef<WithdrawalDecisionDialogResult>>(DialogRef);
  readonly data = inject<WithdrawalDecisionDialogData>(DIALOG_DATA);

  readonly note = signal('');
  readonly isGrant = computed(() => this.data.decision === 'grant');
  readonly canSubmit = computed(() => this.isGrant() || !!this.note().trim());

  onNoteInput(event: Event): void {
    this.note.set((event.target as HTMLTextAreaElement).value);
  }

  onSubmit(): void {
    if (this.canSubmit()) {
      this.dialogRef.close(this.note().trim());
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
