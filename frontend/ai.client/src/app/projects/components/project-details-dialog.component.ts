import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ToastService } from '../../services/toast/toast.service';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

const NAME_MAX = 200;
const DESCRIPTION_MAX = 2000;

export interface ProjectDetailsDialogData {
  project: Project;
}

/** The renamed project, or `undefined` when nothing was saved. */
export type ProjectDetailsDialogResult = Project | undefined;

/** Rename a project or change its description (editors and the owner). */
@Component({
  selector: 'app-project-details-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ReactiveFormsModule],
  template: `
    <app-dialog-shell title="Project details" (closed)="cancel()">
      <form [formGroup]="form" (ngSubmit)="save()" id="project-details-form" class="space-y-4">
        <div>
          <label for="project-details-name" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Name</label>
          <input
            id="project-details-name"
            type="text"
            formControlName="name"
            [attr.maxlength]="nameMax"
            autocomplete="off"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        <div>
          <label for="project-details-description" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            Description <span class="font-normal text-gray-500 dark:text-gray-400">(optional)</span>
          </label>
          <textarea
            id="project-details-description"
            rows="3"
            formControlName="description"
            [attr.maxlength]="descriptionMax"
            placeholder="What is this project for?"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          ></textarea>
        </div>
        @if (error()) {
          <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
        }
      </form>

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="cancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          Cancel
        </button>
        <button
          type="submit"
          form="project-details-form"
          [disabled]="form.invalid || !dirty() || saving()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {{ saving() ? 'Saving…' : 'Save details' }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class ProjectDetailsDialogComponent {
  private dialogRef = inject<DialogRef<ProjectDetailsDialogResult>>(DialogRef);
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  protected readonly data = inject<ProjectDetailsDialogData>(DIALOG_DATA);
  protected readonly nameMax = NAME_MAX;
  protected readonly descriptionMax = DESCRIPTION_MAX;
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly form = new FormGroup({
    name: new FormControl(this.data.project.name, {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(NAME_MAX), Validators.pattern(/\S/)],
    }),
    description: new FormControl(this.data.project.description, {
      nonNullable: true,
      validators: [Validators.maxLength(DESCRIPTION_MAX)],
    }),
  });

  protected dirty(): boolean {
    const { name, description } = this.form.getRawValue();
    return name.trim() !== this.data.project.name || description.trim() !== this.data.project.description;
  }

  protected async save(): Promise<void> {
    if (this.form.invalid || !this.dirty() || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const { name, description } = this.form.getRawValue();
      const project = await firstValueFrom(
        this.api.update(this.data.project.projectId, { name: name.trim(), description: description.trim() }),
      );
      this.toast.success('Details saved');
      this.dialogRef.close(project);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The details could not be saved.'));
    } finally {
      this.saving.set(false);
    }
  }

  protected cancel(): void {
    this.dialogRef.close(undefined);
  }
}
