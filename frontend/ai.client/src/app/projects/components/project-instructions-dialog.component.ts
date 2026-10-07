import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { toSignal } from '@angular/core/rxjs-interop';
import { firstValueFrom } from 'rxjs';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ToastService } from '../../services/toast/toast.service';
import { InstructionsResponse } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

export const INSTRUCTIONS_MAX = 100_000;

export interface ProjectInstructionsDialogData {
  projectId: string;
  projectName: string;
  instructions: string;
  version: number | null;
  /** Editor or owner on an active project. Otherwise the text is read-only. */
  canEdit: boolean;
}

/** The saved instructions, or `undefined` when nothing was saved. */
export type ProjectInstructionsDialogResult = InstructionsResponse | undefined;

/**
 * Edit (or read) what the project's assistant is told in every task. One save is
 * one version in the project's history, so the dialog saves on Save, not on type.
 */
@Component({
  selector: 'app-project-instructions-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ReactiveFormsModule],
  template: `
    <app-dialog-shell
      title="Project instructions"
      [description]="description"
      size="lg"
      (closed)="cancel()"
    >
      <label for="project-instructions" class="sr-only">Instructions</label>
      <textarea
        id="project-instructions"
        rows="14"
        [formControl]="control"
        [attr.maxlength]="max"
        placeholder="e.g. You support the integrations team. Be terse, and name the file you relied on."
        aria-describedby="project-instructions-count"
        class="block w-full resize-y rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:font-sans placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:bg-gray-50 disabled:text-gray-700 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400 dark:disabled:bg-gray-900 dark:disabled:text-gray-300"
      ></textarea>
      <div class="mt-2 flex flex-wrap items-center justify-between gap-3 text-xs/5 text-gray-600 dark:text-gray-400">
        <span id="project-instructions-count">{{ value().length.toLocaleString() }} / {{ max.toLocaleString() }} characters</span>
        @if (data.version) {
          <span>Version {{ data.version }}</span>
        }
      </div>
      @if (error()) {
        <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
      }

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="cancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          {{ data.canEdit ? 'Cancel' : 'Close' }}
        </button>
        @if (data.canEdit) {
          <button
            type="button"
            (click)="save()"
            [disabled]="!dirty() || control.invalid || saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : 'Save instructions' }}
          </button>
        }
      </div>
    </app-dialog-shell>
  `,
})
export class ProjectInstructionsDialogComponent {
  private dialogRef = inject<DialogRef<ProjectInstructionsDialogResult>>(DialogRef);
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  protected readonly data = inject<ProjectInstructionsDialogData>(DIALOG_DATA);
  protected readonly max = INSTRUCTIONS_MAX;
  protected readonly description =
    `What the assistant is told in every task in ${this.data.projectName}. Where they conflict with a member’s personal instructions, these win.`;

  protected readonly control = new FormControl(this.data.instructions, {
    nonNullable: true,
    validators: [Validators.maxLength(INSTRUCTIONS_MAX)],
  });
  protected readonly value = toSignal(this.control.valueChanges, { initialValue: this.data.instructions });
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly dirty = computed(() => this.value() !== this.data.instructions);

  constructor() {
    if (!this.data.canEdit) this.control.disable({ emitEvent: false });
  }

  protected async save(): Promise<void> {
    if (this.control.invalid || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.saveInstructions(this.data.projectId, this.control.value));
      this.toast.success('Instructions saved');
      this.dialogRef.close(response);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The instructions could not be saved.'));
    } finally {
      this.saving.set(false);
    }
  }

  protected cancel(): void {
    this.dialogRef.close(undefined);
  }
}
