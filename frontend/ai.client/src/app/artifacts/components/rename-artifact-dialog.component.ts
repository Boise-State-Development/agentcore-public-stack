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
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { DialogDismissDirective } from '../../components/dialog/dialog-dismiss.directive';

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
        aria-labelledby="rename-artifact-title"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <h2
            id="rename-artifact-title"
            class="text-lg/7 font-semibold text-gray-900 dark:text-white"
          >
            Rename artifact
          </h2>
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
        </div>

        <div
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
      </div>
    </div>
  `,
  styles: `
    @reference "../../../styles/theme.css";

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }

    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.97);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
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
    // Select rather than just focus: the field is pre-filled, and
    // replacing the whole name is far more common than editing a word of
    // it. The CDK has already trapped focus in the panel by now.
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
