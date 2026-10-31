import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { quotaAdminErrorMessage } from '../services/quota-requests-admin.service';

export interface QuotaNoteDialogData<T> {
  title: string;
  description: string;
  label: string;
  placeholder?: string;
  /** Starts the note with a suggestion the admin can edit. */
  initialNote?: string;
  confirmLabel: string;
  /** Deny and revoke read as destructive; request-info does not. */
  tone: 'primary' | 'danger';
  /** Sends the note. A refusal stays in the dialog, worded, so the admin can fix it or cancel. */
  submit: (note: string) => Promise<T>;
}

/** What `submit` resolved to, or undefined if cancelled. */
export type QuotaNoteDialogResult<T> = T | undefined;

/**
 * One required note, sent to the API from inside the dialog.
 *
 * Ask for info, Deny and Revoke all need exactly this, and all three send the
 * note to the user verbatim, so the description says so. Submitting here
 * rather than in the opener keeps a refusal (a request another admin just
 * decided, your own request) where the admin is looking.
 */
@Component({
  selector: 'app-quota-note-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell [title]="data.title" [description]="data.description" (closed)="cancel()">
      <label for="quota-note" class="block text-sm/6 font-medium text-gray-900 dark:text-white">{{ data.label }}</label>
      <textarea
        id="quota-note"
        rows="4"
        cdkFocusInitial
        [value]="note()"
        (input)="onInput($event)"
        [attr.placeholder]="data.placeholder ?? null"
        [attr.aria-invalid]="error() ? 'true' : null"
        [attr.aria-describedby]="error() ? 'quota-note-error' : null"
        class="mt-2 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
      ></textarea>
      @if (error(); as message) {
        <p id="quota-note-error" role="alert" class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
      }

      <div dialogFooter class="flex justify-end gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="cancel()"
          class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          [disabled]="!note().trim() || saving()"
          (click)="send()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-white focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          [class]="data.tone === 'danger'
            ? 'bg-state-danger-600 hover:bg-state-danger-700 focus-visible:outline-state-danger-500'
            : 'bg-primary-accessible hover:brightness-95 focus-visible:outline-primary-500'"
        >
          {{ saving() ? 'Sending…' : data.confirmLabel }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class QuotaNoteDialogComponent<T = unknown> {
  private dialogRef = inject<DialogRef<QuotaNoteDialogResult<T>>>(DialogRef);
  readonly data = inject<QuotaNoteDialogData<T>>(DIALOG_DATA);

  readonly note = signal(this.data.initialNote ?? '');
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);

  onInput(event: Event): void {
    this.note.set((event.target as HTMLTextAreaElement).value);
  }

  async send(): Promise<void> {
    const note = this.note().trim();
    if (!note || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      this.dialogRef.close(await this.data.submit(note));
    } catch (error) {
      this.error.set(quotaAdminErrorMessage(error, 'That did not go through. Try again.'));
    } finally {
      this.saving.set(false);
    }
  }

  cancel(): void {
    this.dialogRef.close(undefined);
  }
}
