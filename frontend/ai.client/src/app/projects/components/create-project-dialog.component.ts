import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DialogRef } from '@angular/cdk/dialog';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { Project } from '../models/project.model';
import { ProjectsService, projectErrorMessage } from '../services/projects.service';

/** The created project, or `undefined` when cancelled. */
export type CreateProjectDialogResult = Project | undefined;

const NAME_MAX = 200;
const DESCRIPTION_MAX = 2000;

@Component({
  selector: 'app-create-project-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ReactiveFormsModule],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="New project"
      description="A shared space with its own instructions, files and members. You'll be its owner."
      (closed)="onCancel()"
    >
      <form [formGroup]="form" (ngSubmit)="onCreate()" id="create-project-form" class="space-y-4">
        <div>
          <label for="project-name" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Name</label>
          <input
            id="project-name"
            type="text"
            cdkFocusInitial
            formControlName="name"
            [attr.maxlength]="nameMax"
            autocomplete="off"
            placeholder="e.g. Canvas Enrollment Sync"
            class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>
        <div>
          <label for="project-description" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            Description <span class="font-normal text-gray-500 dark:text-gray-400">(optional)</span>
          </label>
          <textarea
            id="project-description"
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
          (click)="onCancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          Cancel
        </button>
        <button
          type="submit"
          form="create-project-form"
          [disabled]="form.invalid || saving()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {{ saving() ? 'Creating…' : 'Create project' }}
        </button>
      </div>
    </app-dialog-shell>
  `,
})
export class CreateProjectDialogComponent {
  private dialogRef = inject<DialogRef<CreateProjectDialogResult>>(DialogRef);
  private projects = inject(ProjectsService);

  protected readonly nameMax = NAME_MAX;
  protected readonly descriptionMax = DESCRIPTION_MAX;
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly form = new FormGroup({
    name: new FormControl('', { nonNullable: true, validators: [Validators.required, Validators.maxLength(NAME_MAX), Validators.pattern(/\S/)] }),
    description: new FormControl('', { nonNullable: true, validators: [Validators.maxLength(DESCRIPTION_MAX)] }),
  });

  async onCreate(): Promise<void> {
    if (this.form.invalid || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const { name, description } = this.form.getRawValue();
      const project = await this.projects.create(name.trim(), description.trim());
      this.dialogRef.close(project);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The project could not be created.'));
    } finally {
      this.saving.set(false);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }
}
