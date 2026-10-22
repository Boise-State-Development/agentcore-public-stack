import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';

/** Mirrors the backend's `MAX_ARTIFACT_TITLE_LENGTH`. Enforced there too —
 *  this copy exists so the user finds out before the round trip, not so
 *  the server can trust it. */
export const MAX_ARTIFACT_TITLE_LENGTH = 200;

export interface RenameArtifactDialogData {
  /** Current title. Pre-filled and pre-selected so the common case —
   *  replacing the name outright — is a single keystroke. */
  title: string;
}

/**
 * The new title, trimmed. `undefined` means cancelled, per the app's
 * dialog convention.
 */
export type RenameArtifactDialogResult = string | undefined;

/**
 * Rename dialog for a single artifact, shared by the library page and the
 * session-side artifact panel.
 *
 * Deliberately not the generic confirmation dialog: this collects a
 * value rather than an approval, and the empty/unchanged/too-long cases
 * need to disable the confirm button rather than round-trip to a 400.
 */
@Component({
  selector: 'app-rename-artifact-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Rename artifact" (closed)="onCancel()">
      <label
        for="rename-artifact-input"
        class="block text-sm/6 font-medium text-gray-900 dark:text-white"
      >
        Title
      </label>
      <input
        #input
        id="rename-artifact-input"
        type="text"
        cdkFocusInitial
        [value]="draft()"
        (input)="onInput($event)"
        (keydown.enter)="onEnter()"
        [attr.maxlength]="maxLength"
        [attr.aria-describedby]="tooLong() ? 'rename-artifact-hint' : null"
        [attr.aria-invalid]="tooLong() ? 'true' : null"
        class="mt-1.5 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
      />
      @if (tooLong()) {
        <p
          id="rename-artifact-hint"
          class="mt-2 text-xs/5 text-state-danger-600 dark:text-state-danger-400"
        >
          Titles are limited to {{ maxLength }} characters.
        </p>
      }

      <div
        dialogFooter
        class="flex items-center justify-end gap-2 border-t border-gray-200 px-6 py-3 dark:border-gray-700"
      >
        <button
          type="button"
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-200 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          type="button"
          (click)="confirm()"
          [disabled]="!canConfirm()"
          class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:brightness-110"
        >
          Save
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class RenameArtifactDialogComponent implements AfterViewInit {
  protected readonly dialogRef = inject(DialogRef<RenameArtifactDialogResult>);
  protected readonly data = inject<RenameArtifactDialogData>(DIALOG_DATA);

  private readonly input = viewChild<ElementRef<HTMLInputElement>>('input');

  protected readonly maxLength = MAX_ARTIFACT_TITLE_LENGTH;
  protected readonly draft = signal(this.data.title ?? '');

  protected readonly tooLong = computed(
    () => this.draft().trim().length > MAX_ARTIFACT_TITLE_LENGTH,
  );

  /**
   * Confirm is gated on a title that is non-empty, within the cap, and
   * actually different. Blocking the no-op case keeps a stray Enter from
   * firing a write that renames every version row to what they already
   * say.
   */
  protected readonly canConfirm = computed(() => {
    const next = this.draft().trim();
    return next.length > 0 && !this.tooLong() && next !== (this.data.title ?? '').trim();
  });

  ngAfterViewInit(): void {
    // Select as well as focus (`cdkFocusInitial` puts focus here): the
    // field is pre-filled, and replacing the whole name is far more common
    // than editing a word of it.
    this.input()?.nativeElement.select();
  }

  protected onInput(event: Event): void {
    this.draft.set((event.target as HTMLInputElement).value);
  }

  protected onEnter(): void {
    if (this.canConfirm()) this.confirm();
  }

  protected confirm(): void {
    this.dialogRef.close(this.draft().trim());
  }

  protected onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
