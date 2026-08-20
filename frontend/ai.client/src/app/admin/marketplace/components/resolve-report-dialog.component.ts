import { Component, ChangeDetectionStrategy, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { AdminReportRow } from '../models/marketplace.model';
import { DialogDismissDirective } from '../../../components/dialog/dialog-dismiss.directive';

export interface ResolveReportDialogData {
  report: AdminReportRow;
  decision: 'resolve' | 'dismiss';
}

/** The admin's note, or undefined if cancelled. An empty string is a valid submission. */
export type ResolveReportDialogResult = { note?: string } | undefined;

/**
 * Close out a report (D15.5).
 *
 * The callout is load-bearing copy, and it is the mirror image of the takedown dialog's:
 * closing a report **changes nothing about the agent**. An admin who believes "Resolve"
 * delists will use it for something it does not do — and worse, will believe a problem
 * has been acted on when only the queue has been tidied.
 *
 * The note is the admin's own record and is deliberately labelled as such. It is never
 * forwarded to the author (D15.1): when a report is actionable, the author-facing channel
 * is the reason field on request-changes or takedown, and piping a user's raw words to
 * the person who built the thing is how one bad message ends a volunteer's willingness to
 * publish.
 */
@Component({
  selector: 'app-resolve-report-dialog',
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
        aria-labelledby="resolve-report-title"
        aria-describedby="resolve-report-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2
              id="resolve-report-title"
              class="text-lg/7 font-semibold text-gray-900 dark:text-white"
            >
              {{ isDismiss() ? 'Dismiss' : 'Resolve' }} this report?
            </h2>
            <p
              id="resolve-report-description"
              class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400"
            >
              {{ isDismiss() ? 'Dismissing' : 'Resolving' }} takes it off the queue and records
              who decided. The reporter is not notified.
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
              This does not change the agent
            </h3>
            <p class="mt-1 text-sm/6 text-gray-700 dark:text-gray-300">
              “{{ data.report.agentName }}” stays exactly as it is. If this report warrants
              action, use <strong>Request changes</strong> or <strong>Take down</strong> on the
              Listings table — that is the channel the author actually sees, and it is recorded
              separately.
            </p>
          </div>

          <label
            for="resolution-note"
            class="mt-4 block text-sm/6 font-medium text-gray-900 dark:text-white"
          >
            Your note <span class="font-normal text-gray-500">(optional, admin-only)</span>
          </label>
          <p class="mt-0.5 text-sm/6 text-gray-500 dark:text-gray-400">
            What you did about it. Never sent to the author or the reporter.
          </p>
          <textarea
            id="resolution-note"
            rows="3"
            maxlength="2000"
            [value]="note()"
            (input)="onNoteInput($event)"
            [placeholder]="
              isDismiss() ? 'Why this needs no action' : 'What you changed, or who you contacted'
            "
            class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>
        </div>

        <div
          class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700"
        >
          <button
            type="button"
            (click)="onCancel()"
            class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          <button
            type="button"
            (click)="onSubmit()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          >
            {{ isDismiss() ? 'Dismiss report' : 'Resolve report' }}
          </button>
        </div>
      </div>
    </div>
  `,
})
export class ResolveReportDialogComponent {
  private dialogRef = inject<DialogRef<ResolveReportDialogResult>>(DialogRef);
  readonly data = inject<ResolveReportDialogData>(DIALOG_DATA);

  readonly note = signal('');

  isDismiss(): boolean {
    return this.data.decision === 'dismiss';
  }

  onNoteInput(event: Event): void {
    this.note.set((event.target as HTMLTextAreaElement).value);
  }

  onSubmit(): void {
    this.dialogRef.close({ note: this.note().trim() || undefined });
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
