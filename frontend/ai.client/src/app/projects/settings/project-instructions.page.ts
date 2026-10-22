import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { ToastService } from '../../services/toast/toast.service';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';
import { HasUnsavedChanges } from './unsaved-changes.guard';

export const INSTRUCTIONS_MAX = 100_000;

/**
 * `/projects/:id/instructions` — what the project's assistant is told in every task.
 *
 * One save is one version in the project's history, so the page saves on Save (or
 * ⌘S / Ctrl+S in the editor), not on type, and asks before a link leaves an unsaved
 * draft. Viewers, and everyone on an archived project, read it.
 */
@Component({
  selector: 'app-project-instructions-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProjectSettingsShellComponent, ReactiveFormsModule, RouterLink],
  host: { '(window:beforeunload)': 'onBeforeUnload($event)' },
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading() || (!loaded() && !loadError())"
      [error]="state.error()"
      heading="Instructions"
      [description]="description()"
    >
      @if (state.canEdit() && loaded()) {
        <div shellActions class="flex items-center gap-3">
          <button
            type="button"
            (click)="discard()"
            [disabled]="!dirty() || saving()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
          >
            Discard changes
          </button>
          <button
            type="button"
            (click)="save()"
            [disabled]="!dirty() || control.invalid || saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : 'Save instructions' }}
          </button>
        </div>
      }

      @if (state.project(); as p) {
        @if (loadError()) {
          <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ loadError() }}</p>
        } @else {
          <label for="project-instructions" class="sr-only">Instructions</label>
          <textarea
            id="project-instructions"
            rows="22"
            [formControl]="control"
            [attr.maxlength]="max"
            (keydown.meta.s)="saveFromKeyboard($event)"
            (keydown.control.s)="saveFromKeyboard($event)"
            placeholder="e.g. You support the integrations team. Be terse, and name the file you relied on."
            aria-describedby="project-instructions-count"
            class="block w-full resize-y rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:font-sans placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:bg-gray-50 disabled:text-gray-700 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400 dark:disabled:bg-gray-900 dark:disabled:text-gray-300"
          ></textarea>
          <div class="mt-2 flex flex-wrap items-center justify-between gap-3 text-xs/5 text-gray-600 dark:text-gray-400">
            <span id="project-instructions-count">{{ value().length.toLocaleString() }} / {{ max.toLocaleString() }} characters</span>
            @if (version(); as v) {
              <a
                [routerLink]="['/projects', p.projectId, 'history']"
                [queryParams]="{ version: v }"
                class="rounded-sm font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
              >Version {{ v }}</a>
            }
          </div>
          @if (error()) {
            <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
          }
        }
      }
    </app-project-settings-shell>
  `,
})
export class ProjectInstructionsPage implements HasUnsavedChanges {
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  /** Route param (`withComponentInputBinding`). */
  readonly id = input.required<string>();

  protected readonly state = injectProjectPage(this.id);
  protected readonly max = INSTRUCTIONS_MAX;

  protected readonly control = new FormControl('', { nonNullable: true, validators: [Validators.maxLength(INSTRUCTIONS_MAX)] });
  protected readonly value = toSignal(this.control.valueChanges, { initialValue: '' });
  /** The text as last saved, which Discard returns to and `dirty` compares against. */
  private readonly saved = signal('');
  protected readonly version = signal<number | null>(null);
  protected readonly loaded = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly dirty = computed(() => this.loaded() && this.value() !== this.saved());

  protected readonly description = computed(() => {
    const name = this.state.project()?.name ?? 'this project';
    return `What the assistant is told in every conversation in ${name}. Where they conflict with a member’s personal instructions, these win.`;
  });

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => void this.load(id));
    });
    // Read-only follows the project: a viewer, or an archived project.
    effect(() => {
      const editable = this.state.canEdit();
      untracked(() => (editable ? this.control.enable({ emitEvent: false }) : this.control.disable({ emitEvent: false })));
    });
  }

  private async load(projectId: string): Promise<void> {
    this.loaded.set(false);
    this.loadError.set(null);
    try {
      const response = await firstValueFrom(this.api.instructions(projectId));
      if (projectId !== this.id()) return;
      this.saved.set(response.instructions);
      this.version.set(response.version);
      this.control.setValue(response.instructions);
      this.loaded.set(true);
    } catch (err) {
      if (projectId === this.id()) this.loadError.set(projectErrorMessage(err, 'The instructions could not be loaded.'));
    }
  }

  hasUnsavedChanges(): boolean {
    return this.dirty();
  }

  protected onBeforeUnload(event: BeforeUnloadEvent): void {
    if (this.dirty()) event.preventDefault();
  }

  protected discard(): void {
    this.control.setValue(this.saved());
    this.error.set(null);
  }

  protected saveFromKeyboard(event: Event): void {
    event.preventDefault();
    if (this.state.canEdit()) void this.save();
  }

  protected async save(): Promise<void> {
    if (!this.dirty() || this.control.invalid || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.saveInstructions(this.id(), this.control.value));
      this.saved.set(response.instructions);
      if (this.control.value !== response.instructions) this.control.setValue(response.instructions);
      this.version.set(response.version);
      this.toast.success('Instructions saved');
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The instructions could not be saved.'));
    } finally {
      this.saving.set(false);
    }
  }
}
